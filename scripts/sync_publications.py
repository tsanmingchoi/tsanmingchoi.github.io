"""Weekly publication sync.

Reads Google Scholar (profile page) and OpenAlex (via ORCID), adds any new
publications to content/publications.json and refreshes the Google Scholar
metrics in content/impact.json. Entries written by hand are never changed.

Settings live in content/sync.json so they can be edited from the site editor.
Run locally with:  python scripts/sync_publications.py
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT = os.path.join(ROOT, "content")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

UTD_FT50 = {
    "manufacturing & service operations management", "management science", "operations research",
    "production and operations management", "journal of operations management",
    "information systems research", "mis quarterly", "marketing science", "journal of marketing",
    "journal of marketing research", "journal of consumer research", "strategic management journal",
    "academy of management journal", "academy of management review", "administrative science quarterly",
    "organization science", "journal of finance", "journal of financial economics",
    "review of financial studies", "the accounting review", "journal of accounting research",
    "journal of accounting and economics", "journal of international business studies",
    "journal of management", "journal of management studies", "research policy",
    "american economic review", "econometrica", "quarterly journal of economics",
    "journal of political economy", "review of economic studies", "harvard business review",
    "mit sloan management review", "journal of business ethics", "human resource management",
}
# Journal ranks (ABS rank, FT50 flag) come from the Journal Ranking Finder data.
# ABS4 below is only a fallback for weeks when that file cannot be downloaded.
RANKS_URL = "https://mayukhpsm.github.io/journalrankingfinder/journals.json"
ABS4 = {
    "european journal of operational research", "british journal of management", "risk analysis",
    "international journal of operations & production management",
    "international journal of operations and production management",
    "journal of supply chain management",
}
TOP_GROUP, ABS4_GROUP, OTHER_GROUP = "UTD24 / FT50 journals", "Other ABS 4 journals", "Other selected journals"
KEEP_TYPES = {"article", "review", "book", "book-chapter"}
# Front matter and notes that are not research papers
SKIP_TITLE = re.compile(r"best paper award|editorial|corrigendum|erratum|in memoriam|reviewers|"
                        r"^\d+ years of|call for papers|retraction", re.I)


def load(name):
    with open(os.path.join(CONTENT, name), encoding="utf8") as f:
        return json.load(f)


def save(name, data):
    with open(os.path.join(CONTENT, name), "w", encoding="utf8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get(url, as_json=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf8")
    return json.loads(body) if as_json else body


def tokens(text):
    text = text.lower().replace("iour", "ior")
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text) if len(w) > 2}


def is_known(title, doi, existing):
    """True if a title or DOI already appears in an existing entry."""
    t = tokens(title)
    if not t:
        return True
    for p in existing:
        if doi and doi.lower() in (p.get("link", "") + p.get("citation", "")).lower():
            return True
        other = tokens(p.get("title") or p["citation"])
        if len(t & other) / min(len(t), len(other) or 1) >= 0.85:
            return True
    return False


def _key(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("&", "and"))


def load_ranks():
    """Journal Ranking Finder data indexed by normalised title, or None if unavailable."""
    try:
        return {_key(j["Journal Title"]): j for j in get(RANKS_URL, as_json=True)["journals"]}
    except Exception:
        return None


def rank_of(journal, ranks):
    key = _key(journal)
    if key in ranks:
        return ranks[key]
    # Short forms such as "Transportation Research Part E" match one full title
    hits = [v for k, v in ranks.items() if len(key) >= 12 and k.startswith(key)]
    return hits[0] if len(hits) == 1 else None


def category_for(journal, kind, ranks=None):
    if kind in ("book", "book-chapter") and not journal:
        return "Books"
    j = journal.lower().strip()
    if j in UTD_FT50:
        return TOP_GROUP
    hit = rank_of(journal, ranks) if ranks else None
    if hit and (hit["ABS Rank"] or hit["FT50"]):
        if hit["FT50"]:
            return TOP_GROUP
        return ABS4_GROUP if hit["ABS Rank"] in ("4", "4*") else OTHER_GROUP
    return ABS4_GROUP if j in ABS4 else OTHER_GROUP


def from_openalex(orcid, from_year):
    url = ("https://api.openalex.org/works?filter=author.orcid:{},from_publication_date:{}-01-01"
           "&sort=publication_date:desc&per-page=100").format(orcid, from_year)
    found = []
    for w in get(url, as_json=True)["results"]:
        if w.get("type") not in KEEP_TYPES or not w.get("title"):
            continue
        src = ((w.get("primary_location") or {}).get("source") or {})
        journal = src.get("display_name") or ""
        names = []
        for a in w.get("authorships", []):
            parts = a["author"]["display_name"].split()
            if parts:
                initials = "".join(p[0] + "." for p in parts[:-1])
                names.append(f"{parts[-1]}, {initials}" if initials else parts[-1])
        bib = w.get("biblio") or {}
        details = ""
        if bib.get("volume"):
            details = bib["volume"] + (f"({bib['issue']})" if bib.get("issue") else "")
            if bib.get("first_page"):
                details += f", {bib['first_page']}" + (f"-{bib['last_page']}" if bib.get("last_page") else "")
        title = html.unescape(re.sub(r"<[^>]+>", "", w["title"]))
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        citation = f"{', '.join(names)} ({w['publication_year']}). {sentence(title)} {journal}"
        citation += f", {details}." if details else (", published online." if journal else ".")
        found.append({"title": title, "doi": doi, "year": w["publication_year"], "journal": journal,
                      "citation": citation, "kind": w["type"], "source": "openalex"})
    return found


def sentence(title):
    """Title followed by a full stop, unless it already ends with ? ! or ."""
    title = title.strip()
    return title if title[-1:] in "?!." else title + "."


def from_scholar(user, from_year, attempts=3):
    """Read the Scholar profile page directly. Google often blocks data-centre servers
    (such as GitHub's), so retry a few times before giving up."""
    url = f"https://scholar.google.com/citations?user={user}&hl=en&sortby=pubdate&pagesize=100"
    for n in range(attempts):
        try:
            page = get(url)
            if "gsc_a_at" in page:
                break
            reason = "no publication rows (blocked or CAPTCHA page)"
        except Exception as e:
            reason = str(e)
        if n < attempts - 1:
            time.sleep(20 * (n + 1))
    else:
        raise RuntimeError(f"Google Scholar blocked the request: {reason}")
    stats = re.findall(r'class="gsc_rsb_std">(\d+)<', page)
    metrics = {"citations": stats[0], "h": stats[2], "i10": stats[4]} if len(stats) >= 6 else None
    found = []
    for row in re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', page, re.S):
        title = re.search(r'class="gsc_a_at"[^>]*>(.*?)</a>', row, re.S)
        grays = re.findall(r'<div class="gs_gray">(.*?)</div>', row, re.S)
        year = re.search(r'class="gsc_a_h gsc_a_hc gs_ibl">(\d{4})<', row)
        if not (title and year) or int(year.group(1)) < from_year:
            continue
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        authors = clean(grays[0]) if grays else ""
        venue = clean(grays[1]) if len(grays) > 1 else ""
        journal = re.split(r"\s+\d", venue)[0].strip(" ,")
        t = clean(title.group(1))
        found.append({"title": t, "doi": "", "year": int(year.group(1)), "journal": journal,
                      "citation": f"{authors} ({year.group(1)}). {sentence(t)} {sentence(venue)}",
                      "kind": "article", "source": "scholar"})
    return found, metrics


def from_serpapi(key, user, from_year):
    """Same data through SerpAPI's Google Scholar Author API, which is not blocked.
    Used only when the SERPAPI_KEY secret is set and the direct read failed."""
    q = urllib.parse.urlencode({"engine": "google_scholar_author", "author_id": user, "hl": "en",
                                "sort": "pubdate", "num": 100, "api_key": key})
    data = get(f"https://serpapi.com/search.json?{q}", as_json=True)
    if data.get("error"):
        raise RuntimeError(f"SerpAPI: {data['error']}")
    table = {k: v for row in (data.get("cited_by") or {}).get("table", []) for k, v in row.items()}
    metrics = None
    if {"citations", "h_index", "i10_index"} <= table.keys():
        metrics = {"citations": table["citations"]["all"], "h": str(table["h_index"]["all"]),
                   "i10": str(table["i10_index"]["all"])}
    found = []
    for a in data.get("articles", []):
        year = str(a.get("year") or "")
        if not (a.get("title") and year.isdigit()) or int(year) < from_year:
            continue
        venue = a.get("publication", "")
        journal = re.split(r"\s+\d", venue)[0].strip(" ,")
        found.append({"title": a["title"], "doi": "", "year": int(year), "journal": journal,
                      "citation": f"{a.get('authors', '')} ({year}). {sentence(a['title'])} {sentence(venue or journal)}",
                      "kind": "article", "source": "scholar"})
    return found, metrics


def doi_for(title, year):
    """Find a DOI on Crossref for a paper Scholar listed without one (exact title match only)."""
    q = urllib.parse.urlencode({"query.bibliographic": title, "rows": 3, "select": "DOI,title",
                                "filter": f"from-pub-date:{year - 1}"})
    for item in get(f"https://api.crossref.org/works?{q}", as_json=True)["message"]["items"]:
        found = (item.get("title") or [""])[0]
        a, b = tokens(title), tokens(found)
        if a and b and len(a & b) / max(len(a), len(b)) >= 0.9:
            return item["DOI"]
    return ""


def fmt(n):
    return f"{int(n):,}"


def main():
    cfg = load("sync.json")
    if not cfg.get("enabled", True):
        print("Sync disabled in content/sync.json")
        return
    pubs = load("publications.json")
    from_year = int(cfg.get("auto_add_from_year", dt.date.today().year))
    excluded = [tokens(t) for t in cfg.get("exclude_titles", [])]
    skip_venues = [v.lower() for v in cfg.get("exclude_venues", [])]
    candidates, notes = [], []

    status = {"last_run": dt.date.today().isoformat()}
    try:
        candidates += from_openalex(cfg["orcid"], from_year)
        status["openalex"] = "ok"
    except Exception as e:  # keep going with Scholar
        status["openalex"] = f"failed: {e}"
        notes.append(f"OpenAlex failed: {e}")

    metrics = None
    try:
        time.sleep(1)
        found, metrics = from_scholar(cfg["scholar_id"], from_year)
        status["google_scholar"] = "ok (direct)"
    except Exception as e:
        found, status["google_scholar"] = [], f"blocked: {e}"
        key = os.environ.get("SERPAPI_KEY", "").strip()
        if key:
            try:
                found, metrics = from_serpapi(key, cfg["scholar_id"], from_year)
                status["google_scholar"] = "ok (SerpAPI fallback)"
            except Exception as e2:
                status["google_scholar"] += f"; SerpAPI also failed: {e2}"
    candidates += found
    if not status["google_scholar"].startswith("ok"):
        notes.append(f"Google Scholar skipped: {status['google_scholar']}")
        # Shows as a yellow warning on the GitHub Actions run instead of a silent success
        print(f"::warning::Google Scholar not updated this week ({status['google_scholar']}). "
              "Citation numbers on the site keep their last value.")

    for c in candidates:
        if c["source"] == "scholar" and not c["doi"]:
            try:
                c["doi"] = doi_for(c["title"], c["year"])
            except Exception:
                pass  # the paper is still added, just without a DOI link

    ranks = load_ranks()
    status["journal_ranks"] = "ok" if ranks else "unavailable (used built-in list)"
    added = []
    for c in candidates:
        t = tokens(c["title"])
        if any(t and len(t & x) / len(t) >= 0.85 for x in excluded):
            continue
        if SKIP_TITLE.search(c["title"]) or any(v in c["citation"].lower() for v in skip_venues):
            continue
        if is_known(c["title"], c["doi"], pubs + added):
            continue
        added.append({
            "category": category_for(c["journal"], c["kind"], ranks),
            "title": c["title"],
            "year": c["year"],
            "citation": c["citation"],
            "journal": c["journal"],
            "link": f"https://doi.org/{c['doi']}" if c["doi"] else "",
            "source": c["source"],
            "added": dt.date.today().isoformat(),
            "hidden": False,
            # Left blank: the site guesses the research area from the title until it is set by hand
            "area": "",
        })

    if added:
        save("publications.json", added + pubs)

    impact = load("impact.json")
    if metrics and cfg.get("update_metrics", True):
        new_values = {"Google Scholar citations": fmt(metrics["citations"]),
                      "Google Scholar h-index": str(metrics["h"]),
                      "Google Scholar i10-index": str(metrics["i10"])}
        for m in impact["metrics"]:
            if m["label"] in new_values:
                m["value"] = new_values[m["label"]]
        # The site shows this date under the numbers, so it moves even when they do not
        impact["metrics_updated"] = dt.date.today().strftime("%d %B %Y").lstrip("0")
        save("impact.json", impact)
        notes.append("Google Scholar metrics updated")

    status["papers_added"] = len(added)
    status["citations_as_of"] = impact.get("metrics_updated", "")
    save("sync_status.json", status)

    print(f"Added {len(added)} publication(s)")
    for a in added:
        print(f"  + [{a['source']}] {a['citation'][:110]}")
    for n in notes:
        print(n)


if __name__ == "__main__":
    sys.exit(main())

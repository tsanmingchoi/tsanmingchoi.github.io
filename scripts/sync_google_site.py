"""Daily Google Site sync.

Reads Prof. Choi's Google Site (https://sites.google.com/view/jason-choi) and copies
his day-to-day updates into the website:

  Home         -> portrait, positions, bio, contact details; captioned photos become
                  "Latest updates" cards
  My Team      -> research team groups and members, team photos
  My Teaching  -> courses and teaching photos
  My Services  -> service roles and journal cover images
  Funding      -> funded projects (impact highlights and citation numbers are not touched)
  Announcements (optional page) -> "Latest updates" cards

Publications and citation numbers come from Google Scholar / ORCID (sync_publications.py),
and the Research section stays as edited on the website.

A section is only updated when what was read looks sensible. Otherwise the website keeps
its current version for that section and the run ends with an error, so GitHub sends an
email. Run locally with:  python scripts/sync_google_site.py
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT = os.path.join(ROOT, "content")
IMAGES = os.path.join(ROOT, "images", "gsite")
SITE = "https://sites.google.com/view/jason-choi/"
PAGES = {
    "home": ["menu_1"],
    "team": ["menu_1/my-team"],
    "teaching": ["menu_1/my-teaching"],
    "services": ["menu_1/my-services"],
    "funding": ["menu_1/funding-and-impact"],
    "announcements": ["menu_1/announcements", "announcements", "menu_1/news", "news"],
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DATE_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})", re.I)


# --------------------------------------------------------------------------- reading
class Blocks(HTMLParser):
    """Turns a Google Sites page into an ordered list of text blocks and images."""

    def __init__(self):
        super().__init__()
        self.out, self.buf, self.tag, self.skip = [], None, None, 0

    def handle_starttag(self, t, a):
        a = dict(a)
        if t in ("script", "style", "nav", "header"):
            self.skip += 1
        if self.skip:
            return
        if t == "img" and "sitesv-images" in a.get("src", ""):
            self.out.append({"kind": "img", "src": a["src"]})
        if t in ("h1", "h2", "h3", "p", "li") and self.buf is None:
            self.buf, self.tag = [], t

    def handle_endtag(self, t):
        if t in ("script", "style", "nav", "header"):
            self.skip -= 1
        if self.buf is not None and t == self.tag:
            text = re.sub(r"\s+", " ", html.unescape("".join(self.buf))).strip()
            text = text.replace("�", "’")  # broken apostrophes on the Google Site
            if text:
                self.out.append({"kind": self.tag, "text": text})
            self.buf = None

    def handle_data(self, d):
        if self.buf is not None and not self.skip:
            self.buf.append(d)


def fetch(url, binary=False, attempts=3):
    last = None
    for n in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            return data if binary else data.decode("utf8")
        except Exception as e:  # retry, then report
            last = e
            if getattr(e, "code", None) == 404:
                break
            time.sleep(5 * (n + 1))
    raise RuntimeError(f"could not read {url}: {last}")


def read_page(paths):
    """Blocks for the first path that exists; None when no path exists (optional pages)."""
    for i, path in enumerate(paths):
        try:
            page = fetch(SITE + path)
        except RuntimeError as e:
            if "404" in str(e) and i < len(paths) - 1:
                continue
            if "404" in str(e):
                return None
            raise
        p = Blocks()
        p.feed(page)
        return p.out
    return None


# --------------------------------------------------------------------------- helpers
def load(name):
    with open(os.path.join(CONTENT, name), encoding="utf8") as f:
        return json.load(f)


def save(name, data):
    with open(os.path.join(CONTENT, name), "w", encoding="utf8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def tidy(text):
    return text.strip().rstrip(",.;").strip()


def is_caption(block):
    """A short paragraph straight after an image is that image's caption."""
    return block["kind"] == "p" and len(block["text"]) <= 160 and not block["text"].endswith(":")


def photos_with_captions(blocks, skip_first=False):
    found = []
    imgs = [i for i, b in enumerate(blocks) if b["kind"] == "img"]
    for k, i in enumerate(imgs):
        if skip_first and k == 0:
            continue
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if nxt and is_caption(nxt):
            found.append({"src": blocks[i]["src"], "caption": tidy(nxt["text"])})
    return found


def save_image(src, page_name):
    """Copy a Google image into images/gsite, named by its content so repeat runs reuse it."""
    data = fetch(src, binary=True)
    ext = "png" if data[:4] == b"\x89PNG" else "jpg"
    name = f"{page_name}-{hashlib.sha1(data).hexdigest()[:12]}.{ext}"
    os.makedirs(IMAGES, exist_ok=True)
    path = os.path.join(IMAGES, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return f"images/gsite/{name}"


def month_value(text):
    m = DATE_RE.search(text or "")
    if not m:
        return 0
    return int(m.group(2)) * 12 + MONTHS.index(m.group(1)[:3].lower())


def date_label(text):
    m = DATE_RE.search(text or "")
    return f"{m.group(1)[:3].title()} {m.group(2)}" if m else ""


class SectionError(Exception):
    pass


def check(ok, message):
    if not ok:
        raise SectionError(message)


# --------------------------------------------------------------------------- sections
def parse_home(blocks, profile):
    texts = [b["text"] for b in blocks if b["kind"] != "img"]
    imgs = [b for b in blocks if b["kind"] == "img"]
    check(imgs, "no portrait photo found")

    # Lines between the name heading and "Last updated" hold positions and affiliation
    start = next((i for i, t in enumerate(texts) if "CHOI" in t and "PhD" in t), None)
    stop = next((i for i, t in enumerate(texts) if t.lower().startswith("last updated")), None)
    check(start is not None and stop is not None and stop > start, "name or 'Last updated' line not found")
    positions, affiliation = [], profile.get("affiliation", "")
    for line in texts[start + 1:stop]:
        line = tidy(line)
        if re.search(r"\b(school|university)\b", line, re.I):
            affiliation = line
        elif re.search(r"\d|kingdom|merseyside", line, re.I) or len(line.split()) <= 2:
            continue  # address lines
        else:
            positions.append(line)
    check(positions, "no position lines found")

    bio_at = next((i for i, t in enumerate(texts) if t.lower().strip(" :") == "my brief bio"), None)
    check(bio_at is not None and bio_at + 1 < len(texts), "'My Brief Bio' not found")
    bio = texts[bio_at + 1]
    check(len(bio) > 200, "biography is unexpectedly short")

    contact = {}
    links, identifiers = [], []
    label_map = {"linkedin": "LinkedIn", "researchgate": "ResearchGate", "google scholar": "Google Scholar",
                 "orcid": "ORCID", "ulms homepage": "ULMS profile"}
    for t in texts:
        if ":" not in t:
            continue
        key, val = [x.strip() for x in t.split(":", 1)]
        k = key.lower()
        url = re.search(r"https?://\S+", val)
        if k == "address":
            contact["address"] = tidy(val)
        elif k == "email":
            contact["email"] = tidy(val)
        elif k == "phone":
            contact["phone"] = tidy(val)
        elif k in label_map and url:
            links.append({"label": label_map[k], "url": url.group(0).rstrip(".,")})
            if k == "orcid":
                identifiers.append({"label": "ORCID", "value": url.group(0).rstrip("/.").rsplit("/", 1)[-1]})
        elif k == "scopus id":
            identifiers.append({"label": "Scopus ID", "value": tidy(val)})
        elif k.startswith("web of science researcherid"):
            identifiers.append({"label": "Web of Science ResearcherID", "value": tidy(val)})
    check("@" in contact.get("email", ""), "email address not found")

    return {
        "portrait_src": imgs[0]["src"],
        "positions": positions,
        "affiliation": affiliation,
        "bio": bio,
        "contact": contact,
        "links": links,
        "identifiers": identifiers,
        "photos": photos_with_captions(blocks, skip_first=True),
    }


def parse_team(blocks):
    groups, current = [], None
    for b in blocks:
        if b["kind"] == "img":
            continue
        t = b["text"]
        if b["kind"] == "p" and t.endswith(":"):
            title = re.sub(r"\s*\((starting year|start year)\)\s*", "", t[:-1], flags=re.I).strip()
            current = {"title": title, "members": []}
            groups.append(current)
        elif b["kind"] == "li" and current is not None:
            sub = re.match(r"^(current|past)\s*:\s*(.+)$", t, re.I)
            if sub:  # "Current: a, b, c" inside a group
                g = {"title": f"{current['title']} ({sub.group(1).lower()})", "members": []}
                for name in sub.group(2).split(","):
                    if tidy(name):
                        g["members"].append({"name": tidy(name), "note": ""})
                groups.append(g)
                continue
            m = re.match(r"^(.+?)\s*\(([^)]*)\)\s*[,.]?$", t)
            name, note = (m.group(1), m.group(2)) if m else (tidy(t), "")
            current["members"].append({"name": tidy(name), "note": note.strip()})
    groups = [g for g in groups if g["members"]]
    return {"groups": groups, "photos": photos_with_captions(blocks)}


def parse_teaching(blocks):
    courses = []
    for b in blocks:
        if b["kind"] == "li":
            m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*[,.]?$", b["text"])
            courses.append({"title": tidy(m.group(1)), "level": m.group(2).strip()} if m
                           else {"title": tidy(b["text"]), "level": ""})
    return {"courses": courses, "photos": photos_with_captions(blocks)}


def parse_services(blocks):
    roles = []
    for b in blocks:
        if b["kind"] == "li" and ":" in b["text"]:
            role, detail = b["text"].split(":", 1)
            roles.append({"role": role.strip(), "detail": tidy(detail)})
    images = [b["src"] for b in blocks if b["kind"] == "img"]
    return {"roles": roles, "images": images}


AMOUNT_RE = re.compile(r"(HK|TW|US|NT|RMB|GBP|EUR|AUD|CAD|SGD)?\s?\$+\s?\d[\d,]*\d(?:\.\d+)?|£\s?\d[\d,]*\d")


def parse_funding(blocks):
    projects = []
    for b in blocks:
        if b["kind"] != "li":
            continue
        m = re.match(r"^((?:PI|Co)(?:\s*\([^)]*\))?)\s*:\s*(.+)$", b["text"])
        if not m:
            continue
        role, rest = m.group(1).strip(), m.group(2)
        amt = AMOUNT_RE.search(rest)
        year = re.search(r"awarded year\s*:\s*(\d{4})", rest, re.I)
        title = tidy(rest[:amt.start()]) if amt else tidy(rest.split(",")[0])
        amount = re.sub(r"\s|\$+", lambda x: "" if x.group().isspace() else "$", amt.group(0)) if amt else ""
        after = rest[amt.end():] if amt else rest
        funder = tidy(re.split(r"awarded year", after, flags=re.I)[0].strip(" ,."))
        note = ""
        if year:
            tail = rest[year.end():].strip()
            n = re.match(r"^\((.*)\)\.?$", tail)
            note = n.group(1).strip() if n else ""
        projects.append({"role": role, "title": title, "amount": amount, "funder": funder,
                         "year": int(year.group(1)) if year else 0, "note": note})

    # Impact lines sit between "Research Impact" and "Funded Projects". Citation numbers
    # are skipped because they come from Google Scholar.
    texts = [b["text"] for b in blocks if b["kind"] == "p"]
    start = next((i for i, t in enumerate(texts) if t.lower().startswith("research impact")), None)
    stop = next((i for i, t in enumerate(texts) if t.lower().startswith("funded projects")), len(texts))
    highlights = []
    if start is not None:
        for t in texts[start + 1:stop]:
            if re.match(r"^(web of science|google scholar|scopus)\s*:", t, re.I):
                continue
            t = re.sub(r"^impacts?\s*:\s*", "", t, flags=re.I)
            t = re.sub(r"\s+([).,:])", r"\1", t).strip()
            if t:
                highlights.append(t)
    images = [b["src"] for b in blocks if b["kind"] == "img"]
    return {"projects": projects, "highlights": highlights,
            "badge": images[0] if images else "", "photos": images[1:]}


def parse_announcements(blocks):
    """Optional page: each heading starts an entry; following text and first photo belong to it."""
    entries, cur = [], None
    for b in blocks[1:]:  # skip the page title
        if b["kind"] in ("h1", "h2", "h3"):
            cur = {"title": tidy(b["text"]), "text": [], "src": ""}
            entries.append(cur)
        elif cur is None:
            continue
        elif b["kind"] == "img" and not cur["src"]:
            cur["src"] = b["src"]
        elif b["kind"] in ("p", "li"):
            cur["text"].append(b["text"])
    return entries


# --------------------------------------------------------------------------- main
def main():
    today = dt.date.today()
    status = {"last_run": today.isoformat(), "sections": {}}
    old_status = {}
    try:
        old_status = load("gsite_status.json")
    except FileNotFoundError:
        pass
    status["last_change"] = old_status.get("last_change", "")
    changes, problems = [], []

    profile, team, teaching, service, impact, news = (load(f) for f in (
        "profile.json", "team.json", "teaching.json", "service.json", "impact.json", "news.json"))
    before = json.dumps([profile, team, teaching, service, impact, news], sort_keys=True)

    def run(name, fn):
        try:
            fn()
            status["sections"].setdefault(name, "ok")
        except SectionError as e:
            status["sections"][name] = f"skipped: {e}"
            problems.append(f"{name}: {e}")
        except Exception as e:  # unreadable page, image download failure, etc.
            status["sections"][name] = f"failed: {e}"
            problems.append(f"{name}: {e}")

    synced_news = []

    def do_home():
        h = parse_home(read_page(PAGES["home"]), profile)
        photo = save_image(h["portrait_src"], "portrait")
        if photo != profile.get("photo"):
            changes.append("Home: portrait photo changed")
        for key in ("positions", "affiliation", "bio"):
            if profile.get(key) != h[key]:
                changes.append(f"Home: {key} updated")
                profile[key] = h[key]
        profile["photo"] = photo
        for key, val in h["contact"].items():
            if profile.get(key) != val:
                changes.append(f"Home: {key} updated")
                profile[key] = val
        if h["links"] and h["links"] != profile.get("links"):
            profile["links"] = h["links"]
            changes.append("Home: profile links updated")
        if h["identifiers"] and h["identifiers"] != profile.get("identifiers"):
            profile["identifiers"] = h["identifiers"]
            changes.append("Home: researcher identifiers updated")
        for ph in h["photos"]:
            title = re.sub(r"\s*\([^)]*\)\s*$", "", ph["caption"]).strip() or ph["caption"]
            inside = re.search(r"\(([^)]*)\)\s*$", ph["caption"])
            extra = re.sub(DATE_RE, "", inside.group(1)).strip(" ,") if inside else ""
            synced_news.append({"date": date_label(ph["caption"]), "title": title, "text": extra,
                                "image": save_image(ph["src"], "home"), "link": "", "source": "google-site"})

    def do_team():
        t = parse_team(read_page(PAGES["team"]))
        old_members = {m["name"].lower(): m for g in team["groups"] for m in g["members"]}
        count = sum(len(g["members"]) for g in t["groups"])
        check(len(t["groups"]) >= 2 and count >= max(5, 0.6 * len(old_members)),
              f"only {count} team members found (website has {len(old_members)})")
        for g in t["groups"]:
            for m in g["members"]:
                m["linkedin"] = old_members.get(m["name"].lower(), {}).get("linkedin", "")
        new_names = {m["name"].lower(): m["name"] for g in t["groups"] for m in g["members"]}
        for k, v in new_names.items():
            if k not in old_members:
                changes.append(f"Team: added {v}")
        for k, m in old_members.items():
            if k not in new_names:
                changes.append(f"Team: removed {m['name']}")
        team["groups"] = t["groups"]
        if t["photos"] or not team.get("photos"):
            photos = [{"image": save_image(p["src"], "team"), "caption": p["caption"]} for p in t["photos"]]
            if [p["caption"] for p in photos] != [p["caption"] for p in team.get("photos", [])]:
                changes.append("Team: photos updated")
            team["photos"] = photos

    def do_teaching():
        t = parse_teaching(read_page(PAGES["teaching"]))
        check(t["courses"], "no courses found")
        if t["courses"] != teaching["courses"]:
            changes.append("Teaching: courses updated")
        teaching["courses"] = t["courses"]
        if t["photos"] or not teaching.get("photos"):
            photos = [{"image": save_image(p["src"], "teaching"), "caption": p["caption"]} for p in t["photos"]]
            if [p["caption"] for p in photos] != [p["caption"] for p in teaching.get("photos", [])]:
                changes.append("Teaching: photos updated")
            teaching["photos"] = photos

    def do_services():
        s = parse_services(read_page(PAGES["services"]))
        check(len(s["roles"]) >= max(3, 0.6 * len(service["roles"])),
              f"only {len(s['roles'])} service roles found")
        if s["roles"] != service["roles"]:
            changes.append("Services: roles updated")
        service["roles"] = s["roles"]
        if s["images"]:
            old = service.get("images", [])
            imgs = []
            for i, src in enumerate(s["images"]):  # keep website captions by position
                imgs.append({"image": save_image(src, "service"),
                             "caption": old[i]["caption"] if i < len(old) else ""})
            if [x["image"] for x in imgs] != [x["image"] for x in old]:
                changes.append("Services: images updated")
            service["images"] = imgs

    def do_funding():
        f = parse_funding(read_page(PAGES["funding"]))
        check(len(f["projects"]) >= max(3, 0.6 * len(impact["projects"])),
              f"only {len(f['projects'])} funded projects found")
        if f["projects"] != impact["projects"]:
            changes.append("Funding: projects updated")
        impact["projects"] = f["projects"]
        if f["highlights"]:
            if f["highlights"] != impact.get("highlights"):
                changes.append("Impact: highlights updated")
            impact["highlights"] = f["highlights"]
        if f["badge"]:
            badge = save_image(f["badge"], "impact")
            if badge != impact.get("badge"):
                changes.append("Impact: badge image changed")
            impact["badge"] = badge
        photos = [{"image": save_image(src, "impact"), "caption": ""} for src in f["photos"]]
        if [p["image"] for p in photos] != [p["image"] for p in impact.get("photos", [])]:
            changes.append("Impact: photos updated")
        impact["photos"] = photos

    def do_announcements():
        blocks = read_page(PAGES["announcements"])
        if blocks is None:
            status["sections"]["announcements"] = "no page"
            return
        for e in parse_announcements(blocks):
            text = " ".join(e["text"])
            synced_news.append({"date": date_label(e["title"]) or date_label(text),
                                "title": re.sub(DATE_RE, "", e["title"]).strip(" ,()-") or e["title"],
                                "text": text, "image": save_image(e["src"], "news") if e["src"] else "",
                                "link": "", "source": "google-site"})

    run("home", do_home)
    run("team", do_team)
    run("teaching", do_teaching)
    run("services", do_services)
    run("funding", do_funding)
    run("announcements", do_announcements)

    # Latest updates: hand-made cards stay; Google Site cards are replaced by today's set
    if status["sections"].get("home") == "ok":
        manual = [n for n in news if n.get("source") != "google-site"]
        old_synced = [n for n in news if n.get("source") == "google-site"]
        if [(n["title"], n["image"]) for n in synced_news] != [(n["title"], n["image"]) for n in old_synced]:
            changes.append("Latest updates: cards from the Google Site updated")
        merged = manual + synced_news
        merged.sort(key=lambda n: month_value(n.get("date", "")), reverse=True)
        news[:] = merged

    if changes:
        status["last_change"] = today.isoformat()
        profile["last_updated"] = today.strftime("%d %B %Y").lstrip("0")
    after = json.dumps([profile, team, teaching, service, impact, news], sort_keys=True)
    if after != before:
        for name, data in (("profile.json", profile), ("team.json", team), ("teaching.json", teaching),
                           ("service.json", service), ("impact.json", impact), ("news.json", news)):
            save(name, data)
    status["changes"] = changes if changes else old_status.get("changes", [])
    status["problems"] = problems
    save("gsite_status.json", status)

    # Remove copies of photos that are no longer used anywhere
    used = after + json.dumps([load("research.json")])
    if os.path.isdir(IMAGES):
        for f in os.listdir(IMAGES):
            if f"images/gsite/{f}" not in used:
                os.remove(os.path.join(IMAGES, f))

    print("Changes:" if changes else "No changes on the Google Site.")
    for c in changes:
        print("  -", c)
    for name, s in status["sections"].items():
        print(f"  {name}: {s}")
    if problems:
        print("::error::Google Site sync could not read: " + "; ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

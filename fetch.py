"""Fetch recent federated learning papers from arXiv and OpenAlex, merge duplicates, write papers.json."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

FEDERATED = ['"federated learning"', '"federated optimization"', '"federated training"',
             '"federated averaging"', '"federated fine-tuning"', 'FedAvg']
GRAPH = ['"graph neural"', 'GNN', 'GNNs', '"graph learning"', '"graph convolution"', 'GCN',
         '"graph attention"', 'subgraph', '"knowledge graph"', '"graph embedding"', '"graph representation"']


def arxiv_any(terms):
    return "(" + " OR ".join("abs:" + t for t in terms) + ")"


def openalex_any(terms):
    return "(" + " OR ".join(terms) + ")"


# Each topic has an arXiv query, an OpenAlex query, how many days of papers it keeps,
# and how many of the newest arXiv papers to fetch per run. A paper matching several topics gets several tags.
TOPICS = {
    "fl-gnn": {
        "arxiv": 'abs:federated AND ' + arxiv_any(GRAPH),
        "openalex": 'federated AND ' + openalex_any(GRAPH),
        "days": 180,
        "arxiv_max": 500,
    },
    "fl": {
        "arxiv": arxiv_any(FEDERATED),
        "openalex": openalex_any(FEDERATED),
        "days": 60,
        "arxiv_max": 100,
    },
    # OpenAlex only: AI/ML papers from selected Nature journals (any AI topic, not just FL).
    "nature-ai": {
        "openalex": openalex_any(['"machine learning"', '"deep learning"', '"neural network"',
                                  '"artificial intelligence"', '"language model"', '"foundation model"',
                                  '"reinforcement learning"']),
        "journals": ["Nature", "Nature Communications", "Nature Machine Intelligence",
                     "Nature Computational Science", "Nature Human Behaviour"],
        "days": 60,
    },
    # Federated learning papers accepted at top conferences, this year and last year.
    # NeurIPS, ICML and ICLR come from OpenReview (free, no key, with abstracts).
    # AAAI and IJCAI come from the OpenAlex searches above (see CONF_FROM_VENUE).
    "conf": {
        "openreview": ["NeurIPS", "ICML", "ICLR"],
        "match": r"federat",  # kept if the title or abstract matches
        # no "days": conference papers stay while their conference is this year's or last year's
    },
}

# Published papers are kept only if their venue matches one of these (case-insensitive regexes).
# arXiv preprints are always kept.
TOP_VENUES = [
    r"^nature\b",                                            # Nature and Nature-family journals
    r"\bieee transactions\b",
    r"neural information processing systems|\bneurips\b",
    r"international conference on machine learning(?! and)",
    r"international conference on learning representations",
    r"sigkdd|\bkdd\b",
    r"aaai conference on artificial intelligence",
    r"international joint conference on artificial intelligence|\bijcai\b",
    r"\bweb conference\b|world wide web conference",
    r"computer vision and pattern recognition",
    r"international conference on computer vision(?! and)",
    r"\binfocom\b",
    r"international conference on data engineering(?! and)",
    r"\bvldb\b|very large data bases",
    r"\bsigmod\b",
    r"web search and data mining",
    r"information and knowledge management",
    r"international conference on data mining(?! and)",
    r"\bmobicom\b",
]
OPENALEX_MAX_PAGES = 100  # 100 papers per page; most are dropped by the venue filter
OUT = "papers.json"
NS = {"a": "http://www.w3.org/2005/Atom"}


def clean(text):
    return " ".join((text or "").split())


def norm_title(title):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def norm_doi(doi):
    return doi.lower().removeprefix("https://doi.org/")


OPENREVIEW_HEADERS = {"User-Agent": "paper-feed/1.0 (+https://s-masif.github.io/paper-feed/)"}

# OpenAlex venues that also go into the conference tab, with their short name.
CONF_FROM_VENUE = [
    (r"aaai conference on artificial intelligence", "AAAI"),
    (r"international joint conference on artificial intelligence|\bijcai\b", "IJCAI"),
]


def openreview_login(username, password):
    """Log in to OpenReview's API (required since late 2026) and return a bearer token, or None."""
    req = urllib.request.Request("https://api2.openreview.net/login", method="POST",
                                 data=json.dumps({"id": username, "password": password}).encode(),
                                 headers={**OPENREVIEW_HEADERS, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["token"]
    except urllib.error.HTTPError as e:
        print(f"OpenReview login failed (HTTP {e.code}): {e.read()[:200]!r}")
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        print(f"OpenReview login failed ({type(e).__name__}: {e})")
    return None


def fetch_openreview(conference, year, pattern, token):
    """(matching papers, number of accepted papers) for one OpenReview conference.
    Returns None if OpenReview could not be read, so the conference is retried next run."""
    venue_id = f"{conference}.cc/{year}/Conference"
    papers, offset, limit, total = [], 0, 1000, 0
    while True:
        params = urllib.parse.urlencode({"content.venueid": venue_id, "limit": limit, "offset": offset})
        body = b""
        try:
            body = get("https://api2.openreview.net/notes?" + params,
                       {**OPENREVIEW_HEADERS, "Authorization": f"Bearer {token}"})
            notes = json.loads(body)["notes"]
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            print(f"OpenReview {venue_id} failed ({type(e).__name__}: {e}); reply started with: {body[:150]!r}")
            return None
        total += len(notes)
        for n in notes:
            c = n.get("content") or {}
            val = lambda k: (c.get(k) or {}).get("value")
            title, abstract = clean(val("title")), clean(val("abstract"))
            if not title or not re.search(pattern, title + " " + abstract, re.I):
                continue
            ms = n.get("pdate") or n.get("cdate")
            papers.append({
                "id": "openreview:" + n["id"],
                "title": title,
                "authors": val("authors") or [],
                "abstract": abstract,
                "published": datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if ms else f"{year}-01-01",
                "venue": val("venue") or f"{conference} {year}",  # e.g. "ICLR 2026 Poster"
                "conf": f"{conference} {year}",
                "dois": [],
                "links": {"OpenReview": f"https://openreview.net/forum?id={n['id']}",
                          "PDF": f"https://openreview.net/pdf?id={n['id']}"},
            })
        if len(notes) < limit:
            return papers, total
        offset += limit
        time.sleep(1)  # be polite to OpenReview's free API


openalex_calls = 0  # counted so each run can report its usage


def get(url, headers=None):
    global openalex_calls
    if "api.openalex.org" in url:
        openalex_calls += 1
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers) if headers else url, timeout=60) as resp:
        return resp.read()


def is_top_venue(venue):
    return bool(venue) and any(re.search(p, venue, re.I) for p in TOP_VENUES)


def fetch_arxiv(query, max_results):
    params = urllib.parse.urlencode({
        "search_query": query, "sortBy": "submittedDate",
        "sortOrder": "descending", "max_results": max_results,
    })
    root = ET.fromstring(get("http://export.arxiv.org/api/query?" + params))
    papers = []
    for e in root.findall("a:entry", NS):
        abs_url = e.find("a:id", NS).text.strip()
        arxiv_id = abs_url.rsplit("/abs/", 1)[-1].split("v")[0]
        links = {"arXiv": abs_url}
        pdf = next((l.get("href") for l in e.findall("a:link", NS) if l.get("title") == "pdf"), None)
        if pdf:
            links["PDF"] = pdf
        papers.append({
            "id": "arxiv:" + arxiv_id,
            "title": clean(e.find("a:title", NS).text),
            "authors": [a.find("a:name", NS).text for a in e.findall("a:author", NS)],
            "abstract": clean(e.find("a:summary", NS).text),
            "published": e.find("a:published", NS).text[:10],
            "venue": None,
            "dois": ["10.48550/arxiv." + arxiv_id.lower()],  # arXiv's own DOI, which OpenAlex uses
            "links": links,
        })
    return papers


def abstract_from_index(inverted):
    if not inverted:
        return ""
    words = {pos: word for word, positions in inverted.items() for pos in positions}
    return clean(" ".join(words[i] for i in sorted(words)))


def openalex_source_ids(names, api_key):
    """Look up each journal's OpenAlex ID by exact name."""
    ids = []
    for name in names:
        params = urllib.parse.urlencode({"search": name, "per_page": 25, "select": "id,display_name", "api_key": api_key})
        results = json.loads(get("https://api.openalex.org/sources?" + params))["results"]
        match = next((r["id"] for r in results if r["display_name"].lower() == name.lower()), None)
        if match:
            ids.append(match.rsplit("/", 1)[-1])
        else:
            print(f"Journal not found in OpenAlex, skipped: {name}")
    return ids


def fetch_openalex(query, api_key, days, extra_filter=""):
    today = date.today()
    base = {
        "search.title_abstract_keywords": query,  # keywords also catch papers with no abstract
        "filter": f"from_publication_date:{today - timedelta(days=days)},to_publication_date:{today}" + extra_filter,
        "sort": "publication_date:desc",
        "per_page": 100,
        "select": "id,doi,title,publication_date,authorships,abstract_inverted_index,primary_location,open_access",
        "api_key": api_key,
    }
    papers, cursor = [], "*"
    for _ in range(OPENALEX_MAX_PAGES):
        data = json.loads(get("https://api.openalex.org/works?" + urllib.parse.urlencode({**base, "cursor": cursor})))
        for w in data["results"]:
            if not w.get("title"):
                continue
            doi = norm_doi(w["doi"]) if w.get("doi") else None
            loc = w.get("primary_location") or {}
            is_arxiv = bool(doi and doi.startswith("10.48550/arxiv."))
            if not is_arxiv and not is_top_venue((loc.get("source") or {}).get("display_name")):
                continue
            page = loc.get("landing_page_url") or w.get("doi")
            links = {}
            if page:
                links["arXiv" if is_arxiv else "Publisher"] = page
            pdf = loc.get("pdf_url") or (w.get("open_access") or {}).get("oa_url")
            if pdf:
                links["PDF"] = pdf
            papers.append({
                "id": doi or w["id"],
                "title": clean(w["title"]),
                "authors": [a["author"]["display_name"] for a in w.get("authorships", [])],
                "abstract": abstract_from_index(w.get("abstract_inverted_index")),
                "published": w["publication_date"],
                "venue": None if is_arxiv else (loc.get("source") or {}).get("display_name"),
                "dois": [doi] if doi else [],
                "links": links,
            })
            venue = papers[-1]["venue"] or ""
            short = next((abbr for pat, abbr in CONF_FROM_VENUE if re.search(pat, venue, re.I)), None)
            if short:
                papers[-1]["conf"] = f"{short} {w['publication_date'][:4]}"
        cursor = data["meta"].get("next_cursor")
        if not cursor:
            break
    return papers


class Store:
    """Papers keyed so that the same paper from two sources becomes one entry (match on DOI, else title)."""

    def __init__(self, papers):
        self.papers, self.by_doi, self.by_title = [], {}, {}
        for p in papers:
            self.add(p, set(p["tags"]))

    def _index(self, p):
        for d in p["dois"]:
            self.by_doi[d] = p
        self.by_title[norm_title(p["title"])] = p

    def add(self, new, tags):
        match = next((self.by_doi[d] for d in new["dois"] if d in self.by_doi), None) \
            or self.by_title.get(norm_title(new["title"]))
        if match is None:
            new["tags"] = sorted(tags)
            self.papers.append(new)
            self._index(new)
            return
        match["tags"] = sorted(set(match["tags"]) | tags)
        match["dois"] = sorted(set(match["dois"]) | set(new["dois"]))
        for label, url in new["links"].items():
            match["links"].setdefault(label, url)
        match["abstract"] = match["abstract"] or new["abstract"]
        match["venue"] = match["venue"] or new["venue"]
        if new.get("conf"):
            match.setdefault("conf", new["conf"])
        match["published"] = min(match["published"], new["published"])
        self._index(match)


def main():
    try:
        with open(OUT) as f:
            saved = json.load(f)
    except FileNotFoundError:
        saved = {"papers": []}
    store = Store(saved["papers"])
    conf_done = set(saved.get("conf_done", []))  # conference-years already fetched; accepted lists don't change

    for i, (tag, q) in enumerate(t for t in TOPICS.items() if "arxiv" in t[1]):
        if i:
            time.sleep(3)  # arXiv asks for 3 s between API calls
        for p in fetch_arxiv(q["arxiv"], q["arxiv_max"]):
            store.add(p, {tag})

    or_user, or_pass = os.environ.get("OPENREVIEW_USERNAME"), os.environ.get("OPENREVIEW_PASSWORD")
    or_token = openreview_login(or_user, or_pass) if or_user and or_pass else None
    if not (or_user and or_pass):
        print("OPENREVIEW_USERNAME/OPENREVIEW_PASSWORD not set: skipping NeurIPS, ICML and ICLR")
    for tag, q in TOPICS.items():
        if "openreview" not in q or not or_token:
            continue
        for year in (date.today().year, date.today().year - 1):
            for conference in q["openreview"]:
                if f"{conference} {year}" in conf_done:
                    continue
                result = fetch_openreview(conference, year, q["match"], or_token)
                if result is None:
                    continue  # failed: try again next run
                papers, accepted = result
                for p in papers:
                    store.add(p, {tag})
                if accepted:  # no accepted papers yet means decisions aren't out, so check again later
                    conf_done.add(f"{conference} {year}")
                print(f"OpenReview {conference} {year}: {accepted} accepted, {len(papers)} on federated learning")
                time.sleep(1)

    api_key = os.environ.get("OPENALEX_API_KEY")
    if api_key:
        for tag, q in TOPICS.items():
            if "openalex" not in q:
                continue
            extra = ""
            if "journals" in q:
                ids = openalex_source_ids(q["journals"], api_key)
                if not ids:  # never run this search without the journal restriction
                    continue
                extra = ",primary_location.source.id:" + "|".join(ids)
            for p in fetch_openalex(q["openalex"], api_key, q["days"], extra):
                store.add(p, {tag} | ({"conf"} if p.get("conf") else set()))
    else:
        print("OPENALEX_API_KEY not set: skipping OpenAlex, feed has arXiv papers only")

    # A paper stays while it belongs in at least one of its topics: dated topics keep their last N days,
    # the conference topic keeps this year's and last year's conferences (same years as it fetches).
    cutoffs = {tag: (date.today() - timedelta(days=q["days"])).isoformat() for tag, q in TOPICS.items() if "days" in q}
    first_conf_year = date.today().year - 1

    def keep(p):
        return any(int(p["conf"].rsplit(" ", 1)[1]) >= first_conf_year if t == "conf" else p["published"] >= cutoffs[t]
                   for t in p["tags"])

    papers = sorted((p for p in store.papers if keep(p)), key=lambda p: p["published"], reverse=True)

    with open(OUT, "w") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "conf_done": sorted(conf_done), "papers": papers}, f, indent=1)
    print(f"{len(papers)} papers in feed")
    print(f"OpenAlex calls this run: {openalex_calls} (about ${openalex_calls * 0.001:.3f} of the free $1/day)")


if __name__ == "__main__":
    main()

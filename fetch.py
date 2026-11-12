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
    # DBLP only (free, no key): federated learning papers accepted at these conferences,
    # this year and last year. DBLP terms are prefixes, so "federat" matches "federated".
    "conf": {
        "dblp": "federat",
        "conferences": ["NeurIPS", "ICML", "ICLR", "AAAI", "IJCAI"],
        "days": 730,
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


DBLP_HOSTS = ["https://dblp.org", "https://dblp.uni-trier.de"]  # main site, then official mirror
DBLP_HEADERS = {"User-Agent": "paper-feed/1.0 (+https://s-masif.github.io/paper-feed/)"}


def dblp_search(params):
    """DBLP search result as JSON, or None if every host gave a bad reply."""
    for host in DBLP_HOSTS:
        body = b""
        try:
            body = get(f"{host}/search/publ/api?{params}", DBLP_HEADERS)
            return json.loads(body)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            print(f"DBLP {host} failed ({type(e).__name__}: {e}); reply started with: {body[:150]!r}")
            time.sleep(2)
    return None


def fetch_dblp(query, conference, year):
    """Accepted papers at one conference in one year whose title matches the query."""
    params = urllib.parse.urlencode({"q": f"{query} venue:{conference} year:{year}", "format": "json", "h": 1000})
    data = dblp_search(params)
    if data is None:
        print(f"Skipped {conference} {year}: DBLP unavailable this run")
        return []
    hits = data["result"]["hits"].get("hit", [])
    papers = []
    for h in hits:
        info = h["info"]
        venues = info.get("venue")
        venues = venues if isinstance(venues, list) else [venues]
        if conference.lower() not in [str(v).lower() for v in venues] or str(info.get("year")) != str(year):
            continue  # skip workshops and other venues the prefix search also matched
        authors = (info.get("authors") or {}).get("author", [])
        authors = authors if isinstance(authors, list) else [authors]
        ee = info.get("ee")
        ee = ee[0] if isinstance(ee, list) else ee
        papers.append({
            "id": "dblp:" + info.get("key", info["title"]),
            "title": clean(info["title"]).rstrip("."),
            "authors": [re.sub(r"\s\d{4}$", "", a["text"]) for a in authors],  # drop "0001" name suffixes
            "abstract": "",
            "published": f"{year}-01-01",  # DBLP only gives the year
            "date_from_year": True,
            "venue": conference,
            "conf": f"{conference} {year}",
            "dois": [norm_doi(info["doi"])] if info.get("doi") else [],
            "links": {"Publisher": ee} if ee else {},
        })
    return papers


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
        if new.get("date_from_year"):
            pass  # keep the existing date
        elif match.get("date_from_year"):
            match["published"] = new["published"]
            match["date_from_year"] = False
        else:
            match["published"] = min(match["published"], new["published"])
        self._index(match)


def main():
    try:
        with open(OUT) as f:
            store = Store(json.load(f)["papers"])
    except FileNotFoundError:
        store = Store([])

    for i, (tag, q) in enumerate(t for t in TOPICS.items() if "arxiv" in t[1]):
        if i:
            time.sleep(3)  # arXiv asks for 3 s between API calls
        for p in fetch_arxiv(q["arxiv"], q["arxiv_max"]):
            store.add(p, {tag})

    for tag, q in TOPICS.items():
        if "dblp" not in q:
            continue
        for year in (date.today().year, date.today().year - 1):
            for conference in q["conferences"]:
                time.sleep(1)  # be polite to DBLP's free API
                for p in fetch_dblp(q["dblp"], conference, year):
                    store.add(p, {tag})

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
                store.add(p, {tag})
    else:
        print("OPENALEX_API_KEY not set: skipping OpenAlex, feed has arXiv papers only")

    # A paper stays while it is within the time window of at least one of its topics.
    cutoffs = {tag: (date.today() - timedelta(days=q["days"])).isoformat() for tag, q in TOPICS.items()}
    papers = sorted((p for p in store.papers if any(p["published"] >= cutoffs[t] for t in p["tags"])),
                    key=lambda p: p["published"], reverse=True)

    with open(OUT, "w") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "papers": papers}, f, indent=1)
    print(f"{len(papers)} papers in feed")
    print(f"OpenAlex calls this run: {openalex_calls} (about ${openalex_calls * 0.001:.3f} of the free $1/day)")


if __name__ == "__main__":
    main()

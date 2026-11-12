"""Fetch recent federated learning papers from arXiv and OpenAlex, merge duplicates, write papers.json."""
import json
import os
import re
import time
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


def get(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
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


def fetch_openalex(query, api_key, days):
    today = date.today()
    base = {
        "search.title_abstract_keywords": query,  # keywords also catch papers with no abstract
        "filter": f"from_publication_date:{today - timedelta(days=days)},to_publication_date:{today}",
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
        match["published"] = min(match["published"], new["published"])
        self._index(match)


def main():
    try:
        with open(OUT) as f:
            store = Store(json.load(f)["papers"])
    except FileNotFoundError:
        store = Store([])

    for i, (tag, q) in enumerate(TOPICS.items()):
        if i:
            time.sleep(3)  # arXiv asks for 3 s between API calls
        for p in fetch_arxiv(q["arxiv"], q["arxiv_max"]):
            store.add(p, {tag})

    api_key = os.environ.get("OPENALEX_API_KEY")
    if api_key:
        for tag, q in TOPICS.items():
            for p in fetch_openalex(q["openalex"], api_key, q["days"]):
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


if __name__ == "__main__":
    main()

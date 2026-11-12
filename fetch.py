"""Fetch recent federated learning papers from arXiv and OpenAlex, merge duplicates, write papers.json."""
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

# Each topic has an arXiv query and an OpenAlex query. A paper matching several gets several tags.
TOPICS = {
    "fl-gnn": {
        "arxiv": 'abs:"federated" AND (abs:"graph neural" OR abs:"GNN" OR abs:"graph learning")',
        "openalex": 'federated AND ("graph neural" OR GNN OR "graph learning")',
    },
    "fl": {
        "arxiv": 'abs:"federated learning"',
        "openalex": '"federated learning"',
    },
}
ARXIV_MAX = 100          # newest arXiv papers per topic per run
OPENALEX_LOOKBACK = 14   # days; journals and conferences reach OpenAlex with some delay
OPENALEX_MAX_PAGES = 10  # 100 papers per page
KEEP_DAYS = 60           # papers older than this drop out of the feed
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


def fetch_arxiv(query):
    params = urllib.parse.urlencode({
        "search_query": query, "sortBy": "submittedDate",
        "sortOrder": "descending", "max_results": ARXIV_MAX,
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


def fetch_openalex(query, api_key):
    today = date.today()
    base = {
        "search.title_and_abstract": query,
        "filter": f"from_publication_date:{today - timedelta(days=OPENALEX_LOOKBACK)},to_publication_date:{today}",
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
        for p in fetch_arxiv(q["arxiv"]):
            store.add(p, {tag})

    api_key = os.environ.get("OPENALEX_API_KEY")
    if api_key:
        for tag, q in TOPICS.items():
            for p in fetch_openalex(q["openalex"], api_key):
                store.add(p, {tag})
    else:
        print("OPENALEX_API_KEY not set: skipping OpenAlex, feed has arXiv papers only")

    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    papers = sorted((p for p in store.papers if p["published"] >= cutoff),
                    key=lambda p: p["published"], reverse=True)

    with open(OUT, "w") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "papers": papers}, f, indent=1)
    print(f"{len(papers)} papers in feed")


if __name__ == "__main__":
    main()

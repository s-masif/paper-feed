"""Fetch new federated learning papers (and a curated set of AI papers from Nature journals), merge duplicates,
and write papers.json (the fetcher's full state) plus the small files in feed/ that the web page loads.

Tabs on the page, and where their papers come from:
  preprint  arXiv preprints on federated learning (arXiv API, free)
  ieee-tx   federated learning papers in IEEE Transactions journals (OpenAlex, filter-only listing)
  conf      federated learning papers at top conferences (OpenReview for NeurIPS/ICML/ICLR, dblp for the rest,
            abstracts filled in from OpenAlex by DOI)
  nature    AI papers from Nature journals, picked by a relevance score (OpenAlex, filter-only listing)
No OpenAlex keyword searches are used, apart from looking up journal IDs once a month.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

SITE_URL = "https://s-masif.github.io/paper-feed/"
USER_AGENT = {"User-Agent": f"paper-feed/2.0 (+{SITE_URL})"}

# ---------------------------------------------------------------------------------------------------------
# What to track
# ---------------------------------------------------------------------------------------------------------
FEDERATED = ['"federated learning"', '"federated optimization"', '"federated training"',
             '"federated averaging"', '"federated fine-tuning"', '"federated unlearning"', 'FedAvg']
GRAPH = ['"graph neural"', 'GNN', 'GNNs', '"graph learning"', '"graph convolution"', 'GCN',
         '"graph attention"', 'subgraph', '"knowledge graph"', '"graph embedding"', '"graph representation"']


def arxiv_any(terms):
    return "(" + " OR ".join("abs:" + t for t in terms) + ")"


# arXiv searches: (query, how many of the newest papers to read per run).
ARXIV_QUERIES = [
    (arxiv_any(FEDERATED), 150),
    ("abs:federated AND " + arxiv_any(GRAPH), 500),
]

# How long each tab keeps a paper, in days (by its publication date).
KEEP_DAYS = {"preprint": 90, "preprint-graph": 180, "ieee-tx": 180, "nature": 90}
# Conference papers stay while their conference is this year's or last year's.

# A paper counts as federated learning if its title or abstract matches this (used for IEEE, Nature, dblp).
FED_RE = re.compile(r"federat|\bfedavg\b", re.I)
# ...unless the title is about a different kind of federation (federated databases, identity, ...).
NOT_FL_RE = re.compile(r"federated (quer|databases?|search|identit|sparql|catalog|data ?lake)", re.I)
GRAPH_RE = re.compile(r"graph neural|\bgnns?\b|graph learning|graph convolution|\bgcn\b|graph attention|subgraph"
                      r"|knowledge graph|graph embedding|graph representation|graph transformer", re.I)
IEEE_TX_RE = re.compile(r"^ieee(/[a-z]+)? transactions\b", re.I)

# Nature tab: journals listed in full, then scored (see nature_score). Names must match OpenAlex exactly;
# a journal OpenAlex doesn't know is skipped with a message in the log.
NATURE_JOURNALS = ["Nature", "Nature Communications", "Nature Machine Intelligence",
                   "Nature Computational Science", "npj Artificial Intelligence"]
NATURE_KEEP_ALL = {"Nature Machine Intelligence", "npj Artificial Intelligence"}  # every paper is kept
NATURE_MIN_SCORE = 6
NATURE_RE = re.compile(r"^(nature|npj)\b", re.I)

# Conferences read from dblp: (short name, dblp key prefix). The key prefix is how dblp files the venue's
# papers (dblp.org/db/<prefix>), so results are checked against it and nothing from another venue gets in.
DBLP_VENUES = [
    # machine learning and AI
    ("NeurIPS", "conf/nips"), ("ICML", "conf/icml"), ("ICLR", "conf/iclr"), ("AAAI", "conf/aaai"),
    ("IJCAI", "conf/ijcai"), ("AISTATS", "conf/aistats"), ("MLSys", "conf/mlsys"),
    # data mining, web, information retrieval, databases
    ("KDD", "conf/kdd"), ("WWW", "conf/www"), ("SIGIR", "conf/sigir"), ("CIKM", "conf/cikm"),
    ("WSDM", "conf/wsdm"), ("ICDM", "conf/icdm"), ("ICDE", "conf/icde"), ("SIGMOD", "journals/pacmmod"),
    ("VLDB", "journals/pvldb"),
    # vision, multimedia, language
    ("CVPR", "conf/cvpr"), ("ICCV", "conf/iccv"), ("ECCV", "conf/eccv"), ("ACM MM", "conf/mm"),
    ("ACL", "conf/acl"), ("EMNLP", "conf/emnlp"), ("NAACL", "conf/naacl"),
    # networking, mobile and systems
    ("INFOCOM", "conf/infocom"), ("MobiCom", "conf/mobicom"), ("MobiSys", "conf/mobisys"),
    ("MobiHoc", "conf/mobihoc"), ("SenSys", "conf/sensys"),
    # security and privacy (attacks on and defenses for federated learning)
    ("CCS", "conf/ccs"), ("USENIX Security", "conf/uss"), ("IEEE S&P", "conf/sp"), ("NDSS", "conf/ndss"),
]
# Proceedings parts that aren't main-conference papers.
DBLP_SKIP_VENUE = re.compile(r"workshop|companion|findings|demo|adjunct|poster", re.I)
DBLP_EVERY_DAYS = 7  # proceedings appear in batches, so dblp is read once a week

# NeurIPS, ICML and ICLR also come from OpenReview (with abstracts), when OpenReview login details are set.
OPENREVIEW_CONFS = ["NeurIPS", "ICML", "ICLR"]

# IEEE Xplore (optional): used only when an IEEE_API_KEY secret is set. It finds IEEE Transactions papers,
# early access included, the day IEEE adds them, with abstracts. If the key is missing, not yet approved or the
# API fails, the run carries on with OpenAlex alone. Each run asks only for papers inserted since the last
# successful run (minus a few days of overlap), so it uses a handful of the 200 free calls a day.
IEEE_CONTENT_TYPES = ["Journals", "Early Access Articles"]
IEEE_MAX_PAGES = 10  # per content type, 200 records a page

# OpenAlex listings re-read RECENT_DAYS on a normal run and their whole window once a week
# (FULL_SWEEP_WEEKDAY, 6 = Sunday), which catches papers OpenAlex adds late.
RECENT_DAYS = 21
FULL_SWEEP_WEEKDAY = 6
ABSTRACT_RETRY_DAYS = 7  # how often to ask OpenAlex again for an abstract it didn't have

OUT = "papers.json"
FEED_DIR = "feed"
RSS_ITEMS = 60
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
TODAY = date.today()


def clean(text):
    return " ".join((text or "").split())


def norm_title(title):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def norm_doi(doi):
    return doi.lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/")


# ---------------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------------
openalex_calls = {"search": 0, "list": 0}  # counted so each run can report its cost


def get(url, headers=None):
    if "api.openalex.org" in url:
        query = urllib.parse.urlparse(url).query
        openalex_calls["search" if "search" in query else "list"] += 1
    req = urllib.request.Request(url, headers={**USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


# ---------------------------------------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------------------------------------
ACCEPTED_RE = re.compile(r"\b(accepted|to appear|published|camera[- ]ready|presented)\b[^.;\n]*", re.I)


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
        # "Accepted at AAAI 2026" or a journal reference, shown under the title of a preprint.
        journal, comment = (clean(getattr(e.find(f"arxiv:{tag}", NS), "text", "")) for tag in ("journal_ref", "comment"))
        m = ACCEPTED_RE.search(comment)
        note = journal and f"Published in {journal}" or (m and m.group(0)[:120].rstrip(" ,:")) or ""
        papers.append({
            "id": "arxiv:" + arxiv_id,
            "title": clean(e.find("a:title", NS).text),
            "authors": [a.find("a:name", NS).text for a in e.findall("a:author", NS)],
            "abstract": clean(e.find("a:summary", NS).text),
            "published": e.find("a:published", NS).text[:10],
            "venue": None,
            "dois": ["10.48550/arxiv." + arxiv_id.lower()],  # arXiv's own DOI, which OpenAlex uses
            "links": links,
            **({"note": note[0].upper() + note[1:]} if note else {}),
        })
    return papers


# ---------------------------------------------------------------------------------------------------------
# OpenReview (NeurIPS, ICML, ICLR)
# ---------------------------------------------------------------------------------------------------------
OPENREVIEW_HEADERS = USER_AGENT
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




# ---------------------------------------------------------------------------------------------------------
# dblp (all other conferences): titles, authors and DOIs, no abstracts
# ---------------------------------------------------------------------------------------------------------
def dblp_hits(query):
    """All hits of one dblp search, as dblp 'info' records."""
    hits, offset = [], 0
    while True:
        params = urllib.parse.urlencode({"q": query, "format": "json", "h": 1000, "f": offset})
        result = json.loads(get("https://dblp.org/search/publ/api?" + params))["result"]
        page = [h["info"] for h in (result.get("hits") or {}).get("hit") or []]
        hits += page
        total = int((result.get("hits") or {}).get("@total", 0))
        offset += len(page)
        if not page or offset >= min(total, 5000):
            return hits
        time.sleep(1)


def as_list(value):
    return value if isinstance(value, list) else [] if value is None else [value]


def fetch_dblp_venue(label, prefix):
    """Federated learning papers from this year's and last year's edition of one venue, or None on failure."""
    first_year = TODAY.year - 1
    # dblp's query syntax: "streamid:" limits a search to one venue. The plain venue name is a fallback;
    # either way, results are checked against the venue's key prefix below.
    for query in (f"federat streamid:{prefix}:", f"federat venue:{label.split()[-1]}"):
        try:
            hits = dblp_hits(query)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"dblp {label} failed ({type(e).__name__}: {e})")
            return None
        papers = []
        for info in hits:
            if not info.get("key", "").startswith(prefix + "/"):
                continue
            title = clean(info.get("title", "")).rstrip(".")
            venue = " ".join(as_list(info.get("venue")))
            year = int(info.get("year") or 0)
            if year < first_year or DBLP_SKIP_VENUE.search(venue) or not FED_RE.search(title) \
                    or NOT_FL_RE.search(title):
                continue
            doi = norm_doi(info["doi"]) if info.get("doi") else None
            ee = next((u for u in as_list(info.get("ee")) if u.startswith("http")), None)
            authors = [re.sub(r" \d{4}$", "", a["text"] if isinstance(a, dict) else a)  # "Wei Wang 0001"
                       for a in as_list((info.get("authors") or {}).get("author"))]
            links = {"Publisher": f"https://doi.org/{doi}" if doi else ee or info.get("url")}
            if info.get("access") == "open" and ee and ee.lower().endswith(".pdf"):
                links["PDF"] = ee
            papers.append({
                "id": doi or "dblp:" + info["key"],
                "title": title,
                "authors": authors,
                "abstract": "",
                "published": f"{year}-01-01",
                "venue": label,
                "conf": f"{label} {year}",
                "dois": [doi] if doi else [],
                "links": links,
            })
        if hits or query.startswith("federat venue:"):
            return papers
    return []


# ---------------------------------------------------------------------------------------------------------
# IEEE Xplore (optional)
# ---------------------------------------------------------------------------------------------------------
def ieee_date(a):
    """The date IEEE first put the paper online, as YYYY-MM-DD (early access date when there is one)."""
    for key in ("online_date", "insert_date", "publication_date"):
        value = clean(str(a.get(key) or ""))
        if re.fullmatch(r"\d{8}", value):
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        for fmt in ("%d %b. %Y", "%d %B %Y", "%b. %Y", "%B %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.replace("Sept.", "Sep."), fmt).date().isoformat()
            except ValueError:
                pass
    return TODAY.isoformat()


def fetch_ieee_xplore(api_key, since):
    """Federated learning papers in IEEE Transactions inserted in IEEE Xplore since `since` (a date).
    Returns None if the API can't be used (key pending or rejected, limit reached, outage)."""
    papers = []
    for content_type in IEEE_CONTENT_TYPES:
        start = 1
        for _ in range(IEEE_MAX_PAGES):
            params = urllib.parse.urlencode({
                "apikey": api_key, "querytext": "federated", "content_type": content_type,
                "start_date": since.strftime("%Y%m%d"), "max_records": 200, "start_record": start,
                "sort_field": "article_number", "sort_order": "desc", "format": "json",
            })
            try:
                data = json.loads(get("https://ieeexploreapi.ieee.org/api/v1/search/articles?" + params))
            except urllib.error.HTTPError as e:
                print(f"IEEE Xplore not available (HTTP {e.code}: {e.read()[:120]!r}); using OpenAlex only")
                return None
            except (urllib.error.URLError, json.JSONDecodeError) as e:
                print(f"IEEE Xplore failed ({type(e).__name__}: {e}); using OpenAlex only")
                return None
            articles = data.get("articles") or []
            for a in articles:
                title = clean(re.sub(r"<[^>]+>", "", a.get("title") or ""))
                venue = clean(a.get("publication_title") or "")
                abstract = clean(re.sub(r"<[^>]+>", "", a.get("abstract") or ""))
                if not title or not IEEE_TX_RE.search(venue) or not is_fl(title + " " + abstract, title):
                    continue
                doi = norm_doi(a["doi"]) if a.get("doi") else None
                links = {"Publisher": a.get("html_url") or (f"https://doi.org/{doi}" if doi else None)}
                if a.get("access_type") in ("OPEN_ACCESS", "Open Access") and a.get("pdf_url"):
                    links["PDF"] = a["pdf_url"]
                papers.append({
                    "id": doi or f"ieee:{a.get('article_number')}",
                    "title": title,
                    "authors": [x.get("full_name", "") for x in (a.get("authors") or {}).get("authors") or []],
                    "abstract": abstract,
                    "published": ieee_date(a),
                    "venue": venue,
                    "dois": [doi] if doi else [],
                    "links": {k: v for k, v in links.items() if v},
                })
            start += len(articles)
            if not articles or start > int(data.get("total_records") or 0):
                break
            time.sleep(0.2)  # the free key allows 10 calls a second
    print(f"IEEE Xplore: {len(papers)} federated learning papers in IEEE Transactions inserted since {since}")
    return papers


# ---------------------------------------------------------------------------------------------------------
# OpenAlex (filter-only listings: $0.0001 per page of 100)
# ---------------------------------------------------------------------------------------------------------
OPENALEX_SELECT = ("id,doi,title,publication_date,authorships,abstract_inverted_index,primary_location,"
                   "open_access,type,primary_topic,topics")


def abstract_from_index(inverted):
    if not inverted:
        return ""
    words = {pos: word for word, positions in inverted.items() for pos in positions}
    return clean(" ".join(words[i] for i in sorted(words)))


def openalex_list(path, params, api_key):
    """Every result of a filter-only OpenAlex list, via cursor paging."""
    cursor = "*"
    while cursor:
        data = json.loads(get(f"https://api.openalex.org/{path}?" + urllib.parse.urlencode(
            {**params, "per_page": 100, "cursor": cursor, "api_key": api_key})))
        yield from data["results"]
        cursor = data["meta"].get("next_cursor") if data["results"] else None


def paper_from_work(w):
    """One OpenAlex work as a feed paper, or None if it has no title."""
    if not w.get("title"):
        return None
    doi = norm_doi(w["doi"]) if w.get("doi") else None
    loc = w.get("primary_location") or {}
    links = {}
    page = loc.get("landing_page_url") or w.get("doi")
    if page:
        links["Publisher"] = page
    pdf = loc.get("pdf_url") or (w.get("open_access") or {}).get("oa_url")
    if pdf:
        links["PDF"] = pdf
    return {
        "id": doi or w["id"],
        "title": clean(re.sub(r"<[^>]+>", "", w["title"])),  # some titles carry <i>, <sub> tags
        "authors": [a["author"]["display_name"] for a in w.get("authorships", [])],
        "abstract": abstract_from_index(w.get("abstract_inverted_index")),
        "published": w["publication_date"],
        "venue": (loc.get("source") or {}).get("display_name"),
        "dois": [doi] if doi else [],
        "links": links,
    }


def cached(cache, days=30):
    return cache if cache and cache.get("checked", "") >= (TODAY - timedelta(days=days)).isoformat() else None


def ieee_transactions_sources(api_key, cache):
    """OpenAlex IDs of IEEE Transactions journals active this year or last (looked up once a month)."""
    if cached(cache):
        return cache
    # IEEE's publisher ID, read from a journal that is certainly IEEE's (TPAMI, ISSN 0162-8828).
    tpami = json.loads(get("https://api.openalex.org/sources?" + urllib.parse.urlencode(
        {"filter": "issn:0162-8828", "select": "id,host_organization", "api_key": api_key})))["results"]
    publisher = tpami[0]["host_organization"].rsplit("/", 1)[-1]
    recent, ids = {TODAY.year, TODAY.year - 1}, []
    params = {"select": "id,display_name,counts_by_year"}
    try:
        sources = list(openalex_list("sources", {**params, "filter": f"host_organization_lineage:{publisher},type:journal"}, api_key))
    except urllib.error.HTTPError:  # older API without the lineage filter
        sources = list(openalex_list("sources", {**params, "filter": f"host_organization:{publisher},type:journal"}, api_key))
    for s in sources:
        active = any(c["year"] in recent and c["works_count"] for c in s.get("counts_by_year") or [])
        if active and IEEE_TX_RE.search(s.get("display_name") or ""):
            ids.append(s["id"].rsplit("/", 1)[-1])
    print(f"IEEE Transactions journals found in OpenAlex: {len(ids)}")
    return {"checked": TODAY.isoformat(), "ids": sorted(ids)} if ids else cache


def journal_sources(names, api_key, cache):
    """OpenAlex IDs of journals by exact name (looked up once a month; one search call per journal)."""
    if cached(cache) and set(cache["names"]) == set(names):
        return cache
    ids = {}
    for name in names:
        params = urllib.parse.urlencode({"search": name, "per_page": 25, "select": "id,display_name", "api_key": api_key})
        results = json.loads(get("https://api.openalex.org/sources?" + params))["results"]
        match = next((r["id"] for r in results if r["display_name"].lower() == name.lower()), None)
        if match:
            ids[name] = match.rsplit("/", 1)[-1]
        else:
            print(f"Journal not found in OpenAlex, skipped: {name}")
    return {"checked": TODAY.isoformat(), "names": names, "ids": ids} if ids else cache


def list_works(source_ids, api_key, days):
    """Every work published in the last `days` days in these journals."""
    for i in range(0, len(source_ids), 50):  # OpenAlex allows up to 100 OR'ed values; 50 keeps URLs short
        params = {
            "filter": f"primary_location.source.id:{'|'.join(source_ids[i:i + 50])},"
                      f"from_publication_date:{TODAY - timedelta(days=days)},to_publication_date:{TODAY}",
            "select": OPENALEX_SELECT,
        }
        yield from openalex_list("works", params, api_key)


def is_fl(text, title):
    return bool(FED_RE.search(text)) and not NOT_FL_RE.search(title)


# --- Nature scoring ---------------------------------------------------------------------------------------
AI_TERMS = re.compile(
    r"machine learning|deep learning|neural network|artificial intelligence|\bAI\b|language models?\b|\bLLMs?\b"
    r"|foundation models?|transformers?\b|reinforcement learning|generative|diffusion models?|self-supervised"
    r"|graph neural|neural operators?|physics-informed|\bagents?\b|agentic|multimodal|representation learning"
    r"|learned|learning-based|computer vision|large models?|in-context|fine-tun|neuromorphic|federated", re.I)
METHOD_TERMS = re.compile(  # signs a paper advances AI itself rather than only using it
    r"we (propose|introduce|present|develop) (a|an|the)? ?(new|novel|general)?\s*(framework|model|method|architecture|algorithm)"
    r"|scaling|benchmark|generali[sz]|interpretab|explainab|robust|uncertainty|reasoning|efficien|privacy"
    r"|theor|emergent|alignment|world model|out-of-distribution|continual|few-shot|zero-shot", re.I)
SCIENCE_TERMS = re.compile(
    r"protein|molecul|material|drug|genom|cell|climate|weather|physics|chemi|quantum|clinical|medical"
    r"|patholog|neuro|astronom|earth|ocean|catalys|crystal|enzyme", re.I)
AI_SUBFIELDS = {"Artificial Intelligence", "Computer Vision and Pattern Recognition"}
NOT_RESEARCH = re.compile(r"^(author correction|publisher correction|correction|retraction|reply to|matters arising"
                          r"|editorial|expression of concern|addendum)\b", re.I)


def nature_score(w, p):
    """How likely a Nature-journal paper is to interest an AI researcher. None = not a research paper."""
    if (p["dois"] and p["dois"][0].startswith("10.1038/d41586")) or NOT_RESEARCH.search(p["title"]) \
            or w.get("type") not in (None, "article", "review", "preprint"):
        return None  # news, comment pieces, corrections
    score = 0
    if AI_TERMS.search(p["title"]):
        score += 4
    score += min(4, len({m.group(0).lower() for m in AI_TERMS.finditer(p["abstract"])}))
    primary = ((w.get("primary_topic") or {}).get("subfield") or {}).get("display_name")
    if primary in AI_SUBFIELDS:
        score += 3
    elif any(((t.get("subfield") or {}).get("display_name")) in AI_SUBFIELDS for t in w.get("topics") or []):
        score += 1
    text = p["title"] + " " + p["abstract"]
    if score >= 3 and METHOD_TERMS.search(text):
        score += 2
    if score >= 3 and SCIENCE_TERMS.search(text):
        score += 1  # AI for science
    if is_fl(text, p["title"]):
        score += 10
    if p["venue"] in NATURE_KEEP_ALL:
        score = max(score, NATURE_MIN_SCORE + 2)
    return score


def enrich_abstracts(papers, api_key, tried):
    """Fill in missing abstracts from OpenAlex by DOI, 50 DOIs per list call. `tried` remembers when each
    DOI was last asked for, so a missing abstract is asked for again only once a week."""
    retry_before = (TODAY - timedelta(days=ABSTRACT_RETRY_DAYS)).isoformat()
    todo = {}
    for p in papers:
        if p["abstract"]:
            continue
        for d in p["dois"]:
            if not d.startswith("10.48550/") and tried.get(d, "") < retry_before:
                todo[d] = p
    dois, found = list(todo), 0
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        params = urllib.parse.urlencode({"filter": "doi:" + "|".join(chunk), "per_page": 50,
                                         "select": "doi,abstract_inverted_index", "api_key": api_key})
        try:
            results = json.loads(get("https://api.openalex.org/works?" + params))["results"]
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            print(f"OpenAlex abstract lookup failed ({type(e).__name__}: {e})")
            break
        for w in results:
            p = todo.get(norm_doi(w.get("doi") or ""))
            abstract = abstract_from_index(w.get("abstract_inverted_index"))
            if p and abstract and not p["abstract"]:
                p["abstract"] = abstract
                found += 1
        for d in chunk:
            tried[d] = TODAY.isoformat()
    if dois:
        print(f"Abstracts: looked up {len(dois)} DOIs, found {found}")


# ---------------------------------------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------------------------------------
class Store:
    """Papers keyed so that the same paper from two sources becomes one entry (match on DOI, else title)."""

    def __init__(self, papers, stamp):
        self.papers, self.by_doi, self.by_title, self.stamp = [], {}, {}, stamp
        for p in papers:
            p.pop("tags", None)  # from the old tab system
            p.setdefault("added", p["published"])
            self.add(p)

    def _index(self, p):
        for d in p["dois"]:
            self.by_doi[d] = p
        self.by_title[norm_title(p["title"])] = p

    def add(self, new):
        match = next((self.by_doi[d] for d in new["dois"] if d in self.by_doi), None) \
            or self.by_title.get(norm_title(new["title"]))
        if match is None:
            new.setdefault("added", self.stamp)  # when the feed first saw it, for "new since your last visit"
            self.papers.append(new)
            self._index(new)
            return
        match["dois"] = sorted(set(match["dois"]) | set(new["dois"]))
        for label, url in new["links"].items():
            match["links"].setdefault(label, url)
        match["abstract"] = match["abstract"] or new["abstract"]
        if len(new["authors"]) > len(match["authors"]):
            match["authors"] = new["authors"]
        # The published version decides the tab and the date: a preprint that turns up in a journal or at a
        # conference moves there, dated by that version.
        if new["venue"] and not match["venue"]:
            match["venue"], match["published"] = new["venue"], new["published"]
        elif bool(new["venue"]) == bool(match["venue"]):
            match["published"] = min(match["published"], new["published"])
        if new.get("conf"):
            match.setdefault("conf", new["conf"])
        if "score" in new:
            match["score"] = new["score"]
        if new.get("note") and not match.get("note"):
            match["note"] = new["note"]
        self._index(match)


def tab_of(p):
    if p.get("conf"):
        return "conf"
    if not p["venue"]:
        return "preprint"
    if IEEE_TX_RE.search(p["venue"]):
        return "ieee-tx"
    if NATURE_RE.search(p["venue"]):
        return "nature"
    return None  # any other venue (left over from an older version of this script)


def still_current(p, tab, graph):
    if tab == "conf":
        return int(p["conf"].rsplit(" ", 1)[1]) >= TODAY.year - 1
    days = KEEP_DAYS["preprint-graph" if tab == "preprint" and graph else tab]
    if tab == "nature" and not (p.get("fl") or (p.get("score") or 0) >= NATURE_MIN_SCORE):
        return False
    return p["published"] >= (TODAY - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------------------
def main():
    try:
        with open(OUT) as f:
            saved = json.load(f)
    except FileNotFoundError:
        saved = {"papers": []}
    # The 18:17 run is a backup: if the 06:17 run already updated the feed today, stop before any API call.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and saved.get("updated", "")[:10] == TODAY.isoformat():
        print("Feed already updated today: skipping this backup run.")
        return
    stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    store = Store(saved["papers"], stamp)
    state = {k: saved.get(k) for k in ("conf_done", "full_sweep", "ieee_sources", "nature_sources",
                                        "dblp_checked", "abs_tried", "ieee_xplore_checked")}
    conf_done = set(state["conf_done"] or [])
    full_sweep = state["full_sweep"] or {}
    abs_tried = state["abs_tried"] or {}

    # arXiv
    for i, (query, max_results) in enumerate(ARXIV_QUERIES):
        if i:
            time.sleep(3)  # arXiv asks for 3 s between API calls
        try:
            for p in fetch_arxiv(query, max_results):
                store.add(p)
        except (urllib.error.URLError, ET.ParseError) as e:
            print(f"arXiv failed ({type(e).__name__}: {e})")

    # OpenReview: NeurIPS, ICML, ICLR, with abstracts
    or_user, or_pass = os.environ.get("OPENREVIEW_USERNAME"), os.environ.get("OPENREVIEW_PASSWORD")
    or_token = openreview_login(or_user, or_pass) if or_user and or_pass else None
    if not (or_user and or_pass):
        print("OPENREVIEW_USERNAME/OPENREVIEW_PASSWORD not set: NeurIPS, ICML and ICLR come from dblp only")
    for year in (TODAY.year, TODAY.year - 1):
        for conference in OPENREVIEW_CONFS if or_token else []:
            if f"{conference} {year}" in conf_done:
                continue
            result = fetch_openreview(conference, year, r"federat", or_token)
            if result is None:
                continue  # failed: try again next run
            papers, accepted = result
            for p in papers:
                store.add(p)
            if accepted:  # no accepted papers yet means decisions aren't out, so check again later
                conf_done.add(f"{conference} {year}")
            print(f"OpenReview {conference} {year}: {accepted} accepted, {len(papers)} on federated learning")
            time.sleep(1)

    # dblp: every other conference, once a week
    if (state["dblp_checked"] or "") <= (TODAY - timedelta(days=DBLP_EVERY_DAYS)).isoformat():
        ok = 0
        for label, prefix in DBLP_VENUES:
            papers = fetch_dblp_venue(label, prefix)
            if papers is not None:
                ok += 1
                for p in papers:
                    store.add(p)
                print(f"dblp {label}: {len(papers)} federated learning papers ({TODAY.year - 1}-{TODAY.year})")
            time.sleep(1.5)  # dblp asks for a gentle pace
        if ok == len(DBLP_VENUES):
            state["dblp_checked"] = TODAY.isoformat()

    # IEEE Xplore (optional): new IEEE Transactions papers, early access included, with abstracts
    ieee_key = os.environ.get("IEEE_API_KEY")
    if ieee_key:
        last = state["ieee_xplore_checked"]
        since = (date.fromisoformat(last) - timedelta(days=3)) if last else TODAY - timedelta(days=KEEP_DAYS["ieee-tx"])
        papers = fetch_ieee_xplore(ieee_key, since)
        if papers is not None:
            for p in papers:
                store.add(p)
            state["ieee_xplore_checked"] = TODAY.isoformat()

    # OpenAlex: IEEE Transactions and Nature journals, listed in full, filtered here
    api_key = os.environ.get("OPENALEX_API_KEY")
    if api_key:
        def window(tab):
            full = tab not in full_sweep or TODAY.weekday() == FULL_SWEEP_WEEKDAY
            return full, KEEP_DAYS[tab] if full else min(KEEP_DAYS[tab], RECENT_DAYS)

        try:
            state["ieee_sources"] = ieee_transactions_sources(api_key, state["ieee_sources"])
            full, days = window("ieee-tx")
            seen = kept = 0
            for w in list_works(state["ieee_sources"]["ids"], api_key, days):
                seen += 1
                p = paper_from_work(w)
                if p and is_fl(p["title"] + " " + p["abstract"], p["title"]):
                    store.add(p)
                    kept += 1
            print(f"IEEE Transactions: {seen} papers listed ({days} days), {kept} on federated learning")
            if full:
                full_sweep["ieee-tx"] = TODAY.isoformat()
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            print(f"IEEE Transactions failed ({type(e).__name__}: {e}); trying again next run")

        try:
            state["nature_sources"] = journal_sources(NATURE_JOURNALS, api_key, state["nature_sources"])
            full, days = window("nature")
            seen = kept = 0
            for w in list_works(list(state["nature_sources"]["ids"].values()), api_key, days):
                seen += 1
                p = paper_from_work(w)
                score = p and nature_score(w, p)
                if score is not None and score >= NATURE_MIN_SCORE:
                    p["score"] = score
                    store.add(p)
                    kept += 1
            print(f"Nature journals: {seen} papers listed ({days} days), {kept} picked")
            if full:
                full_sweep["nature"] = TODAY.isoformat()
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            print(f"Nature journals failed ({type(e).__name__}: {e}); trying again next run")

        enrich_abstracts([p for p in store.papers if tab_of(p) in ("conf", "ieee-tx")], api_key, abs_tried)
    else:
        print("OPENALEX_API_KEY not set: skipping IEEE Transactions, Nature and conference abstracts")

    # Decide each paper's tab and whether it is still current.
    papers = []
    for p in store.papers:
        tab = tab_of(p)
        text = p["title"] + " " + p["abstract"]
        graph = bool(GRAPH_RE.search(text))
        p["fl"] = tab != "nature" or is_fl(text, p["title"])
        if tab and still_current(p, tab, graph):
            p["tab"], p["graph"] = tab, graph
            papers.append(p)
    papers.sort(key=lambda p: p["published"], reverse=True)
    live = {d for p in papers for d in p["dois"]}
    abs_tried = {d: t for d, t in abs_tried.items() if d in live}

    with open(OUT, "w") as f:
        json.dump({"updated": stamp, **state, "conf_done": sorted(conf_done), "full_sweep": full_sweep,
                   "abs_tried": abs_tried, "papers": papers}, f, indent=1)
    write_feed_files(papers, stamp)
    counts = {t: sum(p["tab"] == t for p in papers) for t in ("preprint", "ieee-tx", "conf", "nature")}
    print(f"{len(papers)} papers in feed: {counts}")
    cost = openalex_calls["search"] * 0.001 + openalex_calls["list"] * 0.0001
    print(f"OpenAlex calls this run: {openalex_calls['search']} search, {openalex_calls['list']} list "
          f"(about ${cost:.3f} of the free $1/day)")


# ---------------------------------------------------------------------------------------------------------
# Files for the web page
# ---------------------------------------------------------------------------------------------------------
TAB_TITLES = {"preprint": "Preprints", "ieee-tx": "IEEE Transactions", "conf": "Top conferences",
              "nature": "Nature · AI"}
LITE_FIELDS = ("id", "title", "authors", "venue", "published", "links", "conf", "note", "score", "fl", "graph", "added")


def main_link(p):
    links = p["links"]
    return links.get("Publisher") or links.get("OpenReview") or links.get("arXiv") or links.get("PDF") or SITE_URL


def write_feed_files(papers, updated):
    """Per tab: feed/<tab>.json (papers with a 300-character preview, loaded when the tab opens),
    feed/<tab>-abs.json (full abstracts by id, loaded in the background) and feed/<tab>.xml (RSS).
    feed/meta.json has the update time and per-tab counts for the menu."""
    os.makedirs(FEED_DIR, exist_ok=True)
    compact = {"separators": (",", ":"), "ensure_ascii": False}
    meta = {"updated": updated, "tabs": {}}
    for tab, title in TAB_TITLES.items():
        rows = [p for p in papers if p["tab"] == tab]
        lite = []
        for p in rows:
            row = {k: p[k] for k in LITE_FIELDS if p.get(k) not in (None, "", [], {})}
            row["preview"] = p["abstract"][:300]
            row["doi"] = next((d for d in p["dois"] if not d.startswith("10.48550/")), p["dois"][0] if p["dois"] else "")
            lite.append(row)
        with open(f"{FEED_DIR}/{tab}.json", "w", encoding="utf-8") as f:
            json.dump({"updated": updated, "papers": lite}, f, **compact)
        with open(f"{FEED_DIR}/{tab}-abs.json", "w", encoding="utf-8") as f:
            json.dump({p["id"]: p["abstract"] for p in rows if len(p["abstract"]) > 300}, f, **compact)
        meta["tabs"][tab] = {"count": len(rows), "graph": sum(p["graph"] for p in rows)}
        write_rss(tab, title, rows)
    with open(f"{FEED_DIR}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, **compact)


def write_rss(tab, title, rows):
    newest = sorted(rows, key=lambda p: (p.get("added", ""), p["published"]), reverse=True)[:RSS_ITEMS]
    items = []
    for p in newest:
        venue = p["venue"] or "arXiv preprint"
        try:
            when = datetime.fromisoformat(p.get("added", p["published"])).replace(tzinfo=timezone.utc)
        except ValueError:
            when = datetime.now(timezone.utc)
        items.append(
            "<item>"
            f"<title>{escape(p['title'])}</title>"
            f"<link>{escape(main_link(p))}</link>"
            f"<guid isPermaLink=\"false\">{escape(p['id'])}</guid>"
            f"<pubDate>{when.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>{escape(venue + ' · ' + ', '.join(p['authors'][:6]) + chr(10) + chr(10) + p['abstract'])}</description>"
            "</item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
           f"<title>paper feed: {escape(title)}</title><link>{SITE_URL}#{tab}</link>"
           f"<description>New papers in {escape(title)}, updated daily.</description>"
           + "".join(items) + "</channel></rss>\n")
    with open(f"{FEED_DIR}/{tab}.xml", "w", encoding="utf-8") as f:
        f.write(xml)


if __name__ == "__main__":
    main()

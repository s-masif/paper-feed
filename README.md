# paper feed

A daily-updated, self-hosted feed of new research papers, built for federated learning and federated graph learning
plus a curated set of AI papers from Nature journals. It runs entirely on GitHub for free: a GitHub Actions job
collects papers every morning, and GitHub Pages serves the page.

Live example: https://s-masif.github.io/paper-feed/

It can be set up for any research area: see [Make it your own](#make-it-your-own).

## Features

**Tabs, by where a paper appears**

| Tab | What it shows | Kept for |
|---|---|---|
| Preprints | arXiv preprints on the topic | 3 months (6 for graph papers) |
| IEEE Transactions | papers in IEEE Transactions journals, early access included | 6 months |
| Top conferences | papers at ~30 top AI, data, vision, language, systems and security venues | this year and last |
| Nature · AI | AI papers from Nature journals, picked by a relevance score | 3 months |
| ★ Saved | papers you starred | until you remove them |

**On the page**

- **Graph learning only** switch, which works on every tab and is remembered between visits.
- **Conference chips** (NeurIPS, ICML, AAAI, KDD...) with paper counts, to narrow the conference tab.
- **Top picks / Newest** sort on the Nature tab.
- **New since your last visit**: papers added since you last looked are marked, with a switch to show only those.
- **☆ Save** keeps a paper in your browser, even after it leaves the feed.
- **Cite** copies a BibTeX entry.
- **Abs** opens the full abstract; links to arXiv, OpenReview, the publisher and the PDF where available.
- Preprints show the authors' notes such as "Accepted at AAAI 2026", and a preprint moves to its venue's tab once
  the published version is found. The same paper from several sources appears once.
- **RSS feed for every tab** (`feed/<tab>.xml`), to follow a tab in a feed reader or email digest.
- Search box (`/` or Ctrl+K), dark mode that follows your system, links that open a tab directly (`#conf`).
- Fast: each run writes the update info and the first 40 preprints straight into `index.html`, so the first screen
  appears as soon as the page itself arrives. The rest of the opening tab, the other tabs and the full abstracts
  load in the background, and papers are drawn as you scroll.

## Where the papers come from

| Source | Used for | Account needed | Cost |
|---|---|---|---|
| [arXiv API](https://info.arxiv.org/help/api/) | Preprints | none | free |
| [OpenAlex](https://openalex.org) | IEEE Transactions and Nature journals (every new paper listed, then filtered here); abstracts for conference papers | free API key | about $0.01 a day of the free $1/day |
| [dblp](https://dblp.org) (SPARQL service) | conference papers: titles, authors, DOIs | none | free |
| [OpenReview](https://openreview.net) | NeurIPS, ICML, ICLR, with abstracts | free account | free |
| [IEEE Xplore API](https://developer.ieee.org) (optional) | IEEE Transactions papers the day IEEE posts them, with abstracts | free API key | free (1–3 of 200 calls a day) |

Every source is optional except arXiv: if a key is missing or a service is down, the run says so in its log and
carries on with the others. Each run prints what it found per source and what it spent on OpenAlex.

How the Nature tab picks papers: every paper in the listed journals is scored for being about AI methods or AI for
science (AI terms in the title and abstract, OpenAlex's AI topic labels, signs of method work such as new models,
benchmarks or theory, and science applications). News, corrections and unsigned pieces are dropped. Papers in
Nature Machine Intelligence and npj Artificial Intelligence are always kept, and so are papers on the main topic.

## Set it up (about 15 minutes)

1. **Copy the repository.** Click **Use this template** (or fork it) to make your own copy. Make it **public**:
   GitHub Pages on private repositories needs a paid plan.
2. **Let the workflow save its results.** Settings → Actions → General → Workflow permissions → choose
   **Read and write permissions** and save.
3. **Add your keys** under Settings → Secrets and variables → Actions → New repository secret. All are optional,
   but the OpenAlex key unlocks most of the feed:

   | Secret name | What to put in it |
   |---|---|
   | `OPENALEX_API_KEY` | free key from https://openalex.org/settings/api |
   | `OPENREVIEW_USERNAME` | the email of a (free) openreview.net account |
   | `OPENREVIEW_PASSWORD` | that account's password (a separate account just for this is fine) |
   | `IEEE_API_KEY` | free key from https://developer.ieee.org (approval can take a few days; the feed works without it meanwhile) |

4. **Run it once.** Actions tab → **Update paper feed** → **Run workflow**. The first run takes a few minutes and
   creates `papers.json` and the `feed/` folder.
5. **Turn on the page.** Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder `/ (root)`.
   Your feed appears at `https://<username>.github.io/<repository>/` within a couple of minutes.

After that it updates itself every day at 06:17 UTC, with a backup run at 18:17 UTC that only does anything if the
morning run failed. You can run it by hand from the Actions tab at any time.

## Make it your own

Everything about *what* is tracked is at the top of `fetch.py`, and everything about *how it looks* is in
`index.html`. The variable names mention federated learning, but they work for any topic.

### 1. Your topic (`fetch.py`)

| Setting | What it does | Federated learning example |
|---|---|---|
| `FEDERATED` | phrases for the arXiv search | `'"federated learning"', 'FedAvg', ...` |
| `GRAPH` | phrases for a second, narrower arXiv search (your sub-topic) | `'"graph neural"', 'GNN', ...` |
| `ARXIV_QUERIES` | the arXiv searches, and how many newest papers each reads per run | built from the two lists above |
| `SEARCH_WORDS` | word(s) sent to dblp and IEEE Xplore to find candidates in titles | `"federated"` |
| `FED_RE` | what counts as on-topic, checked on every title and abstract | `federat\|\bfedavg\b` |
| `NOT_FL_RE` | titles to exclude even though they match | `federated (quer\|databases?\|...)` |
| `GRAPH_RE` | what the sub-topic switch on the page shows | `graph neural\|\bgnns?\b\|...` |
| `KEEP_DAYS` | how long each tab keeps papers | 90 / 180 days |

Example for someone working on diffusion models, with video generation as the sub-topic:

```python
FEDERATED = ['"diffusion model"', '"diffusion models"', '"score-based generative"', '"denoising diffusion"']
GRAPH = ['video', '"text-to-video"']
ARXIV_QUERIES = [(arxiv_any(FEDERATED), 200), ("abs:diffusion AND " + arxiv_any(GRAPH), 300)]
SEARCH_WORDS = "diffusion"
FED_RE = re.compile(r"diffusion (models?|probabilistic|transformer|polic)|score-based generative|denoising diffusion", re.I)
NOT_FL_RE = re.compile(r"diffusion (mri|tensor|weighted)|heat diffusion|reaction-diffusion", re.I)
GRAPH_RE = re.compile(r"\bvideo", re.I)
```

arXiv query syntax: https://info.arxiv.org/help/api/user-manual.html#query_details. Keep `SEARCH_WORDS` to one
distinctive word: dblp and IEEE use it only to find candidates, and `FED_RE` decides what is kept.

### 2. Venues (`fetch.py`)

- `DBLP_VENUES`: conferences as `(short name, dblp path)`. The path is the venue's address on dblp, for example
  `conf/kdd` for https://dblp.org/db/conf/kdd/. Add or remove lines freely; the log shows how many papers each
  venue gave.
- `OPENREVIEW_CONFS`: conferences read from OpenReview (with abstracts).
- `NATURE_JOURNALS`: the journals scored for the Nature tab (names exactly as OpenAlex spells them; unknown names
  are skipped with a message). `NATURE_KEEP_ALL` lists journals whose every paper is kept, and `NATURE_MIN_SCORE`
  sets how strict the pick is: higher means fewer, stronger papers. `AI_TERMS`, `METHOD_TERMS` and
  `SCIENCE_TERMS` above `nature_score` decide what scores well; change them to curate for another field.
- The IEEE Transactions tab finds all IEEE Transactions journals by itself. To use other journals, change
  `IEEE_TX_RE` (which journal names count).

### 3. The page (`index.html`)

- The top bar (`<nav>`): your name and the links to your own site.
- `<title>`, the description under the heading, and the footer.
- `TAB_INFO` in the script: the text shown under each tab. Keep it in line with your settings in `fetch.py`.
- The label of the sub-topic switch ("Graph learning only").
- Colors: `--theme` (light mode) and the dark-mode `--theme` at the top of the `<style>` block.

Also set `SITE_URL` near the top of `fetch.py` to your page's address (it goes into the RSS feeds).

After changing `fetch.py`, run the workflow by hand to see the result straight away. Papers that no longer match
drop out on the next runs as they age out; to start completely fresh, delete `papers.json` and the `feed/` folder
before running.

## How it works

- `fetch.py` runs in GitHub Actions (`.github/workflows/update.yml`), merges papers from all sources, and writes:
  - `papers.json`: its full memory between runs (all papers plus what it has already checked). The page never
    downloads it.
  - `feed/meta.json`: update time and per-tab counts.
  - `feed/<tab>.json` (papers with a short preview), `feed/<tab>-abs.json` (full abstracts, loaded in the
    background) and `feed/<tab>.xml` (RSS).
  - the first papers of the opening tab, inside the `<script id="boot">` element of `index.html`. Because every
    run changes that line, pull the latest version before editing `index.html` yourself.
- **Duplicates**: papers are merged when they share a DOI or have the same title (ignoring case and punctuation).
  The published version decides the tab and the date. A paper whose title changed between preprint and
  publication can still appear twice.
- **Keeping costs down**: OpenAlex listings use filter-only calls ($0.0001 per 100 papers); keyword searches are used
  only to look up journal IDs once a month. Daily runs re-read the last 45 days and a full window once a week
  (Sunday), which catches papers OpenAlex indexes late. Missing abstracts are asked for again once a week.

## Troubleshooting

Open the latest run under the Actions tab and read the `Run python fetch.py` step.

| Log line | Meaning |
|---|---|
| `IEEE Xplore not available (HTTP 403 ... Developer Inactive)` | the IEEE key isn't approved yet; the feed uses OpenAlex meanwhile |
| `dblp SPARQL failed (...)` | dblp's service was unreachable; conferences are read again next run |
| `OpenReview <conf> <year>: 0 accepted` | that conference hasn't published its accepted papers yet; checked again every run |
| `OPENALEX_API_KEY not set` | add the secret; without it the IEEE and Nature tabs stay empty |
| `Journal not found in OpenAlex, skipped: ...` | a name in `NATURE_JOURNALS` doesn't match OpenAlex's spelling |
| a tab says its file isn't there | the workflow hasn't run since setup, or Pages hasn't redeployed yet |

If the page looks out of date after a run, wait a minute for Pages and reload with Ctrl+Shift+R.

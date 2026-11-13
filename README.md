# Paper feed

A daily-updated feed of new papers (arXiv plus journals and conferences via OpenAlex) on federated learning, with a separate view for federated graph learning.

## Setup (one time, ~10 minutes)

1. Create a new **public** GitHub repository (GitHub Pages on private repos needs a paid plan).
2. Upload `index.html`, `fetch.py`, `README.md` and the `.github/workflows/update.yml` file, keeping that folder path.
3. **Settings → Actions → General → Workflow permissions:** choose "Read and write permissions" and save.
   Then get a free OpenAlex API key (sign up at openalex.org, copy the key from openalex.org/settings/api) and add it under
   **Settings → Secrets and variables → Actions → New repository secret**, named `OPENALEX_API_KEY`.
   Without it the feed still works, with arXiv papers only.
4. **Actions tab → "Update paper feed" → Run workflow.** This creates `papers.json` with the first batch of papers.
5. **Settings → Pages:** source "Deploy from a branch", branch `main`, folder `/ (root)`. Your feed will be at `https://<username>.github.io/<repo>/` within a minute or two.

After that it updates itself every day at 06:00 UTC.

## Tabs and sources

| Tab | What | Source | Cost |
|---|---|---|---|
| Preprints | federated learning preprints | arXiv API | free |
| IEEE Transactions | federated learning papers in IEEE Transactions journals | OpenAlex, filter-only listing of every new paper, matched here | ~$0.01–0.05 a day |
| Top conferences | federated learning papers at ~30 top AI, data, vision, NLP, systems and security venues | OpenReview (NeurIPS, ICML, ICLR, with abstracts) and dblp (all venues, weekly); missing abstracts filled from OpenAlex by DOI | free / fractions of a cent |
| Nature · AI | AI papers from Nature journals that are about AI methods or AI for science | OpenAlex, filter-only listing, scored here (`nature_score`) | ~$0.01 a day |
| ★ Saved | papers you starred | your browser | — |

**Optional IEEE Xplore source.** With a free IEEE Xplore API key (developer.ieee.org) saved as the repository secret
`IEEE_API_KEY`, each run also asks IEEE Xplore for federated learning papers in IEEE Transactions added since the
previous run, early access included and with abstracts (1–3 of the 200 free calls a day). If the key is missing,
still waiting for approval, or the API fails, the run says so in the log and carries on with OpenAlex alone.

The "Graph learning only" switch works on every tab. Every tab also has an RSS feed (`feed/<tab>.xml`).
Each run prints what it spent; OpenAlex keyword searches are only used to look up journal IDs once a month.

## Changing what it tracks

All in `fetch.py`:

- `ARXIV_QUERIES`: arXiv searches (syntax: https://info.arxiv.org/help/api/user-manual.html#query_details).
- `DBLP_VENUES`: conferences, as (short name, dblp key prefix). The prefix is the venue's dblp path, e.g.
  `conf/kdd` for https://dblp.org/db/conf/kdd/. Check the log after a run: a venue that always shows
  0 papers may have a different prefix.
- `NATURE_JOURNALS`, `NATURE_KEEP_ALL`, `NATURE_MIN_SCORE` and the term lists above `nature_score`: which Nature
  papers are picked. Raise `NATURE_MIN_SCORE` for fewer, lower it for more.
- `KEEP_DAYS`: how long each tab keeps papers.
- `FED_RE` / `GRAPH_RE`: what counts as federated learning and as graph learning.

## Duplicates

The same paper often appears as an arXiv preprint, a dblp record and a published version. They are merged into
one entry when they share a DOI or have the same title (ignoring case and punctuation). The published version
decides the tab, so a preprint moves to Top conferences or IEEE Transactions once it is published.

## Speed

The page loads `feed/meta.json` and the open tab's `feed/<tab>.json` (no abstracts, only a preview), then the
other tabs and the abstracts (`feed/<tab>-abs.json`) in the background. `papers.json` is the fetcher's full state
and is never downloaded by the page.

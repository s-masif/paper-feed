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

## Changing what it tracks

Edit `FEDERATED`, `GRAPH` and `TOPICS` in `fetch.py`. Each topic sets how many days of papers it keeps
(graph topic: 180, all federated learning: 60). Each topic has an arXiv query
(syntax: https://info.arxiv.org/help/api/user-manual.html#query_details)
and an OpenAlex query (syntax: https://help.openalex.org/api/searching/).
If you add a topic, add a matching button in `index.html`.

## Duplicates

The same paper often appears as an arXiv preprint and as a published version. They are merged into one entry
when they share a DOI or have the same title (ignoring case and punctuation), showing both the arXiv and
Publisher links. Papers whose title changed between preprint and publication will still appear twice.

## Venue filter

Published papers are kept only if their venue matches `TOP_VENUES` in `fetch.py` (Nature-family journals,
IEEE Transactions, and top ML/data/systems conferences). arXiv preprints are always kept.
To add a venue, add a line such as `r"internet of things journal",`.

# Aviation Daily Brief

A self-updating aviation news site. A scheduled GitHub Action pulls RSS feeds
from aviation trade press every morning (05:00 Dubai time), classifies stories
into six sections, extracts each article's lead image, and writes everything
to `data/news.json`. GitHub Pages serves the site; the frontend renders the
three most recent stories per section as cards, with a click-to-expand archive
grouped by day.

## Structure
- `index.html` — the site (no build step, plain HTML/JS)
- `data/news.json` — rolling news archive, written by the daily job
- `scripts/fetch_news.py` — the fetcher/classifier
- `.github/workflows/update-news.yml` — daily schedule + manual trigger

## Deploy (browser only)
1. Create the repository (public) and upload these files, keeping the folder structure.
2. Settings → Actions → General → Workflow permissions → "Read and write permissions" → Save.
3. Actions tab → "Update news" → Run workflow (first fill).
4. Settings → Pages → Source: "Deploy from a branch", branch `main`, folder `/ (root)` → Save.
5. Your site is at `https://<username>.github.io/<repo-name>/` after a minute or two.

## Maintenance
- **Add/remove sources**: edit the `FEEDS` list in `scripts/fetch_news.py`.
  Feeds that stop working are skipped automatically (check the Action log).
- **Tune classification**: edit the `KEYWORDS` lists in the same file.
- **Change schedule**: edit the `cron` line in the workflow (UTC time).
- **Archive size**: `HISTORY_CAP` in the script (default 120 items/section).
- **Run on demand**: Actions → Update news → Run workflow.

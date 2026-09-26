# pw-harvest

A config-driven Playwright scraper for JavaScript-heavy sites, built to run unattended on a headless Linux box.

You describe each journey in `cases.yaml` (start page, how to reach the next page, what counts as one record, which fields to read), and `harvest.py` turns it into a run that:

- waits for real rendered content instead of fixed sleeps, and handles both "next page" links and infinite scroll;
- gives every case its own browser context (separate cookies, storage and cache), with its own user agent, viewport and locale;
- checks `robots.txt` before each page and pauses a random 1 to 2.5 seconds between page loads;
- retries page loads and image downloads with exponential backoff, and keeps going when a single record or image fails;
- downloads images through the same context (so cookies and headers match) into `output/<case>/images/`, with stable hashed file names so reruns skip what is already there;
- writes `items.jsonl` and `items.csv` per case, a JSON-lines `run.log`, and a `summary.json` with counts per case. The exit code is non-zero when anything failed, so cron or CI can alert on it.

## Demo run

The bundled cases target [toscrape.com](https://toscrape.com), a sandbox made for scraping practice:

| case | what it exercises | result (3 pages) |
|---|---|---|
| `books` | pagination, product images | 60 records, 60 images, 0 errors |
| `quotes_js` | content rendered by JavaScript after load | 30 records, 0 errors |
| `quotes_scroll` | infinite scroll with no pagination links | 100 records (all of them), 0 errors |

All three run in parallel in about 20 seconds.

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # on a fresh Linux server
python harvest.py                                   # every case
python harvest.py --case books --max-pages 2        # one case, quick check
python harvest.py --headed                          # watch the browser while debugging
```

Sample log lines:

```
{"event": "page", "case": "books", "url": "https://books.toscrape.com/catalogue/page-2.html", "items": 20}
{"event": "case_done", "case": "books", "pages": 3, "items": 60, "images": 60, "skipped": 0, "errors": 0, "seconds": 19.7}
```

## Adding a case

```yaml
cases:
  listings:
    start_url: https://example.com/listings
    item: .listing-card              # one record
    wait_for: .listing-card .price   # proves the JS has rendered
    fields:
      title: h2
      price: .price
      link: a@href                   # "selector@attribute"; links and images become absolute URLs
      photo: img@src
    next: a[rel=next]                # or `scroll: true` for infinite scroll
    image_field: photo
    max_pages: 20
    delay: [2, 4]                    # slower for stricter sites
```

## Running on a server

- **Docker:** `docker build -t pw-harvest . && docker run --rm -v "$PWD/output:/app/output" pw-harvest`
- **cron:** see `crontab.example` for a nightly run with a dated output folder per day.

## Notes

Only scrape pages you are allowed to scrape: the tool honours `robots.txt` and throttles itself, but the site's terms still apply.

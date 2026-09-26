"""pw-harvest: config-driven Playwright scraper for JavaScript-heavy sites.

Each "case" in cases.yaml describes one journey: where to start, how to reach the next page
(link, infinite scroll or nothing), which items to read and which fields to pull from each.
Runs headless on Linux, one isolated browser context per case, with polite throttling,
robots.txt checks, retries and a JSON-lines log that tells you exactly what was saved or skipped.

    python harvest.py                      # run every case in cases.yaml
    python harvest.py --case books --max-pages 2
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import yaml
from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PWTimeout, async_playwright

log = logging.getLogger('harvest')


# ---------- config ----------

@dataclass
class Case:
    name: str
    start_url: str
    item: str                                   # CSS selector for one record on a page
    fields: dict[str, str]                      # name -> "css" | "css@attr"
    wait_for: str | None = None                 # selector that proves the page has rendered
    next: str | None = None                     # CSS selector of the "next page" link
    scroll: bool = False                        # infinite scroll instead of pagination
    image_field: str | None = None              # field holding an image URL to download
    max_pages: int = 50
    user_agent: str = 'pw-harvest/1.0 (+https://github.com/luoy16002-svg/pw-harvest)'
    viewport: dict = field(default_factory=lambda: {'width': 1366, 'height': 900})
    locale: str = 'en-US'
    delay: tuple[float, float] = (1.0, 2.5)     # polite pause between page loads, seconds


def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    cases = []
    for name, c in raw['cases'].items():
        if 'delay' in c:
            c['delay'] = tuple(c['delay'])
        cases.append(Case(name=name, **c))
    return cases


# ---------- helpers ----------

class Robots:
    """Caches one robots.txt per host and answers can_fetch for our user agent."""

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser] = {}

    def allowed(self, url: str, agent: str) -> bool:
        host = '{0.scheme}://{0.netloc}'.format(urlparse(url))
        if host not in self._cache:
            rp = RobotFileParser(host + '/robots.txt')
            try:
                rp.read()
            except Exception:  # unreachable robots.txt: treat as allowed, like most crawlers
                rp.parse([])
            self._cache[host] = rp
        return self._cache[host].can_fetch(agent, url)


async def retry(what: str, fn, attempts: int = 3, base: float = 1.5):
    """Run an async callable with exponential backoff; re-raise after the last attempt."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except (PWTimeout, OSError, RuntimeError) as e:
            if i == attempts:
                raise
            wait = base ** i + random.random()
            log.warning(json.dumps({'event': 'retry', 'what': what, 'attempt': i, 'error': str(e)[:200], 'wait_s': round(wait, 1)}))
            await asyncio.sleep(wait)


async def read_field(el, spec: str) -> str | None:
    css, _, attr = spec.partition('@')
    target = await el.query_selector(css) if css else el
    if target is None:
        return None
    value = await (target.get_attribute(attr) if attr else target.inner_text())
    return re.sub(r'\s+', ' ', value).strip() if value else None


# ---------- the run ----------

class Harvester:
    def __init__(self, browser: Browser, out: Path, robots: Robots) -> None:
        self.browser, self.out, self.robots = browser, out, robots

    async def run(self, case: Case) -> dict:
        started = time.time()
        folder = self.out / case.name
        (folder / 'images').mkdir(parents=True, exist_ok=True)
        stats = {'case': case.name, 'pages': 0, 'items': 0, 'images': 0, 'skipped': 0, 'errors': 0}
        # One context per case: separate cookies, storage and cache, so journeys never leak into each other.
        ctx: BrowserContext = await self.browser.new_context(
            user_agent=case.user_agent, viewport=case.viewport, locale=case.locale)
        page = await ctx.new_page()
        seen: set[str] = set()
        records: list[dict] = []
        try:
            url: str | None = case.start_url
            while url and stats['pages'] < case.max_pages:
                if not self.robots.allowed(url, case.user_agent):
                    log.info(json.dumps({'event': 'robots_block', 'case': case.name, 'url': url}))
                    break
                await retry(f'goto {url}', lambda: self._open(page, url, case))
                stats['pages'] += 1
                if case.scroll:
                    await self._scroll_to_end(page, case)
                new = await self._extract(page, case, seen, stats)
                records.extend(new)
                log.info(json.dumps({'event': 'page', 'case': case.name, 'url': url, 'items': len(new)}))
                url = await self._next_url(page, case)
                if url:
                    await asyncio.sleep(random.uniform(*case.delay))
            if case.image_field:
                await self._download_images(ctx, case, records, folder, stats)
        except Exception as e:  # keep what we have; the log says where it stopped
            stats['errors'] += 1
            log.error(json.dumps({'event': 'case_failed', 'case': case.name, 'error': str(e)[:300]}))
        finally:
            await ctx.close()
        self._write(folder, records)
        stats['seconds'] = round(time.time() - started, 1)
        log.info(json.dumps({'event': 'case_done', **stats}))
        return stats

    async def _open(self, page: Page, url: str, case: Case) -> None:
        resp = await page.goto(url, wait_until='domcontentloaded', timeout=30_000)
        if resp is not None and resp.status >= 500:
            raise RuntimeError(f'HTTP {resp.status}')
        await page.wait_for_selector(case.wait_for or case.item, timeout=20_000)

    async def _scroll_to_end(self, page: Page, case: Case, patience: int = 3) -> None:
        """Scroll until the item count stops growing for a few rounds."""
        last, still = -1, 0
        while still < patience:
            count = await page.locator(case.item).count()
            still = still + 1 if count == last else 0
            last = count
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(700)

    async def _extract(self, page: Page, case: Case, seen: set[str], stats: dict) -> list[dict]:
        out = []
        for el in await page.query_selector_all(case.item):
            try:
                rec = {k: await read_field(el, spec) for k, spec in case.fields.items()}
            except Exception as e:
                stats['errors'] += 1
                log.warning(json.dumps({'event': 'item_error', 'case': case.name, 'error': str(e)[:200]}))
                continue
            for k, spec in case.fields.items():  # links and images come back relative
                if rec.get(k) and spec.endswith(('@href', '@src')):
                    rec[k] = urljoin(page.url, rec[k])
            key = hashlib.sha1(json.dumps(rec, sort_keys=True).encode()).hexdigest()
            if key in seen:  # infinite scroll and overlapping pages repeat items
                stats['skipped'] += 1
                continue
            seen.add(key)
            rec['source_url'] = page.url
            out.append(rec)
        stats['items'] += len(out)
        return out

    async def _next_url(self, page: Page, case: Case) -> str | None:
        if not case.next:
            return None
        link = await page.query_selector(case.next)
        href = await link.get_attribute('href') if link else None
        return urljoin(page.url, href) if href else None

    async def _download_images(self, ctx: BrowserContext, case: Case, records: list[dict], folder: Path, stats: dict) -> None:
        sem = asyncio.Semaphore(4)

        async def one(rec: dict) -> None:
            src = rec[case.image_field]
            name = hashlib.sha1(src.encode()).hexdigest()[:16] + (Path(urlparse(src).path).suffix or '.jpg')
            dest = folder / 'images' / name
            rec['image_file'] = f'images/{name}'
            if dest.exists():
                return
            async with sem:
                try:
                    resp = await retry(f'image {src}', lambda: ctx.request.get(src, timeout=30_000))
                    if not resp.ok:
                        raise RuntimeError(f'HTTP {resp.status}')
                    dest.write_bytes(await resp.body())
                    stats['images'] += 1
                except Exception as e:
                    rec['image_file'] = None
                    stats['errors'] += 1
                    log.warning(json.dumps({'event': 'image_failed', 'case': case.name, 'url': src, 'error': str(e)[:200]}))

        await asyncio.gather(*(one(r) for r in records if r.get(case.image_field)))

    @staticmethod
    def _write(folder: Path, records: list[dict]) -> None:
        with open(folder / 'items.jsonl', 'w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        if records:
            cols = list(dict.fromkeys(k for r in records for k in r))
            with open(folder / 'items.csv', 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(records)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cases', default='cases.yaml', type=Path)
    ap.add_argument('--case', action='append', help='run only these case names')
    ap.add_argument('--out', default='output', type=Path)
    ap.add_argument('--max-pages', type=int, help='override max_pages for a quick run')
    ap.add_argument('--headed', action='store_true', help='show the browser (debugging)')
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(args.out / 'run.log', encoding='utf-8')])
    cases = [c for c in load_cases(args.cases) if not args.case or c.name in args.case]
    for c in cases:
        if args.max_pages:
            c.max_pages = args.max_pages
    robots = Robots()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        h = Harvester(browser, args.out, robots)
        results = await asyncio.gather(*(h.run(c) for c in cases))
        await browser.close()
    (args.out / 'summary.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    return 1 if any(r['errors'] for r in results) else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))

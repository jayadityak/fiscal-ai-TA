"""Browser-driven report discovery.

Given an entry-point URL, a real (headed) browser navigates the site the way a
person would -- dismissing cookie banners, passing age gates, and rendering
JavaScript -- then collects and classifies the annual-report PDF links it finds.
This is the discovery path for sites whose bot protection blocks a plain-HTTP
crawler (Akamai 403s, JS age gates). It reuses the deterministic classifier from
``scrape.py`` so a discovered PDF is categorised exactly like a seeded one.

Nestle's investor site sits behind Cloudflare Turnstile, which standard browser
automation cannot reliably pass; for that issuer the entry point is a navigable
aggregator page instead. The navigation logic is otherwise site-agnostic: give it
an entry point and it returns the reports.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .scrape import classify_pdf, infer_report_year, _first_pages_text

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


@dataclass
class DiscoveredLink:
    url: str
    link_text: str
    source_page: str


def _accept_cookies(page) -> None:
    """Best-effort dismissal of a cookie/consent banner (privacy-preserving:
    prefer 'reject' where offered, else 'accept' to proceed)."""
    labels = [
        "Reject all", "Reject All", "Decline", "Only necessary",
        "Accept all", "Accept All", "I Accept", "Allow all", "Agree",
    ]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=2500)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def _pass_age_gate(page) -> bool:
    """Detect and pass a date-of-birth / country age gate. Returns True if one
    was handled. Sets the native <select> via JS to bypass custom dropdown UI."""
    has_gate = bool(
        page.query_selector("input[name='dob_year']")
        or "age" in (page.title() or "").lower()
    )
    if not has_gate:
        return False
    # Country: set the underlying native <select> value directly.
    page.evaluate(
        """() => {
            const s = document.querySelector("select[name='country']");
            if (s) {
                for (const o of s.options) {
                    if (/united states|netherlands|united kingdom/i.test(o.textContent)) {
                        s.value = o.value; break;
                    }
                }
                if (!s.value && s.options.length > 1) s.value = s.options[1].value;
                s.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }"""
    )
    for field, value in (("dob_day", "15"), ("dob_month", "6"), ("dob_year", "1985")):
        el = page.query_selector(f"input[name='{field}']")
        if el:
            try:
                el.fill(value)
            except Exception:
                pass
    page.wait_for_timeout(400)
    for sel in (
        "input[name='op']", "input[type='submit']",
        "button[type='submit']", "button:has-text('Enter')",
    ):
        el = page.query_selector(sel)
        if el:
            try:
                el.click(timeout=3000)
                break
            except Exception:
                continue
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)
    return True


def _collect_pdf_links(page) -> list[DiscoveredLink]:
    raw = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => ({href: e.href, text: (e.textContent||'').trim()}))",
    )
    out: dict[str, DiscoveredLink] = {}
    for item in raw:
        href = item.get("href", "")
        if ".pdf" not in urlparse(href).path.lower():
            continue
        out[href] = DiscoveredLink(href, item.get("text", "")[:200], page.url)
    return list(out.values())


def _looks_like_report(link: DiscoveredLink) -> bool:
    hay = f"{link.url} {link.link_text}".lower()
    return any(k in hay for k in ("annual", "financial", "report", "account"))



_FETCH_PDF_JS = """async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) return {ok: false, status: r.status};
    const buf = new Uint8Array(await r.arrayBuffer());
    let s = ''; const CH = 0x8000;
    for (let i = 0; i < buf.length; i += CH) {
        s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
    }
    return {ok: true, status: r.status, b64: btoa(s)};
}"""


def _fetch_pdf_in_page(page, url: str) -> bytes:
    """Download a PDF using the page's own fetch -- the real browser network
    stack and session, which passes Akamai where a separate request context 403s."""
    import base64
    res = page.evaluate(_FETCH_PDF_JS, url)
    if not res.get("ok"):
        raise RuntimeError(f"HTTP {res.get('status')} for {url}")
    body = base64.b64decode(res["b64"])
    if not body.startswith(b"%PDF"):
        raise ValueError(f"Not a PDF: {url}")
    return body


def _year_in_url(text: str) -> int | None:
    """Read the report year, preferring the filename basename over the full path.

    A URL like ``.../2026-02/2025_..._Annual_Report.pdf`` must read as 2025 (the
    report), not 2026 (the publication folder). ``infer_report_year`` is tried
    first because it anchors on the phrase 'annual report'/'financial statements'
    and so reads ``...annual-report-2021-25-02-2022.pdf`` as 2021 rather than
    taking the latest year present.
    """
    name = unquote(urlparse(text).path.rsplit("/", 1)[-1]) if "/" in text else text
    anchored = infer_report_year(name)
    if anchored is not None:
        return anchored
    years = [int(y) for y in re.findall(r"20\d\d", name)]
    if not years:  # basename had none -- fall back to the whole string
        years = [int(y) for y in re.findall(r"20\d\d", text)]
    return max(years) if years else None


def _classify_report(text: str) -> tuple[str, int]:
    """Classify a discovered PDF, preferring `annual_report` when a document
    carries strong annual-report signals even if it also contains governance or
    sustainability sections (e.g. an aggregator's combined 'Annual Review')."""
    doc_type, score = classify_pdf(text)
    low = text.lower()
    strong = sum(
        k in low
        for k in (
            "annual report", "annual review", "consolidated financial",
            "consolidated income", "consolidated balance", "financial statements",
        )
    )
    if doc_type in ("governance_or_remuneration", "sustainability", "other") and strong >= 2:
        return "annual_report", strong
    return doc_type, score


def _report_preference(link: DiscoveredLink) -> int:
    h = (link.url + " " + link.link_text).lower()
    score = 0
    if "annual-report-and-accounts" in h or "annual report" in h:
        score += 3
    if "financial" in h:
        score += 2
    if any(k in h for k in ("remuneration", "20-f", "20f", "governance", "sustainab")):
        score -= 4
    return score


# Words that suggest a link leads towards annual reports, and words that lead
# away from them (press releases, events, governance-only pages).
_TOWARDS = (
    ("annual report", 10), ("annual-report", 10), ("annual review", 8),
    ("financial report", 7), ("financial-report", 7), ("results and report", 7),
    ("results-and-report", 7), ("report and account", 9), ("reports-and-account", 9),
    ("archive", 6), ("publication", 5), ("results center", 5), ("results-center", 5),
    ("financial statement", 7), ("reporting", 4), ("annual", 4), ("report", 3),
    ("investor", 2), ("financial", 2),
)
_AWAY = (
    ("press release", 6), ("news", 4), ("event", 4), ("webcast", 4),
    ("conference", 5), ("calendar", 5), ("podcast", 5), ("consensus", 5),
    ("sustainab", 3), ("policy", 4), ("career", 6), ("contact", 5),
    ("privacy", 6), ("cookie", 6), ("subscribe", 5), ("brand", 4),
)


def _nav_score(href: str, text: str) -> int:
    """Score how likely a link leads towards annual reports."""
    hay = f"{href} {text}".lower()
    score = sum(w for kw, w in _TOWARDS if kw in hay)
    score -= sum(w for kw, w in _AWAY if kw in hay)
    if re.search(r"20\d\d", hay):
        score += 2
    return score


def _navigate_and_collect(
    page,
    entry_point: str,
    follow_pattern: str | None = None,
    max_pages: int = 6,
) -> list[DiscoveredLink]:
    """Navigate from an entry point and return the report PDF links found.

    Loads the entry point (handling cookie banners, age gates, and bot
    challenges), collects any report PDFs linked there, and then *navigates*:
    same-site links are scored for how likely they lead towards annual reports
    (`annual report`, `archive`, `publications`, ... while penalising press
    releases, events, and calendars) and the most promising are visited in turn,
    up to ``max_pages``. This is what lets a bare investor-relations entry point
    -- where the reports sit a click or two away -- still come back with reports.

    ``follow_pattern`` remains supported as an explicit override: when supplied,
    a link matching it is preferred over the heuristic ranking.
    """
    def _load(url: str) -> bool:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            return False
        for _ in range(10):
            page.wait_for_timeout(1200)
            if "just a moment" not in (page.title() or "").lower():
                break
        _accept_cookies(page)
        if _pass_age_gate(page):
            _accept_cookies(page)
        return True

    if not _load(entry_point):
        return []

    origin = urlparse(entry_point).netloc
    found: dict[str, DiscoveredLink] = {}
    for link in _collect_pdf_links(page):
        found[link.url] = link

    visited = {entry_point.rstrip("/")}
    # Rank same-site candidate pages from the entry point.
    candidates = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => ({href: e.href, text: (e.textContent||'').trim().slice(0,120)}))",
    )
    ranked: list[tuple[int, str, str]] = []
    for item in candidates:
        href = (item.get("href") or "").split("#")[0]
        if not href or urlparse(href).netloc != origin:
            continue
        if href.rstrip("/") in visited or ".pdf" in urlparse(href).path.lower():
            continue
        text = item.get("text") or ""
        score = _nav_score(href, text)
        if follow_pattern and re.search(follow_pattern, f"{href} {text}", re.I):
            score += 20
        if score > 0:
            ranked.append((score, href, text))
    ranked.sort(key=lambda r: -r[0])

    # Visit the most promising pages, collecting reports and queueing one deeper
    # hop from each (archives are often a click below a reports landing page).
    queue = ranked[:max_pages]
    pages_left = max_pages
    while queue and pages_left > 0:
        score, href, text = queue.pop(0)
        key = href.rstrip("/")
        if key in visited:
            continue
        visited.add(key)
        pages_left -= 1
        if not _load(href):
            continue
        new_links = _collect_pdf_links(page)
        for link in new_links:
            found.setdefault(link.url, link)
        # If this page yielded nothing, look one level deeper from here.
        if not new_links and pages_left > 0:
            deeper = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({href: e.href, text: (e.textContent||'').trim().slice(0,120)}))",
            )
            sub: list[tuple[int, str, str]] = []
            for item in deeper:
                h = (item.get("href") or "").split("#")[0]
                if not h or urlparse(h).netloc != origin:
                    continue
                if h.rstrip("/") in visited or ".pdf" in urlparse(h).path.lower():
                    continue
                t = item.get("text") or ""
                s = _nav_score(h, t)
                if s >= 6:
                    sub.append((s, h, t))
            sub.sort(key=lambda r: -r[0])
            queue = sub[:2] + queue

    return list(found.values())


def _seed_for_year(seed_urls: tuple[str, ...], year: int) -> str | None:
    for url in seed_urls:
        if _year_in_url(url) == year:
            return url
    return None


def browser_scrape_company(
    company,
    pdf_dir: Path = Path(".cache/pdf"),
    headed: bool = True,
) -> list[dict]:
    """End-to-end browser discovery for one company across its entry points.
    Navigates each entry point (handling cookie banners, age gates, and bot
    challenges), downloads the best report per target year in-session, classifies
    it, and for any year no entry point surfaces (e.g. a current-year report the
    aggregator lags on), falls back to the configured direct URL -- tagged
    ``seed_fallback`` so the manifest is honest about it."""
    pdf_dir.mkdir(parents=True, exist_ok=True)
    target_years = set(company.selected_report_years)
    rows: list[dict] = []
    got: set[int] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT, locale="en-US",
            timezone_id="America/New_York", viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()
        try:
            for entry in company.entry_points:
                if target_years <= got:
                    break
                links = _navigate_and_collect(page, entry, company.follow_pattern)
                by_year: dict[int, list[DiscoveredLink]] = {}
                for link in links:
                    if not _looks_like_report(link):
                        continue
                    yr = _year_in_url(link.url) or _year_in_url(link.link_text)
                    if yr and yr in target_years and yr not in got:
                        by_year.setdefault(yr, []).append(link)
                for yr in sorted(by_year, reverse=True):
                    link = max(by_year[yr], key=_report_preference)
                    filename = unquote(Path(urlparse(link.url).path).name)
                    base = {
                        "company": company.slug, "url": link.url, "archive_url": entry,
                        "link_text": link.link_text,
                        "context": f"Browser-discovered from {link.source_page}",
                        "discovery_source": "browser",
                    }
                    try:
                        body = _fetch_pdf_in_page(page, link.url)
                        digest = hashlib.sha256(body).hexdigest()
                        dest = pdf_dir / f"{digest}.pdf"
                        if not dest.exists():
                            dest.write_bytes(body)
                        text = " ".join((filename, link.link_text, _first_pages_text(dest)))
                        doc_type, score = _classify_report(text)
                        rows.append({**base, "filename": filename, "sha256": digest,
                                     "local_path": str(dest), "document_type": doc_type,
                                     "classification_score": score,
                                     "report_year": yr, "status": "downloaded", "error": ""})
                        got.add(yr)
                    except Exception as exc:
                        rows.append({**base, "filename": filename, "sha256": "",
                                     "local_path": "", "document_type": "other",
                                     "classification_score": 0, "report_year": yr,
                                     "status": "failed", "error": str(exc)})
        finally:
            browser.close()

    # Fallback: years no entry point surfaced (aggregator lag on the newest report).
    import requests
    for yr in sorted(target_years - got):
        seed = _seed_for_year(company.seed_pdf_urls, yr)
        if not seed:
            continue
        filename = unquote(Path(urlparse(seed).path).name)
        base = {
            "company": company.slug, "url": seed, "archive_url": "(direct-url fallback)",
            "link_text": f"{yr} report (seed fallback)",
            "context": "No crawlable source lists this year; configured direct URL",
            "discovery_source": "seed_fallback",
        }
        try:
            resp = requests.get(seed, headers={"User-Agent": USER_AGENT}, timeout=90)
            resp.raise_for_status()
            if not resp.content.startswith(b"%PDF"):
                raise ValueError("not a PDF")
            digest = hashlib.sha256(resp.content).hexdigest()
            dest = pdf_dir / f"{digest}.pdf"
            if not dest.exists():
                dest.write_bytes(resp.content)
            text = " ".join((filename, _first_pages_text(dest)))
            doc_type, score = _classify_report(text)
            rows.append({**base, "filename": filename, "sha256": digest,
                         "local_path": str(dest), "document_type": doc_type,
                         "classification_score": score,
                         "report_year": yr, "status": "downloaded", "error": ""})
        except Exception as exc:
            rows.append({**base, "filename": filename, "sha256": "", "local_path": "",
                         "document_type": "other", "classification_score": 0,
                         "report_year": yr, "status": "failed", "error": str(exc)})
    return rows


def discover_from_url(
    entry_point: str,
    follow_pattern: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    download: bool = False,
    pdf_dir: Path = Path(".cache/pdf"),
    headed: bool = True,
) -> list[dict]:
    """Generic report discovery from an arbitrary entry point.

    Takes any URL a human would start from -- an investor-relations page, a
    report archive, an aggregator listing -- navigates it with a real browser
    (passing cookie banners, age gates, and bot challenges), collects every
    report-like PDF link it finds, and returns one row per report year with the
    best candidate for that year. Set ``download=True`` to fetch the PDFs
    in-session (needed for hosts whose asset CDN also refuses non-browser
    clients) and classify them.

    Company-agnostic: no ``Company`` object required. Directly demonstrates
    'given an entry point, navigate and come back with the reports'.
    """
    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT, locale="en-US",
            timezone_id="America/New_York", viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()
        try:
            links = _navigate_and_collect(page, entry_point, follow_pattern)
            by_year: dict[int, list[DiscoveredLink]] = {}
            for link in links:
                if not _looks_like_report(link):
                    continue
                yr = _year_in_url(link.url) or _year_in_url(link.link_text)
                if yr is None:
                    continue
                if year_min is not None and yr < year_min:
                    continue
                if year_max is not None and yr > year_max:
                    continue
                by_year.setdefault(yr, []).append(link)
            for yr in sorted(by_year, reverse=True):
                link = max(by_year[yr], key=_report_preference)
                filename = unquote(Path(urlparse(link.url).path).name)
                row = {
                    "url": link.url, "entry_point": entry_point,
                    "link_text": link.link_text, "source_page": link.source_page,
                    "report_year": yr, "filename": filename,
                }
                if download:
                    pdf_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        body = _fetch_pdf_in_page(page, link.url)
                        digest = hashlib.sha256(body).hexdigest()
                        dest = pdf_dir / f"{digest}.pdf"
                        if not dest.exists():
                            dest.write_bytes(body)
                        text = " ".join((filename, link.link_text, _first_pages_text(dest)))
                        doc_type, score = _classify_report(text)
                        row.update({
                            "sha256": digest, "local_path": str(dest),
                            "document_type": doc_type, "classification_score": score,
                            "status": "downloaded", "error": "",
                        })
                    except Exception as exc:
                        row.update({
                            "sha256": "", "local_path": "",
                            "document_type": "other", "classification_score": 0,
                            "status": "failed", "error": str(exc),
                        })
                else:
                    row["status"] = "discovered"
                rows.append(row)
        finally:
            browser.close()
    return rows

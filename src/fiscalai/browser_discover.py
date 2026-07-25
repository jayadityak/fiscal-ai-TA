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
from urllib.parse import unquote, urljoin, urlparse

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


def discover(
    entry_point: str,
    follow_pattern: str | None = None,
    headed: bool = True,
) -> tuple[list[DiscoveredLink], dict[str, object]]:
    """Navigate from ``entry_point`` and return discovered PDF links.

    If the entry page exposes no report PDFs but has links matching
    ``follow_pattern`` (regex on href/text), follow the first such link one hop
    -- an archive/reports index -- and collect there.
    """
    meta: dict[str, object] = {"entry_point": entry_point, "steps": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT, locale="en-US",
            timezone_id="America/New_York", viewport={"width": 1280, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()
        try:
            resp = page.goto(entry_point, wait_until="domcontentloaded", timeout=45000)
            meta["status"] = resp.status if resp else None
            # let JS render / bot-challenge settle
            for _ in range(10):
                page.wait_for_timeout(1500)
                if "just a moment" not in (page.title() or "").lower():
                    break
            _accept_cookies(page)
            if _pass_age_gate(page):
                meta["steps"].append("passed_age_gate")
                _accept_cookies(page)
            links = _collect_pdf_links(page)
            meta["steps"].append(f"entry_pdf_links={len(links)}")

            if not links and follow_pattern:
                target = page.eval_on_selector_all(
                    "a[href]",
                    f"""els => {{
                        const re = new RegExp({follow_pattern!r}, 'i');
                        const m = els.find(e => re.test(e.href) || re.test(e.textContent||''));
                        return m ? m.href : null;
                    }}""",
                )
                if target:
                    page.goto(target, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    _accept_cookies(page)
                    links = _collect_pdf_links(page)
                    meta["steps"].append(f"followed_to={target}")
                    meta["steps"].append(f"followed_pdf_links={len(links)}")

            # Save cookie/session state so downloads reuse the passed challenge.
            meta["storage"] = ctx.storage_state()
            meta["title"] = page.title()
        finally:
            browser.close()
    return links, meta


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
    A URL like ``.../2026-02/2025_..._Annual_Report.pdf`` should read as 2025 (the
    report), not 2026 (the publication folder)."""
    name = unquote(urlparse(text).path.rsplit("/", 1)[-1]) if "/" in text else text
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


def _navigate_and_collect(
    page, entry_point: str, follow_pattern: str | None
) -> list[DiscoveredLink]:
    """Navigate one entry point (cookie banner, age gate, bot challenge) and
    return the report PDF links found, following one hop if needed."""
    page.goto(entry_point, wait_until="domcontentloaded", timeout=45000)
    for _ in range(10):
        page.wait_for_timeout(1500)
        if "just a moment" not in (page.title() or "").lower():
            break
    _accept_cookies(page)
    if _pass_age_gate(page):
        _accept_cookies(page)
    links = _collect_pdf_links(page)
    if not links and follow_pattern:
        target = page.eval_on_selector_all(
            "a[href]",
            f"""els => {{ const re=new RegExp({follow_pattern!r},'i');
                const m=els.find(e=>re.test(e.href)||re.test(e.textContent||''));
                return m?m.href:null; }}""",
        )
        if target:
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            _accept_cookies(page)
            links = _collect_pdf_links(page)
    return links


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
                for l in links:
                    if not _looks_like_report(l):
                        continue
                    yr = _year_in_url(l.url) or _year_in_url(l.link_text)
                    if yr and yr in target_years and yr not in got:
                        by_year.setdefault(yr, []).append(l)
                for yr in sorted(by_year, reverse=True):
                    l = max(by_year[yr], key=_report_preference)
                    filename = unquote(Path(urlparse(l.url).path).name)
                    base = {
                        "company": company.slug, "url": l.url, "archive_url": entry,
                        "link_text": l.link_text,
                        "context": f"Browser-discovered from {l.source_page}",
                        "discovery_source": "browser",
                    }
                    try:
                        body = _fetch_pdf_in_page(page, l.url)
                        digest = hashlib.sha256(body).hexdigest()
                        dest = pdf_dir / f"{digest}.pdf"
                        if not dest.exists():
                            dest.write_bytes(body)
                        text = " ".join((filename, l.link_text, _first_pages_text(dest)))
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

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import fitz
import pandas as pd
import requests
from bs4 import BeautifulSoup

from .companies import Company

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)


def classify_pdf(text: str) -> tuple[str, int]:
    value = " ".join(text.lower().split())
    rules = (
        ("sustainability", ("sustainability", "non-financial statement", "creating shared value")),
        ("governance_or_remuneration", ("remuneration", "compensation report", "governance report")),
        ("interim_report", ("half-year", "half year", "interim report")),
        ("presentation", ("presentation", "roadshow", "investor seminar")),
        ("prospectus", ("prospectus", "debt issuance", "bondholder")),
        (
            "annual_report",
            ("annual report", "annual review", "financial statements", "consolidated financial"),
        ),
        ("results_release", ("full-year results", "full year results", "results release")),
    )
    matches = [(label, sum(term in value for term in terms)) for label, terms in rules]
    label, score = max(matches, key=lambda item: item[1])
    return (label, score) if score else ("other", 0)


def infer_report_year(text: str) -> int | None:
    annual_report_match = re.search(
        r"(?:annual[\s_-]+report(?:[\s_-]+and[\s_-]+accounts)?|"
        r"financial[\s_-]+statements?)"
        r"[^0-9]{0,30}(20(?:1[0-9]|2[0-9]))",
        text,
        flags=re.IGNORECASE,
    )
    if annual_report_match:
        return int(annual_report_match.group(1))
    years = [int(year) for year in re.findall(r"\b20(?:1[0-9]|2[0-9])\b", text)]
    return max(years) if years else None


def _download(session: requests.Session, url: str, destination: Path) -> tuple[str, Path]:
    named_cache = destination / unquote(Path(urlparse(url).path).name)
    if named_cache.exists():
        content = named_cache.read_bytes()
    else:
        response = session.get(url, timeout=60)
        response.raise_for_status()
        content = response.content
    if not content.startswith(b"%PDF"):
        raise ValueError(f"Expected PDF content from {url}")
    digest = hashlib.sha256(content).hexdigest()
    target = destination / f"{digest}.pdf"
    if not target.exists():
        target.write_bytes(content)
    return digest, target


def _first_pages_text(path: Path, page_count: int = 3) -> str:
    with fitz.open(path) as document:
        return "\n".join(
            document[index].get_text("text") for index in range(min(page_count, len(document)))
        )


def scrape_company(
    company: Company,
    cache_dir: Path = Path(".cache"),
    artifacts_dir: Path = Path("artifacts"),
) -> pd.DataFrame:
    pdf_dir = cache_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    discovered: dict[str, dict[str, str]] = {}

    for url in company.seed_pdf_urls:
        discovered[url] = {
            "url": url,
            "archive_url": company.archive_pages[0],
            "link_text": f"{company.name} targeted annual report",
            "context": f"Configured filing target for {company.name}",
        }

    for archive_url in company.archive_pages:
        try:
            response = session.get(archive_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a[href]"):
            href = urljoin(archive_url, anchor.get("href", ""))
            if ".pdf" not in urlparse(href).path.lower():
                continue
            context = " ".join(anchor.parent.get_text(" ", strip=True).split())
            discovered[href] = {
                "url": href,
                "archive_url": archive_url,
                "link_text": anchor.get_text(" ", strip=True),
                "context": context[:500],
            }

    rows: list[dict[str, object]] = []
    for url, link in sorted(discovered.items()):
        filename = unquote(Path(urlparse(url).path).name)
        try:
            digest, local_path = _download(session, url, pdf_dir)
            source_text = " ".join(
                (filename, link["link_text"], link["context"], _first_pages_text(local_path))
            )
            identity_text = " ".join((filename, link["link_text"], link["context"]))
            document_type, score = classify_pdf(source_text)
            rows.append(
                {
                    "company": company.slug,
                    **link,
                    "filename": filename,
                    "sha256": digest,
                    "local_path": str(local_path),
                    "document_type": document_type,
                    "classification_score": score,
                    "report_year": infer_report_year(identity_text)
                    or infer_report_year(source_text),
                    "status": "downloaded",
                    "error": "",
                }
            )
        except Exception as exc:  # Manifest failures instead of silently dropping them.
            rows.append(
                {
                    "company": company.slug,
                    **link,
                    "filename": filename,
                    "sha256": "",
                    "local_path": "",
                    "document_type": "other",
                    "classification_score": 0,
                    "report_year": infer_report_year(
                        " ".join((filename, link["link_text"], link["context"]))
                    ),
                    "status": "failed",
                    "error": str(exc),
                }
            )

    manifest = pd.DataFrame(rows)
    manifest_path = artifacts_dir / "filings_manifest.csv"
    if manifest_path.exists():
        existing = pd.read_csv(manifest_path)
        if "company" in existing:
            existing = existing[existing["company"] != company.slug]
            manifest = pd.concat([existing, manifest], ignore_index=True)
    manifest.to_csv(manifest_path, index=False)
    return manifest

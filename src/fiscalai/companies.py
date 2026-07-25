from dataclasses import dataclass


@dataclass(frozen=True)
class Company:
    slug: str
    name: str
    ticker: str
    currency: str
    archive_pages: tuple[str, ...]
    seed_pdf_urls: tuple[str, ...]
    target_years: tuple[int, ...]
    selected_report_years: tuple[int, ...]
    # Entry points for browser-driven discovery (`fiscalai discover`): URLs a
    # human would start from, tried in order until all target years are found.
    # `follow_pattern` optionally lets the crawler take one hop into a matching
    # reports/archive link when an entry page lists no PDFs directly.
    entry_points: tuple[str, ...] = ()
    follow_pattern: str | None = None


NESTLE = Company(
    slug="nestle",
    name="Nestlé S.A.",
    ticker="NESN",
    currency="CHF",
    archive_pages=(
        "https://www.nestle.com/media/mediaeventscalendar/allevents/2025-annual-report",
    ),
    seed_pdf_urls=(
        "https://www.nestle.com/sites/default/files/2026-02/financial-statements-2025-en.pdf",
        "https://www.nestle.com/sites/default/files/2024-02/2023-financial-statements-en.pdf",
        "https://www.nestle.com/sites/default/files/2022-02/2021-financial-statements-en.pdf",
        "https://www.nestle.com/sites/default/files/2020-02/2019-financial-statements-en.pdf",
        "https://www.nestle.com/asset-library/Documents/Library/Documents/"
        "Financial_Statements/2017-financial-statements-en.pdf",
    ),
    target_years=tuple(range(2016, 2026)),
    selected_report_years=(2017, 2019, 2021, 2023, 2025),
    # Nestlé's IR site is behind Cloudflare Turnstile, which standard browser
    # automation cannot reliably pass; a navigable aggregator is the entry point.
    # The aggregator lags ~2 years, so the current-year report falls back to the
    # configured direct URL.
    entry_points=("https://www.annualreports.com/Company/nestle",),
)

HEINEKEN = Company(
    slug="heineken",
    name="Heineken N.V.",
    ticker="HEIA",
    currency="EUR",
    archive_pages=(
        "https://www.theheinekencompany.com/newsroom/heineken-nv-reports-2025-full-year-results/",
    ),
    seed_pdf_urls=(
        "https://www.theheinekencompany.com/sites/heineken-corp/files/2026-02/"
        "2025_Heineken_NV_Annual_Report_Interactive_100226_FINAL.pdf",
        "https://www.theheinekencompany.com/sites/heineken-corp/files/heineken-corp/"
        "investors/governance/agm/2024/heineken-nv-annual-report-2023.pdf",
        "https://www.theheinekencompany.com/sites/heineken-corp/files/heineken-corp/"
        "sustainability-and-responsibility/our-progress/reporting-centre/2022/"
        "heineken-nv-annual-report-2021-25-02-2022.pdf",
        "https://www.heinekenholding.com/sites/heinekenholding-v2/files/"
        "heineken-holding/governance/agm/ava/heineken-nv/2020/"
        "heineken-nv-2019-jaarverslag-uitsluitend-engelse-versie.pdf",
        "https://www.theheinekencompany.com/sites/heineken-corp/files/heineken-corp/"
        "investors/governance/agm/2018/heineken-nv-annual-report-2017.pdf",
    ),
    target_years=tuple(range(2016, 2026)),
    selected_report_years=(2017, 2019, 2021, 2023, 2025),
    # Historical reports from a navigable aggregator; the current-year report from
    # Heineken's own site, whose JS age gate the browser fills in automatically.
    entry_points=(
        "https://www.annualreports.com/Company/heineken-nv",
        "https://www.theheinekencompany.com/investors/results-reports-webcasts-presentations",
    ),
    follow_pattern="annual|report|result",
)

UNILEVER = Company(
    slug="unilever",
    name="Unilever PLC",
    ticker="ULVR",
    currency="EUR",
    archive_pages=(
        "https://www.unilever.com/investors/annual-report-and-accounts/"
        "archive-of-annual-report-and-accounts/",
    ),
    seed_pdf_urls=(
        "https://www.unilever.com/files/unilever-annual-report-and-accounts-2025.pdf",
        "https://www.unilever.com/files/unilever-annual-report-and-accounts-2023.pdf",
        "https://www.unilever.com/files/33321193-0d9a-44dd-93f8-02209fc6bd54/"
        "annual-report-and-accounts-2021.pdf",
        "https://www.unilever.com/files/origin/"
        "1e37dec387a6647bd6bd1c8d1bc8a86cd0135ed7.pdf/"
        "unilever-annual-report-and-accounts-2019.pdf",
        "https://www.unilever.com/files/origin/"
        "6be0d0dbe8c5088374b7f3ff903ef4995a1a6a62.pdf/"
        "Unilever-annual-report-and-accounts-2017.pdf",
    ),
    target_years=tuple(range(2016, 2026)),
    selected_report_years=(2017, 2019, 2021, 2023, 2025),
    # Historical reports from a navigable aggregator; the current-year report from
    # Unilever's own archive, whose Akamai protection a headed browser passes.
    entry_points=(
        "https://www.annualreports.com/Company/unilever-plc",
        "https://www.unilever.com/investors/annual-report-and-accounts/"
        "archive-of-annual-report-and-accounts/",
    ),
)

COMPANIES = {company.slug: company for company in (NESTLE, HEINEKEN, UNILEVER)}


def get_company(slug: str) -> Company:
    try:
        return COMPANIES[slug]
    except KeyError as exc:
        choices = ", ".join(sorted(COMPANIES))
        raise ValueError(f"Unknown company {slug!r}; choose from: {choices}") from exc

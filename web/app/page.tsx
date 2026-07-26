"use client";

import { useMemo, useState } from "react";
import data from "./data.generated.json";

type Statement = (typeof data.companies)[number]["statements"][number];

const repositoryUrl = "https://github.com/jayadityak/fiscal-ai-TA";
const workbookBase =
  `${repositoryUrl}/raw/refs/heads/main/outputs/` +
  "019f8d79-c13d-7a22-850f-fed24fc926c0";

const statementShortNames: Record<string, string> = {
  income_statement: "Income",
  balance_sheet: "Balance Sheet",
  cash_flow: "Cash Flow",
};

function formatValue(
  value: number | null,
  valueKind: string,
  status: string,
) {
  if (value === null) return status === "reported_dash" ? "—" : "";
  const formatted = Math.abs(value).toLocaleString("en-US", {
    maximumFractionDigits: 2,
  });
  const signed = value < 0 ? `(${formatted})` : formatted;
  return valueKind === "percent" ? `${signed}%` : signed;
}

function rowUnit(valueKind: string, currency: string) {
  if (valueKind === "percent") return "%";
  if (valueKind === "currency_per_share") return `${currency}/share`;
  if (valueKind === "shares") return "shares";
  return null;
}

function isKeyRow(label: string) {
  return /(^|\s)(total|profit for the year|net profit|operating profit|cash flow from|cash generated from operations|cash and cash equivalents at end)/i.test(
    label.trim(),
  );
}

export default function Home() {
  const [companySlug, setCompanySlug] = useState("nestle");
  const [statementKey, setStatementKey] = useState("income_statement");

  const company = useMemo(
    () => data.companies.find((item) => item.slug === companySlug)!,
    [companySlug],
  );
  const statement = company.statements.find(
    (item) => item.key === statementKey,
  ) as Statement;
  const annualFilings = company.filings
    .filter((filing) => filing.type === "annual_report")
    .sort((a, b) => b.year - a.year);

  return (
    <main>
      <header className="site-header">
        <a className="brand" href="#top" aria-label="FiscalAI take-home home">
          <span className="brand-mark">F</span>
          <span>FiscalAI Take-Home</span>
        </a>
        <nav aria-label="Primary navigation">
          <a href="#explorer">Data</a>
          <a href="#quality">Quality</a>
          <a href="#method">Method</a>
          <a className="nav-repo" href={repositoryUrl} target="_blank">
            GitHub <span aria-hidden="true">↗</span>
          </a>
        </nav>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <div className="eyebrow">
            <span className="status-dot" />
            FY2016–FY2025 · extraction complete
          </div>
          <h1>
            Financial statements,
            <br />
            <span>grounded in source.</span>
          </h1>
          <p>
            Ten years of consolidated financial statements for three European
            public companies—compiled from annual reports, resolved for
            restatements, and validated before publication.
          </p>
          <div className="hero-actions">
            <a className="button primary" href="#explorer">
              Explore the statements
            </a>
            <a
              className="button secondary"
              href={`${workbookBase}/FiscalAI_Financial_Statements.xlsx`}
            >
              Download workbook <span aria-hidden="true">↓</span>
            </a>
          </div>
        </div>

        <div className="evidence-card" aria-label="Pipeline summary">
          <div className="evidence-heading">
            <span>Delivered dataset</span>
            <span className="live-label">Verified</span>
          </div>
          <div className="metric-grid">
            <div>
              <strong>{data.summary.companies}</strong>
              <span>Companies</span>
            </div>
            <div>
              <strong>{data.summary.annualReports}</strong>
              <span>Annual reports</span>
            </div>
            <div>
              <strong>{data.summary.statements}</strong>
              <span>Statement tables</span>
            </div>
            <div>
              <strong>{data.summary.years}</strong>
              <span>Fiscal years</span>
            </div>
          </div>
          <div className="evidence-footer">
            <span className="check-mark">✓</span>
            <div>
              <strong>
                {data.summary.validationPassed}/{data.summary.validationTotal}
              </strong>
              <span> deterministic checks passed</span>
            </div>
          </div>
          <div className="evidence-footer">
            <span className="check-mark">✓</span>
            <div>
              <strong>{data.summary.consistencyIdentical}</strong>
              <span>
                {" "}
                figures identical across independent reports ·{" "}
                {data.summary.consistencyDiffering} differing across editions
              </span>
            </div>
          </div>
        </div>
      </section>

      <section className="explorer-section" id="explorer">
        <div className="section-heading">
          <div>
            <span className="section-number">01</span>
            <h2>Statement explorer</h2>
          </div>
          <p>
            Select a company and statement. Currency figures are shown in
            millions unless a row is explicitly marked otherwise.
          </p>
        </div>

        <div className="explorer">
          <div className="selector-row">
            <div className="company-selector" aria-label="Select company">
              {data.companies.map((item) => (
                <button
                  type="button"
                  key={item.slug}
                  className={item.slug === companySlug ? "active" : ""}
                  aria-pressed={item.slug === companySlug}
                  onClick={() => setCompanySlug(item.slug)}
                >
                  <span className="company-monogram">
                    {item.name.slice(0, 1)}
                  </span>
                  <span>
                    <strong>{item.name.replace(/ (S\.A\.|N\.V\.|PLC)$/, "")}</strong>
                    <small>
                      {item.ticker} · {item.currency}
                    </small>
                  </span>
                </button>
              ))}
            </div>
          </div>

          <div className="statement-tabs" role="tablist" aria-label="Statement">
            {company.statements.map((item) => (
              <button
                type="button"
                role="tab"
                key={item.key}
                aria-selected={item.key === statementKey}
                className={item.key === statementKey ? "active" : ""}
                onClick={() => setStatementKey(item.key)}
              >
                {statementShortNames[item.key]}
                <span>{item.rows.length} rows</span>
              </button>
            ))}
          </div>

          <div className="table-toolbar">
            <div>
              <span className="table-kicker">{company.ticker}</span>
              <h3>{statement.name}</h3>
            </div>
            <div className="unit-label">
              <span>Reporting currency</span>
              <strong>{statement.currency} millions*</strong>
            </div>
          </div>

          <div className="table-scroll" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Line item</th>
                  {statement.periods.map((period) => (
                    <th scope="col" key={period}>
                      {period}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {statement.rows.map((row) => (
                  <tr
                    key={`${row.label}-${row.occurrence}`}
                    className={isKeyRow(row.label) ? "key-row" : ""}
                  >
                    <th scope="row">
                      {row.label}
                      {rowUnit(row.valueKind, row.currency) && (
                        <span className="row-unit">
                          {rowUnit(row.valueKind, row.currency)}
                        </span>
                      )}
                    </th>
                    {row.values.map((value, index) => (
                      <td
                        key={`${row.label}-${statement.periods[index]}`}
                        className={
                          row.statuses[index] === "not_reported"
                            ? "empty-value"
                            : ""
                        }
                      >
                        {formatValue(
                          value,
                          row.valueKind,
                          row.statuses[index],
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="table-note">
            <span>← Scroll horizontally to compare all ten years →</span>
            <span>
              * Unless row unit indicates %, per share, or shares · Negative
              values in parentheses · A dash is company-reported; an empty cell
              means the line was not separately reported
            </span>
          </div>
          {company.comparabilityNote && (
            <p className="comparability-note">{company.comparabilityNote}</p>
          )}
        </div>
      </section>

      <section className="quality-section" id="quality">
        <div className="section-heading light">
          <div>
            <span className="section-number">02</span>
            <h2>Built for auditability</h2>
          </div>
          <p>
            Each displayed value can be traced back through the winning
            observation to a specific report, page, and document hash.
          </p>
        </div>

        <div className="quality-grid">
          <article className="quality-primary">
            <div className="quality-title">
              <span className="quality-icon">✓</span>
              <div>
                <span>Selected company</span>
                <h3>{company.name}</h3>
              </div>
            </div>
            <div className="quality-metrics">
              <div>
                <strong>
                  {company.validation.passed}/{company.validation.total}
                </strong>
                <span>Validation checks</span>
              </div>
              <div>
                <strong>
                  {company.reconciliation.passed}/
                  {company.reconciliation.total}
                </strong>
                <span>Statements reconciled</span>
              </div>
              <div>
                <strong>{company.reconciliation.cells.toLocaleString()}</strong>
                <span>Source cells audited</span>
              </div>
            </div>
            <p>
              Coverage, balance-sheet identity, profit attribution, cash
              roll-forward, source document identity, and presence checks for
              every extracted label and value all passed.
            </p>
          </article>

          <article className="restatement-card">
            <span className="card-label">Restatement policy</span>
            <h3>The newest report wins.</h3>
            <p>
              When multiple reports contain the same fiscal period, selection
              is deterministic: sort by report year descending and retain the
              most recently reported observation. No model chooses the winner.
            </p>
            <div className="restatement-flow" aria-label="Restatement example">
              <span>FY2022 original</span>
              <span aria-hidden="true">→</span>
              <span>FY2022 in 2023</span>
              <span aria-hidden="true">→</span>
              <strong>FY2022 in 2025</strong>
            </div>
          </article>
        </div>

        <div className="filings-card">
          <div className="filings-heading">
            <div>
              <span className="card-label">Source set</span>
              <h3>{company.name} annual reports</h3>
            </div>
            <span>SHA-256 provenance retained</span>
          </div>
          <div className="filings-list">
            {annualFilings.map((filing) => (
              <a
                href={filing.url}
                target="_blank"
                rel="noreferrer"
                key={`${filing.year}-${filing.sha256}`}
              >
                <span className="filing-year">{filing.year}</span>
                <span className="filing-name">
                  Annual report
                  <small>{filing.filename}</small>
                </span>
                <code>{filing.sha256.slice(0, 12)}…</code>
                <span className="filing-link" aria-hidden="true">
                  ↗
                </span>
              </a>
            ))}
          </div>
        </div>
      </section>

      <section className="method-section" id="method">
        <div className="section-heading">
          <div>
            <span className="section-number">03</span>
            <h2>A narrow LLM boundary</h2>
          </div>
          <p>
            Semantic ambiguity goes to the model. Mechanical work remains
            deterministic, inspectable code.
          </p>
        </div>

        <div className="method-grid">
          <article>
            <span>01</span>
            <h3>Discover</h3>
            <p>
              Find, download, hash, and classify investor-relations PDFs with
              deterministic rules.
            </p>
          </article>
          <article>
            <span>02</span>
            <h3>Locate & extract</h3>
            <p>
              Score likely consolidated-statement pages, then use strict JSON
              schemas to structure only the selected evidence.
            </p>
          </article>
          <article>
            <span>03</span>
            <h3>Compile</h3>
            <p>
              Parse numbers, apply source units, canonicalize labels, resolve
              restatements, and pivot ten years.
            </p>
          </article>
          <article>
            <span>04</span>
            <h3>Validate</h3>
            <p>
              Reconcile every source cell and test coverage plus accounting
              identities before publication.
            </p>
          </article>
        </div>

        <div className="boundary-grid">
          <div className="boundary-card semantic">
            <span>LLM · semantic only</span>
            <ul>
              <li>Disambiguate consolidated statement pages</li>
              <li>Map messy rows into a strict schema</li>
              <li>Group economically equivalent line-item labels</li>
            </ul>
          </div>
          <div className="boundary-card deterministic">
            <span>Code · deterministic</span>
            <ul>
              <li>PDF download and raw text extraction</li>
              <li>Numeric parsing, unit scaling, and restatement priority</li>
              <li>Pivoting, reconciliation, and accounting validation</li>
            </ul>
          </div>
        </div>
      </section>

      <section className="download-section">
        <div>
          <span className="card-label">Complete deliverables</span>
          <h2>Review the data at any depth.</h2>
          <p>
            Use the live explorer for a fast review, or download the complete
            financial and supporting workbooks for cell-level inspection.
          </p>
        </div>
        <div className="download-actions">
          <a
            className="download-card"
            href={`${workbookBase}/FiscalAI_Financial_Statements.xlsx`}
          >
            <span className="download-icon">XLSX</span>
            <span>
              <strong>Financial statements</strong>
              <small>Nine formatted 10-year tables</small>
            </span>
            <b aria-hidden="true">↓</b>
          </a>
          <a
            className="download-card"
            href={`${workbookBase}/FiscalAI_Supporting_Outputs.xlsx`}
          >
            <span className="download-icon">XLSX</span>
            <span>
              <strong>Supporting outputs</strong>
              <small>Filings, validation, reconciliation, lineage</small>
            </span>
            <b aria-hidden="true">↓</b>
          </a>
        </div>
      </section>

      <footer>
        <div className="brand footer-brand">
          <span className="brand-mark">F</span>
          <span>FiscalAI Take-Home</span>
        </div>
        <p>Built by Jayaditya Khamesra · July 2026</p>
        <a href={repositoryUrl} target="_blank">
          View source on GitHub <span aria-hidden="true">↗</span>
        </a>
      </footer>
    </main>
  );
}

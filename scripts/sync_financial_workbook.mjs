import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.cwd();
const moduleRoot = process.env.CODEX_NODE_MODULES;
if (!moduleRoot) {
  throw new Error("CODEX_NODE_MODULES must point to the bundled node_modules directory");
}
const artifactTool = await import(
  pathToFileURL(
    path.join(moduleRoot, "@oai/artifact-tool/dist/artifact_tool.mjs"),
  ).href
);
const { FileBlob, SpreadsheetFile } = artifactTool;

const workbookPath = path.join(
  root,
  "outputs/019f8d79-c13d-7a22-850f-fed24fc926c0/FiscalAI_Financial_Statements.xlsx",
);
const input = await FileBlob.load(workbookPath);
const workbook = await SpreadsheetFile.importXlsx(input);

if (process.argv.includes("--inspect")) {
  const result = await workbook.inspect({
    kind: "workbook,sheet,table",
    maxChars: 20000,
    tableMaxRows: 8,
    tableMaxCols: 16,
    tableMaxCellChars: 120,
  });
  process.stdout.write(`${result.ndjson}\n`);
  process.exit(0);
}

const data = JSON.parse(
  await fs.readFile(path.join(root, "web/app/data.generated.json"), "utf8"),
);
const sheetNames = {
  nestle: {
    income_statement: "Nestlé IS",
    balance_sheet: "Nestlé BS",
    cash_flow: "Nestlé CF",
  },
  heineken: {
    income_statement: "Heineken IS",
    balance_sheet: "Heineken BS",
    cash_flow: "Heineken CF",
  },
  unilever: {
    income_statement: "Unilever IS",
    balance_sheet: "Unilever BS",
    cash_flow: "Unilever CF",
  },
};

const normalized = (value) =>
  String(value ?? "")
    .normalize("NFKC")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();

const indexSheet = (sheet, statement) => {
  const matrix = sheet.getUsedRange().values;
  const headerRow = matrix.findIndex((row) =>
    row.some((cell) => normalized(cell) === "line item"),
  );
  if (headerRow < 0) {
    throw new Error(`Could not find the header row in ${sheet.name}`);
  }
  const headers = matrix[headerRow].map(normalized);
  const labelColumn = headers.indexOf("line item");
  const occurrenceColumn = headers.indexOf("occurrence");
  const periodColumns = new Map();
  statement.periods.forEach((year) => {
    const candidates = [
      normalized(year),
      normalized(`${year}-12-31`),
      normalized(`FY${year}`),
    ];
    const column = headers.findIndex((header) => candidates.includes(header));
    if (column < 0) {
      throw new Error(`Could not find ${year} in ${sheet.name}`);
    }
    periodColumns.set(year, column);
  });

  const rowLookup = new Map();
  const labelOccurrences = new Map();
  matrix.slice(headerRow + 1).forEach((row, offset) => {
    const label = normalized(row[labelColumn]);
    if (label) {
      const occurrence =
        occurrenceColumn >= 0
          ? Number(row[occurrenceColumn] || 1)
          : (labelOccurrences.get(label) || 0) + 1;
      labelOccurrences.set(label, occurrence);
      rowLookup.set(`${label}::${occurrence}`, headerRow + 1 + offset);
    }
  });
  return { matrix, periodColumns, rowLookup };
};

const expectedValue = (row, periodIndex) => {
  const status = row.statuses[periodIndex];
  if (status === "dash" || status === "reported_dash") {
    return "—";
  }
  return status === "reported" ? row.values[periodIndex] : null;
};

for (const company of data.companies) {
  for (const statement of company.statements) {
    const sheetName = sheetNames[company.slug][statement.key];
    const sheet = workbook.worksheets.getItem(sheetName);
    const { periodColumns, rowLookup } = indexSheet(sheet, statement);

    for (const row of statement.rows) {
      const targetRow = rowLookup.get(
        `${normalized(row.label)}::${Number(row.occurrence || 1)}`,
      );
      if (targetRow === undefined) {
        throw new Error(
          `Could not find ${row.label} occurrence ${row.occurrence} in ${sheetName}`,
        );
      }
      statement.periods.forEach((year, periodIndex) => {
        const value = expectedValue(row, periodIndex);
        const column = periodColumns.get(year);
        sheet.getCell(targetRow, column).values = [[value]];
      });
    }
  }
}

const output = await SpreadsheetFile.exportXlsx(workbook);
const temporaryPath = `${workbookPath}.audit-sync`;
await output.save(temporaryPath);
await fs.rename(temporaryPath, workbookPath);
await fs.rm(`${temporaryPath}.inspect.ndjson`, { force: true });

const verifiedInput = await FileBlob.load(workbookPath);
const verified = await SpreadsheetFile.importXlsx(verifiedInput);
let checkedPositions = 0;
const mismatches = [];
const previewDirectory = path.join(root, "audit/workbook_previews");
await fs.mkdir(previewDirectory, { recursive: true });

for (const company of data.companies) {
  for (const statement of company.statements) {
    const sheetName = sheetNames[company.slug][statement.key];
    const sheet = verified.worksheets.getItem(sheetName);
    const { matrix, periodColumns, rowLookup } = indexSheet(sheet, statement);
    for (const row of statement.rows) {
      const targetRow = rowLookup.get(
        `${normalized(row.label)}::${Number(row.occurrence || 1)}`,
      );
      statement.periods.forEach((year, periodIndex) => {
        const expected = expectedValue(row, periodIndex);
        const actual = matrix[targetRow][periodColumns.get(year)];
        const equal =
          expected === null
            ? actual === null
            : typeof expected === "number"
              ? Number(actual) === expected
              : actual === expected;
        checkedPositions += 1;
        if (!equal) {
          mismatches.push({
            sheet: sheetName,
            lineItem: row.label,
            occurrence: row.occurrence,
            year,
            expected,
            actual,
          });
        }
      });
    }
    const preview = await verified.render({
      sheetName,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    const previewName = `${company.slug}_${statement.key}.png`;
    await fs.writeFile(
      path.join(previewDirectory, previewName),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
}

if (mismatches.length) {
  throw new Error(
    `Workbook verification failed: ${JSON.stringify(mismatches.slice(0, 20))}`,
  );
}
process.stdout.write(
  `Synchronized and verified ${checkedPositions} positions in ${workbookPath}\n`,
);

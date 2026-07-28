import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.cwd();
const moduleRoot = process.env.CODEX_NODE_MODULES;
if (!moduleRoot) {
  throw new Error("CODEX_NODE_MODULES must point to the bundled node_modules directory");
}
const { FileBlob, SpreadsheetFile, Workbook } = await import(
  pathToFileURL(
    path.join(moduleRoot, "@oai/artifact-tool/dist/artifact_tool.mjs"),
  ).href
);

const workbookPath = path.join(
  root,
  "outputs/019f8d79-c13d-7a22-850f-fed24fc926c0/FiscalAI_Supporting_Outputs.xlsx",
);
const sources = [
  ["Filings", "artifacts/filings_manifest.csv"],
  ["Validation", "artifacts/validation.csv"],
  ["Reconciliation", "artifacts/reconciliation.csv"],
  ["Lineage", "artifacts/lineage.csv"],
];

const input = await FileBlob.load(workbookPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const expected = new Map();

for (const [sheetName, relativePath] of sources) {
  const csvText = await fs.readFile(path.join(root, relativePath), "utf8");
  const temporary = await Workbook.fromCSV(csvText, { sheetName: "Source" });
  const matrix = temporary.worksheets.getItem("Source").getUsedRange().values;
  expected.set(sheetName, matrix);

  const sheet = workbook.worksheets.getItem(sheetName);
  const old = sheet.getUsedRange().values;
  const oldRows = old.length;
  const oldColumns = Math.max(...old.map((row) => row.length));
  const newRows = matrix.length;
  const newColumns = Math.max(...matrix.map((row) => row.length));
  const clearRows = Math.max(oldRows, newRows);
  const clearColumns = Math.max(oldColumns, newColumns);
  sheet.getRangeByIndexes(0, 0, clearRows, clearColumns).values = Array.from(
    { length: clearRows },
    () => Array(clearColumns).fill(null),
  );
  sheet.getRangeByIndexes(0, 0, newRows, newColumns).values = matrix;

  const used = sheet.getRangeByIndexes(0, 0, newRows, newColumns);
  used.format = {
    font: { name: "Arial", size: 9, color: "#111827" },
    verticalAlignment: "center",
  };
  sheet.getRangeByIndexes(0, 0, 1, newColumns).format = {
    fill: "#243B64",
    font: { name: "Arial", size: 9, bold: true, color: "#FFFFFF" },
    verticalAlignment: "center",
  };
  sheet.freezePanes.freezeRows(1);
}

const output = await SpreadsheetFile.exportXlsx(workbook);
const temporaryPath = `${workbookPath}.audit-sync`;
await output.save(temporaryPath);
await fs.rename(temporaryPath, workbookPath);
await fs.rm(`${temporaryPath}.inspect.ndjson`, { force: true });

const verified = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
let checkedCells = 0;
const mismatches = [];
for (const [sheetName] of sources) {
  const actual = verified.worksheets.getItem(sheetName).getUsedRange().values;
  const wanted = expected.get(sheetName);
  wanted.forEach((row, rowIndex) => {
    row.forEach((value, columnIndex) => {
      checkedCells += 1;
      if (String(actual[rowIndex][columnIndex] ?? "") !== String(value ?? "")) {
        mismatches.push({
          sheetName,
          row: rowIndex + 1,
          column: columnIndex + 1,
          expected: value,
          actual: actual[rowIndex][columnIndex],
        });
      }
    });
  });
}
if (mismatches.length) {
  throw new Error(
    `Supporting workbook verification failed: ${JSON.stringify(
      mismatches.slice(0, 20),
    )}`,
  );
}
process.stdout.write(
  `Synchronized and verified ${checkedCells} supporting cells in ${workbookPath}\n`,
);

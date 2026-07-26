import assert from "node:assert/strict";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("renders the financial statement review experience", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(
    html,
    /<title>FiscalAI Take-Home \| Financial Statement Explorer<\/title>/i,
  );
  assert.match(html, /Financial statements,/);
  assert.match(html, /Statement explorer/);
  assert.match(html, /Built for auditability/);
  assert.match(html, /A narrow LLM boundary/);
  assert.match(html, /Nestlé/);
  assert.match(html, /deterministic checks passed/);
  assert.match(html, /identical across independent reports/);
  assert.match(html, /differing across editions/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

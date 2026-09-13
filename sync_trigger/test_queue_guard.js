const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "src/index.js"), "utf8")
  .replace("export default {", "globalThis.__worker = {");
let currentFetch;
const context = vm.createContext({
  console,
  Date,
  fetch: (...args) => currentFetch(...args),
});
vm.runInContext(`${source}\nglobalThis.__hasActive = hasActiveCommonEngineRun;`, context);

const env = { GITHUB_ACTIONS_DISPATCH_TOKEN: "test-token" };
const oldQueued = { id: 7, status: "queued", created_at: "2026-01-01T00:00:00Z" };
const freshQueued = { id: 8, status: "queued", created_at: new Date().toISOString() };

async function hasActive(runs, jobCount) {
  currentFetch = async (url) => {
    if (url.includes("/runs?")) return { ok: true, json: async () => ({ workflow_runs: runs }) };
    if (url.includes("/jobs?")) return { ok: true, json: async () => ({ total_count: jobCount }) };
    throw new Error(`Unexpected URL: ${url}`);
  };
  return context.__hasActive(env);
}

(async () => {
  assert.equal(await hasActive([oldQueued], 0), false,
    "an old queued run with no job must not block the scheduler");
  assert.equal(await hasActive([oldQueued], 1), true,
    "a queued run that owns a job must retain the single-writer lock");
  assert.equal(await hasActive([freshQueued], 0), true,
    "a fresh queue record must retain the single-writer lock");
  assert.equal(await hasActive([{ id: 9, status: "in_progress" }], 0), true,
    "an in-progress run must retain the single-writer lock");
  console.log("Sync queue guard: only stale jobless GitHub records are ignored.");
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});

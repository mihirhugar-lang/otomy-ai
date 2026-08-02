const GITHUB_OWNER = "mihirhugar-lang";
const GITHUB_REPOSITORY = "otomy-ai";
const GITHUB_WORKFLOW = "common-engine-sync.yml";
const GITHUB_REF = "main";

function dispatchUrl() {
  return `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`;
}

async function dispatchCommonEngine(env) {
  if (!env.GITHUB_ACTIONS_DISPATCH_TOKEN) {
    throw new Error("GITHUB_ACTIONS_DISPATCH_TOKEN is not configured");
  }

  const response = await fetch(dispatchUrl(), {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_ACTIONS_DISPATCH_TOKEN}`,
      "User-Agent": "otomy-sync-trigger",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      ref: GITHUB_REF,
      inputs: {
        sync_mode: "recent",
        full_from: "2026-04-01",
      },
    }),
  });

  if (!response.ok) {
    const detail = (await response.text()).slice(0, 500);
    throw new Error(`GitHub dispatch failed (${response.status}): ${detail}`);
  }
}

export default {
  async scheduled(_controller, env, _ctx) {
    await dispatchCommonEngine(env);
  },

  async fetch() {
    return new Response("Not found", { status: 404 });
  },
};

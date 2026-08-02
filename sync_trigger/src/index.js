const GITHUB_OWNER = "mihirhugar-lang";
const GITHUB_REPOSITORY = "otomy-ai";
const GITHUB_WORKFLOW = "common-engine-sync.yml";
const GITHUB_REF = "main";
const FULL_AUDIT_CRON = "30 19 * * *";
const ENGINE_STATE_KEY = "control/engine_state.json";

async function readEngineState(env) {
  if (!env.OTOMY_DATA) {
    throw new Error("OTOMY_DATA R2 binding is not configured");
  }
  const object = await env.OTOMY_DATA.get(ENGINE_STATE_KEY);
  if (object === null) return { state: "running", source: "default" };
  let state;
  try {
    state = JSON.parse(await object.text());
  } catch (error) {
    throw new Error(`invalid engine control marker: ${error.message}`);
  }
  if (!state || !["running", "paused"].includes(state.state)) {
    throw new Error("invalid engine control state");
  }
  return state;
}

function dispatchUrl() {
  return `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`;
}

async function dispatchCommonEngine(env, mode) {
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
        sync_mode: mode,
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
  async scheduled(controller, env, _ctx) {
    const state = await readEngineState(env);
    if (state.state === "paused") {
      console.log(`Common engine is paused; skipping ${controller.cron} dispatch`);
      return;
    }
    const mode = controller.cron === FULL_AUDIT_CRON ? "full" : "recent";
    console.log(`Dispatching Otomy common engine (${mode}) for cron ${controller.cron}`);
    await dispatchCommonEngine(env, mode);
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/status") {
      try {
        const state = await readEngineState(env);
        return Response.json(state, {
          headers: { "cache-control": "no-store" },
        });
      } catch (error) {
        return Response.json({ state: "unknown", error: error.message }, { status: 503 });
      }
    }
    return new Response("Not found", { status: 404 });
  },
};

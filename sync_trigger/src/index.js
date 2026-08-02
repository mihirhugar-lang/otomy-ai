const GITHUB_OWNER = "mihirhugar-lang";
const GITHUB_REPOSITORY = "otomy-ai";
const GITHUB_WORKFLOW = "common-engine-sync.yml";
const GITHUB_REF = "main";
const ENGINE_STATE_KEY = "control/engine_state.json";

function indiaClock(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const value = (type) => Number(parts.find((part) => part.type === type)?.value || 0);
  return { hour: value("hour"), minute: value("minute") };
}

function shouldDispatchRecent(now = new Date()) {
  const { hour, minute } = indiaClock(now);
  // Daytime (07:00–22:45 IST) stays at 15-minute freshness. Overnight uses
  // only the top-of-hour slot, so GitHub/R2 does no extra sync work at :15–:45.
  return !(hour >= 23 || hour < 7) || minute === 0;
}

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
    if (!shouldDispatchRecent()) {
      console.log(`Overnight hourly policy: skipping ${controller.cron} dispatch`);
      return;
    }
    const state = await readEngineState(env);
    if (state.state === "paused") {
      console.log(`Common engine is paused; skipping ${controller.cron} dispatch`);
      return;
    }
    console.log(`Dispatching Otomy common engine (recent) for cron ${controller.cron}`);
    await dispatchCommonEngine(env, "recent");
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

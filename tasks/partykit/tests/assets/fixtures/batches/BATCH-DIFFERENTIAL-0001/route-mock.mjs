const KEY = Symbol.for("hono-party.BATCH-DIFFERENTIAL-0001.route-mock");

export function getState() {
  if (!globalThis[KEY]) {
    globalThis[KEY] = { calls: [], impl: null };
  }
  return globalThis[KEY];
}

export function reset(impl = null) {
  const state = getState();
  state.calls = [];
  state.impl = impl;
  return state;
}

export async function routePartykitRequest(request, env, options) {
  const state = getState();
  state.calls.push({ request, env, options });
  if (!matchesPartyserverPath(new URL(request.url).pathname, options?.prefix)) {
    return null;
  }
  if (state.impl) {
    return state.impl({ request, env, options });
  }
  return null;
}
import { matchesPartyserverPath } from "../../../shared/batches/partyserver-prefix.mjs";


import { matchesPartyserverPath } from "../../../shared/batches/partyserver-prefix.mjs";

export const calls = [];

let handler = null;

export function setHandler(next) {
  handler = next;
}

export function resetCalls() {
  calls.length = 0;
  handler = null;
}

export async function routePartykitRequest(request, environment, options) {
  const call = {
    request,
    environment,
    options,
    url: request.url,
    method: request.method,
    headers: Object.fromEntries(request.headers),
    forwarded: null
  };
  calls.push(call);

  if (!matchesPartyserverPath(new URL(request.url).pathname, options?.prefix)) {
    return null;
  }

  let current = request;
  const isUpgrade =
    current.headers.get("Upgrade")?.toLowerCase() === "websocket";
  const lobby = { className: "Chat", name: "room", party: "chat" };

  if (isUpgrade && options?.onBeforeConnect) {
    const result = await options.onBeforeConnect(current, lobby);
    if (result instanceof Response) {
      call.forwarded = current;
      return result;
    }
    if (result instanceof Request) current = result;
  } else if (!isUpgrade && options?.onBeforeRequest) {
    const result = await options.onBeforeRequest(current, lobby);
    if (result instanceof Response) {
      call.forwarded = current;
      return result;
    }
    if (result instanceof Request) current = result;
  }

  call.forwarded = current;
  if (!handler) return null;
  return await handler(current, environment, options, call);
}

let handler = null;

export function setRoutePartykitHandler(next) {
  handler = next;
}

export async function routePartykitRequest(...args) {
  if (typeof handler !== "function") {
    throw new Error("partyserver route handler is not configured");
  }
  return handler(...args);
}

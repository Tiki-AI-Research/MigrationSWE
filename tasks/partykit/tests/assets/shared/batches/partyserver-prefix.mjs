// Match the baseline routePartykitRequest, including its default prefix and
// required namespace/room segments. Do not normalize the configured prefix:
// PartyServer splits it verbatim, while it drops empty request-path segments.
export function matchesPartyserverPath(pathname, prefix) {
  const prefixParts = (prefix || "parties").split("/");
  const parts = pathname.split("/").filter(Boolean);
  return parts.length >= prefixParts.length + 2 &&
    prefixParts.every((part, index) => parts[index] === part);
}

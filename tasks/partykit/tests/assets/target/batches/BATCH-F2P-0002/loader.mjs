const shared = new URL("../../../shared/batches/BATCH-F2P-0002/partyserver-stub.mjs", import.meta.url);
const workers = new URL("./cloudflare-workers-stub.mjs", import.meta.url);

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "partyserver") {
    return { url: shared.href, shortCircuit: true };
  }
  if (specifier === "cloudflare:workers") {
    return { url: workers.href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}

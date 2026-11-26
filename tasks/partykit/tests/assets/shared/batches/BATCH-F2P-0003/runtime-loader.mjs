// Resolve computed imports left in the adapter bundle by esbuild. Restrict
// each registration to its own bundle so unrelated modules are unaffected.
let modules;

export function initialize(data) {
  modules = data;
}

export async function resolve(specifier, context, nextResolve) {
  if (context.parentURL === modules.bundleURL) {
    if (specifier === "cloudflare:workers") {
      return { url: modules.cloudflareURL, shortCircuit: true };
    }
    if (specifier === "partyserver") {
      return { url: modules.partyserverURL, shortCircuit: true };
    }
  }
  return nextResolve(specifier, context);
}

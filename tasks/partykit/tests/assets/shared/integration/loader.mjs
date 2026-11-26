import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const repoRoot = path.resolve(process.env.REPO_ROOT || "/workspace/repo");
const requireFromRepo = createRequire(path.join(repoRoot, "package.json"));

function fileUrl(target) {
  return target ? pathToFileURL(target).href : null;
}

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "elysia" && process.env.ELYSIA_ENTRY) {
    return { url: fileUrl(process.env.ELYSIA_ENTRY), shortCircuit: true };
  }
  if (specifier === "partyserver" && process.env.DIFFERENTIAL_ROUTE_MOCK_URL) {
    return {
      url: fileUrl(process.env.DIFFERENTIAL_ROUTE_MOCK_URL),
      shortCircuit: true
    };
  }
  if (specifier === "cloudflare:workers" && process.env.DIFFERENTIAL_CLOUDFLARE_STUB_URL) {
    return {
      url: fileUrl(process.env.DIFFERENTIAL_CLOUDFLARE_STUB_URL),
      shortCircuit: true
    };
  }
  if (specifier === "elysia" || specifier === "hono" || specifier.startsWith("hono/")) {
    try {
      return {
        url: pathToFileURL(requireFromRepo.resolve(specifier)).href,
        shortCircuit: true
      };
    } catch {}
  }
  return nextResolve(specifier, context);
}

import { pathToFileURL } from "node:url";

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "cloudflare:workers" && process.env.CLOUDFLARE_STUB_URL) {
    return {
      url: pathToFileURL(process.env.CLOUDFLARE_STUB_URL).href,
      shortCircuit: true
    };
  }

  if (specifier === "partyserver" && process.env.PARTYSERVER_MOCK_URL) {
    return {
      url: pathToFileURL(process.env.PARTYSERVER_MOCK_URL).href,
      shortCircuit: true
    };
  }

  if (specifier === "hono/adapter" && process.env.HONO_ADAPTER_URL) {
    return {
      url: pathToFileURL(process.env.HONO_ADAPTER_URL).href,
      shortCircuit: true
    };
  }

  if (specifier === "hono/factory" && process.env.HONO_FACTORY_URL) {
    return {
      url: pathToFileURL(process.env.HONO_FACTORY_URL).href,
      shortCircuit: true
    };
  }

  return nextResolve(specifier, context);
}

import fs from "node:fs";

const partyserverMock = new URL(
  "../../fixtures/batches/BATCH-F2P-0001/partyserver-mock.mjs",
  import.meta.url
);
const cloudflareStub = new URL(
  "../../fixtures/batches/BATCH-F2P-0001/cloudflare-workers-stub.mjs",
  import.meta.url
);

function exists(url) {
  try {
    return fs.existsSync(url);
  } catch {
    return false;
  }
}

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "partyserver" && exists(partyserverMock)) {
    return { url: partyserverMock.href, shortCircuit: true };
  }
  if (specifier === "cloudflare:workers" && exists(cloudflareStub)) {
    return { url: cloudflareStub.href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}

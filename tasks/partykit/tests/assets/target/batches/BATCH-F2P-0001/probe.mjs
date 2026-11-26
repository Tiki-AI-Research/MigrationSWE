import path from "node:path";
import { fileURLToPath } from "node:url";

const sideDirectory = path.dirname(fileURLToPath(import.meta.url));

process.env.SIDE ??= "target";
process.env.REPO_ROOT ??= path.resolve(sideDirectory, "../../..");
process.env.PACKAGE_ENTRY ??= path.join(
  process.env.REPO_ROOT,
  "packages/hono-party/src/index.ts"
);

await import("../../../shared/batches/BATCH-F2P-0001/probe.mjs");

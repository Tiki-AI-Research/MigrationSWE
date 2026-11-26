import { register } from "node:module";
import { pathToFileURL } from "node:url";

const loader = process.env.MODULE_LOADER_URL;

if (!loader) {
  throw new Error("MODULE_LOADER_URL is required");
}

register(pathToFileURL(loader));

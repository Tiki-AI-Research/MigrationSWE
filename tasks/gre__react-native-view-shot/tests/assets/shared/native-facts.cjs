"use strict";
const path = require("path");
const fs = require("fs");
const { spawnSync } = require("child_process");
const { missingReactHeaders } = require("./native-source.cjs");
const cache = new Map();

function nativeFacts(root) {
  if (!cache.has(root)) {
    const run = spawnSync("python3", [path.join(__dirname, "native-validation.py"), root],
      { encoding: "utf8", timeout: 30000 });
    // Missing parser/output is an execution error, never an assumed pass.
    if (run.error) throw run.error;
    const result = JSON.parse(run.stdout);
    if (!Array.isArray(result.parsed_files) || typeof result.ok !== "boolean") {
      throw new Error("invalid native parser result");
    }
    const iosPath = path.join(root, "ios/RNViewShot.mm");
    result.ios_missing_react_headers = fs.existsSync(iosPath)
      ? missingReactHeaders(fs.readFileSync(iosPath, "utf8"),
        "/opt/migration/dependencies/node_modules/react-native") : [];
    for (const missing of result.ios_missing_react_headers) {
      result.failures.push({path: "ios/RNViewShot.mm", reason: "missing_react_header", ...missing});
    }
    result.ok = result.ok && result.ios_missing_react_headers.length === 0;
    cache.set(root, result);
  }
  return cache.get(root);
}
module.exports = { nativeFacts };

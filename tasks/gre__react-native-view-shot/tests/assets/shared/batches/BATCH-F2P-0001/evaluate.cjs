"use strict";

// Shared, side-agnostic semantic evaluator for BATCH-F2P-0001.
//
// Every observation of this F2P batch is evaluated with one predicate set; the
// evaluator never branches on whether it is looking at the source or the target
// worktree. It reports the RAW result of the target postcondition, so the same
// predicate yields FAIL on the 0.84.1 source worktree and PASS on the 0.85.0
// target worktree.
//
// The checks intentionally observe structure/existence of the required target
// API surface (symbols, module wiring, dependency resolution) rather than
// human-readable messages. No long-running command is executed by default; the
// `runCommand` helper wraps any future command with an explicit `timeout` so
// that the harness can never block forever.

const { codeOnly, codeWithoutStrings } = require("../../../shared/native-source.cjs");
const { nativeFacts } = require("../../native-facts.cjs");
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const MANIFEST = path.join(__dirname, "manifest.json");

const REL = {
  androidModule:
    "android/src/main/java/fr/greweb/reactnativeviewshot/RNViewShotModule.java",
  androidGradle: "android/build.gradle",
  ios: "ios/RNViewShot.mm",
  specs: "src/specs/NativeRNViewShot.ts",
  index: "src/index.tsx",
  packageJson: "package.json",
  packageLock: "package-lock.json",
  reactNativeConfig: "react-native.config.cjs",
};

function readFile(repo, rel) {
  try {
    return codeOnly(fs.readFileSync(path.join(repo, rel), "utf8"));
  } catch (e) {
    return null;
  }
}

function exists(repo, rel) {
  try {
    return fs.existsSync(path.join(repo, rel));
  } catch (e) {
    return false;
  }
}

// Explicit, non-blocking command runner. Every potentially long-running command
// MUST flow through here so it is guarded by `timeout` and a hard fallback.
function runCommand(command, timeoutSeconds) {
  const seconds = Math.max(1, timeoutSeconds || 60);
  const res = spawnSync("timeout", ["-k", "5", String(seconds)].concat(command), {
    encoding: "utf8",
    timeout: (seconds + 10) * 1000,
  });
  return {
    status: res.status,
    timedOut: res.error && res.error.code === "ETIMEDOUT",
    stdout: res.stdout || "",
    stderr: res.stderr || "",
  };
}

function collectChecks(repo) {
  const android = readFile(repo, REL.androidModule);
  const gradle = readFile(repo, REL.androidGradle);
  const ios = readFile(repo, REL.ios);
  const specs = readFile(repo, REL.specs);
  const index = readFile(repo, REL.index);

  const has = (text, re) => typeof text === "string" && re.test(text);
  const hasCode = (text, re) => typeof text === "string" && re.test(codeWithoutStrings(text));
  const native = nativeFacts(repo);

  let reactNativeResolution = null;
  try {
    const pkg = JSON.parse(readFile(repo, REL.packageJson) || "{}");
    const lock = JSON.parse(readFile(repo, REL.packageLock) || "{}");
    const packages = (lock && lock.packages) || {};
    const rootRn = packages["node_modules/react-native"];
    const rootCodegen = packages["node_modules/@react-native/codegen"];
    reactNativeResolution = {
      devRange:
        (pkg.devDependencies && pkg.devDependencies["react-native"]) || null,
      peerRange:
        (pkg.peerDependencies && pkg.peerDependencies["react-native"]) || null,
      lockVersion: rootRn ? rootRn.version : null,
      codegenVersion: rootCodegen ? rootCodegen.version : null,
    };
  } catch (e) {
    reactNativeResolution = null;
  }

  return {
    android_runtime_routing: () =>
      native.ok && native.android_fabric_route,

    android_release_confinement: () =>
      has(android, /getExternalCacheDir\(\)/) &&
      has(android, /getCacheDir\(\)/) &&
      has(android, /parent\.equals\(/),

    android_e_unable: () =>
      has(android, /ERROR_UNABLE_TO_SNAPSHOT/) ||
      has(android, /E_UNABLE_TO_SNAPSHOT/),

    android_webm_alias: () =>
      has(android, /"webm"\.equals\(\s*extension\s*\)/),

    ios_view_registry: () =>
      hasCode(ios, /viewRegistry_DEPRECATED/) &&
      hasCode(ios, /viewForReactTag:/) &&
      !hasCode(ios, /self\.bridge\.uiManager/),

    ios_release_confinement: () =>
      has(ios, /hasPrefix:prefix/) && has(ios, /containsString:@"\/"/),

    ios_data_uri: () => has(ios, /data:image\/%@;base64,%@/),

    ios_base64: () => has(ios, /base64EncodedStringWithOptions/),

    rndep_085: () =>
      !!reactNativeResolution &&
      /0\.85\.0/.test(reactNativeResolution.devRange || "") &&
      reactNativeResolution.lockVersion === "0.85.0" &&
      reactNativeResolution.peerRange === ">=0.76.0",

    public_capture_ref: () =>
      has(index, /export function captureRef/) && has(specs, /captureRef:/),

    public_capture_screen: () =>
      has(index, /export function captureScreen/) && has(specs, /captureScreen:/),

    public_release_capture: () =>
      has(index, /export function releaseCapture/) &&
      has(specs, /releaseCapture:/),

    public_viewshot_component: () =>
      has(index, /export default ViewShot/) && has(index, /\.capture = capture/),

    react_native_config: () => exists(repo, REL.reactNativeConfig),
  };
}

function evaluateObservations(repoRoot) {
  const manifest = JSON.parse(fs.readFileSync(MANIFEST, "utf8"));
  const checks = collectChecks(repoRoot);
  return manifest.observations.map(function (obs) {
    const results = obs.checks.map(function (name) {
      const fn = checks[name];
      if (typeof fn !== "function") {
        return { name: name, status: "INFRA_ERROR" };
      }
      let ok = false;
      try {
        ok = !!fn();
      } catch (e) {
        return { name: name, status: "INFRA_ERROR", error: String(e) };
      }
      return { name: name, status: ok ? "PASS" : "FAIL" };
    });
    const status = results.every(function (r) {
      return r.status === "PASS";
    })
      ? "PASS"
      : results.some(function (r) {
            return r.status === "INFRA_ERROR";
          })
        ? "INFRA_ERROR"
        : "FAIL";
    return {
      observation_id: obs.observation_id,
      requirement_ids: obs.requirement_ids,
      status: status,
      checks: results,
    };
  });
}

module.exports = {
  evaluateObservations: evaluateObservations,
  runCommand: runCommand,
  REL: REL,
};

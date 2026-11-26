"use strict";

// Shared F2P probe library for BATCH-F2P-0003.
//
// A single semantic predicate is evaluated against whichever repository root is
// supplied. The predicate has two layers:
//   1. target-technology projection: the react-native runtime actually resolved
//      for the repository (package-lock.json -> node_modules/react-native) must
//      be the target 0.85.0 runtime, not the source 0.84.1 runtime;
//   2. per-observation surface projection: the scoped entry/call/interface that
//      the atomic claim names must be present on the migrated surface and the
//      interface removed in the target must be gone.
//
// The same code path is used for both sides; the status is derived from the
// repository, never hard-coded per role.

const { codeOnly, containsSource } = require("../../../shared/native-source.cjs");
const { nativeFacts } = require("../../native-facts.cjs");
const fs = require("fs");
const path = require("path");

const TARGET_RN_VERSION = "0.85.0";
const SOURCE_RN_VERSION = "0.84.1";

const ANDROID_MODULE =
  "android/src/main/java/fr/greweb/reactnativeviewshot/RNViewShotModule.java";
const IOS_MODULE = "ios/RNViewShot.mm";
const WINDOWS_MODULE = "windows/RNViewShot/RNViewShotModule.cs";
const SPEC = "src/specs/NativeRNViewShot.ts";
const JS_INDEX = "src/index.tsx";

const OBJECT_PROTOTYPE_BLACKLIST = ["__proto__", "constructor", "prototype"];

function isSafeKey(key) {
  return !OBJECT_PROTOTYPE_BLACKLIST.includes(key);
}

function readJsonFile(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (e) {
    return null;
  }
}

function fileExists(repoRoot, relativePath) {
  if (typeof relativePath !== "string" || relativePath.length === 0) return false;
  if (!isSafeKey(relativePath)) return false;
  return fs.existsSync(path.join(repoRoot, relativePath));
}

function fileContains(repoRoot, relativePath, needle) {
  if (!fileExists(repoRoot, relativePath)) return false;
  try {
    return containsSource(fs.readFileSync(path.join(repoRoot, relativePath), "utf8"), needle);
  } catch (e) {
    return false;
  }
}

function resolvedRnVersion(repoRoot) {
  const lock = readJsonFile(path.join(repoRoot, "package-lock.json"));
  if (!lock) return null;
  if (lock.packages && lock.packages["node_modules/react-native"]) {
    const entry = lock.packages["node_modules/react-native"];
    if (entry && typeof entry.version === "string") return entry.version;
  }
  return null;
}

function declaredRnRange(repoRoot) {
  const pkg = readJsonFile(path.join(repoRoot, "package.json"));
  if (!pkg) return null;
  if (pkg.devDependencies && typeof pkg.devDependencies["react-native"] === "string") {
    return pkg.devDependencies["react-native"];
  }
  return null;
}

// Projections derived from each observation's atomic_claim surface. `removed`
// lists interfaces that the source runtime depended on and that must be gone on
// the migrated surface; `contains` lists interfaces/entry points that must be
// present. All strings are exact, scanner-matchable tokens.
const PROJECTIONS = {
  "F2P-049": {
    exists: IOS_MODULE,
    removed: ["self.bridge.uiManager addUIBlock:"],
  },
  "F2P-050": {
    exists: ANDROID_MODULE,
    contains: [{ path: "android/src/main/java/fr/greweb/reactnativeviewshot/ViewShot.java", value: "E_UNABLE_TO_SNAPSHOT" }],
  },
  "F2P-051": {
    exists: SPEC,
    contains: [{ path: SPEC, value: "captureScreen" }],
  },
  "F2P-052": {
    exists: IOS_MODULE,
    contains: [{ path: JS_INDEX, value: "base64" }],
  },
  "F2P-053": {
    exists: SPEC,
    contains: [{ path: SPEC, value: "captureRef" }],
  },
  "F2P-054": {
    exists: SPEC,
    contains: [{ path: SPEC, value: "releaseCapture" }],
  },
  "F2P-055": {
    exists: WINDOWS_MODULE,
    contains: [{ path: WINDOWS_MODULE, value: "releaseCapture" }],
  },
  "F2P-056": {
    exists: SPEC,
    contains: [{ path: SPEC, value: "captureRef" }],
  },
  "F2P-057": {
    exists: WINDOWS_MODULE,
    contains: [{ path: WINDOWS_MODULE, value: "releaseCapture" }],
  },
  "F2P-058": {
    exists: IOS_MODULE,
    removed: ["self.bridge.uiManager addUIBlock:"],
  },
  "F2P-059": {
    exists: ANDROID_MODULE,
    fabricRoute: true,
  },
  "F2P-060": {
    exists: SPEC,
    contains: [{ path: SPEC, value: "releaseCapture" }],
  },
  "F2P-061": {
    exists: IOS_MODULE,
    contains: [{ path: JS_INDEX, value: "tmpfile" }],
    removed: ["self.bridge.uiManager addUIBlock:"],
  },
  "F2P-062": {
    exists: JS_INDEX,
    contains: [{ path: JS_INDEX, value: "webm" }],
  },
  "F2P-063": {
    exists: IOS_MODULE,
    contains: [{ path: JS_INDEX, value: "data-uri" }],
    removed: ["self.bridge.uiManager addUIBlock:"],
  },
  "F2P-064": {
    exists: IOS_MODULE,
    removed: ["self.bridge.uiManager addUIBlock:"],
  },
  "F2P-065": {
    exists: IOS_MODULE,
    removed: ["self.bridge.uiManager addUIBlock:"],
  },
  "F2P-066": {
    exists: IOS_MODULE,
    removed: ["viewRegistry[target]"],
  },
  "F2P-067": {
    exists: WINDOWS_MODULE,
    contains: [{ path: WINDOWS_MODULE, value: "captureRef" }],
  },
  "F2P-068": {
    exists: "android/src/main",
  },
  "F2P-069": {
    exists: WINDOWS_MODULE,
    contains: [{ path: "windows/RNViewShot/ViewShot.cs", value: "E_UNABLE_TO_SNAPSHOT" }],
  },
  "F2P-070": {
    exists: SPEC,
    contains: [{ path: SPEC, value: "captureScreen" }],
  },
  "F2P-071": {
    exists: SPEC,
    contains: [{ path: SPEC, value: "releaseCapture" }],
  },
  "F2P-072": {
    exists: ANDROID_MODULE,
    fabricRoute: true,
  },
};

function targetTechnologyActive(repoRoot) {
  const resolved = resolvedRnVersion(repoRoot);
  if (resolved === null) return null;
  return resolved === TARGET_RN_VERSION;
}

function evaluateObservation(repoRoot, observationId) {
  const technology = targetTechnologyActive(repoRoot);
  if (technology === null) {
    return {
      observation_id: observationId,
      status: "INCONCLUSIVE",
      reason: "react-native runtime version could not be resolved from package-lock.json",
    };
  }
  if (technology === false) {
    return {
      observation_id: observationId,
      status: "FAIL",
      reason:
        "resolved react-native runtime is not the target " +
        TARGET_RN_VERSION +
        " runtime (source runtime is " +
        SOURCE_RN_VERSION +
        "); the target postcondition cannot hold",
    };
  }
  const projection = Object.prototype.hasOwnProperty.call(PROJECTIONS, observationId)
    ? PROJECTIONS[observationId]
    : null;
  if (!projection) {
    return {
      observation_id: observationId,
      status: "PASS",
      reason: "target runtime active but no surface projection was declared",
    };
  }
  if (projection.exists && !fileExists(repoRoot, projection.exists)) {
    return {
      observation_id: observationId,
      status: "FAIL",
      reason: "scoped surface is missing: " + projection.exists,
    };
  }
  const removed = projection.removed || [];
  if (projection.fabricRoute && !nativeFacts(repoRoot).android_fabric_route) {
    return { observation_id: observationId, status: "FAIL",
      reason: "captureRef does not dispatch its capture block through the Fabric UIManager" };
  }
  for (let i = 0; i < removed.length; i += 1) {
    const token = removed[i];
    const surface = projection.exists || IOS_MODULE;
    if (fileContains(repoRoot, surface, token)) {
      return {
        observation_id: observationId,
        status: "FAIL",
        reason:
          "scoped surface still references an interface removed in the target runtime: " +
          token,
      };
    }
  }
  const required = projection.contains || [];
  for (let i = 0; i < required.length; i += 1) {
    const item = required[i];
    if (!fileContains(repoRoot, item.path, item.value)) {
      return {
        observation_id: observationId,
        status: "FAIL",
        reason: "required target interface is missing: " + item.value,
      };
    }
  }
  return {
    observation_id: observationId,
    status: "PASS",
    reason: "target runtime active and scoped surface projection satisfied",
  };
}

function loadObservationIds(repoRoot) {
  const fixture = readJsonFile(
    path.join(repoRoot, "fixtures", "batches", "BATCH-F2P-0003", "observations.json"),
  );
  if (fixture && Array.isArray(fixture.observations)) {
    return fixture.observations.map(function (o) {
      return o.observation_id;
    });
  }
  return Object.keys(PROJECTIONS);
}

function runAll(repoRoot) {
  const ids = loadObservationIds(repoRoot);
  return ids.map(function (id) {
    return evaluateObservation(repoRoot, id);
  });
}

module.exports = {
  TARGET_RN_VERSION: TARGET_RN_VERSION,
  SOURCE_RN_VERSION: SOURCE_RN_VERSION,
  PROJECTIONS: PROJECTIONS,
  resolvedRnVersion: resolvedRnVersion,
  declaredRnRange: declaredRnRange,
  targetTechnologyActive: targetTechnologyActive,
  evaluateObservation: evaluateObservation,
  runAll: runAll,
};

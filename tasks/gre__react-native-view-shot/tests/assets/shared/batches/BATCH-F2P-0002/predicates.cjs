"use strict";

// Shared semantic predicates for BATCH-F2P-0002.
//
// Every F2P observation is evaluated with the SAME predicate on both the source
// and the target checkout: the observed behavior AND the target-technology
// postcondition (the scoped implementation is served by the React Native 0.85.0
// APIs). The source technology (0.84.1) fails the target-technology half, so
// the source side yields FAIL and the target side yields PASS without any
// role-based hardcoding.

const { codeOnly, codeWithoutStrings } = require("../../../shared/native-source.cjs");
const { nativeFacts } = require("../../native-facts.cjs");
const { bridgeFacts } = require("../../bridge-contract.cjs");
const fs = require("fs");
const path = require("path");

const FILES = {
  androidBuildGradle: "android/build.gradle",
  androidModule:
    "android/src/main/java/fr/greweb/reactnativeviewshot/RNViewShotModule.java",
  androidSpec:
    "android/src/paper/java/fr/greweb/reactnativeviewshot/NativeRNViewShotSpec.java",
  androidViewShot:
    "android/src/main/java/fr/greweb/reactnativeviewshot/ViewShot.java",
  iosModule: "ios/RNViewShot.mm",
  winModule: "windows/RNViewShot/RNViewShotModule.cs",
  winHelpers: "windows/RNViewShot/Helpers.cs",
  jsIndex: "src/index.tsx",
  jsSpec: "src/specs/NativeRNViewShot.ts",
  packageJson: "package.json",
  packageLock: "package-lock.json",
};

function read(root, rel) {
  try {
    return codeOnly(fs.readFileSync(path.join(root, rel), "utf8"));
  } catch (_e) {
    return null;
  }
}

function readJson(root, rel) {
  const text = read(root, rel);
  if (text === null) return null;
  try {
    return JSON.parse(text);
  } catch (_e) {
    return null;
  }
}

function has(root, rel, needle) {
  const text = read(root, rel);
  return text !== null && text.indexOf(needle) !== -1;
}

function hasCode(root, rel, needle) {
  let text = read(root, rel);
  if (text !== null && rel.endsWith(".mm")) text = text.replace(/@"(?:""|[^\"])*"/g, (s) => s.replace(/[^\n]/g, " "));
  if (text !== null && rel.endsWith(".java")) text = text.replace(/"(?:\\.|[^"\\])*"/g, (s) => s.replace(/[^\n]/g, " "));
  return text !== null && codeWithoutStrings(text).indexOf(needle) !== -1;
}

function lacksCode(root, rel, needle) {
  let text = read(root, rel);
  if (text !== null && rel.endsWith(".mm")) text = text.replace(/@"(?:""|[^\"])*"/g, (s) => s.replace(/[^\n]/g, " "));
  if (text !== null && rel.endsWith(".java")) text = text.replace(/"(?:\\.|[^"\\])*"/g, (s) => s.replace(/[^\n]/g, " "));
  return text !== null && codeWithoutStrings(text).indexOf(needle) === -1;
}

function lacks(root, rel, needle) {
  const text = read(root, rel);
  return text !== null && text.indexOf(needle) === -1;
}

function fileExists(root, rel) {
  try {
    return fs.statSync(path.join(root, rel)).isFile();
  } catch (_e) {
    return false;
  }
}

function resolvedReactNative(root) {
  const lock = readJson(root, FILES.packageLock);
  if (!lock || !lock.packages) return { range: null, version: null };
  const entry = lock.packages["node_modules/react-native"];
  const pkg = readJson(root, FILES.packageJson) || {};
  return {
    range: (pkg.devDependencies || {})["react-native"] || null,
    version: entry ? entry.version : null,
  };
}

function require_(_root, _rel, _needles) {
  // reserved: kept for symmetry with future dynamic-loading probes
  return true;
}

// ---------------------------------------------------------------------------
// target-technology postconditions (R001, R002, R004, R005, R036, R037)
// ---------------------------------------------------------------------------

function techR001(root) {
  // Importing the library on React Native 0.85.0 exposes captureRef,
  // captureScreen and releaseCapture on every layer; the repo root resolves
  // react-native 0.85.0 (the 0.85 codegen/type surface).
  const resolved = resolvedReactNative(root);
  const rangeOk = /0\.85\.0/.test(resolved.range || "");
  const rnOk = resolved.version === "0.85.0";
  const lock = readJson(root, FILES.packageLock) || {};
  const codegen =
    lock.packages && lock.packages["node_modules/@react-native/codegen"];
  const codegenOk = !!codegen && codegen.version === "0.85.0";
  const exportsOk =
    has(root, FILES.jsIndex, "export function captureRef(") &&
    has(root, FILES.jsIndex, "export function captureScreen(") &&
    has(root, FILES.jsIndex, "export function releaseCapture(") &&
    has(root, FILES.jsIndex, "export default ViewShot");
  const specOk =
    has(root, FILES.jsSpec, "RNViewShot") &&
    fileExists(root, FILES.androidSpec) &&
    fileExists(root, FILES.iosModule) &&
    fileExists(root, FILES.winModule);
  const ok = rangeOk && rnOk && codegenOk && exportsOk && specOk;
  return {
    ok,
    detail: `rtRange=${resolved.range} rtVersion=${resolved.version} codegen=${codegen && codegen.version} exports=${exportsOk} layers=${specOk}`,
  };
}

function scopedSourcesAdapted(root) {
  const native = nativeFacts(root);
  const iosAgnostic =
    hasCode(root, FILES.iosModule, "viewRegistry_DEPRECATED") &&
    hasCode(root, FILES.iosModule, "viewForReactTag:");
  const iosNoLegacyBridge = lacksCode(root, FILES.iosModule, "self.bridge.uiManager");
  return {
    androidFabricRoute: native.android_fabric_route,
    iosAgnostic,
    iosNoLegacyBridge,
    ok: native.ok && native.android_fabric_route && iosAgnostic && iosNoLegacyBridge,
  };
}

function techR002(root) {
  const r = scopedSourcesAdapted(root);
  return {
    ok: r.ok,
    detail: `androidFabricRoute=${r.androidFabricRoute} iosAgnostic=${r.iosAgnostic} iosNoLegacyBridge=${r.iosNoLegacyBridge}`,
  };
}

function techR004(root) {
  // Production code must not reference interfaces removed or changed in 0.85.0
  // (the iOS bridge-based view scheduling and the Android compile-time gate).
  const iosNoBridge = lacksCode(root, FILES.iosModule, "self.bridge.uiManager");
  const iosUsesRegistry =
    hasCode(root, FILES.iosModule, "viewRegistry_DEPRECATED") &&
    hasCode(root, FILES.iosModule, "viewForReactTag:");
  const native = nativeFacts(root);
  return {
    ok: iosNoBridge && iosUsesRegistry && native.ok && native.android_fabric_route,
    detail: `iosNoBridge=${iosNoBridge} iosRegistry=${iosUsesRegistry} nativeParse=${native.ok} androidFabricRoute=${native.android_fabric_route}`,
  };
}

function techR005(root) {
  // The capture responsibilities are provided by the React Native 0.85.0
  // runtime resolved from the repo root, with no nested 0.84.1 copy and no
  // dynamic loading of 0.84.1.
  const resolved = resolvedReactNative(root);
  const rangeOk = /0\.85\.0/.test(resolved.range || "");
  const rnOk = resolved.version === "0.85.0";
  const nestedPath = path.join(root, "node_modules/react-native/package.json");
  let nestedVersion = null;
  try {
    nestedVersion = JSON.parse(fs.readFileSync(nestedPath, "utf8")).version;
  } catch (_e) {
    nestedVersion = null;
  }
  const nestedOk = nestedVersion === null || nestedVersion === "0.85.0";
  const jsNoDynamic =
    lacks(root, FILES.jsSpec, "0.84.1") &&
    lacks(root, FILES.jsIndex, "0.84.1");
  const ok = rangeOk && rnOk && nestedOk && jsNoDynamic;
  return {
    ok,
    detail: `range=${resolved.range} root=${resolved.version} nested=${nestedVersion} noDynamic084=${jsNoDynamic}`,
  };
}

function techR036(root) {
  const ios = codeWithoutStrings(read(root, FILES.iosModule) || "");
  const ok =
    ios.indexOf("viewRegistry_DEPRECATED") !== -1 &&
    (ios.indexOf("addUIBlock:") !== -1 || ios.indexOf("RCTExecuteOnMainQueue(") !== -1) &&
    ios.indexOf("viewForReactTag:") !== -1 &&
    ios.indexOf("self.bridge.uiManager") === -1;
  return { ok, detail: `iosRegistry="${ok}"` };
}

function techR037(root) {
  const android = read(root, FILES.androidModule) || "";
  const native = nativeFacts(root);
  return { ok: native.ok && native.android_fabric_route,
    detail: `nativeParse=${native.ok} androidFabricRoute=${native.android_fabric_route}` };
}

// ---------------------------------------------------------------------------
// observable behavior predicates
// ---------------------------------------------------------------------------

function behaviorIosTmpfile(root) {
  const ios = read(root, FILES.iosModule) || "";
  const tmpfile =
    ios.indexOf('else if ([result isEqualToString:@"data-uri"])') !== -1 &&
    ios.indexOf("RCTTempFilePath(format, &error)") !== -1 &&
    ios.indexOf("resolve(res)") !== -1;
  return { ok: tmpfile, detail: `iosTmpfile=${tmpfile}` };
}

function behaviorWindowsCaptureRefSig(root) {
  const cs = read(root, FILES.winModule) || "";
  const ok =
    cs.indexOf("public Task<string> captureRef(int tag, JSValue options)") !== -1;
  return { ok, detail: `winCaptureRefSig=${ok}` };
}

function behaviorWindowsReleaseCaptureSig(root) {
  const cs = read(root, FILES.winModule) || "";
  const ok = cs.indexOf("public void releaseCapture(string uri)") !== -1;
  return { ok, detail: `winReleaseCaptureSig=${ok}` };
}

function behaviorWindowsReleaseConfinement(root) {
  const ok = nativeFacts(root).windows_release_confined;
  return { ok, detail: `winReleaseRawInputAndContainment=${ok}` };
}

function behaviorWindowsError(root) {
  const cs = read(root, FILES.winModule) || "";
  const native = nativeFacts(root);
  const ok = native.windows_size_guard &&
    (native.windows_capture_error_code || bridgeFacts(root).windowsErrorNormalized) &&
    /throw\s+new\s+ArgumentException\s*\(/.test(cs);
  return { ok, detail: `winError=${ok}` };
}

function behaviorDataUri(root) {
  const android = read(root, FILES.androidViewShot) || "";
  const ios = read(root, FILES.iosModule) || "";
  const win = read(root, FILES.winHelpers) || "";
  const ok =
    android.indexOf('promise.resolve("data:image/" + imageFormat + ";base64," + data)') !== -1 &&
    /"jpg"\.equals\(extension\) \? "jpeg"/.test(android) &&
    ios.indexOf('@"data:image/%@;base64,%@", imageFormat, base64') !== -1 &&
    /\(\[format isEqualToString:@"jpg"\]\) \? @"jpeg" : format/.test(ios) &&
    win.indexOf('return "data:image/" + MapMimeSubtype(extension) + ";base64," + base64;') !== -1;
  return { ok, detail: `dataUri=${ok}` };
}

function behaviorBase64(root) {
  const android = read(root, FILES.androidViewShot) || "";
  const ios = read(root, FILES.iosModule) || "";
  const ok =
    android.indexOf("Results.BASE_64") !== -1 &&
    android.indexOf("Base64.encodeToString(os.innerBuffer()") !== -1 &&
    ios.indexOf("res = [data base64EncodedStringWithOptions: 0];") !== -1;
  return { ok, detail: `base64=${ok}` };
}

function behaviorTmpfileAndroid(root) {
  const android = read(root, FILES.androidModule) || "";
  // The receiver of createTempFile may be a renamed local (e.g. `context =
  // getReactApplicationContext()`); only the tmpfile behavior is required.
  const ok =
    android.indexOf("Results.TEMP_FILE.equals(resultStreamFormat)") !== -1 &&
    /createTempFile\([^\n;]*extension\s*,\s*fileName\s*\)/.test(android) &&
    android.indexOf("File.createTempFile(TEMP_FILE_PREFIX, suffix, cacheDir)") !== -1;
  return { ok, detail: `tmpfileAndroid=${ok}` };
}

function behaviorWebmAlias(root) {
  const android = read(root, FILES.androidModule) || "";
  const js = read(root, FILES.jsIndex) || "";
  const ok =
    android.indexOf('("webp".equals(extension) || "webm".equals(extension))') !== -1 &&
    android.indexOf("Formats.WEBP") !== -1 &&
    js.indexOf('options.format === "webm"') !== -1 &&
    js.indexOf('["webp", "webm", "raw"]') !== -1;
  return { ok, detail: `webmAlias=${ok}` };
}

function behaviorErrorCode(root) {
  const android = read(root, FILES.androidViewShot) || "";
  const module = read(root, FILES.androidModule) || "";
  const ok =
    android.indexOf('ERROR_UNABLE_TO_SNAPSHOT = "E_UNABLE_TO_SNAPSHOT"') !== -1 &&
    android.indexOf("promise.reject(ERROR_UNABLE_TO_SNAPSHOT,") !== -1 &&
    module.indexOf("promise.reject(ViewShot.ERROR_UNABLE_TO_SNAPSHOT,") !== -1;
  return { ok, detail: `errorCode=${ok}` };
}

function behaviorReleaseConfinementAndroid(root) {
  const android = (read(root, FILES.androidModule) || "").replace(/\s+/g, "");
  const ok =
    android.indexOf("parent.equals(reactContext.getExternalCacheDir())||parent.equals(reactContext.getCacheDir())") !== -1 &&
    android.indexOf("if(!file.exists())return;") !== -1;
  return { ok, detail: `releaseConfinementAndroid=${ok}` };
}

function behaviorAndroidSpecCaptureRef(root) {
  const spec = read(root, FILES.androidSpec) || "";
  const ok =
    spec.indexOf("public abstract void captureRef(@Nullable Double target, ReadableMap withOptions, Promise promise);") !== -1;
  return { ok, detail: `specCaptureRef=${ok}` };
}

function behaviorAndroidSpecCaptureScreen(root) {
  const spec = read(root, FILES.androidSpec) || "";
  const ok =
    spec.indexOf("public abstract void captureScreen(ReadableMap options, Promise promise);") !== -1;
  return { ok, detail: `specCaptureScreen=${ok}` };
}

function behaviorAndroidSpecReleaseCapture(root) {
  const spec = read(root, FILES.androidSpec) || "";
  const ok =
    spec.indexOf("public abstract void releaseCapture(String uri);") !== -1 &&
    spec.indexOf('public static final String NAME = "RNViewShot";') !== -1;
  return { ok, detail: `specReleaseCapture=${ok}` };
}

function behaviorJsExports(root) {
  const js = read(root, FILES.jsIndex) || "";
  const ok =
    js.indexOf("export function captureRef(") !== -1 &&
    js.indexOf("export function captureScreen(") !== -1 &&
    js.indexOf("export function releaseCapture(") !== -1 &&
    js.indexOf("export default ViewShot;") !== -1 &&
    js.indexOf("ViewShot.captureRef = captureRef;") !== -1;
  return { ok, detail: `jsExports=${ok}` };
}

function behaviorAndroidUiManagerRuntime(root) {
  return techR037(root);
}

function behaviorIosViewRegistry(root) {
  return techR036(root);
}

function behaviorScopedImplementations(root) {
  const ok =
    fileExists(root, FILES.jsIndex) &&
    fileExists(root, FILES.jsSpec) &&
    fileExists(root, FILES.androidModule) &&
    fileExists(root, FILES.androidSpec) &&
    fileExists(root, FILES.iosModule) &&
    fileExists(root, FILES.winModule) &&
    fileExists(root, FILES.androidBuildGradle);
  return { ok, detail: `allScopedLayers=${ok}` };
}

const PREDICATES = {
  tech_r001: techR001,
  tech_r002: techR002,
  tech_r004: techR004,
  tech_r005: techR005,
  tech_r036: techR036,
  tech_r037: techR037,
  behavior_ios_tmpfile: behaviorIosTmpfile,
  behavior_windows_captureref_sig: behaviorWindowsCaptureRefSig,
  behavior_windows_releasecapture_sig: behaviorWindowsReleaseCaptureSig,
  behavior_windows_release_confinement: behaviorWindowsReleaseConfinement,
  behavior_windows_error: behaviorWindowsError,
  behavior_datauri: behaviorDataUri,
  behavior_base64: behaviorBase64,
  behavior_tmpfile_android: behaviorTmpfileAndroid,
  behavior_webm_alias: behaviorWebmAlias,
  behavior_error_code: behaviorErrorCode,
  behavior_release_confinement_android: behaviorReleaseConfinementAndroid,
  behavior_android_spec_captureref: behaviorAndroidSpecCaptureRef,
  behavior_android_spec_capturescreen: behaviorAndroidSpecCaptureScreen,
  behavior_android_spec_releasecapture: behaviorAndroidSpecReleaseCapture,
  behavior_js_exports: behaviorJsExports,
  behavior_android_ui_manager_runtime: behaviorAndroidUiManagerRuntime,
  behavior_ios_viewregistry: behaviorIosViewRegistry,
  behavior_scoped_implementations: behaviorScopedImplementations,
};

// Evaluate one observation specification against a checkout root.
function evaluate(root, spec) {
  const behaviorFn = PREDICATES[spec.behavior];
  const techFn = spec.tech ? PREDICATES[spec.tech] : null;
  const behavior = behaviorFn ? behaviorFn(root) : { ok: true, detail: "n/a" };
  const tech = techFn ? techFn(root) : { ok: true, detail: "n/a" };
  const ok = behavior.ok && tech.ok;
  return {
    observation_id: spec.id,
    requirement_ids: spec.requirement_ids,
    behavior: { id: spec.behavior, ok: behavior.ok, detail: behavior.detail },
    target_technology: { id: spec.tech || null, ok: tech.ok, detail: tech.detail },
    status: ok ? "PASS" : "FAIL",
  };
}

module.exports = {
  FILES,
  PREDICATES,
  evaluate,
  resolvedReactNative,
  scopedSourcesAdapted,
  techR001,
  techR002,
  techR004,
  techR005,
  techR036,
  techR037,
};

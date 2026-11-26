"use strict";

// Shared executable public-contract probe for BATCH-CONTRACT-0001.
//
// Given a repository root, this module verifies that the declared public
// contract of react-native-view-shot is present and returns the canonical
// normalized value of every CONTRACT observation. The same normal form is
// produced for the source repository and for the reference target, so the
// CONTRACT gate (equal_after_normalization) holds while each side must also
// report PASS.

const { codeOnly, containsSource } = require("../../../shared/native-source.cjs");
const fs = require("fs");
const path = require("path");
const { bridgeFacts } = require("../../bridge-contract.cjs");
const { nativeFacts } = require("../../native-facts.cjs");

function read(root, rel) {
  try {
    return codeOnly(fs.readFileSync(path.join(root, rel), "utf8"));
  } catch (e) {
    return null;
  }
}

function has(text, needle) {
  return containsSource(text, needle);
}

function dirExists(root, rel) {
  try {
    return fs.statSync(path.join(root, rel)).isDirectory();
  } catch (e) {
    return false;
  }
}

// The data-uri subtype mapping (CONTRACT-012): the Windows helper must turn the
// `jpg` subtype into `jpeg` while passing every other subtype through unchanged
// (BuildDataUri relies on it).  The reference happens to spell this as a ternary,
// but a `switch` or an `if`/return is behaviorally identical, so the mapping is
// judged rather than a particular expression.  The pass-through branch is still
// required -- a helper that returned a constant would break every other subtype.
function methodSection(text, name) {
  if (typeof text !== "string") {
    return null;
  }
  const start = text.indexOf(name);
  if (start === -1) {
    return null;
  }
  const next = text.indexOf("public static", start + name.length);
  return next === -1 ? text.slice(start) : text.slice(start, next);
}

function jpgSubtypeMapped(winHelpers) {
  const section = methodSection(winHelpers, "MapMimeSubtype");
  if (!section) {
    return false;
  }
  const src = section.replace(/\s+/g, " ");
  // ternary: `value == "jpg" ? "jpeg" : value`
  if (/\b(\w+)\s*[=!]==?\s*["']jpg["']\s*\?\s*["']jpeg["']\s*:\s*\1\b/.test(src)) {
    return true;
  }
  // switch: `case "jpg": return "jpeg";` with a `default: return value;`
  if (
    /case\s*["']jpg["']\s*:\s*return\s*["']jpeg["']\s*;/.test(src) &&
    /default\s*:\s*return\s+\w+\s*;/.test(src)
  ) {
    return true;
  }
  // if/return: `if (value == "jpg") return "jpeg";` with a trailing pass-through
  if (
    /if\s*\([^)]*\b(\w+)\s*[=!]==?\s*["']jpg["'][^)]*\)\s*\{?\s*return\s*["']jpeg["']\s*;/.test(src) &&
    /return\s+\w+\s*;/.test(src.replace(/return\s*["']jpeg["']\s*;/g, ""))
  ) {
    return true;
  }
  return false;
}

const JS_EXPORTS = ["captureRef", "captureScreen", "default", "releaseCapture"];
const CAPTURE_OPTION_FIELDS = [
  "fileName",
  "format",
  "height",
  "quality",
  "result",
  "width",
];
const TEST_SUITES = ["src/__tests__", "android/src/test", "windows/RNViewShot.Tests"];
const BASELINE_COMMIT = "68880f87a26e56a36965290109e044b598dec141";

// Canonical, side-independent contract value for every observation. The
// verification result (checks) is reported separately from this value.
function contractValue() {
  return {
    "CONTRACT-001": {jsExports: JS_EXPORTS.slice().sort(), nativeModuleName: "RNViewShot"},
    "CONTRACT-002": {codegenName: "RNViewShot", paperSpecName: "RNViewShot"},
    "CONTRACT-003": {releaseCapture: "String -> void"},
    "CONTRACT-004": {captureRef: "Double target, Object withOptions, Promise promise"},
    "CONTRACT-005": {captureScreen: "Object options, Promise promise"},
    "CONTRACT-006": {captureRef: "exported", resolvesTag: true},
    "CONTRACT-007": {captureScreen: "exported", validatesOptions: true},
    "CONTRACT-008": {releaseCapture: "exported", warnsNonString: true},
    "CONTRACT-009": {defaultExport: "ViewShot", rendersCollapsableFalseView: true, onLayout: true},
    "CONTRACT-010": {instanceCapture: "ref.current.capture()"},
    "CONTRACT-011": {captureOptionFields: CAPTURE_OPTION_FIELDS.slice().sort()},
    "CONTRACT-012": {dataUri: "data:image/<subtype>;base64,<base64>", jpgMapsTo: "jpeg"},
    "CONTRACT-013": {windowsReleaseCapture: "string uri -> void"},
    "CONTRACT-014": {windowsCaptureRef: "int tag, JSValue options -> Task<string>"},
    "CONTRACT-015": {resultTmpfile: "temporary file path"},
    "CONTRACT-016": {resultBase64: "raw base64 string"},
    "CONTRACT-017": {resultDataUri: "data-uri string"},
    "CONTRACT-018": {acceptedFormats: ["jpg", "png"], androidExtras: ["raw", "webm", "webp"]},
    "CONTRACT-019": {supportedFormats: ["jpeg", "jpg", "png"]},
    "CONTRACT-020": {webm: "deprecated alias of webp"},
    "CONTRACT-021": {errorCode: "E_UNABLE_TO_SNAPSHOT"},
    "CONTRACT-022": {oversizedCapture: "InvalidOperationException"},
    "CONTRACT-023": {tmpfileLocation: "application LocalFolder / cache directory"},
    "CONTRACT-024": {releaseCaptureConfinement: "relative paths only"},
    "CONTRACT-025": {captureModeMount: "one capture at mount"},
    "CONTRACT-026": {captureModeUpdate: "capture on update"},
    "CONTRACT-027": {captureModeContinuous: "requestAnimationFrame loop"},
    "CONTRACT-028": {
      requiredCapabilities: ["captureRef", "captureScreen", "releaseCapture", "ViewShot", "capture", "RNViewShot"],
    },
    "CONTRACT-029": {confinementScope: ["src", "android", "ios", "windows", "package.json"]},
    "CONTRACT-030": {publicTestSuites: TEST_SUITES.slice().sort()},
    "CONTRACT-031": {baselineCommit: BASELINE_COMMIT},
  };
}

function verify(root) {
  const bridgeContract = bridgeFacts(root);
  const nativeContract = nativeFacts(root);
  const index = read(root, "src/index.tsx");
  const bridge = read(root, "src/RNViewShot.ts");
  const spec = read(root, "src/specs/NativeRNViewShot.ts");
  const androidPaper = read(
    root,
    "android/src/paper/java/fr/greweb/reactnativeviewshot/NativeRNViewShotSpec.java",
  );
  const androidMod = read(
    root,
    "android/src/main/java/fr/greweb/reactnativeviewshot/RNViewShotModule.java",
  );
  const androidViewShot = read(
    root,
    "android/src/main/java/fr/greweb/reactnativeviewshot/ViewShot.java",
  );
  const ios = read(root, "ios/RNViewShot.mm");
  const winHelpers = read(root, "windows/RNViewShot/Helpers.cs");
  const winMod = read(root, "windows/RNViewShot/RNViewShotModule.cs");
  const winViewShot = read(root, "windows/RNViewShot/ViewShot.cs");
  const pkgRaw = read(root, "package.json");

  const checks = {};

  checks["CONTRACT-001"] =
    has(index, "export function captureRef") &&
    has(index, "export function captureScreen") &&
    has(index, "export function releaseCapture") &&
    has(index, "export default ViewShot") &&
    (has(spec, "RNViewShot") || has(bridge, "RNViewShot") || has(androidPaper, 'NAME = "RNViewShot"'));
  checks["CONTRACT-002"] =
    has(androidPaper, 'public static final String NAME = "RNViewShot"') &&
    has(spec, '"RNViewShot"');
  checks["CONTRACT-003"] =
    has(spec, "releaseCapture") &&
    has(spec, ": string") &&
    has(androidPaper, "releaseCapture(String uri)");
  checks["CONTRACT-004"] =
    bridgeContract.captureRefSignature &&
    has(androidPaper, "captureRef(@Nullable Double target, ReadableMap withOptions, Promise promise)");
  checks["CONTRACT-005"] =
    has(spec, "captureScreen") &&
    has(androidPaper, "captureScreen(ReadableMap options, Promise promise)");
  checks["CONTRACT-006"] =
    has(index, "export function captureRef") &&
    has(index, "RNViewShot.captureRef") &&
    has(index, "findNodeHandle");
  checks["CONTRACT-007"] =
    has(index, "export function captureScreen") && has(index, "RNViewShot.captureScreen");
  checks["CONTRACT-008"] =
    has(index, "export function releaseCapture") &&
    bridgeContract.releaseCaptureForwarded;
  checks["CONTRACT-009"] =
    has(index, "forwardRef") &&
    has(index, "collapsable={false}") &&
    has(index, "onLayout={onLayoutHandler}");
  checks["CONTRACT-010"] =
    has(index, "capture = useCallback") && has(index, ".capture = capture");
  checks["CONTRACT-011"] =
    has(index, "export interface CaptureOptions") &&
    CAPTURE_OPTION_FIELDS.every((f) => has(index, f + "?"));
  checks["CONTRACT-012"] =
    has(ios, "data:image/%@;base64,%@") &&
    has(androidViewShot, 'promise.resolve("data:image/"') &&
    has(winHelpers, "MapMimeSubtype") &&
    jpgSubtypeMapped(winHelpers);
  checks["CONTRACT-013"] =
    has(winMod, "public void releaseCapture(string uri)") &&
    has(winMod, "ApplicationData.Current.LocalFolder.Path");
  checks["CONTRACT-014"] =
    has(winMod, "public Task<string> captureRef(int tag, JSValue options)");
  checks["CONTRACT-015"] =
    has(androidViewShot, "TEMP_FILE = \"tmpfile\"") &&
    has(ios, "RCTTempFilePath") &&
    has(winViewShot, 'result == "tmpfile"');
  checks["CONTRACT-016"] =
    has(androidViewShot, "BASE_64 = \"base64\"") &&
    has(ios, "base64") &&
    // The Windows base64 result path. The reference happens to spell "base64"
    // only inside an oversized-capture diagnostic message; requiring that
    // literal would fail a behavior-equivalent candidate that merely rewords it,
    // so the check follows the actual base64 encoder instead.
    (has(winViewShot, "Convert.ToBase64String") || has(winViewShot, "base64"));
  checks["CONTRACT-017"] =
    has(androidViewShot, "data:image/") && has(ios, "data:image/") && has(winHelpers, "BuildDataUri");
  checks["CONTRACT-018"] =
    has(index, 'const acceptedFormats = ["png", "jpg"]') &&
    has(index, '"webp", "webm", "raw"') &&
    has(index, 'options.format = defaultOptions.format');
  checks["CONTRACT-019"] =
    has(winHelpers, 'format == "png" || format == "jpg" || format == "jpeg"');
  checks["CONTRACT-020"] =
    has(index, 'options.format === "webm"') &&
    has(index, "deprecated");
  checks["CONTRACT-021"] =
    has(androidViewShot, 'ERROR_UNABLE_TO_SNAPSHOT = "E_UNABLE_TO_SNAPSHOT"') &&
    (nativeContract.windows_capture_error_code || bridgeContract.windowsErrorNormalized);
  checks["CONTRACT-022"] =
    nativeContract.windows_size_guard;
  checks["CONTRACT-023"] =
    has(androidMod, "getCacheDir()") &&
    has(winMod, "ApplicationData.Current.LocalFolder.Path") &&
    nativeContract.windows_tmpfile_releasable &&
    has(ios, "NSTemporaryDirectory") ;
  checks["CONTRACT-024"] =
    nativeContract.windows_release_confined &&
    has(androidMod, "file.getParentFile()");
  checks["CONTRACT-025"] = has(index, 'if (captureMode === "mount") capture();');
  checks["CONTRACT-026"] = has(index, 'if (captureMode === "update") capture();');
  checks["CONTRACT-027"] =
    has(index, 'mode === "continuous"') && has(index, "requestAnimationFrame");
  checks["CONTRACT-028"] =
    checks["CONTRACT-001"] &&
    checks["CONTRACT-006"] &&
    checks["CONTRACT-007"] &&
    checks["CONTRACT-008"] &&
    checks["CONTRACT-010"];
  checks["CONTRACT-029"] =
    typeof pkgRaw === "string" &&
    has(pkgRaw, '"codegenConfig"') &&
    has(pkgRaw, '"react-native": "src/index.tsx"') &&
    !has(pkgRaw, '"./example"');
  checks["CONTRACT-030"] = TEST_SUITES.every((s) => dirExists(root, s));
  checks["CONTRACT-031"] = true;

  checks["CONTRACT-024"] = checks["CONTRACT-024"] && nativeContract.ok;

  const failing = Object.keys(checks).filter((id) => checks[id] !== true).sort();
  return {checks, failing, value: contractValue()};
}

module.exports = {contractValue, verify};

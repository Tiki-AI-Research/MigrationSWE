#!/usr/bin/env node
"use strict";

/*
 * BATCH-DIFFERENTIAL-0001 shared-case-corpus probe.
 *
 * Runs the same declared case corpus against one repository root (source or
 * reference target) and emits a normalized, side-agnostic observation report.
 * The differential gate for every DIFF-* observation is that the source and
 * target reports normalize to the same value.
 *
 * Usage:
 *   node probe.cjs <repo-root> [report-path]
 *
 * Environment:
 *   SHARED_CORPUS  override path to shared/batches/BATCH-DIFFERENTIAL-0001/cases.json
 *   REPORT_PATH    fallback report path (default /logs/result.json)
 *
 * Every potentially long-running command is wrapped in an explicit `timeout`
 * so the harness can never wait forever. The optional jest run is skipped when
 * the repository has no installed test runner.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const {spawnSync} = require("child_process");

const BATCH_ID = "BATCH-DIFFERENTIAL-0001";
const JEST_TIMEOUT_SEC = 600;

function sha256(value) {
  return "sha256:" + crypto.createHash("sha256").update(value).digest("hex");
}

function main() {
  const repo = path.resolve(
    process.argv[2] || process.env.REPO_ROOT || process.cwd(),
  );
  const reportPath = path.resolve(
    process.argv[3] || process.env.REPORT_PATH || "/logs/result.json",
  );
  const corpusPath =
    process.env.SHARED_CORPUS ||
    path.resolve(
      __dirname,
      "..",
      "..",
      "..",
      "shared",
      "batches",
      BATCH_ID,
      "cases.json",
    );

  const corpus = JSON.parse(fs.readFileSync(corpusPath, "utf8"));

  const read = rel => {
    try {
      return fs.readFileSync(path.join(repo, rel), "utf8");
    } catch {
      return null;
    }
  };
  const exists = rel => {
    try {
      fs.accessSync(path.join(repo, rel));
      return true;
    } catch {
      return false;
    }
  };
  const has = (rel, needle) => {
    const text = read(rel);
    return typeof text === "string" && text.indexOf(needle) !== -1;
  };
  const match = (rel, re) => {
    const text = read(rel);
    return typeof text === "string" && re.test(text);
  };

  const INDEX = "src/index.tsx";
  const SPEC = "src/specs/NativeRNViewShot.ts";
  const WEB = "src/RNViewShot.web.ts";
  const WIN_MODULE = "windows/RNViewShot/RNViewShotModule.cs";
  const WIN_HELPERS = "windows/RNViewShot/Helpers.cs";
  const WIN_VIEWSHOT = "windows/RNViewShot/ViewShot.cs";
  const ANDROID_MODULE =
    "android/src/main/java/fr/greweb/reactnativeviewshot/RNViewShotModule.java";
  const ANDROID_VIEWSHOT =
    "android/src/main/java/fr/greweb/reactnativeviewshot/ViewShot.java";

  const publicExports = {
    captureRef_export: match(INDEX, /export function captureRef\b/),
    captureScreen_export: match(INDEX, /export function captureScreen\b/),
    releaseCapture_export: match(INDEX, /export function releaseCapture\b/),
    default_export_ViewShot: match(INDEX, /export default ViewShot\b/),
    static_captureRef: match(INDEX, /ViewShot\.captureRef = captureRef/),
    static_releaseCapture: match(
      INDEX,
      /ViewShot\.releaseCapture = releaseCapture/,
    ),
  };

  // Behavior-equivalent Windows confinement: any form that rejects rooted
  // inputs and enforces containment against the capture directory, whether
  // inline in releaseCapture or delegated to a path-resolution helper.
  const winCombined = (read(WIN_MODULE) || "") + "\n" + (read(WIN_HELPERS) || "");

  const observed = {
    "DIFF-001": Object.assign({}, publicExports, {
      native_module_name: has(SPEC, '"RNViewShot"'),
    }),
    "DIFF-002": {
      turbomodule_registry: match(
        SPEC,
        /TurboModuleRegistry\.get<Spec>\("RNViewShot"\)/,
      ),
      legacy_fallback: match(SPEC, /NativeModules\.RNViewShot/),
    },
    "DIFF-003": {
      releaseCapture_method: match(
        SPEC,
        /releaseCapture: \(uri: string\) => void/,
      ),
    },
    "DIFF-004": {
      captureRef_method: match(SPEC, /captureRef:/),
      captureRef_withOptions: match(SPEC, /withOptions: Object/),
      captureRef_promise: match(SPEC, /withOptions: Object,\s*\) => Promise<string>/),
    },
    "DIFF-005": {
      captureScreen_method: match(
        SPEC,
        /captureScreen: \(options: Object\) => Promise<string>/,
      ),
    },
    "DIFF-006": {
      uses_findNodeHandle: match(INDEX, /findNodeHandle\(viewHandle/),
      forwards_to_native_captureRef: match(
        INDEX,
        /RNViewShot\.captureRef\(viewHandle as number, options\)/,
      ),
    },
    "DIFF-007": {
      forwards_to_native_captureScreen: match(
        INDEX,
        /RNViewShot\.captureScreen\(options\)/,
      ),
    },
    "DIFF-008": {
      string_guard: match(INDEX, /typeof uri !== "string"/),
      forwards_to_native_releaseCapture: match(
        INDEX,
        /RNViewShot\.releaseCapture\(uri\)/,
      ),
    },
    "DIFF-009": {
      forwardRef: match(
        INDEX,
        /forwardRef<ViewShotRef, ViewShotProperties>/,
      ),
      collapsable_false: match(INDEX, /collapsable=\{false\}/),
      onLayout_handler: match(INDEX, /onLayout=\{onLayoutHandler\}/),
    },
    "DIFF-010": {
      waits_first_layout: match(INDEX, /firstLayoutPromise/),
      attaches_capture: match(INDEX, /capture = capture\b/),
    },
    "DIFF-011": {
      fileName: match(INDEX, /fileName\?: string/),
      width: match(INDEX, /width\?: number/),
      height: match(INDEX, /height\?: number/),
      format: match(INDEX, /format\?: "jpg" \| "png" \| "webp" \| "webm" \| "raw"/),
      quality: match(INDEX, /quality\?: number/),
      result: has(
        INDEX,
        '"tmpfile" | "base64" | "data-uri" | "zip-base64"',
      ),
    },
    "DIFF-012": {
      web_jpg_maps_to_jpeg: match(
        WEB,
        /options\.format === "jpg" \? "jpeg" : options\.format/,
      ),
      web_data_uri_prefix: match(WEB, /"image\/" \+/),
      windows_data_uri_builder: match(
        WIN_HELPERS,
        /"data:image\/" \+ MapMimeSubtype/,
      ),
    },
    "DIFF-013": {
      windows_releaseCapture_signature: match(
        WIN_MODULE,
        /public void releaseCapture\(string uri\)/,
      ),
    },
    "DIFF-014": {
      windows_captureRef_signature: match(
        WIN_MODULE,
        /public Task<string> captureRef\(int tag, JSValue options\)/,
      ),
    },
    "DIFF-015": {
      default_result_tmpfile: match(INDEX, /result: "tmpfile"/),
      accepted_tmpfile: has(INDEX, '["tmpfile", "base64", "data-uri"]'),
    },
    "DIFF-016": {
      web_strips_data_uri_prefix: match(
        WEB,
        /replace\(\/data:image\\\/\(\\w\+\);base64,\/, ""\)/,
      ),
    },
    "DIFF-017": {
      web_data_uri_branch: match(WEB, /options\.result === "data-uri"/),
    },
    "DIFF-018": {
      accepted_formats_base: match(
        INDEX,
        /const acceptedFormats = \["png", "jpg"\]\.concat\(/,
      ),
      accepted_formats_android: match(
        INDEX,
        /Platform\.OS === "android" \? \["webp", "webm", "raw"\] : \[\]/,
      ),
    },
    "DIFF-019": {
      windows_format_whitelist: match(
        WIN_HELPERS,
        /format == "png" \|\| format == "jpg" \|\| format == "jpeg"/,
      ),
    },
    "DIFF-020": {
      webm_branch: match(INDEX, /options\.format === "webm"/),
      deprecation_warning: match(INDEX, /deprecated/),
    },
    "DIFF-021": {
      android_error_code: has(
        ANDROID_VIEWSHOT,
        'ERROR_UNABLE_TO_SNAPSHOT = "E_UNABLE_TO_SNAPSHOT"',
      ),
      windows_error_code: has(WIN_VIEWSHOT, 'ErrorUnableToSnapshot = "E_UNABLE_TO_SNAPSHOT"'),
      android_rejects: match(ANDROID_MODULE, /promise\.reject\(/),
    },
    "DIFF-022": {
      windows_oversized_error: match(
        WIN_MODULE,
        /InvalidOperationException/,
      ),
    },
    "DIFF-023": {
      windows_local_folder: match(
        WIN_MODULE,
        /ApplicationData\.Current\.LocalFolder/,
      ),
      windows_resolve_file_name: match(WIN_HELPERS, /ResolveFileName/),
      android_cache_dir: match(ANDROID_MODULE, /getCacheDir\(\)/),
    },
    "DIFF-024": {
      windows_rejects_parent_escape:
        winCombined.indexOf('StartsWith(".."') !== -1 ||
        (winCombined.indexOf("Path.GetFullPath") !== -1 &&
          winCombined.indexOf("StartsWith(") !== -1) ||
        (winCombined.indexOf("Path.GetRelativePath") !== -1 &&
          winCombined.indexOf("StartsWith(") !== -1),
      windows_rejects_rooted: winCombined.indexOf("Path.IsPathRooted") !== -1,
    },
    "DIFF-025": {
      mount_mode: match(INDEX, /captureMode === "mount"/),
    },
    "DIFF-026": {
      update_mode: match(INDEX, /captureMode === "update"/),
    },
    "DIFF-027": {
      raf_loop: match(INDEX, /requestAnimationFrame\(loop\)/),
      cancels_raf: match(INDEX, /cancelAnimationFrame\(rafRef\.current as number\)/),
    },
    "DIFF-028": Object.assign({}, publicExports, {
      capture_instance_method: match(INDEX, /capture = capture\b/),
    }),
    "DIFF-029": {
      in_scope_present:
        exists(INDEX) &&
        exists("src/RNViewShot.ts") &&
        exists(SPEC) &&
        exists("android/src/main") &&
        exists("android/src/paper") &&
        exists("ios/RNViewShot.mm") &&
        exists("windows/RNViewShot") &&
        exists("package.json"),
      out_of_scope_present:
        exists("example") &&
        exists("example-expo") &&
        exists("example-windows") &&
        exists("example-web"),
    },
    "DIFF-030": {
      js_suite_present: exists("src/__tests__/ViewShot.test.tsx"),
      android_suite_present: exists(
        "android/src/test/java/fr/greweb/reactnativeviewshot/ViewShotStateMachineTest.java",
      ),
      windows_suite_present: exists("windows/RNViewShot.Tests/HelpersTests.cs"),
    },
    "DIFF-031": {
      marker: "working-tree-patch",
      base_commit: "68880f87a26e56a36965290109e044b598dec141",
    },
  };

  const jest = runJest(repo);

  const report = {
    schema_version: "migration-differential-report:v1",
    batch_id: BATCH_ID,
    corpus_id: corpus.corpus_id,
    repo,
    cases: observed,
    case_ids: corpus.cases.map(c => c.id),
    jest,
    normalized_signature: sha256(JSON.stringify(observed)),
  };

  fs.mkdirSync(path.dirname(reportPath), {recursive: true});
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2) + "\n");
  process.stdout.write(report.normalized_signature + "\n");
}

function runJest(repo) {
  const localJest = path.join(repo, "node_modules", ".bin", "jest");
  if (!fs.existsSync(localJest)) {
    return {available: false, reason: "node_modules/.bin/jest not installed"};
  }
  const outFile = path.join(
    os.tmpdir(),
    "viewshot-differential-" + process.pid + ".json",
  );
  const result = spawnSync(
    "timeout",
    [
      String(JEST_TIMEOUT_SEC),
      "node",
      localJest,
      "--ci",
      "--json",
      "--outputFile",
      outFile,
    ],
    {cwd: repo, encoding: "utf8", timeout: (JEST_TIMEOUT_SEC + 60) * 1000},
  );
  let summary = null;
  try {
    const parsed = JSON.parse(fs.readFileSync(outFile, "utf8"));
    summary = {
      total: parsed.numTotalTests,
      passed: parsed.numPassedTests,
      failed: parsed.numFailedTests,
      suites: parsed.numTotalTestSuites,
    };
  } catch {
    summary = null;
  }
  return {
    available: true,
    exit_code: result.status,
    timed_out: result.error && result.error.code === "ETIMEDOUT",
    summary,
  };
}

main();

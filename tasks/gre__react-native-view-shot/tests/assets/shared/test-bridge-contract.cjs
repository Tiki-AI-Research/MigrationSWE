"use strict";
const assert = require("assert/strict");
const {captureRefSignature, releaseCaptureForwarded, windowsErrorNormalized} = require("./bridge-contract.cjs");

const signature = target => `export interface Spec extends TurboModule {
  captureRef: (target: ${target}, withOptions: Object) => Promise<string>;
}`;
for (const target of ["WithDefault<number, -1>", "CodegenTypes.WithDefault<number, -1>",
  "number | null", "number | null | undefined", "CodegenTypes.Double | null"]) {
  assert.equal(captureRefSignature(signature(target)), true, target);
}
for (const target of ["string", "CodegenTypes.Int32", "number | string", "any"]) {
  assert.equal(captureRefSignature(signature(target)), false, target);
}
assert.equal(captureRefSignature('// ' + signature("number")), false);
assert.equal(releaseCaptureForwarded(`export function releaseCapture(uri: string): void {
  if (typeof uri !== "string") { console.warn("invalid"); }
  else { RNViewShot?.releaseCapture(uri); }
}`), true);
assert.equal(releaseCaptureForwarded(`export function releaseCapture(uri: string): void {
  const text = 'typeof uri !== "string"; RNViewShot.releaseCapture(uri)';
}`), false);

const wrapper = `function withCode(run) {
  return Promise.resolve().then(run).catch(error => {
    throw Object.assign(error, {code: "E_UNABLE_TO_SNAPSHOT"});
  });
}
const module = {
  captureRef: (tag, options) => withCode(() => native.captureRef(tag, options)),
  captureScreen: options => withCode(() => native.captureScreen(options)),
};`;
assert.equal(windowsErrorNormalized(wrapper), true);
assert.equal(windowsErrorNormalized(wrapper.replace('code:', 'message:')), false);

// The RCTViewRegistry declaration lives in RCTBridgeModule.h. An import of a
// nonexistent same-named header must fail even when capture uses the right API.
const fs = require("fs"), os = require("os"), path = require("path");
const {missingReactHeaders} = require("./native-source.cjs");
const headerFixture = fs.mkdtempSync(path.join(os.tmpdir(), "view-shot-headers-"));
try {
  fs.writeFileSync(path.join(headerFixture, "package.json"), JSON.stringify({version: "0.85.0"}));
  fs.mkdirSync(path.join(headerFixture, "React/Base"), {recursive: true});
  fs.writeFileSync(path.join(headerFixture, "React/Base/RCTBridgeModule.h"), "@interface RCTViewRegistry : NSObject\n@end\n");
  const valid = '#import <React/RCTBridgeModule.h>\n';
  const missing = '#import <React/RCTViewRegistry.h>\n';
  assert.deepEqual(missingReactHeaders(valid, headerFixture), []);
  assert.deepEqual(missingReactHeaders(valid + missing, headerFixture),
    [{header: "React/RCTViewRegistry.h", line: 2}]);
  assert.deepEqual(missingReactHeaders('// ' + missing + '/*\n' + missing + '*/\n', headerFixture), []);
  assert.deepEqual(missingReactHeaders('#if __has_include(<React/RCTViewRegistry.h>)\n' + missing + '#endif\n', headerFixture), []);
  assert.deepEqual(missingReactHeaders('#if 0\n' + missing + '#endif\n', headerFixture), []);
  assert.deepEqual(missingReactHeaders('#if 1\n' + missing + '#endif\n', headerFixture),
    [{header: "React/RCTViewRegistry.h", line: 2}]);
  assert.deepEqual(missingReactHeaders('#include "React/AbsentHeader.h"\n', headerFixture),
    [{header: "React/AbsentHeader.h", line: 1}]);
  assert.deepEqual(missingReactHeaders('#if 0\n#elif 1\n' + missing + '#endif\n', headerFixture),
    [{header: "React/RCTViewRegistry.h", line: 3}]);
  assert.deepEqual(missingReactHeaders('#if 1\n#elif 0\n#else\n' + missing + '#endif\n', headerFixture), []);
  assert.deepEqual(missingReactHeaders('const char *s = "continued\\\n' + missing.trimEnd() + '";\n', headerFixture), []);

  // Check the shared nativeFacts binding used by both the contract hard gate
  // and the migration probes, without requiring an Android or Xcode build.
  const vm = require("vm"), binding = {exports: {}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "native-facts.cjs"), "utf8"), {
    module: binding, __dirname,
    require: name => {
      if (name === "child_process") return {spawnSync: () => ({stdout: JSON.stringify({
        ok: true, parsed_files: [], failures: [],
      })})};
      if (name === "./native-source.cjs") return {missingReactHeaders: text => missingReactHeaders(text, headerFixture)};
      return require(name);
    },
  });
  fs.mkdirSync(path.join(headerFixture, "ios"));
  fs.writeFileSync(path.join(headerFixture, "ios/RNViewShot.mm"), valid + missing);
  const facts = binding.exports.nativeFacts(headerFixture);
  assert.equal(facts.ok, false);
  assert.equal(facts.failures[0].reason, "missing_react_header");
  assert.equal(facts.failures[0].header, "React/RCTViewRegistry.h");
  assert.equal(facts.failures[0].line, 2);
} finally {
  fs.rmSync(headerFixture, {recursive: true, force: true});
}

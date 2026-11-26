"use strict";

// Target-side (Zod 3.23.8) DIFFERENTIAL probe for BATCH-DIFFERENTIAL-0001.
// Loads the migrated schema modules from the repository under test and produces
// the shared normalized projection used by the `equal_after_normalization`
// relation.
//
// Usage: node probe.js <repoRoot> <corpusPath> <outPath>
//   repoRoot   checkout that contains src/ (migrated repository when verifying)
//   corpusPath fixtures/batches/BATCH-DIFFERENTIAL-0001/fixtures.json
//   outPath    normalized JSON result file

const fs = require("fs");
const path = require("path");
const { createValidationResult, unionReportSkipsAlternatives } = require("../../../shared/integration/config-validation.cjs");
const { loadTs, runCorpus } = require(
  "../../../shared/batches/BATCH-DIFFERENTIAL-0001/harness.js"
);

const MODULES = {
  entitySharedConfigStruct: "src/shared/config/entity-config.ts",
  entityNameStruct: "src/shared/config/entity-config.ts",
  appearanceSharedConfigStruct: "src/shared/config/appearance-config.ts",
  layoutStruct: "src/utils/layout.ts",
  actionConfigStruct: "src/ha/panels/lovelace/editor/structs/action-struct.ts",
  lovelaceCardConfigStruct: "src/shared/config/lovelace-card-config.ts",
  lovelaceBadgeConfigStruct: "src/shared/config/lovelace-badge-config.ts",
  entityCardConfigStruct: "src/cards/entity-card/entity-card-config.ts",
};

function createAdapter(repoRoot) {
  const errorHandling = loadTs(
    repoRoot,
    "src/ha/common/structs/handle-errors.ts"
  );
  const validateConfig = createValidationResult(repoRoot, errorHandling, require("typescript"));
  const { handleStructError } = validateConfig;
  const schemas = {};
  for (const [name, rel] of Object.entries(MODULES)) {
    const mod = loadTs(repoRoot, rel);
    if (name in mod) schemas[name] = mod[name];
  }
  return {
    loadSchemas: () => schemas,
    decide: (schema, input) => {
      const parsed = validateConfig(schema, input);
      return { accepted: parsed.success, err: parsed.error };
    },
    handle: (hass, err, input) => {
      const output = handleStructError(hass, err, input);
      // Normalize the optional single aggregate union warning to the same
      // projection as an omitted union report. Expanded alternatives, wrong
      // paths/values, and unrelated errors remain visible to the comparison.
      if (err.issues?.length === 1 && err.issues[0].code === "invalid_union" &&
          unionReportSkipsAlternatives(handleStructError, err, input, err.issues[0].path)) {
        return { warnings: [], errors: [] };
      }
      return output;
    },
    schemaKeys: (schema) => {
      // Preprocessing/refinement wrappers do not change the public config type.
      // Inspect the enclosed object instead of requiring a top-level .shape.
      const visited = new Set();
      while (schema && !schema.shape && !visited.has(schema) &&
             schema._def?.typeName === "ZodEffects") {
        visited.add(schema);
        schema = schema.innerType();
      }
      return Object.keys(schema?.shape || {});
    },
    hasCapability: (schema) => schema != null,
    handleNative: (hass, message) =>
      handleStructError(hass, new Error(message)),
  };
}

module.exports = { createAdapter, MODULES };

if (require.main === module) {
  const repoRoot = process.argv[2];
  const corpusPath = process.argv[3];
  const outPath = process.argv[4];
  const corpus = JSON.parse(fs.readFileSync(corpusPath, "utf8")).corpus;
  const results = runCorpus(corpus, createAdapter(repoRoot), repoRoot);
  fs.writeFileSync(outPath, JSON.stringify(results, null, 2));
  console.log("target probe wrote", outPath);
}

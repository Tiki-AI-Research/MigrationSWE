// Resolve declaration exports using the evaluator's compiler. Candidate code
// is parsed as data; no package entrypoint or candidate compiler is executed.
const { declarationExports } = require('../assets/shared/typescript.cjs');
try {
  process.stdout.write(JSON.stringify(declarationExports(...process.argv.slice(2))));
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exit(2);
}

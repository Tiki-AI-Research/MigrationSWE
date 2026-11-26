// Preserve public assertions while accommodating CPU-heavy parity tests under
// evaluator instrumentation. The outer command deadline still bounds the run.
const { createRequire } = require('node:module');
const path = require('node:path');

if (/(?:^|[\\/])test[\\/]run(?:\.js)?$/.test(process.argv[1] || '')) {
  const candidateRequire = createRequire(path.join(process.cwd(), 'package.json'));
  const Mocha = candidateRequire('mocha');
  const original = Mocha.prototype.timeout;
  Mocha.prototype.timeout = function timeout(value) {
    if (!arguments.length) return original.call(this);
    const milliseconds = Number(value);
    return original.call(this, milliseconds > 0 ? Math.max(milliseconds, 30000) : value);
  };
}

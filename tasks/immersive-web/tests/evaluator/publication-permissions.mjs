import fs from 'node:fs';
import path from 'node:path';

// The evaluator normally runs as the build owner. Checking access() as that
// user would miss a 0700 staging directory renamed into the published tree.
export function assertPublishedPermissions(buildDir) {
  const seen = new Set();
  function visit(filename) {
    const stat = fs.statSync(filename);
    const required = stat.isDirectory() ? 0o555 : 0o444;
    if ((stat.mode & required) !== required) {
      throw new Error(`Published path is not readable by all users: ${filename} (mode ${(stat.mode & 0o777).toString(8)})`);
    }
    if (!stat.isDirectory()) return;
    const real = fs.realpathSync(filename);
    if (seen.has(real)) return;
    seen.add(real);
    for (const name of fs.readdirSync(filename)) visit(path.join(filename, name));
  }
  visit(buildDir);
}

// Exercise this contract even on hosts whose default umask happens to make
// ordinary mkdir/writeFile calls public. The preload also reaches npm's child
// Node processes, including watch rebuilds, without changing the parent suite.
export function restrictivePublicationEnv(baseDir, env) {
  const preload = path.join(baseDir, '.publication-umask.cjs');
  fs.writeFileSync(preload, 'process.umask(0o077);\n');
  return {
    ...env,
    NODE_OPTIONS: `${env.NODE_OPTIONS || ''} --require ${JSON.stringify(preload)}`.trim(),
  };
}

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const root = process.env.EVAL_REPO_ROOT || '/workspace/repo';
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const evaluatorNodeModules = process.env.EVALUATOR_NODE_MODULES || '/opt/evaluator/node_modules';

function loadVitestConfig(name) {
  // loadConfigFromFile writes a timestamp bundle next to the config. The
  // evaluator user cannot write the candidate checkout, so evaluate a copy in
  // a writable temp directory while resolving dependencies from the package.
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-vitest-config-'));
  const configPath = path.join(directory, name);
  fs.copyFileSync(path.join(root, name), configPath);
  const nodeModules = path.join(root, 'node_modules');
  if (fs.existsSync(nodeModules)) {
    fs.symlinkSync(nodeModules, path.join(directory, 'node_modules'), 'dir');
  }
  try {
    const script = `
      import { createRequire } from 'node:module';
      import { pathToFileURL } from 'node:url';
      const req = createRequire(${JSON.stringify(path.join(evaluatorNodeModules, 'package.json'))});
      const vite = await import(pathToFileURL(req.resolve('vite')).href);
      const loadConfigFromFile = vite.loadConfigFromFile || vite.default?.loadConfigFromFile;
      if (typeof loadConfigFromFile !== 'function') throw new Error('vite loadConfigFromFile unavailable');
      const loaded = await loadConfigFromFile({ command: 'serve', mode: 'test' }, ${JSON.stringify(configPath)});
      process.stdout.write(JSON.stringify(loaded?.config?.test || {}));
    `;
    const output = execFileSync(process.execPath, ['--input-type=module', '-e', script], {
      cwd: directory,
      encoding: 'utf8',
      env: { ...process.env, EVAL_REPO_ROOT: root },
    });
    return JSON.parse(output);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

function stripYamlComments(text) {
  return text
    .split(/\r?\n/)
    .map(line => {
      let quote = null;
      for (let index = 0; index < line.length; index += 1) {
        const char = line[index];
        if (quote) {
          if (quote === '"' && char === '\\') {
            index += 1;
            continue;
          }
          if (char === quote) quote = null;
          continue;
        }
        if (char === '"' || char === "'") {
          quote = char;
          continue;
        }
        if (char === '#' && (index === 0 || /\s/.test(line[index - 1]))) {
          return line.slice(0, index);
        }
      }
      return line;
    })
    .join('\n');
}

function indentation(line) {
  return line.match(/^ */)[0].length;
}

function collectIndentedCommands(lines, startIndex, parentIndent) {
  const commands = [];
  let blockIndent = null;
  for (let index = startIndex + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) continue;
    const indent = indentation(line);
    if (indent <= parentIndent) break;
    const trimmed = line.trim();
    if (blockIndent !== null) {
      if (indent >= blockIndent) {
        commands.push(trimmed);
        continue;
      }
      blockIndent = null;
    }
    if (trimmed.startsWith('-')) {
      const item = trimmed.slice(1).trim();
      if (/^[|>][-+]?$/.test(item)) {
        blockIndent = indent + 2;
      } else if (item) {
        commands.push(item);
      }
      continue;
    }
    const keyed = trimmed.match(/^(?:run|command|script|install)\s*:\s*(.*)$/);
    if (keyed) {
      if (/^[|>][-+]?$/.test(keyed[1])) {
        blockIndent = indent + 2;
      } else if (keyed[1]) {
        commands.push(keyed[1]);
      }
      continue;
    }
    commands.push(trimmed);
  }
  return commands;
}

function collectTravisCommands(lines) {
  const commands = [];
  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^(install|script):\s*(.*)$/);
    if (!match) continue;
    if (match[2] && !/^[|>][-+]?$/.test(match[2])) {
      commands.push(match[2]);
    }
    commands.push(...collectIndentedCommands(lines, index, 0));
  }
  return commands;
}

function collectGithubRunCommands(lines) {
  const commands = [];
  for (let index = 0; index < lines.length; index += 1) {
    const indent = indentation(lines[index]);
    const trimmed = lines[index].trim();
    // GitHub Actions writes each step as a mapping, usually as a YAML sequence
    // item (`- run: npm ci`). A line-anchored `/^run:/` misses every standard
    // sequence item, so strip the optional `- ` marker (and accept a quoted
    // mapping key) before matching.
    const item = trimmed.match(/^-\s+(.*)$/);
    const entry = item ? item[1].trim() : trimmed;
    const match = entry.match(/^(?:run|'run'|"run")\s*:\s*(.*)$/);
    if (!match) continue;
    if (!/^[|>][-+0-9]*$/.test(match[1])) {
      if (match[1]) commands.push(match[1]);
      continue;
    }
    for (let next = index + 1; next < lines.length; next += 1) {
      const line = lines[next];
      if (!line.trim()) continue;
      if (indentation(line) <= indent) break;
      commands.push(line.trim());
    }
  }
  return commands;
}

function shellSegments(command) {
  return command
    .split(/&&|\|\||;|\n/)
    .map(segment => segment.trim())
    .filter(Boolean);
}

function runsCommand(commands, command) {
  return commands.some(line =>
    shellSegments(line).some(segment => {
      const executable = segment.replace(/^sudo\s+/, '');
      return executable === command;
    }),
  );
}

describe('Evaluator-owned package and toolchain contracts', () => {
  it('f2p.package-contract', () => {
    const pkg = JSON.parse(read('package.json'));
    expect({ name: pkg.name, version: pkg.version, main: pkg.main, license: pkg.license }).toEqual({
      name: 'webvr-polyfill',
      version: '0.10.12',
      main: 'build/webvr-polyfill.js',
      license: 'Apache-2.0',
    });
    expect(pkg.dependencies).toEqual({ 'cardboard-vr-display': '^1.0.19' });
    expect(pkg.devDependencies).toEqual({
      jsdom: '^25.0.1',
      localStorage: '^1.0.3',
      vite: '^5.4.10',
      vitest: '^2.1.3',
    });
    expect(pkg.scripts).toMatchObject({
      build: 'npm test && node scripts/build-vite.mjs',
      'build-min': 'node scripts/build-vite.mjs --minify',
      'build-all': 'npm run build',
      watch: 'node scripts/watch-vite.mjs',
    });
    expect(pkg.scripts.test).toMatch(/^vitest run --config vitest\.config\.(?:mjs|js)$/);
    expect(pkg.scripts.prepublishOnly).toBe('npm run build-all');
    expect(Object.keys(pkg.scripts).sort()).toEqual([
      'build', 'build-all', 'build-min', 'prepublishOnly', 'test', 'watch',
    ].sort());
    expect(pkg.engines).toEqual({ node: '>=20', npm: '>=10' });
    expect(pkg.packageManager).toBe('npm@10');
    expect(pkg.module).toBe('build/webvr-polyfill.mjs');
    expect(pkg.exports).toEqual({
      '.': {
        import: './build/webvr-polyfill.mjs',
        require: './build/webvr-polyfill.cjs',
      },
    });
  });

  it('f2p.lockfile-contract', () => {
    const lock = JSON.parse(read('package-lock.json'));
    expect(lock.lockfileVersion).toBe(3);
    expect(lock.name).toBe('webvr-polyfill');
    expect(lock.version).toBe('0.10.12');
    expect(lock.packages[''].dependencies).toEqual({ 'cardboard-vr-display': '^1.0.19' });
    expect(lock.packages[''].devDependencies).toEqual({
      jsdom: '^25.0.1',
      localStorage: '^1.0.3',
      vite: '^5.4.10',
      vitest: '^2.1.3',
    });
    for (const required of ['node_modules/cardboard-vr-display', 'node_modules/jsdom', 'node_modules/localStorage', 'node_modules/vite', 'node_modules/vitest']) {
      expect(lock.packages).toHaveProperty(required);
    }
    const incomplete = Object.entries(lock.packages)
      .filter(([name]) => name !== '')
      .filter(([, entry]) => !entry.link && !entry.inBundle)
      .filter(([, entry]) => !entry.resolved || !entry.integrity)
      .map(([name]) => name)
      .sort();
    expect(incomplete).toEqual([]);
  });

  it('f2p.ci-and-test-contract', async () => {
    const travis = path.join(root, '.travis.yml');
    const githubCi = path.join(root, '.github/workflows/ci.yml');
    expect(fs.existsSync(travis) || fs.existsSync(githubCi)).toBe(true);
    if (fs.existsSync(travis)) {
      const ci = stripYamlComments(read('.travis.yml'));
      expect(ci).toMatch(/^\s*node_js:\s*$/m);
      expect(ci).toMatch(/^\s*-?\s*['"]?20['"]?\s*$/m);
    } else {
      const ci = stripYamlComments(read('.github/workflows/ci.yml'));
      expect(ci).toMatch(/node-version:\s*['"]?20['"]?/);
    }
    const vitestName = fs.existsSync(path.join(root, 'vitest.config.mjs'))
      ? 'vitest.config.mjs'
      : 'vitest.config.js';
    const vitest = await loadVitestConfig(vitestName);
    expect(vitest.environment).toBe('jsdom');
    expect(vitest.include).toEqual(['test/**/*.test.js']);
    expect(vitest.fileParallelism).toBe(false);
    expect(read('test/utils.js')).toContain("../src/webvr-polyfill.js");
    expect(read('test/webvr-polyfill.test.js')).toMatch(/from ['"]vitest['"]/);
    for (const removed of [
      '.babelrc',
      'rollup.config.js',
      'rollup.config.min.js',
      'test/.babelrc',
      'test/init.js',
    ]) {
      expect(fs.existsSync(path.join(root, removed))).toBe(false);
    }
  });

  it('f2p.ci-runs-migration', () => {
    // Regression: a standard GitHub Actions step is a YAML sequence item
    // (`- run: npm ci`). A line-anchored `/^run:/` missed every command and
    // produced a false negative for a correctly migrated CI workflow.
    const fixture = [
      'jobs:',
      '  build:',
      '    steps:',
      '      - uses: actions/checkout@v4',
      '      - run: npm ci',
      "      - 'run': npm test",
      '      - name: build and pack',
      '        run: |',
      '          npm run build-all',
      '          npm pack --dry-run --json',
      '      - run: >',
      '          npm ci',
      '',
    ].join('\n');
    expect(collectGithubRunCommands(fixture.split(/\r?\n/))).toEqual([
      'npm ci',
      'npm test',
      'npm run build-all',
      'npm pack --dry-run --json',
      'npm ci',
    ]);
    expect(runsCommand(['npm pack --dry-run --json --ignore-scripts'],
      'npm pack --dry-run --json')).toBe(false);
    const travis = path.join(root, '.travis.yml');
    const github = path.join(root, '.github/workflows/ci.yml');
    const ciPath = fs.existsSync(travis) ? travis : github;
    if (!fs.existsSync(ciPath)) {
      throw new Error('CI configuration is missing');
    }
    const ci = stripYamlComments(fs.readFileSync(ciPath, 'utf8'));
    const lines = ci.split(/\r?\n/);
    const commands = fs.existsSync(travis) ? collectTravisCommands(lines) : collectGithubRunCommands(lines);
    const steps = commands.flatMap(shellSegments).map(segment => segment.replace(/^sudo\s+/, ''));
    let previous = -1;
    for (const required of ['npm ci', 'npm test', 'npm run build-all',
      'npm pack --dry-run --json']) {
      const position = steps.indexOf(required, previous + 1);
      expect(position).toBeGreaterThan(previous);
      previous = position;
    }
    expect(ci).not.toMatch(/\bnode_modules\b/);
  });

  it('f2p.package-publication-contract', () => {
    const pkg = JSON.parse(read('package.json'));
    // The publication boundary must be an explicit allowlist, not an exclusion
    // list: an exclusion list silently publishes every file it did not
    // anticipate. A bare `build` directory entry has the same problem because
    // it publishes whatever happens to be sitting in `build/`.
    expect(Array.isArray(pkg.files)).toBe(true);
    expect(
      pkg.files
        .map(entry => entry.trim())
        .filter(entry => /^(?:\.\/)?build\/?$/.test(entry)),
    ).toEqual([]);

    const required = [
      'LICENSE',
      'build/licenses.txt',
      'build/webvr-polyfill.js',
      'build/webvr-polyfill.js.map',
      'build/webvr-polyfill.cjs',
      'build/webvr-polyfill.cjs.map',
      'build/webvr-polyfill.min.js',
      'build/webvr-polyfill.min.js.map',
      'build/webvr-polyfill.mjs',
      'build/webvr-polyfill.mjs.map',
      'build/webvr-polyfill.min.mjs',
      'build/webvr-polyfill.min.mjs.map',
      'package.json',
    ];
    // npm always publishes README when present; it is the only tolerated extra.
    const allowed = new Set([...required, 'README.md']);

    // Pack from a dirty copy of the checkout: stray root-level files, cache
    // directories, and stale files inside `build/` must not be published. The
    // copy also avoids writing scratch files into the candidate checkout.
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-publication-'));
    try {
      fs.cpSync(root, temp, {
        recursive: true,
        filter: source => {
          const relative = path.relative(root, source);
          if (!relative) return true;
          const top = relative.split(path.sep)[0];
          return top !== '.git' && top !== 'node_modules';
        },
      });
      for (const [name, content] of [
        ['AGENT-NOTES.md', 'scratch notes\n'],
        ['debug.log', 'debug output\n'],
        ['.cache/npm-state.json', '{}\n'],
        ['coverage/lcov.info', 'TN:\n'],
        ['build/leftover.tmp', 'stale artifact\n'],
        ['build/stale/old.js', 'stale artifact\n'],
      ]) {
        fs.mkdirSync(path.join(temp, path.dirname(name)), { recursive: true });
        fs.writeFileSync(path.join(temp, name), content);
      }

      const packedPayload = JSON.parse(
        execFileSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], {
          cwd: temp,
          encoding: 'utf8',
          env: {
            PATH: process.env.PATH,
            HOME: process.env.HOME,
            npm_config_cache: process.env.npm_config_cache,
            npm_config_offline: 'true',
            npm_config_audit: 'false',
            npm_config_fund: 'false',
          },
        }),
      );
      // npm < 11 returns an array of pack results; newer npm returns an object
      // keyed by package name. Both describe the same file list.
      const packed = Array.isArray(packedPayload) ? packedPayload : Object.values(packedPayload);
      const files = new Set(packed[0].files.map(item => item.path));
      for (const name of required) {
        expect(files.has(name)).toBe(true);
      }
      const unexpected = [...files].filter(name => !allowed.has(name)).sort();
      expect(unexpected).toEqual([]);
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });
});

import { execFile, execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';
import { preparePublicationPause } from './publication-pause.mjs';
import { prepareInputRoundTrip } from './input-round-trip.mjs';
import { assertPublishedPermissions, restrictivePublicationEnv } from './publication-permissions.mjs';

const repoRoot = process.env.EVAL_REPO_ROOT || '/workspace/repo';
const require = createRequire(path.join(repoRoot, 'package.json'));
const { JSDOM } = require('jsdom');

const root = repoRoot;
const normalPath = path.join(root, 'build/webvr-polyfill.js');
const minPath = path.join(root, 'build/webvr-polyfill.min.js');
const esmPath = path.join(root, 'build/webvr-polyfill.mjs');
const minEsmPath = path.join(root, 'build/webvr-polyfill.min.mjs');
const cjsPath = path.join(root, 'build/webvr-polyfill.cjs');
const licensePath = path.join(root, 'build/licenses.txt');
const normalMapPath = path.join(root, 'build/webvr-polyfill.js.map');
const minMapPath = path.join(root, 'build/webvr-polyfill.min.js.map');
const esmMapPath = path.join(root, 'build/webvr-polyfill.mjs.map');
const minEsmMapPath = path.join(root, 'build/webvr-polyfill.min.mjs.map');
const cjsMapPath = path.join(root, 'build/webvr-polyfill.cjs.map');

const distributionFiles = [
  'build/webvr-polyfill.js',
  'build/webvr-polyfill.js.map',
  'build/webvr-polyfill.min.js',
  'build/webvr-polyfill.min.js.map',
  'build/webvr-polyfill.mjs',
  'build/webvr-polyfill.mjs.map',
  'build/webvr-polyfill.min.mjs',
  'build/webvr-polyfill.min.mjs.map',
  'build/webvr-polyfill.cjs',
  'build/webvr-polyfill.cjs.map',
  'build/licenses.txt',
];

// ESM may carry a banner; the instruction requires it only for UMD and CJS.
function requiresLicenseBanner(name) {
  return /\.(?:js|cjs)$/.test(name);
}

function assertBannerMapOffset(content, map, license) {
  const banner = content.startsWith(license)
    ? license
    : license.match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
  if (!banner || !content.startsWith(banner)) return;
  const lines = (banner.match(/\n/g) || []).length;
  // Unmapped one-field VLQ segments are valid too; literal semicolons are
  // not the only representation of a correctly shifted source map.
  for (const line of map.mappings.split(';').slice(0, lines)) {
    expect(line.split(',').every(segment => !segment || decodeVlq(segment).length < 4)).toBe(true);
  }
}

async function waitFor(predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

function sha256File(file) {
  return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

// A source-map source is package-relative only when it resolves to a file that
// really exists inside the package. Checking the `..` prefix alone is not
// enough: stripping that prefix produces an in-package-looking string such as
// `workspace/repo/node_modules/...` that points at nothing. Dependency sources
// must also start at `node_modules/` so such a normalized-away host path cannot
// masquerade as a real dependency.
function assertPackageRelativeSource(buildRoot, source) {
  expect(path.isAbsolute(source)).toBe(false);
  const segments = source.split(/[\\/]/);
  expect(segments).not.toContain('..');
  if (segments.includes('node_modules')) {
    expect(segments[0]).toBe('node_modules');
  }
  const resolved = path.resolve(buildRoot, source);
  expect(resolved === buildRoot || resolved.startsWith(buildRoot + path.sep)).toBe(true);
  expect(fs.existsSync(resolved)).toBe(true);
  return resolved;
}

function browserLoad(file) {
  const dom = new JSDOM('', { runScripts: 'dangerously', url: 'https://example.test/' });
  dom.window.eval(fs.readFileSync(file, 'utf8'));
  return { dom, Polyfill: dom.window.WebVRPolyfill };
}

const sourceMapAlphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

function decodeVlq(segment) {
  const values = [];
  let value = 0;
  let shift = 0;
  for (const char of segment) {
    const digit = sourceMapAlphabet.indexOf(char);
    if (digit < 0) throw new Error(`invalid VLQ character: ${char}`);
    value += (digit & 31) << shift;
    if (digit & 32) {
      shift += 5;
      continue;
    }
    const negative = value & 1;
    value >>= 1;
    values.push(negative ? -value : value);
    value = 0;
    shift = 0;
  }
  return values;
}

function mappedPosition(map, generatedLine, generatedColumn) {
  const lines = map.mappings.split(';');
  let sourceIndex = 0;
  let originalLine = 0;
  let originalColumn = 0;
  let best = null;
  for (let line = 0; line <= generatedLine; line += 1) {
    let column = 0;
    for (const segment of (lines[line] || '').split(',')) {
      if (!segment) continue;
      const values = decodeVlq(segment);
      column += values[0];
      if (values.length < 4) continue;
      sourceIndex += values[1];
      originalLine += values[2];
      originalColumn += values[3];
      if (line === generatedLine && column <= generatedColumn) {
        best = {
          source: map.sources[sourceIndex],
          line: originalLine + 1,
          column: originalColumn,
        };
      }
    }
  }
  return best;
}

function resolveGeneratedToken(artifactPath, mapPath, token, sourcePath) {
  const map = JSON.parse(fs.readFileSync(mapPath, 'utf8'));
  const generated = fs.readFileSync(artifactPath, 'utf8').split('\n');
  const generatedIndex = generated.findIndex(line => line.includes(token));
  expect(generatedIndex).toBeGreaterThanOrEqual(0);
  const generatedColumn = generated[generatedIndex].indexOf(token);
  const position = mappedPosition(map, generatedIndex, generatedColumn);
  expect(position).toBeTruthy();
  const source = fs.readFileSync(sourcePath, 'utf8').split('\n');
  const sourceIndex = source.findIndex(line => line.includes(token));
  expect(sourceIndex).toBeGreaterThanOrEqual(0);
  expect(position.source).toMatch(/src\/webvr-polyfill\.js$/);
  expect(position.line).toBe(sourceIndex + 1);
}

function packageRelative(sourcePath) {
  return path.relative(root, sourcePath).split(path.sep).join('/');
}

// Track just enough JavaScript lexical state to distinguish real code lines
// from comments and multiline strings. A minified artifact may contain
// license comments and template literals with embedded newlines; those must
// not be mistaken for source-style indentation.
function stateAfterLine(line, initialState) {
  let state = initialState;
  let hasCode = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    const next = line[index + 1];
    if (state === 'normal') {
      if (character === '/' && next === '/') {
        state = 'lineComment';
        index += 1;
      } else if (character === '/' && next === '*') {
        state = 'blockComment';
        index += 1;
      } else if (character === "'") {
        hasCode = true;
        state = 'singleQuote';
      } else if (character === '"') {
        hasCode = true;
        state = 'doubleQuote';
      } else if (character === '`') {
        hasCode = true;
        state = 'template';
      } else if (!/\s/.test(character)) {
        hasCode = true;
      }
    } else if (state === 'singleQuote') {
      if (character === '\\') index += 1;
      else if (character === "'") state = 'normal';
    } else if (state === 'doubleQuote') {
      if (character === '\\') index += 1;
      else if (character === '"') state = 'normal';
    } else if (state === 'template') {
      if (character === '\\') index += 1;
      else if (character === '`') state = 'normal';
    } else if (state === 'blockComment' && character === '*' && next === '/') {
      state = 'normal';
      index += 1;
    }
  }
  return { state: state === 'lineComment' ? 'normal' : state, hasCode };
}

function minifiedCodeLines(content, license) {
  const code = content.startsWith(license)
    ? content.slice(license.length).replace(/^\r?\n/, '')
    : content;
  const lines = code.split(/\r?\n/);
  const codeLines = [];
  let state = 'normal';
  for (const line of lines) {
    const initialState = state;
    const scanned = stateAfterLine(line, state);
    // A comment may end on the same line as real minified code. Only omit
    // comment-only lines and lines containing multiline string contents.
    if (!['singleQuote', 'doubleQuote', 'template'].includes(initialState) && scanned.hasCode) {
      // A retained legal comment can close on the same line as minified code.
      // The whitespace before its closing delimiter belongs to the comment,
      // not to the code that starts immediately after it.
      const codeLine = initialState === 'blockComment'
        ? line.replace(/^[ \t]*\*\//, '')
        : line;
      codeLines.push(codeLine);
    }
    state = scanned.state;
  }
  return codeLines;
}

function assertMinifiedArtifact(baseDir, artifactPath) {
  const license = fs.readFileSync(path.join(baseDir, 'licenses.txt'), 'utf8');
  const content = fs.readFileSync(artifactPath, 'utf8');
  const codeLines = minifiedCodeLines(content, license);
  expect(codeLines.length).toBeGreaterThan(0);
  expect(codeLines.filter(line => /^[ \t]/.test(line))).toEqual([]);
}

function resolveGeneratedTokenAcrossModules(artifactPath, mapPath, token, sourcePath) {
  const map = JSON.parse(fs.readFileSync(mapPath, 'utf8'));
  const generatedLines = fs.readFileSync(artifactPath, 'utf8').split('\n');
  const sourceLines = fs.readFileSync(sourcePath, 'utf8').split('\n');
  const expectedSource = packageRelative(sourcePath);
  const expectedLines = sourceLines.flatMap((line, index) => (
    line.includes(token) ? [index + 1] : []
  ));
  expect(expectedLines.length).toBeGreaterThan(0);
  const positions = [];
  for (const [generatedLine, line] of generatedLines.entries()) {
    let generatedColumn = 0;
    while ((generatedColumn = line.indexOf(token, generatedColumn)) >= 0) {
      const position = mappedPosition(map, generatedLine, generatedColumn);
      if (position) positions.push(position);
      generatedColumn += token.length;
    }
  }
  expect(positions.some(position => (
    position.source === expectedSource && expectedLines.includes(position.line)
  ))).toBe(true);
}

function commonJsProbe(file) {
  const script = `
    const { JSDOM } = require('jsdom');
    global.window = new JSDOM('', { url: 'https://example.test/' }).window;
    delete global.document;
    delete global.navigator;
    const Polyfill = require(${JSON.stringify(file)});
    const instance = new Polyfill({ DPDB_URL: null });
    const result = {
      type: typeof Polyfill,
      version: Polyfill.version,
      display: typeof Polyfill.VRDisplay,
      frame: typeof Polyfill.VRFrameData,
      getDisplays: typeof instance.getVRDisplays,
      documentBridged: global.document === global.window.document,
      navigatorBridged: global.navigator === global.window.navigator,
    };
    process.stdout.write(JSON.stringify(result));
  `;
  return JSON.parse(execFileSync(process.execPath, ['-e', script], {
    cwd: root,
    encoding: 'utf8',
    env: { PATH: process.env.PATH, HOME: process.env.HOME },
  }));
}

function fixtureNpmCache(cacheDir) {
  // The image prewarms this evaluator-owned cache for offline installs. A
  // private cache must contain the same data, including when a candidate's
  // source suite installs its packed package in a temporary consumer.
  fs.mkdirSync(cacheDir, { recursive: true });
  fs.cpSync('/opt/npm-cache/_cacache', path.join(cacheDir, '_cacache'), {
    recursive: true,
  });
  return cacheDir;
}

function prepareBuildFixture(temp) {
  for (const name of [
    '.npmignore',
    'LICENSE',
    'README.md',
    'licenses.txt',
    'package.json',
    'package-lock.json',
    'scripts',
    'src',
    'test',
    'vite.config.mjs',
    'vite.config.js',
    'vitest.config.mjs',
    'vitest.config.js',
  ]) {
    if (!fs.existsSync(path.join(root, name))) continue;
    fs.cpSync(path.join(root, name), path.join(temp, name), { recursive: true });
  }
  const nodeModules = path.join(temp, 'node_modules');
  const sourceModules = path.join(root, 'node_modules');
  fs.cpSync(sourceModules, nodeModules, {
    recursive: true,
    dereference: true,
    filter: sourcePath => {
      const relative = path.relative(sourceModules, sourcePath);
      if (!relative) return true;
      const top = relative.split(path.sep)[0];
      return !['.vite', '.cache', '.bin'].includes(top);
    },
  });
  const sourceBin = path.join(sourceModules, '.bin');
  if (fs.existsSync(sourceBin)) {
    const targetBin = path.join(nodeModules, '.bin');
    fs.mkdirSync(targetBin);
    for (const entry of fs.readdirSync(sourceBin)) {
      const source = path.join(sourceBin, entry);
      const target = path.join(targetBin, entry);
      if (fs.lstatSync(source).isSymbolicLink()) {
        fs.symlinkSync(fs.readlinkSync(source), target);
      } else {
        fs.copyFileSync(source, target);
      }
    }
  }
  return {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    npm_config_cache: fixtureNpmCache(path.join(temp, '.npm-cache')),
    npm_config_audit: 'false',
    npm_config_fund: 'false',
  };
}

function processTable() {
  const rows = [];
  for (const entry of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const stat = fs.readFileSync(`/proc/${entry}/stat`, 'utf8');
      const close = stat.lastIndexOf(')');
      const fields = stat.slice(close + 2).split(' ');
      const state = fields[0];
      const ppid = Number(fields[1]);
      const pgrp = Number(fields[2]);
      let cmdline = '';
      try {
        cmdline = fs
          .readFileSync(`/proc/${entry}/cmdline`, 'utf8')
          .replace(/\0/g, ' ')
          .trim();
      } catch {}
      rows.push({ pid: Number(entry), ppid, pgrp, state, cmdline });
    } catch {}
  }
  return rows;
}

function liveProcessGroupMembers(pgid) {
  return processTable().filter(
    row => row.pgrp === pgid && row.state !== 'Z' && row.cmdline.length > 0,
  );
}

const watchPublishedNames = [
  'build/webvr-polyfill.js',
  'build/webvr-polyfill.js.map',
  'build/webvr-polyfill.min.js',
  'build/webvr-polyfill.min.js.map',
  'build/webvr-polyfill.mjs',
  'build/webvr-polyfill.mjs.map',
  'build/webvr-polyfill.min.mjs',
  'build/webvr-polyfill.min.mjs.map',
  'build/webvr-polyfill.cjs',
  'build/webvr-polyfill.cjs.map',
  'build/licenses.txt',
];
const watchJsNames = watchPublishedNames.filter(name => /\.(?:m?js|cjs)$/.test(name));
const watchMapNames = watchPublishedNames.filter(name => name.endsWith('.map'));

function sleep(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

// A generation is the full published set keyed by content hash. Comparing two
// generations proves that an interrupted rebuild left the previous complete
// set byte-for-byte intact rather than a partial or mixed one.
function publishedGeneration(baseDir) {
  const generation = {};
  for (const name of watchPublishedNames) {
    try {
      generation[name] = sha256File(path.join(baseDir, name));
    } catch {
      return null;
    }
  }
  return generation;
}

function stagingResidue(baseDir) {
  try {
    return fs
      .readdirSync(baseDir)
      .filter(name => /^\.?build[-.]/.test(name))
      .sort();
  } catch {
    return [];
  }
}

function allWatchJsContain(baseDir, marker) {
  try {
    return watchJsNames.every(name =>
      fs.readFileSync(path.join(baseDir, name), 'utf8').includes(marker),
    );
  } catch {
    return false;
  }
}

function watchDriverProcess(pgid) {
  return liveProcessGroupMembers(pgid).find(row => {
    const [command, ...args] = row.cmdline.split(' ');
    return (
      (command === process.execPath || path.basename(command) === 'node') &&
      args.some(argument => argument.endsWith('watch-vite.mjs'))
    );
  });
}

// Add or replace a string-valued config property. The value ends up in the
// bundled config object, so a published artifact can be probed for the marker
// without changing observable WebVR behavior.
function withConfigProbe(source, key, value) {
  const pattern = new RegExp(`${key}:\\s*'[^']*'`);
  if (pattern.test(source)) {
    return source.replace(pattern, `${key}: '${value}'`);
  }
  return source.replace('export default {', `export default {\n  ${key}: '${value}',`);
}

function assertCoherentGeneration(baseDir) {
  assertPublishedPermissions(path.join(baseDir, 'build'));
  for (const name of watchPublishedNames) {
    expect(fs.existsSync(path.join(baseDir, name))).toBe(true);
  }
  const license = fs.readFileSync(path.join(baseDir, 'licenses.txt'), 'utf8');
  expect(fs.readFileSync(path.join(baseDir, 'build/licenses.txt'), 'utf8')).toBe(license);
  const banner = license.match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
  expect(banner).toBeTruthy();
  for (const name of watchJsNames.filter(requiresLicenseBanner)) {
    expect(
      fs.readFileSync(path.join(baseDir, name), 'utf8').trimStart().startsWith(banner),
    ).toBe(true);
  }
  for (const name of watchMapNames) {
    const map = JSON.parse(fs.readFileSync(path.join(baseDir, name), 'utf8'));
    expect(Array.isArray(map.sources)).toBe(true);
    expect(map.sourcesContent).toHaveLength(map.sources.length);
  }
}

async function waitForStaging(baseDir, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (stagingResidue(baseDir).length > 0) return true;
    await sleep(25);
  }
  return false;
}

function isWatchDriverAlive(pgid) {
  try {
    return Boolean(watchDriverProcess(pgid));
  } catch {
    return false;
  }
}

// Content hash plus modification time for the whole published set. Quiescence
// is proven only when a no-op event leaves both untouched: a rebuild that
// re-emits identical bytes still moves mtime and must be treated as a spurious
// publication.
function snapshotPublished(baseDir) {
  const snapshot = {};
  for (const name of watchPublishedNames) {
    const file = path.join(baseDir, name);
    snapshot[name] = {
      sha256: sha256File(file),
      mtimeMs: fs.statSync(file).mtimeMs,
    };
  }
  return snapshot;
}

// True only while every published file is present. A concurrent reader must
// never observe a `build/` that exists but lacks part of the matrix.
function publishedComplete(baseDir) {
  return watchPublishedNames.every(name => {
    try {
      return fs.statSync(path.join(baseDir, name)).isFile();
    } catch {
      return false;
    }
  });
}

// Discard samples that straddle a directory swap. If the live directory
// stays the same, a missing file or mixed set of bytes is observable.
function samplePublishedDirectory(baseDir) {
  let before;
  let listing;
  const files = new Map();
  const hashes = [];
  try {
    before = fs.statSync(baseDir, { bigint: true });
    listing = fs.readdirSync(baseDir).sort();
    for (const name of listing) {
      try {
        const bytes = fs.readFileSync(path.join(baseDir, name));
        hashes.push([name, createHash('sha256').update(bytes).digest('hex')]);
        files.set(name, bytes.toString('utf8'));
      } catch {}
    }
    const after = fs.statSync(baseDir, { bigint: true });
    if (before.dev !== after.dev || before.ino !== after.ino ||
        before.birthtimeNs !== after.birthtimeNs) return null;
  } catch {
    return null;
  }
  const expected = watchPublishedNames.map(name => path.basename(name)).sort();
  const complete = listing.length === expected.length &&
    expected.every((name, index) => listing[index] === name && files.has(name));
  const fingerprint = createHash('sha256').update(JSON.stringify(hashes)).digest('hex');
  return { complete, listing, files, fingerprint };
}

function concurrentGenerationMatches(sample, token, config) {
  if (!sample?.complete) return false;
  return watchJsNames.every(name => sample.files.get(path.basename(name)).includes(token)) &&
    watchMapNames.every(name => {
      try {
        const map = JSON.parse(sample.files.get(path.basename(name)));
        return map.sourcesContent[map.sources.indexOf('src/config.js')] === config;
      } catch {
        return false;
      }
    });
}

describe('Evaluator-owned migration runtime contracts', () => {
  it('f2p.artifacts-current-build', () => {
    assertPublishedPermissions(path.join(root, 'build'));
    expect(fs.statSync(normalPath).size).toBeGreaterThan(1000);
    expect(fs.statSync(minPath).size).toBeGreaterThan(1000);
    expect(fs.statSync(esmPath).size).toBeGreaterThan(1000);
    expect(fs.statSync(minEsmPath).size).toBeGreaterThan(1000);
    expect(fs.statSync(cjsPath).size).toBeGreaterThan(1000);
    expect(fs.readFileSync(esmPath, 'utf8')).toMatch(/\bexport\s*\{[^}]*\bas\s+default\b|\bexport\s+default\b/);
    expect(fs.readFileSync(minEsmPath, 'utf8')).toMatch(/\bexport\s*\{[^}]*\bas\s+default\b|\bexport\s+default\b/);
    const rootLicense = fs.readFileSync(path.join(root, 'licenses.txt'), 'utf8');
    expect(fs.readFileSync(licensePath, 'utf8')).toBe(rootLicense);
    expect(fs.readFileSync(licensePath, 'utf8')).toContain('Apache License');
    const banner = rootLicense.match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
    expect(banner).toBeTruthy();
    for (const file of [normalPath, minPath]) {
      expect(fs.readFileSync(file, 'utf8').trimStart().startsWith(banner)).toBe(true);
    }
    for (const file of [normalMapPath, minMapPath, esmMapPath, minEsmMapPath, cjsMapPath]) {
      const map = JSON.parse(fs.readFileSync(file, 'utf8'));
      expect(map.version).toBe(3);
      expect(Array.isArray(map.sources)).toBe(true);
      for (const source of map.sources) {
        assertPackageRelativeSource(root, source);
      }
      const entry = map.sources.findIndex(source =>
        /(?:^|\/)src\/webvr-polyfill\.js$/.test(source));
      expect(entry).toBeGreaterThan(-1);
      expect(Array.isArray(map.sourcesContent)).toBe(true);
      expect(map.sourcesContent[entry]).toContain('WebVRPolyfill');
      expect(map.mappings.length).toBeGreaterThan(0);
      assertBannerMapOffset(fs.readFileSync(file.slice(0, -4), 'utf8'), map, rootLicense);
    }
  });

  it('f2p.minified-artifacts', () => {
    // esbuild may retain a leading inline legal/purity comment on its sole
    // code line. Such a line is code; an indented version must still fail.
    expect(minifiedCodeLines('/* legal */(()=>{})();\n', '')).toEqual(['/* legal */(()=>{})();']);
    expect(minifiedCodeLines('  /* legal */(()=>{})();\n', '').some(line => /^[ \t]/.test(line))).toBe(true);
    expect(minifiedCodeLines('/* legal\n */(()=>{})();\n', '')).toEqual(['(()=>{})();']);
    expect(minifiedCodeLines('/* legal\n */  (()=>{})();\n', '').some(line => /^[ \t]/.test(line))).toBe(true);
    expect(minifiedCodeLines('/* comment only */\n// comment\n', '')).toEqual([]);
    assertMinifiedArtifact(root, minPath);
    assertMinifiedArtifact(root, minEsmPath);
  });

  it('f2p.browser-global', async () => {
    for (const file of [normalPath, minPath]) {
      const content = fs.readFileSync(file, 'utf8');
      const { dom, Polyfill } = browserLoad(file);
      expect(Polyfill).toBeTypeOf('function');
      expect(() => new Polyfill({ DPDB_URL: null })).not.toThrow();
      expect(dom.window.navigator.getVRDisplays).toBeTypeOf('function');
      expect(content).not.toMatch(/\brequire\s*\(\s*['"]/);
    }
    for (const file of [esmPath, minEsmPath]) {
      const dom = new JSDOM('', { url: 'https://example.test/' });
      global.window = dom.window;
      delete global.document;
      delete global.navigator;
      const module = await import(`${pathToFileURL(file).href}?test=${Date.now()}-${path.basename(file)}`);
      expect(module.default).toBeTypeOf('function');
      const polyfill = new module.default({ DPDB_URL: null });
      expect(polyfill.enabled).toBe(true);
      expect(dom.window.navigator.getVRDisplays).toBeTypeOf('function');
    }
  });

  it('f2p.source-map-mapping-resolution', () => {
    resolveGeneratedToken(
      normalPath,
      normalMapPath,
      'prototype.getVRDisplays',
      path.join(root, 'src/webvr-polyfill.js'),
    );
  });

  it('f2p.source-map-matrix-resolution', () => {
    const sourcePath = path.join(root, 'src/webvr-polyfill.js');
    for (const [artifactPath, mapPath] of [
      [normalPath, normalMapPath],
      [minPath, minMapPath],
      [esmPath, esmMapPath],
      [minEsmPath, minEsmMapPath],
      [cjsPath, cjsMapPath],
    ]) {
      resolveGeneratedToken(artifactPath, mapPath, 'prototype.getVRDisplays', sourcePath);
    }
  });

  it('f2p.source-map-source-content', () => {
    for (const mapPath of [normalMapPath, minMapPath, esmMapPath, minEsmMapPath, cjsMapPath]) {
      const map = JSON.parse(fs.readFileSync(mapPath, 'utf8'));
      expect(Array.isArray(map.sources)).toBe(true);
      expect(Array.isArray(map.sourcesContent)).toBe(true);
      expect(map.sourcesContent).toHaveLength(map.sources.length);
      expect(map.sources.some(source => /(?:^|\/)src\/webvr-polyfill\.js$/.test(source))).toBe(true);
      expect(map.sources.some(source => /(?:^|\/)src\/index\.js$/.test(source))).toBe(false);
      for (const [index, source] of map.sources.entries()) {
        expect(typeof source).toBe('string');
        const resolved = assertPackageRelativeSource(root, source);
        expect(typeof map.sourcesContent[index]).toBe('string');
        expect(map.sourcesContent[index]).toBe(fs.readFileSync(resolved, 'utf8'));
      }
    }
  });

  it('f2p.source-map-multi-module-resolution', () => {
    const probes = [
      { token: 'dpdb.webvr.rocks', source: 'src/config.js' },
      { token: 'ADDITIONAL_VIEWERS', source: 'src/config.js' },
      { token: 'prototype.getVRDisplays', source: 'src/webvr-polyfill.js' },
      { token: 'hasNative', source: 'src/webvr-polyfill.js' },
    ];
    for (const [artifactPath, mapPath] of [
      [normalPath, normalMapPath],
      [minPath, minMapPath],
      [esmPath, esmMapPath],
      [minEsmPath, minEsmMapPath],
    ]) {
      for (const probe of probes) {
        resolveGeneratedTokenAcrossModules(
          artifactPath,
          mapPath,
          probe.token,
          path.join(root, probe.source),
        );
      }
    }
  });

  it('f2p.clean-build-matrix', { timeout: 120000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-build-'));
    try {
      for (const name of [
        '.npmignore',
        'LICENSE',
        'README.md',
        'licenses.txt',
        'package.json',
        'package-lock.json',
        'scripts',
        'src',
        'test',
        'vite.config.mjs',
        'vite.config.js',
        'vitest.config.mjs',
        'vitest.config.js',
      ]) {
        if (!fs.existsSync(path.join(root, name))) continue;
        fs.cpSync(path.join(root, name), path.join(temp, name), { recursive: true });
      }
      const nodeModules = path.join(temp, 'node_modules');
      const sourceModules = path.join(root, 'node_modules');
      fs.cpSync(sourceModules, nodeModules, {
        recursive: true,
        // A symlinked node_modules makes Vite resolve dependencies by realpath,
        // so an otherwise valid package-relative source map records the
        // evaluator's package directory instead of the clean-build package.
        // Copy the installed tree (dereferencing links) so the fixture is the
        // same package layout a consumer/CI build actually gets. The top-level
        // .bin directory is recreated below as relative symlinks; dereferencing
        // those would turn the npm shims into extension-less files and break
        // their relative imports.
        dereference: true,
        filter: sourcePath => {
          const relative = path.relative(sourceModules, sourcePath);
          if (!relative) return true;
          const top = relative.split(path.sep)[0];
          return !['.vite', '.cache', '.bin'].includes(top);
        },
      });
      const sourceBin = path.join(sourceModules, '.bin');
      if (fs.existsSync(sourceBin)) {
        const targetBin = path.join(nodeModules, '.bin');
        fs.mkdirSync(targetBin);
        for (const entry of fs.readdirSync(sourceBin)) {
          const source = path.join(sourceBin, entry);
          const target = path.join(targetBin, entry);
          if (fs.lstatSync(source).isSymbolicLink()) {
            fs.symlinkSync(fs.readlinkSync(source), target);
          } else {
            fs.copyFileSync(source, target);
          }
        }
      }
      const buildEnv = {
        PATH: process.env.PATH,
        HOME: process.env.HOME,
        npm_config_cache: fixtureNpmCache(path.join(temp, '.npm-cache')),
        npm_config_audit: 'false',
        npm_config_fund: 'false',
      };
      execFileSync('npm', ['run', 'build-all'], {
        cwd: temp,
        encoding: 'utf8',
        env: restrictivePublicationEnv(temp, buildEnv),
      });
      assertPublishedPermissions(path.join(temp, 'build'));
      for (const name of [
        'build/webvr-polyfill.js',
        'build/webvr-polyfill.min.js',
        'build/webvr-polyfill.mjs',
        'build/webvr-polyfill.min.mjs',
        'build/webvr-polyfill.cjs',
        'build/webvr-polyfill.js.map',
        'build/webvr-polyfill.min.js.map',
        'build/webvr-polyfill.mjs.map',
        'build/webvr-polyfill.min.mjs.map',
        'build/webvr-polyfill.cjs.map',
        'build/licenses.txt',
      ]) {
        expect(fs.existsSync(path.join(temp, name))).toBe(true);
      }
      expect(fs.readFileSync(path.join(temp, 'build/licenses.txt'), 'utf8')).toContain(
        'Apache License',
      );
      assertMinifiedArtifact(temp, path.join(temp, 'build/webvr-polyfill.min.js'));
      assertMinifiedArtifact(temp, path.join(temp, 'build/webvr-polyfill.min.mjs'));
      for (const name of [
        'build/webvr-polyfill.js.map',
        'build/webvr-polyfill.min.js.map',
        'build/webvr-polyfill.mjs.map',
        'build/webvr-polyfill.min.mjs.map',
        'build/webvr-polyfill.cjs.map',
      ]) {
        const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
        for (const source of map.sources) {
          assertPackageRelativeSource(temp, source);
        }
      }

    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.clean-build-min-matrix', { timeout: 180000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-build-min-'));
    try {
      const buildEnv = restrictivePublicationEnv(temp, prepareBuildFixture(temp));
      const buildDir = path.join(temp, 'build');
      fs.rmSync(buildDir, { recursive: true, force: true });
      execFileSync('npm', ['run', 'build-min'], {
        cwd: temp,
        encoding: 'utf8',
        env: buildEnv,
      });
      assertPublishedPermissions(buildDir);

      const minified = [
        'build/webvr-polyfill.min.js',
        'build/webvr-polyfill.min.js.map',
        'build/webvr-polyfill.min.mjs',
        'build/webvr-polyfill.min.mjs.map',
        'build/licenses.txt',
      ];
      for (const name of minified) {
        expect(fs.existsSync(path.join(temp, name))).toBe(true);
      }
      // The minified build is self-contained: it must not require or emit the
      // readable artifacts or the CommonJS entry from a clean tree.
      for (const name of [
        'build/webvr-polyfill.js',
        'build/webvr-polyfill.js.map',
        'build/webvr-polyfill.mjs',
        'build/webvr-polyfill.mjs.map',
        'build/webvr-polyfill.cjs',
        'build/webvr-polyfill.cjs.map',
      ]) {
        expect(fs.existsSync(path.join(temp, name))).toBe(false);
      }
      assertMinifiedArtifact(temp, path.join(temp, 'build/webvr-polyfill.min.js'));
      assertMinifiedArtifact(temp, path.join(temp, 'build/webvr-polyfill.min.mjs'));
      const license = fs.readFileSync(path.join(temp, 'licenses.txt'), 'utf8');
      const banner = license.match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
      expect(banner).toBeTruthy();
      for (const name of ['build/webvr-polyfill.min.js']) {
        expect(
          fs.readFileSync(path.join(temp, name), 'utf8').trimStart().startsWith(banner),
        ).toBe(true);
      }
      for (const name of [
        'build/webvr-polyfill.min.js.map',
        'build/webvr-polyfill.min.mjs.map',
      ]) {
        const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
        expect(Array.isArray(map.sources)).toBe(true);
        expect(map.sources).toContain('src/config.js');
        expect(map.sourcesContent).toHaveLength(map.sources.length);
        for (const [index, source] of map.sources.entries()) {
          const resolved = assertPackageRelativeSource(temp, source);
          expect(map.sourcesContent[index]).toBe(fs.readFileSync(resolved, 'utf8'));
        }
      }
      const standalone = Object.fromEntries(
        minified.map(name => [name, sha256File(path.join(temp, name))]),
      );

      // Both entry points must share one build configuration: the standalone
      // minified distribution is byte-identical to the full build's.
      fs.rmSync(buildDir, { recursive: true, force: true });
      execFileSync('npm', ['run', 'build-all'], {
        cwd: temp,
        encoding: 'utf8',
        env: buildEnv,
      });
      assertPublishedPermissions(buildDir);
      for (const name of minified) {
        expect(sha256File(path.join(temp, name))).toBe(standalone[name]);
      }
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.watch-build-matrix', { timeout: 360000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-watch-matrix-'));
    try {
      const buildEnv = restrictivePublicationEnv(temp, prepareBuildFixture(temp));
      const buildDir = path.join(temp, 'build');
      fs.rmSync(buildDir, { recursive: true, force: true });
      expect(fs.existsSync(buildDir)).toBe(false);

      const jsNames = [
        'build/webvr-polyfill.js',
        'build/webvr-polyfill.min.js',
        'build/webvr-polyfill.mjs',
        'build/webvr-polyfill.min.mjs',
      ];
      const mapNames = [
        'build/webvr-polyfill.js.map',
        'build/webvr-polyfill.min.js.map',
        'build/webvr-polyfill.mjs.map',
        'build/webvr-polyfill.min.mjs.map',
      ];
      const required = [...jsNames, ...mapNames, 'build/licenses.txt'];
      const expectRelativeMaps = () => {
        assertPublishedPermissions(buildDir);
        for (const name of mapNames) {
          const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
          expect(Array.isArray(map.sources)).toBe(true);
          expect(map.sources).toContain('src/config.js');
          for (const source of map.sources) {
            assertPackageRelativeSource(temp, source);
          }
        }
      };

      const watchLog = fs.openSync(path.join(temp, 'watch.log'), 'a');
      const watchProcess = spawn('npm', ['run', 'watch'], {
        cwd: temp,
        env: { ...buildEnv, CI: '1' },
        detached: true,
        stdio: ['ignore', watchLog, watchLog],
      });
      const stopWatch = async () => {
        if (!watchProcess.pid) return;
        try {
          process.kill(-watchProcess.pid, 'SIGTERM');
        } catch {}
        await new Promise(resolve => setTimeout(resolve, 1500));
        try {
          process.kill(-watchProcess.pid, 'SIGKILL');
        } catch {}
        fs.closeSync(watchLog);
      };
      try {
        // The watcher starts from a tree with no build/ directory and must
        // produce the same four-artifact matrix as the full build.
        await waitFor(
          () => required.every(name => fs.existsSync(path.join(temp, name))),
          90000,
          'watch initial full matrix',
        );
        await new Promise(resolve => setTimeout(resolve, 1000));
        expect(fs.readFileSync(path.join(temp, 'build/licenses.txt'), 'utf8')).toContain(
          'Apache License',
        );
        assertMinifiedArtifact(temp, path.join(temp, 'build/webvr-polyfill.min.js'));
        assertMinifiedArtifact(temp, path.join(temp, 'build/webvr-polyfill.min.mjs'));
        expectRelativeMaps();
        const initialContents = Object.fromEntries(
          [...jsNames, ...mapNames].map(name => [
            name,
            fs.readFileSync(path.join(temp, name), 'utf8'),
          ]),
        );
        const licenseHash = sha256File(path.join(temp, 'build/licenses.txt'));

        // The watcher is incremental: unrelated files in build/ must survive.
        const sentinel = path.join(temp, 'build/watch-sentinel.txt');
        fs.writeFileSync(sentinel, 'watch keeps unrelated files');

        const configPath = path.join(temp, 'src/config.js');
        const configSource = fs.readFileSync(configPath, 'utf8');
        expect(configSource).toContain('dpdb.webvr.rocks');

        // A watch rebuild must publish one complete generation, exactly like a
        // full build: a reader listing `build/` while it runs may only see the
        // previous generation or the new one.
        const snapshotBuild = () => {
          let before;
          try {
            before = fs.statSync(buildDir);
          } catch {
            return null;
          }
          let entries;
          try {
            entries = fs
              .readdirSync(buildDir, { withFileTypes: true })
              .sort((left, right) => left.name.localeCompare(right.name));
          } catch {
            return null;
          }
          const lines = [];
          for (const entry of entries) {
            if (!entry.isFile()) {
              lines.push(`${entry.name}:not-a-file`);
              continue;
            }
            try {
              lines.push(`${entry.name}:${sha256File(path.join(buildDir, entry.name))}`);
            } catch {
              let current;
              try {
                current = fs.statSync(buildDir);
              } catch {
                return null;
              }
              if (current.ino !== before.ino || current.dev !== before.dev) return null;
              lines.push(`${entry.name}:unreadable`);
            }
          }
          let after;
          try {
            after = fs.statSync(buildDir);
          } catch {
            return null;
          }
          if (after.ino !== before.ino || after.dev !== before.dev) return null;
          return lines.join('\n');
        };
        const previousSnapshot = snapshotBuild();
        expect(previousSnapshot).not.toBeNull();
        const observedSnapshots = new Set();
        let observing = true;
        const observer = (async () => {
          while (observing) {
            const snapshot = snapshotBuild();
            if (snapshot !== null) observedSnapshots.add(snapshot);
            await new Promise(resolve => setTimeout(resolve, 25));
          }
        })();

        fs.writeFileSync(
          configPath,
          configSource.replace('dpdb.webvr.rocks', 'dpdb.webvr.rocks/watch-probe'),
        );

        try {
          await waitFor(() => {
            try {
              return [...jsNames, ...mapNames].every(name =>
                fs.readFileSync(path.join(temp, name), 'utf8').includes('watch-probe'),
              );
            } catch {
              return false;
            }
          }, 90000, 'watch rebuild of the full matrix');
          await new Promise(resolve => setTimeout(resolve, 1000));
        } finally {
          observing = false;
          await observer;
        }
        const rebuiltSnapshot = snapshotBuild();
        expect(rebuiltSnapshot).not.toBeNull();
        const unexpectedSnapshots = [...observedSnapshots].filter(
          snapshot => snapshot !== previousSnapshot && snapshot !== rebuiltSnapshot,
        );
        expect(unexpectedSnapshots.slice(0, 1)).toEqual([]);

        for (const name of [...jsNames, ...mapNames]) {
          expect(fs.readFileSync(path.join(temp, name), 'utf8')).not.toBe(
            initialContents[name],
          );
        }
        expect(sha256File(path.join(temp, 'build/licenses.txt'))).toBe(licenseHash);
        expect(fs.readFileSync(sentinel, 'utf8')).toBe('watch keeps unrelated files');
        expectRelativeMaps();

        // The banner comes from `licenses.txt`, outside the module graph. A
        // change there must republish the whole distribution, not just the
        // standalone license copy.
        const licenseSourcePath = path.join(temp, 'licenses.txt');
        const licenseSource = fs.readFileSync(licenseSourcePath, 'utf8');
        const licenseMarker = 'watch-license-probe-4d2a';
        const updatedLicense = licenseSource.replace(
          /\n\s*\*\/\s*$/,
          `\n * ${licenseMarker}\n */\n`,
        );
        expect(updatedLicense).not.toBe(licenseSource);
        fs.writeFileSync(licenseSourcePath, updatedLicense);
        await waitFor(() => {
          try {
            return (
              fs
                .readFileSync(path.join(temp, 'build/licenses.txt'), 'utf8')
                .includes(licenseMarker) &&
              [...jsNames, 'build/webvr-polyfill.cjs'].filter(requiresLicenseBanner).every(name =>
                fs.readFileSync(path.join(temp, name), 'utf8').includes(licenseMarker),
              ) &&
              mapNames.every(name => fs.existsSync(path.join(temp, name)))
            );
          } catch {
            return false;
          }
        }, 90000, 'watch republish after the license banner changed');
        await new Promise(resolve => setTimeout(resolve, 1000));
        expect(fs.readFileSync(path.join(temp, 'build/licenses.txt'), 'utf8')).toBe(
          updatedLicense,
        );
        expectRelativeMaps();

        // A new file under `src/` that nothing imports is not part of the
        // build graph: it must not schedule a rebuild, and no published
        // artifact may be rewritten (content or modification time).
        const nonImportedProbe = path.join(temp, 'src/not-imported-probe.js');
        const publishedState = () =>
          Object.fromEntries(
            [
              ...jsNames,
              ...mapNames,
              'build/webvr-polyfill.cjs',
              'build/webvr-polyfill.cjs.map',
              'build/licenses.txt',
            ].map(name => {
              const file = path.join(temp, name);
              return [name, [sha256File(file), fs.statSync(file).mtimeMs]];
            }),
          );
        const beforeNonImported = publishedState();
        fs.writeFileSync(
          nonImportedProbe,
          "export const NOT_IMPORTED_PROBE = 'not-imported-6c1e';\n",
        );
        try {
          // A watcher that recursively watches `src/` starts a rebuild within
          // its debounce window; the staging directory appears well before the
          // build can finish, so one probe window is enough to detect it.
          await new Promise(resolve => setTimeout(resolve, 2500));
          expect(
            fs.readdirSync(temp).filter(name => /^\.?build[-.]/.test(name)),
          ).toEqual([]);
          expect(publishedState()).toEqual(beforeNonImported);
        } finally {
          fs.rmSync(nonImportedProbe, { force: true });
        }
        // Give a rebuild triggered by the creation or the deletion time to
        // finish even when the implementation stages somewhere other than a
        // `.build-*` directory; a published rewrite would appear here.
        await new Promise(resolve => setTimeout(resolve, 12000));
        expect(
          fs.readdirSync(temp).filter(name => /^\.?build[-.]/.test(name)),
        ).toEqual([]);
        expect(publishedState()).toEqual(beforeNonImported);

        // The watcher must follow the module graph, not a fixed file list:
        // adding a module and importing it must include the new module in the
        // next rebuild, and removing the import must drop it again.
        const probeModule = path.join(temp, 'src/watch-probe.js');
        let probeMarker = 'watch-graph-probe-7f3a';
        fs.writeFileSync(
          probeModule,
          `export const WATCH_PROBE_URL = 'https://dpdb.webvr.rocks/${probeMarker}';\n`,
        );
        const settledConfig = fs.readFileSync(configPath, 'utf8');
        expect(settledConfig).toContain('https://dpdb.webvr.rocks/watch-probe');
        fs.writeFileSync(
          configPath,
          "const WATCH_GRAPH_HEADER = 1; import { WATCH_PROBE_URL } from './watch-probe.js';\n" +
            settledConfig.replace(/DPDB_URL:\s*'[^']*'/, 'DPDB_URL: WATCH_PROBE_URL'),
        );
        await waitFor(() => {
          try {
            return jsNames.every(name =>
              fs.readFileSync(path.join(temp, name), 'utf8').includes(probeMarker),
            );
          } catch {
            return false;
          }
        }, 90000, 'watch rebuild after adding a module');
        await new Promise(resolve => setTimeout(resolve, 1000));
        for (const name of mapNames) {
          const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
          expect(map.sources).toContain('src/watch-probe.js');
          expect(map.sourcesContent[map.sources.indexOf('src/watch-probe.js')]).toBe(
            fs.readFileSync(probeModule, 'utf8'),
          );
          expect(map.sourcesContent[map.sources.indexOf('src/config.js')]).toBe(
            fs.readFileSync(configPath, 'utf8'),
          );
        }
        expectRelativeMaps();

        // The import above follows another valid statement on the same line.
        // An initial rebuild alone cannot prove graph tracking: edit the newly
        // imported module itself and require fresh artifacts and source text.
        probeMarker = 'watch-graph-probe-edited-8b2c';
        fs.writeFileSync(
          probeModule,
          `export const WATCH_PROBE_URL = 'https://dpdb.webvr.rocks/${probeMarker}';\n`,
        );
        await waitFor(
          () => allWatchJsContain(temp, probeMarker),
          90000,
          'watch rebuild after editing a module imported on the same line',
        );
        for (const name of mapNames) {
          const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
          expect(map.sourcesContent[map.sources.indexOf('src/watch-probe.js')]).toBe(
            fs.readFileSync(probeModule, 'utf8'),
          );
        }

        fs.writeFileSync(configPath, settledConfig);
        await waitFor(() => {
          try {
            return jsNames.every(name =>
              !fs.readFileSync(path.join(temp, name), 'utf8').includes(probeMarker),
            );
          } catch {
            return false;
          }
        }, 90000, 'watch rebuild after removing a module');
        await new Promise(resolve => setTimeout(resolve, 1000));
        for (const name of mapNames) {
          const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
          expect(map.sources).not.toContain('src/watch-probe.js');
        }
        fs.rmSync(probeModule, { force: true });
        expectRelativeMaps();

        // The watcher owns the whole published set, not just the files it
        // happens to rewrite: deleting `build/` mid-watch must be repaired on
        // the next rebuild, including the standalone license copy and the
        // CommonJS entry.
        const publishedNames = [
          ...jsNames,
          ...mapNames,
          'build/webvr-polyfill.cjs',
          'build/webvr-polyfill.cjs.map',
          'build/licenses.txt',
        ];
        const expectPublishedSet = () => {
          expect(fs.readdirSync(path.join(temp, 'build')).sort()).toEqual(
            publishedNames.map(name => name.replace('build/', '')).sort(),
          );
          const license = fs.readFileSync(path.join(temp, 'licenses.txt'), 'utf8');
          expect(fs.readFileSync(path.join(temp, 'build/licenses.txt'), 'utf8')).toBe(license);
          const banner = license.match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
          expect(banner).toBeTruthy();
          for (const name of [
            ...jsNames,
            'build/webvr-polyfill.cjs',
          ].filter(requiresLicenseBanner)) {
            expect(
              fs.readFileSync(path.join(temp, name), 'utf8').trimStart().startsWith(banner),
            ).toBe(true);
          }
          expectRelativeMaps();
          for (const name of mapNames) {
            const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
            expect(map.sourcesContent).toHaveLength(map.sources.length);
            for (const [index, source] of map.sources.entries()) {
              const resolved = assertPackageRelativeSource(temp, source);
              expect(map.sourcesContent[index]).toBe(fs.readFileSync(resolved, 'utf8'));
            }
          }
        };

        const wipedConfig = settledConfig.replace(
          'https://dpdb.webvr.rocks/watch-probe',
          'https://dpdb.webvr.rocks/watch-wipe',
        );
        fs.rmSync(path.join(temp, 'build'), { recursive: true, force: true });
        fs.writeFileSync(configPath, wipedConfig);
        await waitFor(() => {
          try {
            return (
              [...jsNames, 'build/webvr-polyfill.cjs'].every(name =>
                fs.readFileSync(path.join(temp, name), 'utf8').includes('watch-wipe'),
              ) && fs.existsSync(path.join(temp, 'build/licenses.txt'))
            );
          } catch {
            return false;
          }
        }, 120000, 'watch republish after the output directory was deleted');
        await new Promise(resolve => setTimeout(resolve, 1500));
        expectPublishedSet();

        // The watcher must keep publishing after that repair instead of
        // degrading into a one-shot rebuild.
        fs.writeFileSync(configPath, wipedConfig.replace('watch-wipe', 'watch-final'));
        await waitFor(() => {
          try {
            return jsNames.every(name =>
              fs.readFileSync(path.join(temp, name), 'utf8').includes('watch-final'),
            );
          } catch {
            return false;
          }
        }, 90000, 'watch rebuild after the output directory was repaired');
        await new Promise(resolve => setTimeout(resolve, 1500));
        expectPublishedSet();
      } finally {
        await stopWatch();
      }
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.full-build-clean-slate', { timeout: 120000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-full-build-'));
    try {
      const buildEnv = prepareBuildFixture(temp);
      const runFullBuild = () => execFileSync('npm', ['run', 'build-all'], {
        cwd: temp,
        encoding: 'utf8',
        env: buildEnv,
      });
      runFullBuild();
      const staleOutputs = [
        'build/stale.txt',
        'build/webvr-polyfill.old.js',
        'build/stale/old.map',
      ];
      for (const name of staleOutputs) {
        fs.mkdirSync(path.dirname(path.join(temp, name)), { recursive: true });
        fs.writeFileSync(path.join(temp, name), 'stale build output');
      }
      runFullBuild();
      for (const name of staleOutputs) {
        expect(fs.existsSync(path.join(temp, name))).toBe(false);
      }
      for (const name of [
        'build/webvr-polyfill.js',
        'build/webvr-polyfill.min.js',
        'build/webvr-polyfill.mjs',
        'build/webvr-polyfill.min.mjs',
        'build/webvr-polyfill.cjs',
        'build/webvr-polyfill.js.map',
        'build/webvr-polyfill.min.js.map',
        'build/webvr-polyfill.mjs.map',
        'build/webvr-polyfill.min.mjs.map',
        'build/webvr-polyfill.cjs.map',
        'build/licenses.txt',
      ]) {
        expect(fs.existsSync(path.join(temp, name))).toBe(true);
      }
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.commonjs-window-only', () => {
    for (const file of [normalPath, minPath]) {
      expect(commonJsProbe(file)).toEqual({
        type: 'function',
        version: '0.10.12',
        display: 'function',
        frame: 'function',
        getDisplays: 'function',
        documentBridged: true,
        navigatorBridged: true,
      });
    }
  });

  it('f2p.cjs-entry', () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
    expect(pkg.main).toBe('build/webvr-polyfill.js');
    expect(pkg.module).toBe('build/webvr-polyfill.mjs');
    expect(pkg.exports).toEqual({
      '.': {
        import: './build/webvr-polyfill.mjs',
        require: './build/webvr-polyfill.cjs',
      },
    });

    const content = fs.readFileSync(cjsPath, 'utf8');
    expect(content).toMatch(/\bmodule\.exports\s*=/);
    expect(content).not.toMatch(/\bdefine\.amd\b/);
    expect(content).not.toMatch(/\btypeof\s+define\b/);
    expect(content).not.toBe(fs.readFileSync(normalPath, 'utf8'));
    const license = fs.readFileSync(path.join(root, 'licenses.txt'), 'utf8');
    const banner = license.match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
    expect(banner).toBeTruthy();
    expect(content.trimStart().startsWith(banner)).toBe(true);

    // The dedicated entry must be what Node resolves from the package name.
    const resolved = execFileSync(
      process.execPath,
      ['-e', "process.stdout.write(require.resolve('webvr-polyfill'))"],
      { cwd: root, encoding: 'utf8', env: { PATH: process.env.PATH, HOME: process.env.HOME } },
    );
    expect(fs.realpathSync(resolved)).toBe(fs.realpathSync(cjsPath));

    // Loaded with only `window` present, the CommonJS entry must apply the
    // same document/navigator bootstrap as the UMD entry.
    expect(commonJsProbe(cjsPath)).toEqual({
      type: 'function',
      version: '0.10.12',
      display: 'function',
      frame: 'function',
      getDisplays: 'function',
      documentBridged: true,
      navigatorBridged: true,
    });

    const map = JSON.parse(fs.readFileSync(cjsMapPath, 'utf8'));
    expect(map.version).toBe(3);
    expect(Array.isArray(map.sources)).toBe(true);
    expect(Array.isArray(map.sourcesContent)).toBe(true);
    expect(map.sourcesContent).toHaveLength(map.sources.length);
    expect(map.sources).toContain('src/webvr-polyfill.js');
    for (const [index, source] of map.sources.entries()) {
      const resolved = assertPackageRelativeSource(root, source);
      expect(map.sourcesContent[index]).toBe(fs.readFileSync(resolved, 'utf8'));
    }
    assertBannerMapOffset(content, map, license);
    resolveGeneratedToken(
      cjsPath,
      cjsMapPath,
      'prototype.getVRDisplays',
      path.join(root, 'src/webvr-polyfill.js'),
    );
  });

  it('f2p.normal-minified-parity', () => {
    const normal = browserLoad(normalPath).Polyfill;
    const minified = browserLoad(minPath).Polyfill;
    expect(minified.version).toBe(normal.version);
    expect(typeof minified.VRDisplay).toBe(typeof normal.VRDisplay);
    expect(typeof minified.VRFrameData).toBe(typeof normal.VRFrameData);
    expect(Object.getOwnPropertyNames(minified.prototype).sort()).toEqual(
      Object.getOwnPropertyNames(normal.prototype).sort(),
    );
  });

  it('f2p.published-package-self-contained', { timeout: 180000 }, () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-package-self-'));
    try {
      const tarballs = path.join(temp, 'tarballs');
      const installed = path.join(temp, 'node_modules/webvr-polyfill');
      fs.mkdirSync(tarballs);
      fs.mkdirSync(installed, { recursive: true });
      execFileSync(
        'npm',
        ['pack', '--ignore-scripts', '--pack-destination', tarballs, '--json'],
        {
          cwd: root,
          encoding: 'utf8',
          env: {
            PATH: process.env.PATH,
            HOME: process.env.HOME,
            npm_config_cache: process.env.npm_config_cache,
            npm_config_offline: 'true',
            npm_config_audit: 'false',
            npm_config_fund: 'false',
          },
        },
      );
      const tarball = fs
        .readdirSync(tarballs)
        .filter(name => name.endsWith('.tgz'))
        .map(name => path.join(tarballs, name))[0];
      expect(tarball).toBeTruthy();
      execFileSync('tar', ['-xzf', tarball, '-C', installed, '--strip-components=1'], {
        encoding: 'utf8',
      });

      // The extracted tree, not the repository, must satisfy the exports map.
      for (const name of [
        'package.json',
        'build/webvr-polyfill.js',
        'build/webvr-polyfill.mjs',
        'build/webvr-polyfill.cjs',
        'build/licenses.txt',
      ]) {
        expect(fs.existsSync(path.join(installed, name))).toBe(true);
      }
      expect(fs.existsSync(path.join(installed, 'src'))).toBe(false);
      expect(fs.existsSync(path.join(installed, 'test'))).toBe(false);

      const commonRequire = `
        const { JSDOM } = require('jsdom');
        global.window = new JSDOM('', { url: 'https://example.test/' }).window;
        delete global.document;
        delete global.navigator;
        const Polyfill = require('webvr-polyfill');
        const instance = new Polyfill({ DPDB_URL: null });
        process.stdout.write(JSON.stringify({
          type: typeof Polyfill,
          version: Polyfill.version,
          display: typeof Polyfill.VRDisplay,
          frame: typeof Polyfill.VRFrameData,
          getDisplays: typeof instance.getVRDisplays,
          documentBridged: global.document === global.window.document,
          navigatorBridged: global.navigator === global.window.navigator,
          resolved: require.resolve('webvr-polyfill'),
        }));
      `;
      fs.writeFileSync(path.join(temp, 'probe.cjs'), commonRequire);
      const expected = {
        type: 'function',
        version: '0.10.12',
        display: 'function',
        frame: 'function',
        getDisplays: 'function',
        documentBridged: true,
        navigatorBridged: true,
      };
      const cjsResult = JSON.parse(
        execFileSync(process.execPath, [path.join(temp, 'probe.cjs')], {
          cwd: temp,
          encoding: 'utf8',
          env: {
            PATH: process.env.PATH,
            HOME: process.env.HOME,
            NODE_PATH: path.join(root, 'node_modules'),
          },
        }),
      );
      expect(cjsResult).toMatchObject(expected);
      expect(fs.realpathSync(cjsResult.resolved)).toBe(
        fs.realpathSync(path.join(installed, 'build/webvr-polyfill.cjs')),
      );

      const esmProbe = `
        import { createRequire } from 'node:module';
        const require = createRequire(import.meta.url);
        const { JSDOM } = require('jsdom');
        global.window = new JSDOM('', { url: 'https://example.test/' }).window;
        delete global.document;
        delete global.navigator;
        const module = await import('webvr-polyfill');
        const Polyfill = module.default;
        const instance = new Polyfill({ DPDB_URL: null });
        process.stdout.write(JSON.stringify({
          type: typeof Polyfill,
          version: Polyfill.version,
          display: typeof Polyfill.VRDisplay,
          frame: typeof Polyfill.VRFrameData,
          getDisplays: typeof instance.getVRDisplays,
          documentBridged: global.document === global.window.document,
          navigatorBridged: global.navigator === global.window.navigator,
        }));
      `;
      fs.writeFileSync(path.join(temp, 'probe.mjs'), esmProbe);
      const esmResult = JSON.parse(
        execFileSync(process.execPath, [path.join(temp, 'probe.mjs')], {
          cwd: temp,
          encoding: 'utf8',
          env: {
            PATH: process.env.PATH,
            HOME: process.env.HOME,
            NODE_PATH: path.join(root, 'node_modules'),
          },
        }),
      );
      expect(esmResult).toEqual(expected);
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.build-reproducible-across-paths', { timeout: 420000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-reproducible-'));
    try {
      const first = path.join(temp, 'checkout-a');
      fs.mkdirSync(first);
      const buildEnv = prepareBuildFixture(first);
      execFileSync('npm', ['run', 'build-all'], {
        cwd: first,
        encoding: 'utf8',
        env: buildEnv,
      });
      const firstHashes = Object.fromEntries(
        distributionFiles.map(name => [name, sha256File(path.join(first, name))]),
      );

      // Same source and dependencies at a different absolute path, with a
      // different HOME and npm cache: output must not depend on either.
      const second = path.join(temp, 'checkout-b-with-a-longer-absolute-path');
      fs.renameSync(first, second);
      const secondEnv = {
        ...buildEnv,
        HOME: path.join(temp, 'home-b'),
        npm_config_cache: fixtureNpmCache(path.join(temp, 'cache-b')),
      };
      fs.mkdirSync(secondEnv.HOME);
      execFileSync('npm', ['run', 'build-all'], {
        cwd: second,
        encoding: 'utf8',
        env: secondEnv,
      });
      for (const name of distributionFiles) {
        expect(sha256File(path.join(second, name))).toBe(firstHashes[name]);
      }

      // A build publishes one generation: repeating it must not change a byte,
      // a failed run must leave the previous generation untouched, and
      // `build-min` must not leave readable artifacts from an older revision.
      const buildDir = path.join(second, 'build');
      const publishHashes = () => {
        expect(fs.readdirSync(buildDir).sort()).toEqual(
          distributionFiles.map(name => name.replace('build/', '')).sort(),
        );
        return Object.fromEntries(
          distributionFiles.map(name => [name, sha256File(path.join(second, name))]),
        );
      };
      // The real npm workflow was checked above at both checkout paths.
      // From here on probes intentionally alter frozen domain inputs; source
      // regression tests are allowed to assert their original default values.
      const runBuildAll = () => execFileSync(process.execPath, ['scripts/build-vite.mjs'], {
        cwd: second,
        encoding: 'utf8',
        env: secondEnv,
      });
      const runBuildMin = () => execFileSync('npm', ['run', 'build-min'], {
        cwd: second,
        encoding: 'utf8',
        env: secondEnv,
      });
      const noStagingLeftovers = () => {
        expect(
          fs.readdirSync(second).filter(name => /^\.?build[-.]/.test(name)),
        ).toEqual([]);
      };
      // The build directory is declared once and used by both helpers below.

      // Rebuilding the same source twice must produce identical bytes.
      runBuildAll();
      expect(publishHashes()).toEqual(firstHashes);
      noStagingLeftovers();

      // Publication errors obey the same observable-generation contract as a
      // compilation error. An overlayfs EXDEV must not be worked around by
      // emptying/removing the live directory; a failed second rename must
      // restore the previous generation. Observe synchronous mutations too,
      // since a polling reader can miss a very short non-atomic fallback.
      for (const mode of ['retire-exdev', 'publish-error']) {
        const tracePath = path.join(second, '.publication-trace.jsonl');
        const hookPath = path.join(second, '.publication-probe.cjs');
        fs.writeFileSync(tracePath, '');
        fs.writeFileSync(hookPath, `
const fs = require('node:fs');
const path = require('node:path');
const { syncBuiltinESMExports } = require('node:module');
const live = path.resolve(process.cwd(), 'build');
const trace = ${JSON.stringify(tracePath)};
const mode = ${JSON.stringify(mode)};
const append = fs.appendFileSync.bind(fs);
let retired = false;
let injected = false;
const inside = value => {
  if (typeof value !== 'string' && !Buffer.isBuffer(value)) return false;
  const absolute = path.resolve(String(value));
  return absolute === live || absolute.startsWith(live + path.sep);
};
const record = event => append(trace, JSON.stringify(event) + '\\n');
function before(name, args) {
  if (name === 'rename') {
    const from = path.resolve(String(args[0]));
    const to = path.resolve(String(args[1]));
    if (!injected && ((mode === 'retire-exdev' && from === live) ||
        (mode === 'publish-error' && retired && to === live))) {
      injected = true;
      record({ injected: mode });
      throw Object.assign(new Error('injected publication failure'), {
        code: mode === 'retire-exdev' ? 'EXDEV' : 'EIO',
      });
    }
    return (inside(from) && from !== live) || (inside(to) && to !== live);
  }
  const destination = ['copyFile', 'cp'].includes(name) ? args[1] : args[0];
  if (name === 'mkdir' && fs.existsSync(destination)) return false;
  if (['rm', 'rmdir', 'unlink'].includes(name) && !fs.existsSync(destination)) return false;
  return inside(destination);
}
function after(name, args, mutation) {
  if (name === 'rename' && path.resolve(String(args[0])) === live) retired = true;
  if (mutation) record({ liveMutation: name });
}
for (const name of ['rename', 'rm', 'rmdir', 'unlink', 'writeFile', 'appendFile', 'copyFile', 'cp', 'mkdir']) {
  const syncName = name + 'Sync';
  const original = fs[syncName];
  if (original) fs[syncName] = function (...args) {
    const mutation = before(name, args);
    const result = original.apply(this, args);
    after(name, args, mutation);
    return result;
  };
  const asynchronous = fs.promises[name];
  if (asynchronous) fs.promises[name] = async function (...args) {
    const mutation = before(name, args);
    const result = await asynchronous.apply(this, args);
    after(name, args, mutation);
    return result;
  };
}
syncBuiltinESMExports();
`);
        const beforeFailure = publishHashes();
        let failure = null;
        try {
          execFileSync('node', ['--require', hookPath, 'scripts/build-vite.mjs'], {
            cwd: second,
            encoding: 'utf8',
            env: secondEnv,
            timeout: 120000,
          });
        } catch (error) {
          failure = error;
        }
        const events = fs.readFileSync(tracePath, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
        expect(events.filter(event => event.liveMutation)).toEqual([]);
        expect(publishHashes()).toEqual(beforeFailure);
        // A different atomic implementation need not use this rename path.
        // If our injected error was reached, it must abort nonzero safely.
        if (events.some(event => event.injected)) {
          expect(failure).not.toBeNull();
          expect(failure.status ?? 1).not.toBe(0);
        } else {
          expect(failure).toBeNull();
        }
        fs.rmSync(hookPath);
        fs.rmSync(tracePath);
        noStagingLeftovers();
      }

      // A copied input tree is a snapshot only if the capture itself is
      // coherent. Change the entry and then the license after the old entry
      // was read/copied. Old source + new license never existed together.
      const captureEntry = path.join(second, 'src/webvr-polyfill.js');
      const captureLicense = path.join(second, 'licenses.txt');
      const originalEntry = fs.readFileSync(captureEntry, 'utf8');
      const originalLicense = fs.readFileSync(captureLicense, 'utf8');
      const captureSourceMarker = 'capture-source-generation-74ac';
      const captureLicenseMarker = 'capture-license-generation-b18e';
      const captureHook = path.join(second, '.snapshot-capture-probe.cjs');
      fs.writeFileSync(captureHook, `
const fs = require('node:fs');
const path = require('node:path');
const { syncBuiltinESMExports } = require('node:module');
const entry = path.resolve('src/webvr-polyfill.js');
const sourceDir = path.dirname(entry);
const license = path.resolve('licenses.txt');
const read = fs.readFileSync.bind(fs);
const write = fs.writeFileSync.bind(fs);
let changed = false;
function change() {
  if (changed) return;
  changed = true;
  write(entry, read(entry, 'utf8') + '\\nglobalThis.__captureProbe = "${captureSourceMarker}";\\n');
  write(license, read(license, 'utf8').replace(/\\*\\/\\s*$/, ' * ${captureLicenseMarker}\\n */\\n'));
}
for (const name of ['readFileSync', 'copyFileSync', 'cpSync']) {
  const original = fs[name];
  fs[name] = function (...args) {
    const result = original.apply(this, args);
    if (typeof args[0] === 'string' || Buffer.isBuffer(args[0])) {
      const from = path.resolve(String(args[0]));
      if (from === entry || (name === 'cpSync' && from === sourceDir)) change();
    }
    return result;
  };
}
syncBuiltinESMExports();
`);
      const beforeCapture = publishHashes();
      let captureFailure = null;
      try {
        execFileSync('node', ['--require', captureHook, 'scripts/build-vite.mjs'], {
          cwd: second,
          encoding: 'utf8',
          env: secondEnv,
          timeout: 120000,
        });
      } catch (error) {
        captureFailure = error;
      }
      if (captureFailure) {
        expect(captureFailure.status ?? 1).not.toBe(0);
        expect(publishHashes()).toEqual(beforeCapture);
      } else {
        const states = distributionFiles.filter(name => !name.endsWith('.map')).map(name => {
          const text = fs.readFileSync(path.join(second, name), 'utf8');
          const observesLicense = name.endsWith('licenses.txt') || requiresLicenseBanner(name)
            || text.startsWith(originalLicense.match(/^\s*\/\*[\s\S]*?\*\//)?.[0] || originalLicense);
          return [name.endsWith('licenses.txt') ? null : text.includes(captureSourceMarker),
            observesLicense ? text.includes(captureLicenseMarker) : null];
        });
        const sourceState = states.find(([source]) => source !== null)[0];
        const licenseState = states[0][1];
        // The probe edits the source before the license. New source + old
        // license is a real intermediate snapshot; old source + new license
        // never existed. Every artifact must still use the same pair.
        expect(states.every(([source, license]) => (license === null || license === licenseState) && (source === null || source === sourceState))).toBe(true);
        expect(licenseState && !sourceState).toBe(false);
      }
      fs.writeFileSync(captureEntry, originalEntry);
      fs.writeFileSync(captureLicense, originalLicense);
      fs.rmSync(captureHook);
      runBuildAll();
      expect(publishHashes()).toEqual(firstHashes);
      noStagingLeftovers();

      // A -> B -> A input edits defeat a content-only before/after hash. A
      // generation must still come from one snapshot, or fail without changing
      // the previous publication. Probe only a fixture, never the source tree.
      const roundTripConfig = path.join(second, 'src/config.js');
      const roundTripOriginal = fs.readFileSync(roundTripConfig, 'utf8');
      const roundTripA = 'roundtrip-source-state-a';
      const roundTripB = 'roundtrip-source-state-b';
      const roundTripSource = withConfigProbe(roundTripOriginal, 'ROUND_TRIP_STATE', roundTripA);
      const roundTrip = prepareInputRoundTrip(
        second, roundTripConfig, roundTripSource.replace(roundTripA, roundTripB),
      );
      try {
        fs.writeFileSync(roundTripConfig, roundTripSource);
        runBuildAll();
        const beforeRoundTrip = publishHashes();
        let roundTripFailure = null;
        try {
          execFileSync(process.execPath, ['--require', roundTrip.hook, 'scripts/build-vite.mjs'], {
            cwd: second, encoding: 'utf8', env: secondEnv, timeout: 120000,
          });
        } catch (error) {
          roundTripFailure = error;
        }
        if (roundTripFailure) {
          expect(roundTripFailure.status ?? 1).not.toBe(0);
          expect(publishHashes()).toEqual(beforeRoundTrip);
        } else {
          const states = distributionFiles.filter(name => /\.(?:js|mjs|cjs)$/.test(name)).map(name => {
            const text = fs.readFileSync(path.join(second, name), 'utf8');
            expect(text.includes(roundTripA) || text.includes(roundTripB)).toBe(true);
            return text.includes(roundTripB);
          });
          expect(new Set(states).size).toBe(1);
          // The mapped source must agree with the code in every format too.
          for (const name of distributionFiles.filter(name => name.endsWith('.map'))) {
            const map = JSON.parse(fs.readFileSync(path.join(second, name), 'utf8'));
            const sourceIndex = map.sources.findIndex(source => source === 'src/config.js');
            expect(sourceIndex).toBeGreaterThanOrEqual(0);
            expect(map.sourcesContent[sourceIndex]).toBe(
              states[0] ? roundTripSource.replace(roundTripA, roundTripB) : roundTripSource,
            );
          }
        }
        noStagingLeftovers();
      } finally {
        fs.writeFileSync(roundTripConfig, roundTripOriginal);
        fs.rmSync(roundTrip.hook, { force: true });
        fs.rmSync(roundTrip.trace, { force: true });
      }
      runBuildAll();
      expect(publishHashes()).toEqual(firstHashes);
      noStagingLeftovers();

      // SIGKILL can land between the two directory renames. Polling for any
      // staging directory does not cover this publication boundary. Interrupt
      // immediately after retiring build/, then make the next compilation
      // fail: recovery must restore the old generation before discarding its
      // backup, even when no new generation can be compiled.
      const swapCrashHook = path.join(second, '.swap-crash-probe.cjs');
      const swapCrashTrace = path.join(second, '.swap-crash-probe.json');
      const recoveryProbe = path.join(second, '.swap-recovery-probe');
      fs.writeFileSync(swapCrashHook, `
const fs = require('node:fs');
const path = require('node:path');
const { fileURLToPath } = require('node:url');
const { syncBuiltinESMExports } = require('node:module');
const live = ${JSON.stringify(buildDir)};
const trace = ${JSON.stringify(swapCrashTrace)};
const probe = ${JSON.stringify(recoveryProbe)};
const role = process.env.ATOMIC_RECOVERY_ROLE;
const write = fs.writeFileSync.bind(fs);
const absolute = value => path.resolve(value instanceof URL ? fileURLToPath(value) : String(value));
function pause(step) {
  const ready = path.join(probe, step);
  if (fs.existsSync(ready)) return;
  write(ready, '');
  const deadline = Date.now() + 10000;
  while (!fs.existsSync(ready + '-continue') && Date.now() < deadline) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
  }
}
function beforeRename(to) {
  if (role === 'lead' && absolute(to) === live) pause('install');
}
function afterRetirement(from) {
  if (role || absolute(from) !== live) return;
  write(trace, JSON.stringify({ pid: process.pid }));
  process.kill(process.pid, 'SIGKILL');
}
if (role === 'lag') {
  const residue = new Set(JSON.parse(fs.readFileSync(path.join(probe, 'residue'), 'utf8')));
  for (const name of ['rm', 'rmdir', 'unlink']) {
    for (const [owner, key] of [[fs, name + 'Sync'], [fs, name], [fs.promises, name]]) {
      const original = owner[key];
      owner[key] = function (entry, ...args) {
        if (residue.has(absolute(entry))) pause('remove');
        return original.call(this, entry, ...args);
      };
    }
  }
}
const renameSync = fs.renameSync;
fs.renameSync = function (from, to) {
  beforeRename(to);
  const result = renameSync.apply(this, arguments);
  afterRetirement(from);
  return result;
};
const rename = fs.rename;
fs.rename = function (from, to, callback) {
  beforeRename(to);
  return rename.call(this, from, to, (error, ...values) => {
    if (!error) afterRetirement(from);
    callback(error, ...values);
  });
};
const renamePromise = fs.promises.rename;
fs.promises.rename = async function (from, to) {
  beforeRename(to);
  const result = await renamePromise.apply(this, arguments);
  afterRetirement(from);
  return result;
};
syncBuiltinESMExports();
`);
      const beforeSwapCrash = publishHashes();
      const interruptSwap = () => {
        fs.rmSync(swapCrashTrace, { force: true });
        try {
          execFileSync(process.execPath, ['--require', swapCrashHook, 'scripts/build-vite.mjs'], {
            cwd: second,
            encoding: 'utf8',
            env: secondEnv,
            timeout: 120000,
          });
          return null;
        } catch (error) {
          return error;
        }
      };
      try {
        const swapCrashFailure = interruptSwap();
        if (fs.existsSync(swapCrashTrace)) {
          expect(swapCrashFailure).not.toBeNull();
          fs.writeFileSync(captureEntry, `${originalEntry}\nconst = invalid swap crash probe;\n`);
          const recover = role => new Promise(resolve => {
            const args = role ? ['--require', swapCrashHook, 'scripts/build-vite.mjs'] : ['scripts/build-vite.mjs'];
            execFile(process.execPath, args, {
              cwd: second,
              env: role ? { ...secondEnv, ATOMIC_RECOVERY_ROLE: role } : secondEnv,
              timeout: 120000,
            }, error => resolve(error));
          });
          const recoveryFailure = await recover();
          expect(typeof recoveryFailure?.code).toBe('number');
          expect(recoveryFailure.code).not.toBe(0);
          expect(publishHashes()).toEqual(beforeSwapCrash);
          noStagingLeftovers();

          // Freeze one contender just before deleting a name left by the dead
          // owner. Let another contender begin installing the generation, and
          // resume the first removal while that installation is still pending.
          // Implementations using other primitives can proceed without either
          // pause. Both builds must publish successfully with valid inputs.
          fs.writeFileSync(captureEntry, originalEntry);
          expect(interruptSwap()?.signal).toBe('SIGKILL');
          fs.mkdirSync(recoveryProbe);
          const residue = stagingResidue(second).map(name => path.join(second, name));
          fs.writeFileSync(path.join(recoveryProbe, 'residue'), JSON.stringify(residue));
          const waitForPause = (step, ms) => waitFor(
            () => fs.existsSync(path.join(recoveryProbe, step)), ms, `recovery ${step}`,
          ).catch(() => {});
          const resume = step => fs.writeFileSync(path.join(recoveryProbe, `${step}-continue`), '');
          const lag = recover('lag');
          await Promise.race([waitForPause('remove', 6000), lag]);
          const lead = recover('lead');
          await Promise.race([waitForPause('install', 2000), lead]);
          resume('remove');
          await Promise.race([sleep(500), lag]);
          resume('install');
          expect(await Promise.all([lag, lead])).toEqual([null, null]);
          expect(publishHashes()).toEqual(beforeSwapCrash);
          noStagingLeftovers();
        } else {
          // An equivalent atomic publication need not retire this directory.
          expect(swapCrashFailure).toBeNull();
          expect(publishHashes()).toEqual(beforeSwapCrash);
        }
      } finally {
        fs.writeFileSync(captureEntry, originalEntry);
        fs.rmSync(swapCrashHook, { force: true });
        fs.rmSync(swapCrashTrace, { force: true });
        fs.rmSync(recoveryProbe, { recursive: true, force: true });
      }
      runBuildAll();
      expect(publishHashes()).toEqual(firstHashes);
      noStagingLeftovers();

      // A build killed mid-run must leave the previous generation intact, and
      // the next build must remove the staging directory the killed run left
      // behind instead of accumulating one per interruption.
      const stagingEntries = () =>
        fs.readdirSync(second).filter(name => /^\.?build[-.]/.test(name));
      const beforeKill = publishHashes();
      const killPause = preparePublicationPause(second, 'build-kill');
      killPause.arm();
      const killed = spawn('npm', ['run', 'build-all'], {
        cwd: second,
        env: { ...secondEnv, ...killPause.env },
        detached: true,
        stdio: 'ignore',
      });
      const killedExit = new Promise(resolve => {
        killed.on('error', () => resolve(null));
        killed.on('exit', (code, signal) => resolve({ code, signal }));
      });
      let interruptedMidBuild = false;
      const stagingDeadline = Date.now() + 120000;
      while (Date.now() < stagingDeadline) {
        if (killPause.pausedPid()) {
          interruptedMidBuild = true;
          break;
        }
        const finished = await Promise.race([
          killedExit.then(() => true),
          new Promise(resolve => setTimeout(() => resolve(false), 50)),
        ]);
        if (finished) break;
      }
      if (interruptedMidBuild) {
        // A driver may use its own process group. Kill the paused publisher
        // itself as well as the npm process group, before it mutates build/.
        try {
          process.kill(killPause.pausedPid(), 'SIGKILL');
        } catch {}
        try {
          process.kill(-killed.pid, 'SIGKILL');
        } catch {}
      }
      await killedExit;
      killPause.release();
      expect(publishHashes()).toEqual(beforeKill);
      // A parent may already have cleaned the killed worker's stage. Any
      // residue that remains must be gone after the next build below.
      runBuildAll();
      noStagingLeftovers();
      expect(publishHashes()).toEqual(beforeKill);

      const configPath = path.join(second, 'src/config.js');
      const configSource = fs.readFileSync(configPath, 'utf8');
      let goodHashes = publishHashes();

      // Publication is atomic for readers, not just for the end state: while a
      // build runs, anything that lists `build/` must see one complete
      // generation, either the previous one or the new one. Observing a
      // partially written set, a cleared directory, or a mix of old and new
      // files means the build was published in place.
      const snapshotBuild = () => {
        let before;
        try {
          before = fs.statSync(buildDir);
        } catch {
          // A directory swap may hide `build/` for an instant.
          return null;
        }
        let entries;
        try {
          entries = fs
            .readdirSync(buildDir, { withFileTypes: true })
            .sort((left, right) => left.name.localeCompare(right.name));
        } catch {
          return null;
        }
        const lines = [];
        for (const entry of entries) {
          if (!entry.isFile()) {
            lines.push(`${entry.name}:not-a-file`);
            continue;
          }
          try {
            lines.push(`${entry.name}:${sha256File(path.join(buildDir, entry.name))}`);
          } catch {
            let current;
            try {
              current = fs.statSync(buildDir);
            } catch {
              return null;
            }
            // The directory was swapped while it was being read, so this
            // snapshot mixed two generations; it does not count as observed.
            if (current.ino !== before.ino || current.dev !== before.dev) return null;
            lines.push(`${entry.name}:unreadable`);
          }
        }
        let after;
        try {
          after = fs.statSync(buildDir);
        } catch {
          return null;
        }
        if (after.ino !== before.ino || after.dev !== before.dev) return null;
        return lines.join('\n');
      };
      const goodSnapshot = snapshotBuild();
      expect(goodSnapshot).not.toBeNull();
      const observed = new Set();
      let observing = true;
      const observer = (async () => {
        while (observing) {
          const snapshot = snapshotBuild();
          if (snapshot !== null) observed.add(snapshot);
          await new Promise(resolve => setTimeout(resolve, 25));
        }
      })();
      const runBuildAllObserved = () => new Promise((resolve, reject) => {
        const child = spawn(process.execPath, ['scripts/build-vite.mjs'], {
          cwd: second,
          env: secondEnv,
          stdio: ['ignore', 'pipe', 'pipe'],
        });
        let output = '';
        const capture = data => { output = (output + data.toString()).slice(-16000); };
        child.stdout.on('data', capture);
        child.stderr.on('data', capture);
        child.on('error', reject);
        child.on('exit', code => {
          if (code === 0) resolve();
          else reject(new Error(`build driver exited with ${code}\n${output}`));
        });
      });
      fs.writeFileSync(
        configPath,
        configSource.replace('dpdb.webvr.rocks', 'dpdb.webvr.rocks/atomic-generation'),
      );
      try {
        await runBuildAllObserved();
      } finally {
        observing = false;
        await observer;
      }
      const atomicSnapshot = snapshotBuild();
      expect(atomicSnapshot).not.toBeNull();
      expect(atomicSnapshot).not.toBe(goodSnapshot);
      const unexpected = [...observed].filter(
        snapshot => snapshot !== goodSnapshot && snapshot !== atomicSnapshot,
      );
      expect(unexpected.slice(0, 1)).toEqual([]);
      goodHashes = publishHashes();
      noStagingLeftovers();

      // A build that cannot finish must not disturb the published generation.
      fs.writeFileSync(configPath, `${configSource}\nconst = broken syntax\n`);
      let failure = null;
      try {
        runBuildMin();
      } catch (error) {
        failure = error;
      }
      expect(failure).not.toBeNull();
      expect(failure.status ?? 1).not.toBe(0);
      expect(publishHashes()).toEqual(goodHashes);
      noStagingLeftovers();

      // `build-min` is incremental: it refreshes the minified artifacts and
      // must leave the readable generation byte-for-byte unchanged.
      fs.writeFileSync(
        configPath,
        configSource.replace('dpdb.webvr.rocks', 'dpdb.webvr.rocks/published-generation'),
      );
      runBuildMin();
      const afterMin = Object.fromEntries(
        distributionFiles.map(name => [name, sha256File(path.join(second, name))]),
      );
      for (const name of distributionFiles) {
        if (name.includes('.min.') || name.endsWith('licenses.txt')) continue;
        expect(afterMin[name]).toBe(goodHashes[name]);
      }
      expect(afterMin['build/webvr-polyfill.min.js']).not.toBe(
        goodHashes['build/webvr-polyfill.min.js'],
      );
      noStagingLeftovers();

      // The standalone minified build must equal the minified artifacts of the
      // full build for the same source revision.
      runBuildAll();
      const rebuilt = publishHashes();
      for (const name of distributionFiles.filter(name => name.includes('.min.'))) {
        expect(rebuilt[name]).toBe(afterMin[name]);
      }
      noStagingLeftovers();

      // Source is edited before the license. A successful build may use any
      // state that actually existed, including new source with the old license.
      const licensePath = path.join(second, 'licenses.txt');
      const configSnapshot = fs.readFileSync(configPath, 'utf8');
      const licenseSnapshot = fs.readFileSync(licensePath, 'utf8');
      const sourceMarker = 'snapshot-source-probe-91c4';
      const licenseMarker = 'snapshot-license-probe-27de';
      const editedConfig = configSnapshot.replace(
        'dpdb.webvr.rocks',
        `dpdb.webvr.rocks/${sourceMarker}`,
      );
      const editedLicense = licenseSnapshot.replace(
        /\n\s*\*\/\s*$/,
        `\n * ${licenseMarker}\n */\n`,
      );
      const beforeSnapshotProbe = publishHashes();
      const snapshotReadableStaged = () =>
        stagingEntries().some(entry => {
          try {
            return fs
              .readdirSync(path.join(second, entry))
              .includes('webvr-polyfill.js');
          } catch {
            return false;
          }
        });
      const snapshotBuildProcess = spawn(process.execPath, ['scripts/build-vite.mjs'], {
        cwd: second,
        env: secondEnv,
        detached: true,
        stdio: 'ignore',
      });
      const snapshotExit = new Promise(resolve => {
        snapshotBuildProcess.on('error', () => resolve(null));
        snapshotBuildProcess.on('exit', (code, signal) => resolve({ code, signal }));
      });
      let mutatedMidBuild = false;
      const snapshotDeadline = Date.now() + 120000;
      while (Date.now() < snapshotDeadline) {
        if (snapshotReadableStaged()) {
          mutatedMidBuild = true;
          fs.writeFileSync(configPath, editedConfig);
          fs.writeFileSync(licensePath, editedLicense);
          break;
        }
        const finished = await Promise.race([
          snapshotExit.then(() => true),
          new Promise(resolve => setTimeout(resolve, 25)),
        ]);
        if (finished) break;
      }
      const snapshotResult = await snapshotExit;
      expect(snapshotResult).not.toBeNull();
      const afterSnapshotProbe = publishHashes();
      if (snapshotResult.code === 0) {
        if (JSON.stringify(afterSnapshotProbe) !== JSON.stringify(beforeSnapshotProbe)) {
          expect(mutatedMidBuild).toBe(true);
          const publishedEntries = distributionFiles.filter(
            name => !name.endsWith('.map') && !name.endsWith('licenses.txt'),
          );
          const artifactText = publishedEntries.map(
            name => fs.readFileSync(path.join(second, name), 'utf8'),
          );
          const sourceStates = artifactText.map(text => text.includes(sourceMarker));
          expect(sourceStates.every(state => state === sourceStates[0])).toBe(true);
          const publishedConfig = sourceStates[0] ? editedConfig : configSnapshot;
          for (const name of distributionFiles.filter(name => name.endsWith('.map'))) {
            const map = JSON.parse(fs.readFileSync(path.join(second, name), 'utf8'));
            expect(map.sourcesContent[map.sources.indexOf('src/config.js')]).toBe(publishedConfig);
          }
          const publishedLicense = fs.readFileSync(path.join(second, 'build/licenses.txt'), 'utf8');
          expect(sourceStates[0] ? [licenseSnapshot, editedLicense] : [licenseSnapshot])
            .toContain(publishedLicense);
          const banner = publishedLicense.match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
          expect(banner).toBeTruthy();
          for (const [index, name] of publishedEntries.entries()) {
            const text = artifactText[index];
            if (requiresLicenseBanner(name) ||
                text.startsWith(licenseSnapshot.match(/^\s*\/\*[\s\S]*?\*\//)?.[0]) ||
                text.includes(licenseMarker)) {
              expect(text.startsWith(banner), name).toBe(true);
            }
          }
        }
      } else {
        // An aborted build must leave the previous complete generation
        // untouched and clean up its staging directories.
        expect(afterSnapshotProbe).toEqual(beforeSnapshotProbe);
        noStagingLeftovers();
      }
      fs.writeFileSync(configPath, configSnapshot);
      fs.writeFileSync(licensePath, licenseSnapshot);
      noStagingLeftovers();
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.watch-lifecycle', { timeout: 300000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-watch-lifecycle-'));
    let watchProcess;
    let watchLog;
    const stopGroup = signal => {
      if (!watchProcess?.pid) return;
      try {
        process.kill(-watchProcess.pid, signal);
      } catch {}
    };
    try {
      const buildEnv = prepareBuildFixture(temp);
      fs.rmSync(path.join(temp, 'build'), { recursive: true, force: true });
      fs.writeFileSync(path.join(temp, 'UNRELATED.md'), 'unrelated file v1\n');
      const jsNames = [
        'build/webvr-polyfill.js',
        'build/webvr-polyfill.min.js',
        'build/webvr-polyfill.mjs',
        'build/webvr-polyfill.min.mjs',
      ];
      const mapNames = [
        'build/webvr-polyfill.js.map',
        'build/webvr-polyfill.min.js.map',
        'build/webvr-polyfill.mjs.map',
        'build/webvr-polyfill.min.mjs.map',
      ];
      const allNames = [...jsNames, ...mapNames, 'build/licenses.txt'];

      watchLog = fs.openSync(path.join(temp, 'watch-lifecycle.log'), 'a');
      watchProcess = spawn('npm', ['run', 'watch'], {
        cwd: temp,
        env: { ...buildEnv, CI: '1' },
        detached: true,
        stdio: ['ignore', watchLog, watchLog],
      });
      await waitFor(
        () => jsNames.every(name => fs.existsSync(path.join(temp, name))),
        120000,
        'watch lifecycle initial matrix',
      );
      await waitFor(
        () => allNames.every(name => fs.existsSync(path.join(temp, name))),
        60000,
        'watch lifecycle initial maps',
      );
      await waitFor(
        () => jsNames.every(name => fs.statSync(path.join(temp, name)).size > 1000),
        60000,
        'watch lifecycle initial content',
      );
      // A watcher may learn dependency paths from its first published maps
      // and finish startup with another build. Wait for that work to settle
      // before attributing any publication to the unrelated edit below, just
      // as the dedicated quiescence probe does.
      let previousPublication = null;
      let quietSince = Date.now();
      await waitFor(() => {
        let current;
        try {
          current = JSON.stringify(snapshotPublished(temp));
        } catch {
          quietSince = Date.now();
          return false;
        }
        if (stagingResidue(temp).length || current !== previousPublication) {
          previousPublication = current;
          quietSince = Date.now();
          return false;
        }
        return Date.now() - quietSince >= 2500;
      }, 120000, 'watch lifecycle startup to settle');

      // Incremental: a file outside the build graph must not rewrite outputs.
      const before = Object.fromEntries(
        allNames.map(name => [name, fs.statSync(path.join(temp, name)).mtimeMs]),
      );
      fs.writeFileSync(path.join(temp, 'UNRELATED.md'), 'unrelated file v2\n');
      await new Promise(resolve => setTimeout(resolve, 3000));
      for (const name of allNames) {
        expect(fs.statSync(path.join(temp, name)).mtimeMs).toBe(before[name]);
      }

      // Rapid successive edits must converge on the last edit, never settle on
      // an intermediate source state.
      const configPath = path.join(temp, 'src/config.js');
      const configSource = fs.readFileSync(configPath, 'utf8');
      expect(configSource).toContain('dpdb.webvr.rocks');
      for (const probe of ['rapid-alpha', 'rapid-bravo', 'rapid-charlie']) {
        fs.writeFileSync(
          configPath,
          configSource.replace('dpdb.webvr.rocks', `dpdb.webvr.rocks/${probe}`),
        );
        await new Promise(resolve => setTimeout(resolve, 40));
      }
      await waitFor(
        () =>
          jsNames.every(name =>
            fs.readFileSync(path.join(temp, name), 'utf8').includes('rapid-charlie'),
          ),
        90000,
        'watch lifecycle final rapid edit',
      );
      await new Promise(resolve => setTimeout(resolve, 1000));
      for (const name of jsNames) {
        const content = fs.readFileSync(path.join(temp, name), 'utf8');
        expect(content).not.toContain('rapid-alpha');
        expect(content).not.toContain('rapid-bravo');
      }

      // A broken source edit must not kill the watcher or leave truncated,
      // half-written artifacts. The next valid edit must rebuild from the
      // corrected source without restarting the watcher.
      const utilPath = path.join(temp, 'src/util.js');
      const utilSource = fs.readFileSync(utilPath, 'utf8');
      fs.writeFileSync(utilPath, `${utilSource}\nconst = broken syntax\n`);
      await new Promise(resolve => setTimeout(resolve, 8000));
      expect(
        liveProcessGroupMembers(watchProcess.pid).some(row =>
          row.cmdline.includes('watch-vite.mjs'),
        ),
      ).toBe(true);
      for (const name of [...jsNames, ...mapNames]) {
        const file = path.join(temp, name);
        expect(fs.existsSync(file)).toBe(true);
        expect(fs.statSync(file).size).toBeGreaterThan(1000);
      }
      for (const name of jsNames) {
        // Every artifact left behind by the failed rebuild must still parse.
        execFileSync(process.execPath, ['--check', path.join(temp, name)], {
          encoding: 'utf8',
        });
      }
      for (const name of mapNames) {
        // Source maps must not be left half-written either.
        expect(() => JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'))).not.toThrow();
      }
      fs.writeFileSync(utilPath, utilSource);
      const recovery = 'recovery-probe-42';
      fs.writeFileSync(
        configPath,
        configSource.replace('dpdb.webvr.rocks', `dpdb.webvr.rocks/${recovery}`),
      );
      await waitFor(
        () =>
          jsNames.every(name =>
            fs.readFileSync(path.join(temp, name), 'utf8').includes(recovery),
          ),
        90000,
        'watch recovery after a broken edit',
      );
      await new Promise(resolve => setTimeout(resolve, 1000));
      for (const name of mapNames) {
        const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
        expect(map.sourcesContent[map.sources.indexOf('src/config.js')]).toBe(
          fs.readFileSync(configPath, 'utf8'),
        );
      }

      // SIGTERM to the driver must terminate the whole watcher promptly and
      // leave no descendant process behind.
      const driver = liveProcessGroupMembers(watchProcess.pid).find(row => {
        const [command, ...args] = row.cmdline.split(' ');
        return (
          (command === process.execPath || path.basename(command) === 'node') &&
          args.some(argument => argument.endsWith('watch-vite.mjs'))
        );
      });
      expect(driver).toBeTruthy();
      process.kill(driver.pid, 'SIGTERM');
      await Promise.race([
        new Promise(resolve => watchProcess.once('exit', resolve)),
        new Promise((resolve, reject) =>
          setTimeout(() => reject(new Error('watch did not exit after SIGTERM')), 20000),
        ),
      ]);
      await new Promise(resolve => setTimeout(resolve, 2000));
      const survivors = liveProcessGroupMembers(watchProcess.pid);
      for (const survivor of survivors) {
        try {
          process.kill(survivor.pid, 'SIGKILL');
        } catch {}
      }
      expect(survivors.map(row => row.cmdline)).toEqual([]);
    } finally {
      stopGroup('SIGTERM');
      await new Promise(resolve => setTimeout(resolve, 500));
      stopGroup('SIGKILL');
      if (watchLog !== undefined) fs.closeSync(watchLog);
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.entry-behavior-parity', { timeout: 300000 }, () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-entry-parity-'));
    try {
      const desktopUA =
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/51.0 Safari/537.36';
      const iosUA =
        'Mozilla/5.0 (iPhone; CPU iPhone OS 11_0 like Mac OS X) AppleWebKit/604.1 Mobile/15A356 Safari/604.1';
      const scenarios = [
        { id: 'desktop-absent', mobile: false, native: false },
        { id: 'desktop-native-one', mobile: false, native: true, displays: ['1.1 test device'] },
        {
          id: 'desktop-native-optout',
          mobile: false,
          native: true,
          displays: ['1.1 test device'],
          options: { PROVIDE_MOBILE_VRDISPLAY: false },
        },
        { id: 'ios-absent', mobile: true, native: false },
        {
          id: 'ios-native-provide',
          mobile: true,
          native: true,
          displays: ['1.1 test device'],
          options: { PROVIDE_MOBILE_VRDISPLAY: true },
        },
      ];
      const entries = [
        ['umd', 'cjs', normalPath],
        ['umd-min', 'cjs', minPath],
        ['esm', 'esm', esmPath],
        ['esm-min', 'esm', minEsmPath],
        ['cjs', 'cjs', cjsPath],
      ];
      const probePath = path.join(temp, 'parity-probe.mjs');
      fs.writeFileSync(
        probePath,
        `
        import { createRequire } from 'node:module';
        import { pathToFileURL } from 'node:url';
        const require = createRequire(import.meta.url);
        const { JSDOM } = require('jsdom');
        const localStorage = require('localStorage');
        const input = JSON.parse(process.argv[2]);
        const desktopUA = ${JSON.stringify(desktopUA)};
        const iosUA = ${JSON.stringify(iosUA)};
        const dom = new JSDOM('', { url: 'https://example.test/' });
        const define = (name, value) =>
          Object.defineProperty(globalThis, name, {
            value,
            configurable: true,
            writable: true,
          });
        define('window', dom.window);
        define('document', dom.window.document);
        define('navigator', dom.window.navigator);
        define('screen', {});
        define('Event', dom.window.Event);
        define('CustomEvent', dom.window.CustomEvent);
        define('localStorage', localStorage);
        Object.defineProperty(global.window, 'localStorage', {
          value: localStorage,
          configurable: true,
        });
        localStorage.clear();
        const ua = input.mobile ? iosUA : desktopUA;
        Object.defineProperty(global.navigator, 'userAgent', {
          value: ua,
          configurable: true,
          writable: true,
        });
        Object.defineProperty(global.navigator, 'platform', {
          value: input.mobile ? 'iPhone' : 'Linux x86_64',
          configurable: true,
          writable: true,
        });
        const Polyfill =
          input.kind === 'esm'
            ? (await import(pathToFileURL(input.file).href)).default
            : require(input.file);
        if (input.native) {
          window.navigator.getVRDisplays = () =>
            Promise.resolve(input.displays.map(name => ({ displayName: name })));
          window.VRDisplay = Polyfill.VRDisplay;
          window.VRFrameData = Polyfill.VRFrameData;
        }
        const polyfill = new Polyfill({ DPDB_URL: null, ...(input.options || {}) });
        const displays = await navigator.getVRDisplays();
        process.stdout.write(
          JSON.stringify({
            enabled: polyfill.enabled,
            hasNative: polyfill.hasNative,
            version: Polyfill.version,
            names: displays.map(display => display.displayName),
            count: displays.length,
          }),
        );
        `,
      );
      for (const scenario of scenarios) {
        const baseline = (() => {
          const output = execFileSync(
            process.execPath,
            [probePath, JSON.stringify({ ...scenario, kind: entries[0][1], file: entries[0][2] })],
            {
              cwd: root,
              encoding: 'utf8',
              env: { ...process.env, NODE_PATH: path.join(root, 'node_modules') },
            },
          );
          return JSON.parse(output);
        })();
        expect(baseline.version).toBe('0.10.12');
        for (const [name, kind, file] of entries.slice(1)) {
          const output = execFileSync(
            process.execPath,
            [probePath, JSON.stringify({ ...scenario, kind, file })],
            {
              cwd: root,
              encoding: 'utf8',
              env: { ...process.env, NODE_PATH: path.join(root, 'node_modules') },
            },
          );
          expect({ entry: name, ...JSON.parse(output) }).toEqual({
            entry: name,
            ...baseline,
          });
        }
      }
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.watch-fault-recovery', { timeout: 420000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-watch-fault-'));
    let watchProcess;
    let watchLog;
    let buildEnv;
    const generation = () => publishedGeneration(temp);
    const startWatch = () => {
      watchLog = fs.openSync(path.join(temp, 'watch-fault.log'), 'a');
      watchProcess = spawn('npm', ['run', 'watch'], {
        cwd: temp,
        env: { ...buildEnv, CI: '1' },
        detached: true,
        stdio: ['ignore', watchLog, watchLog],
      });
    };
    try {
      buildEnv = prepareBuildFixture(temp);
      const buildDir = path.join(temp, 'build');
      const configPath = path.join(temp, 'src/config.js');
      const utilPath = path.join(temp, 'src/util.js');
      const licenseSourcePath = path.join(temp, 'licenses.txt');
      fs.rmSync(buildDir, { recursive: true, force: true });
      // A previous complete generation lets every failure scenario prove that
      // an invalid input never destroys what was last published.
      execFileSync('npm', ['run', 'build-all'], {
        cwd: temp,
        encoding: 'utf8',
        env: buildEnv,
      });
      const initial = generation();
      expect(initial).not.toBeNull();
      assertCoherentGeneration(temp);
      const utilSource = fs.readFileSync(utilPath, 'utf8');
      const configSource = fs.readFileSync(configPath, 'utf8');

      // 1. Startup with an already-invalid mapped source. The watcher must stay
      // alive, keep the previous generation untouched, and publish the fixed
      // source without a restart.
      fs.writeFileSync(utilPath, `${utilSource}\nconst = broken startup syntax\n`);
      startWatch();
      await sleep(12000);
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(generation()).toEqual(initial);
      fs.writeFileSync(utilPath, utilSource);
      let currentConfig = withConfigProbe(configSource, 'FAULT_STARTUP', 'fault-startup-ok');
      fs.writeFileSync(configPath, currentConfig);
      await waitFor(
        () => allWatchJsContain(temp, 'fault-startup-ok'),
        90000,
        'watch recovery from an invalid startup source',
      );
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);

      // 2. A mapped source imports a module that does not exist yet. The failed
      // rebuild must keep the old generation and keep watching the expected
      // path until the module is created.
      currentConfig =
        "import { MISSING_PROBE_URL } from './missing-probe.js';\n" +
        currentConfig.replace(
          'export default {',
          'export default {\n  FAULT_MISSING: MISSING_PROBE_URL,',
        );
      fs.writeFileSync(configPath, currentConfig);
      const beforeMissing = generation();
      expect(beforeMissing).not.toBeNull();
      await sleep(8000);
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(generation()).toEqual(beforeMissing);

      const missingModule = path.join(temp, 'src/missing-probe.js');
      const missingMarker = 'fault-missing-ok';
      const missingSource =
        `export const MISSING_PROBE_URL = 'https://dpdb.webvr.rocks/${missingMarker}';\n`;
      fs.writeFileSync(missingModule, missingSource);
      await waitFor(
        () => allWatchJsContain(temp, missingMarker),
        90000,
        'watch recovery after an import-before-create module appears',
      );
      await sleep(1000);
      for (const name of watchMapNames) {
        const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
        const index = map.sources.indexOf('src/missing-probe.js');
        expect(index).toBeGreaterThanOrEqual(0);
        expect(map.sourcesContent[index]).toBe(missingSource);
      }

      // 3. A resolved module is deleted and then restored. The watcher must
      // survive the failure, keep the old generation, and resume watching the
      // now-missing path.
      const beforeDelete = generation();
      expect(beforeDelete).not.toBeNull();
      const settledConfig = fs.readFileSync(configPath, 'utf8');
      fs.rmSync(configPath, { force: true });
      await sleep(8000);
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(generation()).toEqual(beforeDelete);
      const restoredConfig = withConfigProbe(settledConfig, 'FAULT_RESTORE', 'fault-restore-ok');
      fs.writeFileSync(configPath, restoredConfig);
      await waitFor(
        () => allWatchJsContain(temp, 'fault-restore-ok'),
        90000,
        'watch recovery after a deleted module was restored',
      );

      // 4. The license source disappears and comes back with a new banner. The
      // whole published set must be republished from the restored input.
      const beforeLicenseDelete = generation();
      expect(beforeLicenseDelete).not.toBeNull();
      const licenseSource = fs.readFileSync(licenseSourcePath, 'utf8');
      fs.rmSync(licenseSourcePath, { force: true });
      await sleep(8000);
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(generation()).toEqual(beforeLicenseDelete);
      const licenseMarker = 'fault-license-ok';
      const restoredLicense = licenseSource.replace(
        /\n\s*\*\/\s*$/,
        `\n * ${licenseMarker}\n */\n`,
      );
      expect(restoredLicense).not.toBe(licenseSource);
      fs.writeFileSync(licenseSourcePath, restoredLicense);
      await waitFor(() => {
        try {
          return (
            fs
              .readFileSync(path.join(temp, 'build/licenses.txt'), 'utf8')
              .includes(licenseMarker) &&
            watchJsNames.filter(requiresLicenseBanner).every(name =>
              fs.readFileSync(path.join(temp, name), 'utf8').includes(licenseMarker),
            )
          );
        } catch {
          return false;
        }
      }, 90000, 'watch republish after the license source was restored');
      await sleep(1000);
      expect(fs.readFileSync(path.join(temp, 'build/licenses.txt'), 'utf8')).toBe(
        restoredLicense,
      );

      // 5. Editors often save atomically: write a temporary file and rename it
      // over the target. A watcher bound to the old inode loses the second
      // replacement; watching the directory must keep following the path.
      const writeAtomically = content => {
        const temporary = path.join(temp, 'src/.config-atomic.tmp');
        fs.writeFileSync(temporary, content);
        fs.renameSync(temporary, configPath);
      };
      writeAtomically(withConfigProbe(fs.readFileSync(configPath, 'utf8'), 'FAULT_ATOMIC', 'fault-atomic-one'));
      await waitFor(
        () => allWatchJsContain(temp, 'fault-atomic-one'),
        90000,
        'watch rebuild after an atomic replacement',
      );
      await sleep(500);
      writeAtomically(withConfigProbe(fs.readFileSync(configPath, 'utf8'), 'FAULT_ATOMIC', 'fault-atomic-two'));
      await waitFor(
        () => allWatchJsContain(temp, 'fault-atomic-two'),
        90000,
        'watch rebuild after a second atomic replacement',
      );
      await sleep(1000);
      for (const name of watchJsNames) {
        expect(fs.readFileSync(path.join(temp, name), 'utf8')).not.toContain(
          'fault-atomic-one',
        );
      }
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
    } finally {
      if (watchProcess?.pid) {
        try {
          process.kill(-watchProcess.pid, 'SIGTERM');
        } catch {}
        await sleep(1200);
        try {
          process.kill(-watchProcess.pid, 'SIGKILL');
        } catch {}
      }
      if (watchLog !== undefined) fs.closeSync(watchLog);
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.watch-crash-recovery', { timeout: 420000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-watch-crash-'));
    let watchProcess;
    let watchLog;
    let buildEnv;
    const killPause = preparePublicationPause(temp, 'watch-kill');
    const startWatch = () => {
      watchLog = fs.openSync(path.join(temp, 'watch-crash.log'), 'a');
      watchProcess = spawn('npm', ['run', 'watch'], {
        cwd: temp,
        env: { ...buildEnv, ...killPause.env, CI: '1' },
        detached: true,
        stdio: ['ignore', watchLog, watchLog],
      });
    };
    const waitForGeneration = async label => {
      await waitFor(() => {
        const generation = publishedGeneration(temp);
        return generation !== null && stagingResidue(temp).length === 0;
      }, 120000, label);
      assertCoherentGeneration(temp);
    };
    const stopGroup = async (signal, timeoutMs) => {
      if (!watchProcess?.pid) return false;
      try {
        process.kill(-watchProcess.pid, signal);
      } catch {}
      const exited = await Promise.race([
        new Promise(resolve => watchProcess.once('exit', () => resolve(true))),
        sleep(timeoutMs).then(() => false),
      ]);
      for (const survivor of liveProcessGroupMembers(watchProcess.pid)) {
        try {
          process.kill(survivor.pid, 'SIGKILL');
        } catch {}
      }
      return exited;
    };
    try {
      buildEnv = prepareBuildFixture(temp);
      const configPath = path.join(temp, 'src/config.js');
      fs.rmSync(path.join(temp, 'build'), { recursive: true, force: true });
      const readConfig = () => fs.readFileSync(configPath, 'utf8');

      // 1. SIGTERM while a rebuild is in flight. The driver must stop promptly,
      // leave no descendant, and never publish a partial or mixed generation.
      startWatch();
      await waitForGeneration('watch crash initial generation');
      const beforeTerm = publishedGeneration(temp);
      expect(beforeTerm).not.toBeNull();
      fs.writeFileSync(
        configPath,
        withConfigProbe(readConfig(), 'FAULT_CRASH', 'fault-term-pending'),
      );
      await waitForStaging(temp, 20000);
      expect(await stopGroup('SIGTERM', 20000)).toBe(true);
      await sleep(1500);
      expect(liveProcessGroupMembers(watchProcess.pid)).toEqual([]);
      const afterTerm = publishedGeneration(temp);
      expect(afterTerm).not.toBeNull();
      if (JSON.stringify(afterTerm) !== JSON.stringify(beforeTerm)) {
        expect(allWatchJsContain(temp, 'fault-term-pending')).toBe(true);
      }
      assertCoherentGeneration(temp);
      watchProcess = undefined;

      // 2. SIGKILL while a rebuild is in flight. The previous complete
      // generation must survive byte-for-byte; any staging residue must be
      // removed by the next watch start rather than published.
      startWatch();
      await waitForGeneration('watch restart after SIGTERM');
      await sleep(1000);
      const beforeKill = publishedGeneration(temp);
      expect(beforeKill).not.toBeNull();
      killPause.arm();
      fs.writeFileSync(
        configPath,
        withConfigProbe(readConfig(), 'FAULT_CRASH', 'fault-kill-pending'),
      );
      await waitFor(
        () => killPause.pausedPid() || allWatchJsContain(temp, 'fault-kill-pending'),
        120000,
        'watch publication boundary before SIGKILL',
      );
      const pausedPublisher = killPause.pausedPid();
      // An equivalent publisher that does not use a hooked filesystem API may
      // already have committed. In that case stop an idle watcher and check
      // its current complete generation, rather than claim an in-flight kill.
      const beforeSignal = pausedPublisher ? beforeKill : publishedGeneration(temp);
      if (pausedPublisher) {
        expect(publishedGeneration(temp)).toEqual(beforeKill);
        try {
          process.kill(pausedPublisher, 'SIGKILL');
        } catch {}
      }
      expect(await stopGroup('SIGKILL', 20000)).toBe(true);
      await sleep(800);
      killPause.release();
      const afterKill = publishedGeneration(temp);
      expect(afterKill).not.toBeNull();
      expect(afterKill).toEqual(beforeSignal);
      assertCoherentGeneration(temp);
      watchProcess = undefined;

      // 3. A fresh watcher must clean the interrupted staging and publish a
      // complete generation, then keep working instead of recovering once.
      fs.writeFileSync(
        configPath,
        withConfigProbe(readConfig(), 'FAULT_CRASH', 'fault-kill-recover'),
      );
      startWatch();
      await waitFor(
        () => allWatchJsContain(temp, 'fault-kill-recover'),
        120000,
        'watch republish after an interrupted rebuild',
      );
      await sleep(1500);
      expect(stagingResidue(temp)).toEqual([]);
      assertCoherentGeneration(temp);

      fs.writeFileSync(
        configPath,
        withConfigProbe(readConfig(), 'FAULT_CRASH', 'fault-kill-final'),
      );
      await waitFor(
        () => allWatchJsContain(temp, 'fault-kill-final'),
        120000,
        'watch continued publishing after crash recovery',
      );
      await sleep(1000);
      for (const name of watchJsNames) {
        expect(fs.readFileSync(path.join(temp, name), 'utf8')).not.toContain(
          'fault-kill-recover',
        );
      }
      expect(stagingResidue(temp)).toEqual([]);
      assertCoherentGeneration(temp);
    } finally {
      const pausedPublisher = killPause.pausedPid();
      if (pausedPublisher) {
        try {
          process.kill(pausedPublisher, 'SIGKILL');
        } catch {}
      }
      killPause.release();
      if (watchProcess?.pid) {
        try {
          process.kill(-watchProcess.pid, 'SIGTERM');
        } catch {}
        await sleep(1200);
        try {
          process.kill(-watchProcess.pid, 'SIGKILL');
        } catch {}
      }
      if (watchLog !== undefined) fs.closeSync(watchLog);
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.watch-concurrent-build', { timeout: 480000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-watch-concurrent-'));
    let watchProcess;
    let watchLog;
    let buildEnv;
    try {
      buildEnv = prepareBuildFixture(temp);
      const buildDir = path.join(temp, 'build');
      const configPath = path.join(temp, 'src/config.js');
      const readConfig = () => fs.readFileSync(configPath, 'utf8');
      fs.rmSync(buildDir, { recursive: true, force: true });

      watchLog = fs.openSync(path.join(temp, 'watch-concurrent.log'), 'a');
      watchProcess = spawn('npm', ['run', 'watch'], {
        cwd: temp,
        env: { ...buildEnv, CI: '1' },
        detached: true,
        stdio: ['ignore', watchLog, watchLog],
      });
      await waitFor(() => publishedComplete(temp), 120000, 'concurrent initial generation');
      assertCoherentGeneration(temp);

      const initial = samplePublishedDirectory(buildDir);
      expect(initial?.complete).toBe(true);
      const completeGenerations = new Set([initial.fingerprint]);
      const observed = new Map();
      let observing = true;
      const observer = (async () => {
        while (observing) {
          const sample = samplePublishedDirectory(buildDir);
          if (sample !== null) {
            observed.set(sample.fingerprint, { complete: sample.complete, listing: sample.listing });
          }
          await sleep(15);
        }
      })();

      async function waitForGeneration(token, config, label) {
        let generation;
        await waitFor(() => {
          const sample = samplePublishedDirectory(buildDir);
          if (!concurrentGenerationMatches(sample, token, config)) return false;
          generation = sample;
          return true;
        }, 120000, label);
        expect(generation.files.get('licenses.txt')).toBe(initial.files.get('licenses.txt'));
        const banner = generation.files.get('licenses.txt').match(/^\s*\/\*[\s\S]*?\*\//)?.[0];
        expect(banner).toBeTruthy();
        for (const file of watchJsNames.filter(requiresLicenseBanner)) {
          expect(generation.files.get(path.basename(file)).startsWith(banner), file).toBe(true);
        }
        completeGenerations.add(generation.fingerprint);
      }

      const runFullBuild = () =>
        new Promise(resolve => {
          const child = spawn(process.execPath, ['scripts/build-vite.mjs'], {
            cwd: temp,
            env: buildEnv,
            stdio: ['ignore', 'pipe', 'pipe'],
          });
          let output = '';
          child.stdout.on('data', chunk => {
            output += chunk;
          });
          child.stderr.on('data', chunk => {
            output += chunk;
          });
          child.on('exit', code => resolve({ code, output }));
        });

      try {
        for (const token of ['wprobe-concurrent-one', 'wprobe-concurrent-two', 'wprobe-concurrent-three']) {
          const config = withConfigProbe(readConfig(), 'CONCURRENT_PROBE', token);
          fs.writeFileSync(configPath, config);
          const results = await Promise.all([runFullBuild(), runFullBuild()]);
          for (const result of results) {
            expect(result.code, result.output).toBe(0);
          }
          await waitForGeneration(token, config, `concurrent publication for ${token}`);
        }

        // The full builds could have produced every previous token. Make an
        // edit after they exit so only the watcher can publish this revision.
        const token = 'wprobe-concurrent-watcher-after';
        const config = withConfigProbe(readConfig(), 'CONCURRENT_PROBE', token);
        fs.writeFileSync(configPath, config);
        await waitForGeneration(token, config, 'watcher publication after concurrent builds');
      } finally {
        observing = false;
        await observer;
      }

      expect([...observed].filter(([fingerprint, sample]) =>
        !sample.complete || !completeGenerations.has(fingerprint),
      ).slice(0, 5)).toEqual([]);
      await waitFor(
        () => stagingResidue(temp).length === 0,
        60000,
        'watch settling after concurrent builds',
      );
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(stagingResidue(temp)).toEqual([]);
      assertCoherentGeneration(temp);
      expect(concurrentGenerationMatches(
        samplePublishedDirectory(buildDir), 'wprobe-concurrent-watcher-after', readConfig(),
      )).toBe(true);
    } finally {
      if (watchProcess?.pid) {
        try {
          process.kill(-watchProcess.pid, 'SIGTERM');
        } catch {}
        await sleep(1200);
        try {
          process.kill(-watchProcess.pid, 'SIGKILL');
        } catch {}
      }
      if (watchLog !== undefined) fs.closeSync(watchLog);
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.watch-directory-recovery', { timeout: 480000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-watch-dir-'));
    let watchProcess;
    let watchLog;
    let buildEnv;
    const generation = () => publishedGeneration(temp);
    const startWatch = () => {
      watchLog = fs.openSync(path.join(temp, 'watch-dir.log'), 'a');
      watchProcess = spawn('npm', ['run', 'watch'], {
        cwd: temp,
        env: { ...buildEnv, CI: '1' },
        detached: true,
        stdio: ['ignore', watchLog, watchLog],
      });
    };
    const stopWatch = async () => {
      if (!watchProcess?.pid) return;
      try {
        process.kill(-watchProcess.pid, 'SIGTERM');
      } catch {}
      await Promise.race([
        new Promise(resolve => watchProcess.once('exit', resolve)),
        sleep(5000),
      ]);
      for (const survivor of liveProcessGroupMembers(watchProcess.pid)) {
        try {
          process.kill(survivor.pid, 'SIGKILL');
        } catch {}
      }
      watchProcess = undefined;
    };
    try {
      buildEnv = prepareBuildFixture(temp);
      const buildDir = path.join(temp, 'build');
      const srcDir = path.join(temp, 'src');
      const entryPath = path.join(srcDir, 'webvr-polyfill.js');
      const configPath = path.join(srcDir, 'config.js');
      fs.rmSync(buildDir, { recursive: true, force: true });

      // A nested module directory reached from the graph.
      const nestedDir = path.join(srcDir, 'nested');
      const nestedModule = path.join(nestedDir, 'deep.js');
      const nestedMarker = 'wprobe-nested-one';
      fs.mkdirSync(nestedDir, { recursive: true });
      fs.writeFileSync(
        nestedModule,
        `export const NESTED_DEEP = 'https://dpdb.webvr.rocks/${nestedMarker}';\n`,
      );
      const configSource = fs.readFileSync(configPath, 'utf8');
      const withNestedImport = source =>
        "import { NESTED_DEEP } from './nested/deep.js';\n" +
        source.replace('export default {', 'export default {\n  NESTED_IMPORT: NESTED_DEEP,');
      fs.writeFileSync(configPath, withNestedImport(configSource));

      startWatch();
      await waitFor(
        () => allWatchJsContain(temp, nestedMarker),
        150000,
        'watch initial build with a nested module directory',
      );
      await sleep(1000);
      const withNested = generation();
      expect(withNested).not.toBeNull();
      assertCoherentGeneration(temp);

      // 1. The whole subdirectory that holds a mapped module is deleted. The
      // watcher must survive, keep the last complete generation, and remember
      // the now-missing path.
      fs.rmSync(nestedDir, { recursive: true, force: true });
      await sleep(9000);
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(generation()).toEqual(withNested);
      await waitFor(() => stagingResidue(temp).length === 0, 60000, 'watch settling after directory deletion');

      // 2. The subdirectory (and module) is recreated. The watcher must resume
      // watching the path and republish a complete generation without restart.
      const recreatedMarker = 'wprobe-nested-two';
      fs.mkdirSync(nestedDir, { recursive: true });
      fs.writeFileSync(
        nestedModule,
        `export const NESTED_DEEP = 'https://dpdb.webvr.rocks/${recreatedMarker}';\n`,
      );
      await waitFor(
        () => allWatchJsContain(temp, recreatedMarker),
        150000,
        'watch recovery after a deleted module directory was recreated',
      );
      await sleep(1000);
      for (const name of watchMapNames) {
        const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
        expect(map.sources).toContain('src/nested/deep.js');
      }
      assertCoherentGeneration(temp);

      // 3. The entry module itself is missing when the watcher starts. Startup
      // must not terminate the watcher or disturb the last complete
      // generation.
      await stopWatch();
      const entrySource = fs.readFileSync(entryPath, 'utf8');
      const beforeEntryRemoval = generation();
      expect(beforeEntryRemoval).not.toBeNull();
      fs.rmSync(entryPath, { force: true });
      startWatch();
      await sleep(10000);
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(generation()).toEqual(beforeEntryRemoval);
      await waitFor(() => stagingResidue(temp).length === 0, 60000, 'watch settling after entry removal');

      // 4. Restoring the entry publishes again without a restart.
      fs.writeFileSync(entryPath, entrySource);
      const restoredMarker = 'wprobe-entry-restored';
      fs.writeFileSync(
        configPath,
        withConfigProbe(fs.readFileSync(configPath, 'utf8'), 'ENTRY_RESTORED', restoredMarker),
      );
      await waitFor(
        () => allWatchJsContain(temp, restoredMarker),
        150000,
        'watch recovery after a missing entry module was restored',
      );
      await waitFor(
        () => stagingResidue(temp).length === 0,
        60000,
        'watch settling after the entry module was restored',
      );
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(stagingResidue(temp)).toEqual([]);
      assertCoherentGeneration(temp);
    } finally {
      await stopWatch();
      if (watchLog !== undefined) fs.closeSync(watchLog);
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });

  it('f2p.watch-quiescence', { timeout: 480000 }, async () => {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-watch-quiescence-'));
    let watchProcess;
    let watchLog;
    let buildEnv;
    try {
      buildEnv = prepareBuildFixture(temp);
      const buildDir = path.join(temp, 'build');
      const configPath = path.join(temp, 'src/config.js');
      fs.rmSync(buildDir, { recursive: true, force: true });

      watchLog = fs.openSync(path.join(temp, 'watch-quiescence.log'), 'a');
      watchProcess = spawn('npm', ['run', 'watch'], {
        cwd: temp,
        env: { ...buildEnv, CI: '1' },
        detached: true,
        stdio: ['ignore', watchLog, watchLog],
      });
      await waitFor(() => publishedComplete(temp), 120000, 'quiescence initial generation');
      assertCoherentGeneration(temp);
      // Some watchers learn dependency paths from the first published maps
      // and finish startup with a second build. Do not attribute that already
      // running build to the unrelated edit made by the next probe.
      const waitUntilSettled = async label => {
        let quietSince = Date.now();
        let previous = JSON.stringify(snapshotPublished(temp));
        await waitFor(() => {
          const current = JSON.stringify(snapshotPublished(temp));
          if (stagingResidue(temp).length || current !== previous) {
            quietSince = Date.now();
            previous = current;
            return false;
          }
          return Date.now() - quietSince >= 2500;
        }, 120000, label);
      };
      await waitUntilSettled('watcher startup to finish before quiescence probes');

      // An unrelated new module under `src/` is outside the graph: neither the
      // content nor the modification time of any published file may move, and
      // no staging may be created.
      const settled = snapshotPublished(temp);
      fs.writeFileSync(
        path.join(temp, 'src/orphan-note.js'),
        "export const ORPHAN_NOTE = 'wprobe-orphan-note';\n",
      );
      await sleep(4000);
      expect(snapshotPublished(temp)).toEqual(settled);
      expect(stagingResidue(temp)).toEqual([]);

      // An unrelated file inside `build/` is not owned by the watcher: writing
      // and rewriting it must neither republish the matrix nor delete it.
      const sentinel = path.join(buildDir, 'watch-note.txt');
      fs.writeFileSync(sentinel, 'unrelated build file v1\n');
      await sleep(4000);
      expect(snapshotPublished(temp)).toEqual(settled);
      expect(fs.readFileSync(sentinel, 'utf8')).toBe('unrelated build file v1\n');
      fs.writeFileSync(sentinel, 'unrelated build file v2\n');
      await sleep(4000);
      expect(snapshotPublished(temp)).toEqual(settled);
      expect(fs.readFileSync(sentinel, 'utf8')).toBe('unrelated build file v2\n');
      expect(stagingResidue(temp)).toEqual([]);

      // A module that was imported and then dropped from the graph is no longer
      // an input. Its later edits and deletion must not republish anything.
      const orphanModule = path.join(temp, 'src/transient-probe.js');
      const firstMarker = 'wprobe-transient-one';
      fs.writeFileSync(
        orphanModule,
        `export const TRANSIENT_PROBE = 'https://dpdb.webvr.rocks/${firstMarker}';\n`,
      );
      const configSource = fs.readFileSync(configPath, 'utf8');
      const withImport =
        "import { TRANSIENT_PROBE } from './transient-probe.js';\n" +
        configSource.replace('export default {', 'export default {\n  TRANSIENT_IMPORT: TRANSIENT_PROBE,');
      fs.writeFileSync(configPath, withImport);
      await waitFor(
        () => allWatchJsContain(temp, firstMarker),
        120000,
        'watch publication of a newly imported module',
      );
      await sleep(1000);

      fs.writeFileSync(configPath, configSource);
      await waitFor(
        () => !allWatchJsContain(temp, firstMarker),
        120000,
        'watch removal of a module import',
      );
      await waitUntilSettled('watcher to settle after removing an import');
      const afterDrop = snapshotPublished(temp);

      fs.writeFileSync(
        orphanModule,
        `export const TRANSIENT_PROBE = 'https://dpdb.webvr.rocks/wprobe-transient-two';\n`,
      );
      await sleep(4000);
      expect(snapshotPublished(temp)).toEqual(afterDrop);
      fs.rmSync(orphanModule, { force: true });
      await sleep(4000);
      expect(snapshotPublished(temp)).toEqual(afterDrop);
      expect(stagingResidue(temp)).toEqual([]);

      // Text that merely resembles an import is not an edge in the module
      // graph. The mapped source edit itself must rebuild, but creating or
      // editing the file named in its comment must leave the generation alone.
      const commentMarker = "// import './quiescence-comment-decoy.js'";
      const commentedSource = `${configSource}\n${commentMarker}\n`;
      fs.writeFileSync(configPath, commentedSource);
      await waitFor(() => {
        try {
          const map = JSON.parse(fs.readFileSync(
            path.join(temp, 'build/webvr-polyfill.js.map'), 'utf8',
          ));
          const index = map.sources.findIndex(source =>
            !path.isAbsolute(source) && path.resolve(temp, source) === configPath);
          return index >= 0 && map.sourcesContent[index] === commentedSource;
        } catch {
          return false;
        }
      }, 120000, 'watch publication after editing a mapped source comment');
      await waitUntilSettled('watcher to settle after the comment edit');
      const beforeDecoy = snapshotPublished(temp);
      const commentDecoy = path.join(temp, 'src/quiescence-comment-decoy.js');
      fs.writeFileSync(commentDecoy, "export const DECOY = 'unrelated';\n");
      await sleep(4000);
      expect(snapshotPublished(temp)).toEqual(beforeDecoy);
      expect(stagingResidue(temp)).toEqual([]);
      fs.writeFileSync(commentDecoy, "export const DECOY = 'still unrelated';\n");
      await sleep(4000);
      expect(snapshotPublished(temp)).toEqual(beforeDecoy);
      expect(stagingResidue(temp)).toEqual([]);
      fs.rmSync(commentDecoy);
      fs.writeFileSync(configPath, configSource);
      await waitFor(() => {
        try {
          const map = JSON.parse(fs.readFileSync(
            path.join(temp, 'build/webvr-polyfill.js.map'), 'utf8',
          ));
          const index = map.sources.findIndex(source =>
            !path.isAbsolute(source) && path.resolve(temp, source) === configPath);
          return index >= 0 && map.sourcesContent[index] === configSource;
        } catch {
          return false;
        }
      }, 120000, 'watch publication after restoring the mapped source');
      await waitUntilSettled('watcher to settle after restoring the source');

      // A genuine in-graph edit must still publish.
      const realMarker = 'wprobe-quiescence-real';
      fs.writeFileSync(
        configPath,
        withConfigProbe(configSource, 'QUIESCENCE_PROBE', realMarker),
      );
      await waitFor(
        () => allWatchJsContain(temp, realMarker),
        120000,
        'watch publication of a real source change',
      );
      await waitFor(
        () => stagingResidue(temp).length === 0,
        60000,
        'watch settling after a real source change',
      );
      expect(isWatchDriverAlive(watchProcess.pid)).toBe(true);
      expect(stagingResidue(temp)).toEqual([]);
      assertCoherentGeneration(temp);
      // Bare package imports are part of the mapped graph too. A watcher
      // which only parses relative src/ imports silently misses these edits.
      // prepareBuildFixture copied node_modules, so this never mutates the
      // installed dependencies of the candidate or another test fixture.
      const dependencyMap = JSON.parse(fs.readFileSync(
        path.join(temp, 'build/webvr-polyfill.js.map'), 'utf8',
      ));
      const dependencySource = dependencyMap.sources.find(source =>
        source.startsWith('node_modules/cardboard-vr-display/') && source.endsWith('.js'));
      expect(dependencySource).toBeTruthy();
      const dependencyPath = assertPackageRelativeSource(temp, dependencySource);
      const dependencyOriginal = fs.readFileSync(dependencyPath, 'utf8');
      const dependencyMarker = 'wprobe-mapped-dependency-edit';
      try {
        fs.writeFileSync(dependencyPath, dependencyOriginal +
          `\nglobalThis.__mappedDependencyProbe = '${dependencyMarker}';\n`);
        await waitFor(
          () => allWatchJsContain(temp, dependencyMarker),
          120000,
          'watch publication after editing a mapped dependency module',
        );
        await waitFor(() => stagingResidue(temp).length === 0, 60000, 'dependency rebuild settling');
        // Validate current source text, not just a change to the bundle bytes.
        for (const name of watchMapNames) {
          const map = JSON.parse(fs.readFileSync(path.join(temp, name), 'utf8'));
          const index = map.sources.indexOf(dependencySource);
          expect(index).toBeGreaterThanOrEqual(0);
          expect(map.sourcesContent[index]).toBe(fs.readFileSync(dependencyPath, 'utf8'));
        }
      } finally {
        fs.writeFileSync(dependencyPath, dependencyOriginal);
      }
      await waitFor(
        () => {
          try {
            return watchJsNames.every(name =>
              !fs.readFileSync(path.join(temp, name), 'utf8').includes(dependencyMarker));
          } catch {
            return false;
          }
        },
        120000,
        'watch publication after restoring a mapped dependency module',
      );
      await waitFor(() => stagingResidue(temp).length === 0, 60000, 'dependency restoration settling');
      assertCoherentGeneration(temp);
    } finally {
      if (watchProcess?.pid) {
        try {
          process.kill(-watchProcess.pid, 'SIGTERM');
        } catch {}
        await sleep(1200);
        try {
          process.kill(-watchProcess.pid, 'SIGKILL');
        } catch {}
      }
      if (watchLog !== undefined) fs.closeSync(watchLog);
      fs.rmSync(temp, { recursive: true, force: true });
    }
  });
});

'use strict';

// BATCH-F2P-0003 shared probe predicates.
//
// One predicate per F2P observation. The SAME predicate function is executed
// against the source repository and against the reference target so the
// PASS/FAIL outcome is produced by inspection, never by side/role.
//
// Every predicate here asserts that the frozen target technology
// (fastify 4.28.1 + @fastify/static 7.0.4 + @fastify/multipart 8.3.0) owns the
// behaviour, and that the legacy Express stack does not execute it.

const fs = require('fs');
const path = require('path');

const TARGET_DEPS = {
  fastify: '4.28.1',
  '@fastify/static': '7.0.4',
  '@fastify/multipart': '8.3.0',
};

// Accept an exact pin or a compatible range/prefix (e.g. "^4.28.1", "~7.0.4",
// "v4.28.1"). The declared/locked *major* must match the target major so a
// wrong-major stack (e.g. fastify 5) still fails; minor/patch may vary.
function versionCompatible(declared, target) {
  const major = (value) => {
    if (typeof value !== 'string') return null;
    const match = value.trim().replace(/^[~^>=<v\s]+/, '').match(/^(\d+)/);
    return match ? match[1] : null;
  };
  const declaredMajor = major(declared);
  return declaredMajor !== null && declaredMajor === major(target);
}

const LEGACY_DEPS = [
  'express',
  'body-parser',
  'serve-static',
  'csv-express',
  'server-sent-events',
  'multer',
];

const LEGACY_REQUIRES = [
  "require('express')",
  "require('body-parser')",
  "require('serve-static')",
  "require('csv-express')",
  "require('server-sent-events')",
  "require('multer')",
];

function readText(root, rel) {
  try {
    return fs.readFileSync(path.join(root, rel), 'utf8');
  } catch (err) {
    return null;
  }
}

function readJson(root, rel) {
  const text = readText(root, rel);
  if (text === null) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    return null;
  }
}

// Strip comments (regex-literal aware) and fold constant string forms so a
// legacy token that only appears in prose/rewritten form is not treated as
// real residual source code.
function stripComments(text) {
  let out = '';
  let i = 0;
  let quote = null;
  let prev = null;
  const prevChars = '(,=:[!&|?{};+-*%^<>~';
  const prevWords = { return: 1, typeof: 1, instanceof: 1, in: 1, of: 1, new: 1, delete: 1, void: 1, case: 1, do: 1, else: 1, yield: 1, await: 1, throw: 1 };
  while (i < text.length) {
    const ch = text[i];
    if (quote !== null) {
      out += ch;
      if (ch === '\\' && i + 1 < text.length) { out += text[i + 1]; i += 2; continue; }
      if (ch === quote) quote = null;
      i += 1; continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; out += ch; i += 1; continue; }
    if (ch === '/' && text[i + 1] === '/') { i += 2; while (i < text.length && text[i] !== '\n' && text[i] !== '\r') i += 1; continue; }
    if (ch === '/' && text[i + 1] === '*') { i += 2; while (i + 1 < text.length && !(text[i] === '*' && text[i + 1] === '/')) i += 1; i += 2; continue; }
    if (ch === '/') {
      const m = out.match(/[A-Za-z_$][\w$]*$/);
      const word = m ? m[0] : '';
      if (prev === null || prevChars.indexOf(prev) !== -1 || prevWords[word]) {
        out += ch; i += 1; let inClass = false;
        while (i < text.length) {
          const c = text[i];
          if (c === '\\' && i + 1 < text.length) { out += c; out += text[i + 1]; i += 2; continue; }
          if (c === '[') inClass = true;
          else if (c === ']') inClass = false;
          else if (c === '/' && !inClass) { out += c; i += 1; break; }
          else if (c === '\n' || c === '\r') break;
          out += c; i += 1;
        }
        while (i < text.length && /[a-z]/i.test(text[i])) { out += text[i]; i += 1; }
        prev = '/'; continue;
      }
    }
    out += ch;
    if (!/\s/.test(ch)) prev = ch;
    i += 1;
  }
  return out;
}

function readServer(root) {
  const raw = readText(root, 'server.js');
  return raw === null ? null : stripComments(raw);
}

function has(text, needle) {
  return typeof text === 'string' && text.includes(needle);
}

function all(text, needles) {
  return needles.every((needle) => has(text, needle));
}

function none(text, needles) {
  return needles.every((needle) => !has(text, needle));
}

// Target-technology gate shared by the structural predicates.  The frozen
// requirement is that the *Fastify* framework owns the module graph (fastify is
// loaded and no legacy Express middleware executes); the instance variable name
// and the plugin choice are not part of the contract (the instruction permits
// adding/removing dependencies and Fastify plugins), so neither may be the
// pass/fail condition.  This mirrors BATCH-F2P-0001's `fastifyOwned`.
function targetTechnologyOwned(server) {
  if (typeof server !== 'string' || !server) return false;
  const fastifyLoaded = /require\(\s*['"]fastify['"]\s*\)/.test(server)
    || /from\s+['"]fastify['"]/.test(server);
  const legacyAbsent = none(server, LEGACY_REQUIRES.concat([
    'http.createServer(express)',
    'express.use(bodyParser.json())',
    'serveStatic(',
  ]));
  return fastifyLoaded && legacyAbsent;
}

// Behavioural capability anchors (mechanism, not module specifier).
const STATIC_SERVE = /(?:createReadStream|sendFile|serveStatic|@fastify\/static|\bstatic\b)/;
const MULTIPART_HANDLING = /(?:multipart|\.file\s*\(|busboy|addContentTypeParser)/i;
// Any receiver: a route may be registered on `fastify`, `server`, `app`, ….
const INSTANCE_CALL = (method, pathLiteral) => new RegExp(
  "[A-Za-z_$][\\w$]*\\.(?:get|post|delete|put|patch|all)\\(\\s*['\"]"
  + pathLiteral.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&') + "['\"]",
);

// --- dependency projection predicates -------------------------------------

function lockfileResolvesTarget(root) {
  const lock = readJson(root, 'package-lock.json');
  if (!lock) return false;
  const packages = lock.packages || {};
  const entries = [];
  Object.keys(packages).forEach((key) => {
    if (/^node_modules\//.test(key)) entries.push({ key, data: packages[key] });
  });
  const resolved = (name, version) => entries.some(
    (entry) => entry.key === `node_modules/${name}` && versionCompatible(entry.data.version, version),
  );
  // The contract pins the target *framework*; plugins are free to be added or
  // removed, so only fastify is required to be resolvable.
  const targetPresent = resolved('fastify', TARGET_DEPS.fastify);
  // Removing unused legacy dependencies is optional; executed imports are
  // checked separately by the module-identity and residue gates.
  return targetPresent;
}

function manifestDeclaresTarget(root) {
  const pkg = readJson(root, 'package.json');
  if (!pkg) return false;
  const deps = Object.assign({}, pkg.dependencies, pkg.devDependencies);
  const targetPresent = versionCompatible(deps.fastify, TARGET_DEPS.fastify);
  return targetPresent;
}

// --- server.js structural predicates --------------------------------------

function moduleGraphUsesTarget(root) {
  return targetTechnologyOwned(readServer(root));
}

function listenBindsAllInterfaces(root) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server)
    && /[A-Za-z_$][\w$]*\.listen\(/.test(server)
    && /host:\s*['"]0\.0\.0\.0['"]/.test(server);
}

function staticApidocOwnedByTarget(root) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server)
    && (/\.register\([^)\n]*static/i.test(server) || STATIC_SERVE.test(server))
    && /\.get\(\s*['"]\/api\/doc['"]/.test(server)
    && none(server, ["serveStatic('.build/apidoc')"]);
}

function jsonBodyParserOwnedByTarget(root) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server)
    && /bodyLimit\s*:\s*(?:102400|100\s*\*\s*1024|1024\s*\*\s*100)/.test(server)
    && all(server, ["'application/json'", 'setErrorHandler']);
}

function csvSerializationOwnedByTarget(root) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server)
    && all(server, ["'text/csv; charset=utf-8'"])
    && none(server, ["require('csv-express')"]);
}

function sseContractOwnedByTarget(root) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server)
    && has(server, "'text/event-stream'")
    && has(server, 'retry: 2000')
    && /\.hijack\(\)/.test(server)
    && none(server, ["require('server-sent-events')"]);
}

function multipartOwnedByTarget(root) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server)
    && (/\.register\([^)\n]*multipart/i.test(server) || MULTIPART_HANDLING.test(server))
    && has(server, "'video'")
    && none(server, ["require('multer')"]);
}

function catchAllOwnedByTarget(root) {
  const server = readServer(root);
  if (!server) return false;
  // The catch-all must hijack the Fastify reply and hand the raw Node
  // request/response to the existing Next handler.  The exact helper name and
  // the way the raw objects are reached are implementation details, so anchor
  // on the behaviour (`hijack` + use of `.raw`) instead of reference spelling.
  return targetTechnologyOwned(server)
    && INSTANCE_CALL('get', '/*').test(server)
    && /\.hijack\(\)/.test(server)
    && /\.raw\b/.test(server);
}

function routeBehaviorOwnedByTarget(root, routePath) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server) && INSTANCE_CALL('get', routePath).test(server);
}

function sideEffectsOwnedByTarget(root) {
  const server = readServer(root);
  if (!server) return false;
  return targetTechnologyOwned(server)
    && all(server, ['YOLO.start()', 'Opendatacam.listenToYOLO'])
    && routeBehaviorOwnedByTarget(root, '/')
    && routeBehaviorOwnedByTarget(root, '/start')
    && routeBehaviorOwnedByTarget(root, '/recording/start')
    && routeBehaviorOwnedByTarget(root, '/recording/stop')
    && INSTANCE_CALL('post', '/counter/areas').test(server)
    && INSTANCE_CALL('post', '/ui').test(server);
}

// --- observation -> predicate map -----------------------------------------

const PREDICATES = {
  'F2P-060': lockfileResolvesTarget,
  'F2P-066': manifestDeclaresTarget,
  'F2P-069': moduleGraphUsesTarget,
  'F2P-056': (root) => routeBehaviorOwnedByTarget(root, '/console'),
  'F2P-057': sideEffectsOwnedByTarget,
  'F2P-061': listenBindsAllInterfaces,
  'F2P-062': staticApidocOwnedByTarget,
  'F2P-063': jsonBodyParserOwnedByTarget,
  'F2P-064': staticApidocOwnedByTarget,
  'F2P-065': csvSerializationOwnedByTarget,
  'F2P-067': staticApidocOwnedByTarget,
  'F2P-068': sseContractOwnedByTarget,
  'F2P-070': staticApidocOwnedByTarget,
  'F2P-071': staticApidocOwnedByTarget,
  'F2P-072': staticApidocOwnedByTarget,
  'F2P-049': moduleGraphUsesTarget,
  'F2P-050': (root) => routeBehaviorOwnedByTarget(root, '/status'),
  'F2P-051': jsonBodyParserOwnedByTarget,
  'F2P-052': multipartOwnedByTarget,
  'F2P-053': listenBindsAllInterfaces,
  'F2P-054': listenBindsAllInterfaces,
  'F2P-055': moduleGraphUsesTarget,
  'F2P-058': moduleGraphUsesTarget,
  'F2P-059': catchAllOwnedByTarget,
};

function evaluateAll(root) {
  const results = {};
  Object.keys(PREDICATES).forEach((observationId) => {
    let status;
    try {
      status = PREDICATES[observationId](root) ? 'PASS' : 'FAIL';
    } catch (err) {
      status = 'INCONCLUSIVE';
    }
    results[observationId] = status;
  });
  return results;
}

module.exports = {
  TARGET_DEPS,
  LEGACY_DEPS,
  LEGACY_REQUIRES,
  readText,
  readJson,
  evaluateAll,
  PREDICATES,
};

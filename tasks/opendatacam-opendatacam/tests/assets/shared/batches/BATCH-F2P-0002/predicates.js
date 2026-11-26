'use strict';

// Static (side-agnostic) predicate evaluators for BATCH-F2P-0002.
//
// Every evaluator answers the SAME question the target-side dynamic probe
// answers: "does this repository satisfy the target Fastify postcondition?".
// On the pre-migration source tree every predicate is false, on the migrated
// target tree every predicate is true.

const path = require('path');
const { analyzeServerJs, readJson } = require('./harness');

function packageDeps(root) {
  const pkg = readJson(path.join(root, 'package.json'));
  return Object.assign({}, pkg.dependencies, pkg.devDependencies);
}

function evaluateStatic(root, options) {
  const a = analyzeServerJs(root);
  // The integration runner observes the actual Fastify instance. Prefer that
  // evidence to route spelling: route() and registration helpers are valid too.
  if (options && typeof options.fastifyOwnership === 'boolean') {
    a.fastifyOwns = options.fastifyOwnership;
  }
  const deps = packageDeps(root);
  const text = a.text;
  const results = {};

  // Behavioural capability anchors: the contract requires the *capability*
  // (serve /api/doc, handle multipart uploads), not a particular plugin module.
  const STATIC_SERVE = /(?:createReadStream|sendFile|serveStatic|@fastify\/static|\bstatic\b)/;
  const MULTIPART_HANDLING = /(?:multipart|\.file\s*\(|busboy|addContentTypeParser)/i;
  const APIDOC_PATH = /['"]\/api\/doc\/?['"]/;

  const bodyLimit = /bodyLimit\s*:\s*(?:102400|100\s*\*\s*1024)/.test(text);
  results.P_BODY_LIMIT = {
    satisfied: bodyLimit,
    evidence: [`bodyLimit:102400=${bodyLimit}`],
  };

  results.P_MALFORMED_JSON = {
    satisfied: /setErrorHandler/.test(text)
      && /\.type\(\s*['"]text\/plain(?:\s*;\s*charset=[^'"]*)?['"]\s*\)/.test(text)
      && /400/.test(text),
    evidence: ['setErrorHandler+text/plain(+charset)+400'],
  };

  const filesIndex = a.routes['GET /files'];
  const wildcardIndex = a.routes['GET /*'];
  results.P_CATCHALL_PRECEDENCE = {
    satisfied: wildcardIndex !== undefined
      && filesIndex !== undefined
      && filesIndex < wildcardIndex
      && !/express\.get\(\s*['"]\*['"]/.test(text),
    evidence: [`GET /files@${filesIndex}`, `GET /*@${wildcardIndex}`],
  };

  results.P_APIDOC = {
    satisfied: a.fastifyOwns
      && (a.registersStatic || STATIC_SERVE.test(text))
      && APIDOC_PATH.test(text),
    evidence: ['fastifyOwns + static serve (plugin|stream|sendFile) + /api/doc'],
  };

  results.P_FILES_MULTIPART = {
    satisfied: a.fastifyOwns
      && (a.registersMultipart || MULTIPART_HANDLING.test(text))
      && /[!=]==?\s*['"]video['"]|['"]video['"]\s*[!=]==?/.test(text)
      && /\\\.\(mp4\|avi\|mov\)\$/.test(text),
    evidence: ['fastifyOwns + multipart handling (plugin|parser) + video field filter'],
  };

  results.P_NEXT_HIJACK = {
    satisfied: wildcardIndex !== undefined
      && /\.hijack\(\)/.test(text),
    evidence: ['wildcard /* + hijack'],
  };

  results.P_FASTIFY_OWNS = {
    satisfied: a.fastifyOwns,
    evidence: [`routes=${Object.keys(a.routes).length}`, `expressRoutes=${Object.keys(a.expressRoutes).length}`],
  };

  const okSends = (text.match(/\.send\(\s*(?:String\(\s*)?['"]OK['"]|\.send\(\s*(?:http\.)?STATUS_CODES\[\s*200\s*\]|sendStatus\w*\(\s*[^)]*,\s*200\s*\)/g) || []).length;
  results.P_SIDE_EFFECT_BODY = {
    satisfied: okSends >= 3 && !/sendStatus\(200\)/.test(text),
    evidence: [`OK-equivalent send count=${okSends}`],
  };

  results.P_COUNTER_404_500 = {
    satisfied: /\.send\(\s*(?:String\(\s*)?['"]Not Found['"]|(?:http\.)?STATUS_CODES\[\s*404\s*\]|sendStatus\w*\(\s*[^)]*,\s*404\s*\)/.test(text)
      && /\.send\(\s*(?:String\(\s*)?['"]Internal Server Error['"]|(?:http\.)?STATUS_CODES\[\s*500\s*\]|sendStatus\w*\(\s*[^)]*,\s*500\s*\)/.test(text),
    evidence: ["'Not Found'/'Internal Server Error' (literal | STATUS_CODES | sendStatus)"],
  };

  results.P_PLUGINS_REGISTERED = {
    satisfied: a.fastifyOwns
      && Boolean(deps.fastify)
      && (a.registersStatic || STATIC_SERVE.test(text))
      && (a.registersMultipart || MULTIPART_HANDLING.test(text)),
    evidence: [
      `fastifyOwns=${a.fastifyOwns}`,
      `dep fastify=${Boolean(deps.fastify)}`,
      'required capabilities: static serve + multipart handling',
    ],
  };

  const expressRequires = Object.keys(a.requires).filter(
    (k) => ['express', 'serve-static', 'body-parser', 'csv-express', 'server-sent-events', 'multer'].includes(k) && a.requires[k],
  );
  results.P_NO_EXPRESS_IMPORT = {
    satisfied: expressRequires.length === 0 && a.requires.fastify,
    evidence: [`expressRequires=${JSON.stringify(expressRequires)}`],
  };

  results.P_NO_DELEGATION = {
    satisfied: a.fastifyOwns && !a.delegation,
    evidence: [`fastifyOwns=${a.fastifyOwns}`, `delegation=${a.delegation}`],
  };

  return results;
}

module.exports = { evaluateStatic, packageDeps };

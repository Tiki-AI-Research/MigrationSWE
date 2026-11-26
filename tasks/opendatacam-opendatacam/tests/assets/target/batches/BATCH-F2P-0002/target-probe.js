'use strict';

// Target-side probe for BATCH-F2P-0002.
//
//   node target-probe.js [targetRoot] [sourceRoot] [reportPath]
//
// Boots the real /input/target `server.js` with only `next`, the Mongo manager,
// the filesystem manager and the YOLO subprocess manager stubbed, then drives
// the HTTP surface to decide every Fastify target postcondition. Nothing is
// declared PASS without an actual observed response from that booted server.

const path = require('path');
const fs = require('fs');
const h = require('../../../shared/batches/BATCH-F2P-0002/harness');
const { evaluateStatic } = require('../../../shared/batches/BATCH-F2P-0002/predicates');
const contracts = require('../../../shared/batches/BATCH-F2P-0002/contracts.json');
const fixtures = require('../../../fixtures/batches/BATCH-F2P-0002/requests.json');

function contentTypeOf(res) {
  return String(res.headers['content-type'] || '');
}

async function checkBodyLimit(port) {
  const small = await h.jsonRequest(port, 'POST', '/ui', { pad: 'x'.repeat(90000) });
  const large = await h.jsonRequest(port, 'POST', '/ui', { pad: 'x'.repeat(110000) });
  return {
    satisfied: small.statusCode === 200 && large.statusCode !== 200,
    evidence: [`90kB->${small.statusCode}`, `110kB->${large.statusCode}`],
  };
}

async function checkMalformed(port) {
  const res = await h.jsonRequestRaw(port, 'POST', '/counter/areas', '{"');
  return {
    satisfied: res.statusCode === 400 && /text\/plain/.test(contentTypeOf(res)),
    evidence: [`malformed->${res.statusCode}`, `content-type=${contentTypeOf(res)}`],
  };
}

async function checkCatchall(port, staticOk) {
  const res = await h.httpRequest({ port, method: 'GET', path: '/files' });
  const isJson = /application\/json/.test(contentTypeOf(res));
  const isNext = /next-page/.test(res.text);
  return {
    satisfied: staticOk && res.statusCode === 200 && isJson && !isNext,
    evidence: [`static=${staticOk}`, `GET /files->${res.statusCode}`, `json=${isJson}`, `nextPage=${isNext}`],
  };
}

async function checkApidoc(port) {
  const bare = await h.httpRequest({ port, method: 'GET', path: '/api/doc' });
  const index = await h.httpRequest({ port, method: 'GET', path: '/api/doc/index.html' });
  return {
    satisfied: bare.statusCode === 200 && /apidoc-index/.test(bare.text)
      && index.statusCode === 200 && /apidoc-index/.test(index.text),
    evidence: [`GET /api/doc->${bare.statusCode}`, `GET /api/doc/index.html->${index.statusCode}`],
  };
}

async function checkFilesUpload(port, uploadDir) {
  const valid = h.multipartBody('video', 'demo.mp4', 'f2p-video-bytes', 'video/mp4');
  const validRes = await h.httpRequest({
    port,
    method: 'POST',
    path: '/files',
    headers: {
      'Content-Type': `multipart/form-data; boundary=${valid.boundary}`,
      'Content-Length': valid.body.length,
    },
    body: valid.body,
  });
  const stored = path.join(uploadDir, 'demo.mp4');
  const storedOk = fs.existsSync(stored) && fs.readFileSync(stored, 'utf8') === 'f2p-video-bytes';

  const wrongField = h.multipartBody('file', 'demo.mp4', 'x', 'video/mp4');
  const wrongFieldRes = await h.httpRequest({
    port,
    method: 'POST',
    path: '/files',
    headers: {
      'Content-Type': `multipart/form-data; boundary=${wrongField.boundary}`,
      'Content-Length': wrongField.body.length,
    },
    body: wrongField.body,
  });

  const wrongName = h.multipartBody('video', 'demo.txt', 'x', 'text/plain');
  const wrongNameRes = await h.httpRequest({
    port,
    method: 'POST',
    path: '/files',
    headers: {
      'Content-Type': `multipart/form-data; boundary=${wrongName.boundary}`,
      'Content-Length': wrongName.body.length,
    },
    body: wrongName.body,
  });

  return {
    satisfied: validRes.statusCode === 200 && storedOk
      && wrongFieldRes.statusCode === 500 && wrongNameRes.statusCode === 500,
    evidence: [
      `valid->${validRes.statusCode}`,
      `storedBytes=${storedOk}`,
      `wrongField->${wrongFieldRes.statusCode}`,
      `wrongName->${wrongNameRes.statusCode}`,
    ],
  };
}

async function checkNextHijack(port, nextLog) {
  const res = await h.httpRequest({ port, method: 'GET', path: '/a-page-that-does-not-exist' });
  const entry = nextLog.find((e) => e.url && e.url.indexOf('/a-page-that-does-not-exist') === 0);
  return {
    satisfied: res.statusCode === 200 && /next-page/.test(res.text)
      && Boolean(entry) && entry.hasGet === true && entry.protocol !== undefined,
    evidence: [
      `status=${res.statusCode}`,
      `hasGet=${entry && entry.hasGet}`,
      `protocol=${entry && entry.protocol}`,
    ],
  };
}

async function checkSideEffectBody(port) {
  const targets = [
    ['GET', '/start', undefined],
    ['GET', '/recording/start', undefined],
    ['GET', '/recording/stop', undefined],
    ['POST', '/counter/areas', { countingAreas: {} }],
    ['POST', '/ui', { counterEnabled: true }],
  ];
  const evidence = [];
  let ok = true;
  for (const [method, routePath, payload] of targets) {
    // eslint-disable-next-line no-await-in-loop
    const res = await h.jsonRequest(port, method, routePath, payload);
    const good = res.statusCode === 200 && res.text === 'OK' && /text\/plain/.test(contentTypeOf(res));
    if (!good) ok = false;
    evidence.push(`${method} ${routePath}->${res.statusCode}:${JSON.stringify(res.text)}:${contentTypeOf(res)}`);
  }
  return { satisfied: ok, evidence };
}

async function checkCounter(boot) {
  boot.dbControl.mode = 'empty';
  boot.dbControl.data = {};
  const empty = await h.httpRequest({ port: boot.port, method: 'GET', path: '/recording/f2p-probe-id/counter' });

  boot.dbControl.mode = 'reject';
  const failure = await h.httpRequest({ port: boot.port, method: 'GET', path: '/recording/f2p-probe-id/counter' });
  boot.dbControl.mode = 'empty';

  return {
    satisfied: empty.statusCode === 404 && empty.text === 'Not Found'
      && failure.statusCode === 500 && failure.text === 'Internal Server Error',
    evidence: [`empty->${empty.statusCode}:${JSON.stringify(empty.text)}`, `reject->${failure.statusCode}:${JSON.stringify(failure.text)}`],
  };
}

async function main() {
  const root = process.argv[2] || '/input/target';
  const sourceRoot = process.argv[3] || '/input/source';
  const reportPath = process.argv[4] || null;

  const staticResults = evaluateStatic(root);
  const scope = h.diffScope(sourceRoot, root);
  staticResults.P_CHANGE_SCOPE = {
    satisfied: scope.unexpected.length === 0,
    evidence: [`unexpected=${JSON.stringify(scope.unexpected)}`],
  };

  const evaluated = Object.assign({}, staticResults);
  let boot = null;
  let bootError = null;

  const localModules = path.join(root, 'node_modules');
  const nodeModules = fs.existsSync(localModules)
    ? localModules
    : '/opt/migration/dependencies/node_modules';

  try {
    boot = await h.bootServer(root, { nodeModules });
    const port = boot.port;

    evaluated.P_BODY_LIMIT = await checkBodyLimit(port);
    evaluated.P_MALFORMED_JSON = await checkMalformed(port);
    evaluated.P_CATCHALL_PRECEDENCE = await checkCatchall(port, staticResults.P_CATCHALL_PRECEDENCE.satisfied);
    evaluated.P_APIDOC = await checkApidoc(port);
    evaluated.P_FILES_MULTIPART = await checkFilesUpload(port, boot.uploadDir);
    evaluated.P_NEXT_HIJACK = await checkNextHijack(port, boot.nextLog);
    evaluated.P_SIDE_EFFECT_BODY = await checkSideEffectBody(port);
    evaluated.P_COUNTER_404_500 = await checkCounter(boot);

    const status = await h.httpRequest({ port, method: 'GET', path: '/status' });
    evaluated.P_FASTIFY_OWNS = {
      satisfied: staticResults.P_FASTIFY_OWNS.satisfied && status.statusCode === 200,
      evidence: [
        ...staticResults.P_FASTIFY_OWNS.evidence,
        `booted GET /status->${status.statusCode}`,
      ],
    };
  } catch (err) {
    bootError = err;
  }

  const observations = contracts.observations.map((obs) => {
    const r = evaluated[obs.predicate_id];
    if (!r) {
      return {
        observation_id: obs.id,
        requirement_ids: obs.requirement_ids,
        predicate_id: obs.predicate_id,
        status: 'INCONCLUSIVE',
        evidence: bootError ? [`boot error: ${bootError.message}`] : ['predicate not evaluated'],
      };
    }
    return {
      observation_id: obs.id,
      requirement_ids: obs.requirement_ids,
      predicate_id: obs.predicate_id,
      status: r.satisfied ? 'PASS' : (bootError ? 'INCONCLUSIVE' : 'FAIL'),
      evidence: r.evidence,
    };
  });

  const report = {
    batch_id: contracts.batch_id,
    side: 'target',
    root,
    source_root: sourceRoot,
    source_technology: contracts.migration.source,
    target_technology: contracts.migration.target,
    boot_error: bootError ? bootError.message : null,
    observations,
  };

  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  if (reportPath) fs.writeFileSync(reportPath, serialized);
  process.stdout.write(serialized);
  process.exit(0);
}

main().catch((err) => {
  process.stderr.write(`target-probe error: ${err.stack}\n`);
  process.exit(1);
});

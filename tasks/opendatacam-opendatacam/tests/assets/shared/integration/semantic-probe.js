'use strict';

// Run in a fresh process: exercise public contracts through real HTTP, without
// making the spelling of a helper, route registration or parser part of a grade.
const fs = require('fs');
const http = require('http');
const path = require('path');
const crypto = require('crypto');
const zlib = require('zlib');
const { isDeepStrictEqual } = require('util');
const { bootServer } = require('./boot');
const { request, multipartBody } = require('./lib');

async function run(root) {
  const frames = [crypto.randomBytes(91), crypto.randomBytes(127)];
  const upstream = http.createServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'multipart/x-mixed-replace; boundary=semanticframe' });
    frames.forEach((frame) => {
      res.write('--semanticframe\r\nContent-Type: image/jpeg\r\nContent-Length: '
        + frame.length + '\r\n\r\n');
      res.write(frame);
      res.write('\r\n');
    });
    res.end();
  });
  await new Promise((resolve) => upstream.listen(0, resolve));
  const boot = await bootServer(root, { audit: true, mjpegPort: upstream.address().port });
  const results = {};
  const check = async (name, action) => {
    try {
      const result = await action();
      results[name] = result && typeof result.satisfied === 'boolean'
        ? result : { satisfied: Boolean(result) };
    }
    catch (error) { results[name] = { satisfied: false, error: String(error.message || error) }; }
  };
  const req = (method, url, body, headers) => request({ port: boot.port, method,
    path: url, body, headers: headers || {}, timeoutMs: 2500 });
  const json = (method, url, body, extra) => {
    const data = Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
    return req(method, url, data, Object.assign({ 'Content-Type': 'application/json',
      'Content-Length': data.length }, extra));
  };
  const parse = (res) => JSON.parse(res.body.toString());
  const errorPage = (res, status) => res.statusCode === status
    && /^text\/html\b/i.test(res.headers['content-type'] || '')
    && /<pre>/i.test(res.body.toString())
    && res.body.toString().includes(http.STATUS_CODES[status]);

  await check('technology', () => boot.instances.length > 0
    && boot.frameworkVersions.includes('4.28.1'));
  await check('listen', () => boot.instances.some((instance) => {
    const address = instance.server.address();
    return address && address.port === boot.port && ['0.0.0.0', '::'].includes(address.address);
  }));
  await check('mjpeg', async () => {
    const res = await req('GET', '/webcam/stream');
    return res.statusCode === 200 && /multipart\/x-mixed-replace/i.test(res.headers['content-type'] || '')
      && frames.every((frame) => res.body.includes(frame));
  });
  await check('console', () => new Promise((resolve) => {
    const marker = 'console-' + crypto.randomBytes(8).toString('hex');
    console.log(marker);
    let text = '';
    let response;
    const timer = setTimeout(() => { stream.destroy(); resolve(false); }, 2500);
    const stream = http.get({ host: '127.0.0.1', port: boot.port, path: '/console' }, (res) => {
      response = res;
      res.on('data', (chunk) => {
        text += chunk.toString();
        if (text.includes(marker)) {
          clearTimeout(timer);
          stream.destroy();
          resolve(response.statusCode === 200);
        }
      });
    });
    stream.on('error', () => { clearTimeout(timer); resolve(false); });
  }));
  await check('json', async () => {
    const value = { counterEnabled: true, payload: 'x'.repeat(90 * 1024) };
    const valid = await json('POST', '/ui', value);
    const read = await req('GET', '/ui');
    const large = await json('POST', '/ui', { payload: 'x'.repeat(110 * 1024) });
    const malformed = await json('POST', '/ui', '{');
    const primitive = await json('POST', '/ui', '123');
    const getBody = await json('GET', '/config', '{');
    const empty = await json('POST', '/ui', '');
    const compressed = zlib.gzipSync(Buffer.from(JSON.stringify({ counterEnabled: false })));
    const gzip = await req('POST', '/ui', compressed, { 'Content-Type': 'application/json',
      'Content-Encoding': 'gzip', 'Content-Length': compressed.length });
    return valid.statusCode === 200 && parse(read).payload === value.payload
      && errorPage(large, 413) && errorPage(malformed, 400) && errorPage(primitive, 400)
      && errorPage(getBody, 400) && empty.statusCode === 200 && gzip.statusCode === 200;
  });
  await check('routing', async () => {
    // Express's default router matches case-insensitively and permits a final
    // slash. These API requests must still reach the API before the Next route.
    const responses = [];
    for (const route of ['/status', '/config']) {
      const expected = await req('GET', route);
      for (const url of [route + '/', route.toUpperCase()]) {
        const before = boot.calls.length;
        const actual = await req('GET', url);
        const matched = actual.statusCode === expected.statusCode
          && /^application\/json\b/i.test(actual.headers['content-type'] || '')
          && !boot.calls.slice(before).some((call) => call[0] === 'next')
          && isDeepStrictEqual(parse(actual), parse(expected));
        responses.push({ url, statusCode: actual.statusCode, matched });
      }
    }
    // A semicolon is part of an Express route path, not a query delimiter.
    // Unknown paths must therefore reach Next rather than an API prefix.
    const suffix = crypto.randomBytes(8).toString('hex');
    for (const route of ['/status', '/config']) {
      const url = route + ';' + suffix;
      const before = boot.calls.length;
      const actual = await req('GET', url);
      const matched = actual.statusCode === 200
        && actual.body.toString().includes('next-page')
        && boot.calls.slice(before).some((call) => call[0] === 'next'
          && call[4] && call[4].url === url);
      responses.push({ url, statusCode: actual.statusCode, matched });
    }
    // Express did not impose Fastify's default 100-character parameter limit.
    const id = 'route-' + crypto.randomBytes(72).toString('hex');
    const before = boot.calls.length;
    const actual = await req('GET', '/recording/' + id + '/counter');
    const matched = actual.statusCode === 404 && boot.calls.slice(before)
      .some((call) => call[0] === 'counterHistory' && call[1] === id);
    responses.push({ parameterLength: id.length, statusCode: actual.statusCode, matched });
    return { satisfied: responses.every((response) => response.matched), responses };
  });
  await check('next', async () => {
    const marker = crypto.randomBytes(12).toString('hex');
    const url = '/semantic-next-page?name=' + marker
      + '&filter[name]=' + marker + '&tags[]=one&tags[]=two'
      + '&duplicate=first&duplicate=second';
    const home = await req('GET', '/');
    // bodyParser.json() parsed GET bodies before the Express catch-all too.
    // Next receives that parsed body and query on the raw HTTP request, rather
    // than only on Fastify's wrapper. Header helpers alone do not preserve it.
    const other = await json('GET', url, { probe: marker });
    const forwarded = boot.calls.filter((call) => call[0] === 'next');
    const received = forwarded.find((call) => call[4] && call[4].url === url);
    const contract = received && received[4];
    const preserved = contract && contract.method === 'GET'
      && contract.body && contract.body.probe === marker
      && contract.query && contract.query.name === marker;
    // Express 4's default query parser is extended: brackets become objects
    // and arrays before the unchanged Next handler receives the request.
    const extendedQueryPreserved = Boolean(contract && contract.query
      && isDeepStrictEqual(JSON.parse(JSON.stringify(contract.query.filter || null)), { name: marker })
      && isDeepStrictEqual(contract.query.tags, ['one', 'two'])
      && isDeepStrictEqual(contract.query.duplicate, ['first', 'second']));
    return { satisfied: Boolean(home.statusCode === 200 && other.statusCode === 200
      && home.body.toString().includes('next-page') && other.body.toString().includes('next-page')
      && forwarded.length >= 2 && preserved && extendedQueryPreserved),
    homeStatus: home.statusCode, catchallStatus: other.statusCode,
    parsedBodyPreserved: Boolean(contract && contract.body && contract.body.probe === marker),
    parsedQueryPreserved: Boolean(contract && contract.query && contract.query.name === marker),
    extendedQueryPreserved };
  });
  await check('actions', async () => {
    const before = boot.calls.length;
    for (const url of ['/start', '/recording/start', '/recording/stop']) {
      const res = await req('GET', url);
      if (res.statusCode !== 200 || !['', http.STATUS_CODES[200]].includes(res.body.toString())) return false;
    }
    const areas = { testArea: { name: 'semantic area' } };
    const res = await json('POST', '/counter/areas', { countingAreas: areas });
    const read = await req('GET', '/counter/areas');
    const ui = await json('POST', '/ui', { counterEnabled: true });
    const calls = boot.calls.slice(before).map((call) => call[0]);
    return res.statusCode === 200 && ui.statusCode === 200
      && ['', http.STATUS_CODES[200]].includes(res.body.toString())
      && ['', http.STATUS_CODES[200]].includes(ui.body.toString())
      && JSON.stringify(parse(read)) === JSON.stringify(areas)
      && ['yoloStart', 'listenToYOLO', 'startRecording', 'stopRecording', 'areas', 'ui']
        .every((name) => calls.includes(name));
  });
  await check('counter', async () => {
    boot.setCounterData({});
    const missing = await req('GET', '/recording/semantic-id/counter');
    boot.dbControl.mode = 'reject';
    const failure = await req('GET', '/recording/semantic-id/counter');
    boot.dbControl.mode = 'empty';
    return missing.statusCode === 404 && failure.statusCode === 500;
  });
  await check('csv', async () => {
    boot.setCounterData({ dateStart: new Date('2020-01-01'), areas: { a: { name: 'Area' } },
      counterHistory: [[{ timestamp: new Date('2020-01-02'), area: 'a', name: 'car', id: 1,
        bearing: 0, countingDirection: 'rightleft_bottomtop' }]] });
    const res = await req('GET', '/recording/semantic-id/counter/csv');
    return res.statusCode === 200 && res.body.toString().split(/\r?\n/)[0]
      === 'timestamp,area,name,id,bearing,countingDirection'
      && /^text\/csv\b/i.test(res.headers['content-type'] || '')
      && /attachment/i.test(res.headers['content-disposition'] || '');
  });
  await check('static', async () => {
    fs.writeFileSync(path.join(boot.apidocRoot, 'semantic.txt'), 'static contract');
    let index = await req('GET', '/api/doc');
    if ([301, 302, 307, 308].includes(index.statusCode)) index = await req('GET', index.headers.location);
    const asset = await req('GET', '/api/doc/semantic.txt');
    return index.statusCode === 200 && index.body.toString().includes('apidoc-index')
      && asset.statusCode === 200 && asset.body.toString() === 'static contract';
  });
  await check('multipart', async () => {
    // Multer's original disk storage has no video file size limit. This also
    // detects an accidentally inherited Fastify multipart default (1 MiB).
    const data = crypto.randomBytes(1024 * 1024 + 37);
    for (const extension of ['mp4', 'avi', 'mov']) {
      const filename = 'semantic-' + crypto.randomBytes(4).toString('hex') + '.' + extension;
      const part = multipartBody('video', filename, data, 'video/mp4');
      const res = await req('POST', '/files', part.body, { 'Content-Type':
        'multipart/form-data; boundary=' + part.boundary, 'Content-Length': part.body.length });
      if (res.statusCode !== 200) return { satisfied: false, stage: 'upload-response',
        filename, statusCode: res.statusCode, error: res.error || null,
        timedOut: res.timedOut === true };
      const stored = parse(res);
      if (stored !== path.join(boot.uploadDir, filename)) return { satisfied: false,
        stage: 'stored-path', filename, expected: path.join(boot.uploadDir, filename), actual: stored };
      const bytes = fs.readFileSync(stored);
      if (!bytes.equals(data)) return { satisfied: false, stage: 'stored-bytes',
        filename, expectedSize: data.length, actualSize: bytes.length };
    }
    for (const [field, filename] of [['other', 'video.mp4'], ['video', 'video.exe']]) {
      const part = multipartBody(field, filename, Buffer.from('video'));
      const res = await req('POST', '/files', part.body, { 'Content-Type':
        'multipart/form-data; boundary=' + part.boundary, 'Content-Length': part.body.length });
      if (res.statusCode !== 500) return { satisfied: false, stage: 'invalid-upload-response',
        field, filename, statusCode: res.statusCode, error: res.error || null,
        timedOut: res.timedOut === true };
    }
    // The former multer.single('video') rejects a second file. request.file()
    // returns only the first part, so a files:1 limit alone cannot enforce this:
    // the handler must observe completion/errors from the remaining parts.
    const boundary = 'semantic-' + crypto.randomBytes(12).toString('hex');
    const body = Buffer.from(['first.mp4', 'second.mp4'].map((filename) =>
      '--' + boundary + '\r\nContent-Disposition: form-data; name="video"; filename="'
      + filename + '"\r\nContent-Type: video/mp4\r\n\r\nvideo-data\r\n').join('')
      + '--' + boundary + '--\r\n');
    const before = boot.calls.length;
    const multiple = await req('POST', '/files', body, { 'Content-Type':
      'multipart/form-data; boundary=' + boundary, 'Content-Length': body.length });
    const restarted = boot.calls.slice(before).some((call) =>
      call[0] === 'stopRecording' || call[0] === 'yoloStop' || call[0] === 'yoloStart');
    if (multiple.statusCode !== 500 || restarted) return { satisfied: false,
      stage: 'multiple-files', statusCode: multiple.statusCode, restarted,
      error: multiple.error || null, timedOut: multiple.timedOut === true };
    return true;
  });
  await check('sse', () => new Promise((resolve) => {
    let text = '';
    const timer = setTimeout(() => { stream.destroy(); resolve(false); }, 3000);
    const stream = http.get({ host: '127.0.0.1', port: boot.port, path: '/tracker/sse' }, (res) => {
      res.on('data', (chunk) => {
        text += chunk.toString();
        const match = /data:\s*(\{[^\n]+\})\n\n/.exec(text);
        if (!match) return;
        clearTimeout(timer);
        stream.destroy();
        try {
          const event = JSON.parse(match[1]);
          resolve(res.statusCode === 200 && /^text\/event-stream\b/i.test(res.headers['content-type'] || '')
            && ['videoResolution', 'trackerDataForLastFrame', 'appState', 'counterSummary', 'trackerSummary']
              .every((key) => Object.prototype.hasOwnProperty.call(event, key)));
        } catch (error) { resolve(false); }
      });
    });
    stream.on('error', () => { clearTimeout(timer); resolve(false); });
  }));
  boot.restore();
  upstream.close();
  return results;
}

if (require.main === module) {
  // Candidate banners/intercept-stdout must not corrupt the result channel.
  process.stdout.write = () => true;
  run(process.argv[2]).then((results) => {
    fs.writeSync(1, '\nSEMANTIC_RESULT=' + JSON.stringify(results) + '\n');
    process.exit(0);
  }, (error) => {
    fs.writeSync(1, '\nSEMANTIC_RESULT=' + JSON.stringify({ error: String(error.stack || error) }) + '\n');
    process.exit(1);
  });
}
module.exports = { run };

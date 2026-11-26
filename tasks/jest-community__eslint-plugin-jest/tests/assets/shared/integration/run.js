'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { ensurePlugin, readJSON } = require('./probe-lib');
const { getProbe } = require('./probes');

const repoRoot = () => {
  for (const key of ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO']) {
    if (process.env[key]) {
      return path.resolve(process.env[key]);
    }
  }
  return '/workspace/repo';
};

const sanitize = (value, repo) => {
  let text;
  try {
    text = JSON.stringify(value);
  } catch {
    text = JSON.stringify({ error: 'unserializable observation value' });
  }
  const replacements = [
    [path.resolve(repo), '<repo>'],
    [os.tmpdir(), '<tmp>'],
    [process.cwd(), '<cwd>'],
  ];
  for (const [from, to] of replacements) {
    if (from && from !== '/') {
      text = text.split(from).join(to);
    }
  }
  try {
    return JSON.parse(text);
  } catch {
    return { value: text };
  }
};

const run = async options => {
  const side = options.side;
  const assets = options.assets;
  const reportFile = options.report;
  const repo = repoRoot();
  const inventory = readJSON(path.join(assets, 'shared/integration/observations.json'));
  let loaded;
  const observations = [];
  try {
    loaded = ensurePlugin(repo);
  } catch (error) {
    loaded = null;
    for (const observation of inventory) {
      observations.push({
        id: observation.id,
        kind: observation.kind,
        status: 'INCONCLUSIVE',
        value: {
          type: 'json',
          value: {
            error: String(error && error.message ? error.message : error),
            repo,
            side,
          },
        },
      });
    }
    const report = { schema_version: 'migration-observations:v6', observations };
    writeReport(reportFile, report);
    return report;
  }

  for (const observation of inventory) {
    const probe = getProbe(observation.id);
    if (!probe) {
      observations.push({
        id: observation.id,
        kind: observation.kind,
        status: 'INCONCLUSIVE',
        value: { type: 'json', value: { error: 'probe not implemented' } },
      });
      continue;
    }
    try {
      const outcome = await probe(repo, loaded.plugin, loaded.root);
      observations.push({
        id: observation.id,
        kind: observation.kind,
        status: outcome.ok ? 'PASS' : 'FAIL',
        value: { type: 'json', value: sanitize(outcome.value, repo) },
      });
    } catch (error) {
      observations.push({
        id: observation.id,
        kind: observation.kind,
        status:
          observation.kind === 'F2P' && side === 'source' ? 'FAIL' : 'INCONCLUSIVE',
        value: {
          type: 'json',
          value: sanitize(
            { error: String(error && error.message ? error.message : error) },
            repo,
          ),
        },
      });
    }
  }
  const report = { schema_version: 'migration-observations:v6', observations };
  writeReport(reportFile, report);
  return report;
};

const writeReport = (file, report) => {
  if (!file) {
    return;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(report, null, 2)}\n`);
};

module.exports = { run, repoRoot, sanitize };

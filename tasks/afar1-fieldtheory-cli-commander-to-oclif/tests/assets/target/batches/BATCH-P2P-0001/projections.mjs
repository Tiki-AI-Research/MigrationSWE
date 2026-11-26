import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const repo = path.resolve(process.argv[2] || process.cwd());
const load = (rel) => import(pathToFileURL(path.join(repo, rel)).href);
const values = {};

function tmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

async function withEnv(key, value, fn) {
  const saved = process.env[key];
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
  try {
    return await fn();
  } finally {
    if (saved === undefined) delete process.env[key];
    else process.env[key] = saved;
  }
}

async function guard(id, fn) {
  try {
    values[id] = { ok: true, value: await fn() };
  } catch (error) {
    values[id] = {
      ok: false,
      error: String(error?.stack || error?.message || error).split(repo).join('<repo>').slice(0, 1200),
    };
  }
}

function shEngine(name, script) {
  return {
    name,
    label: name,
    config: { bin: '/bin/sh', args: () => ['-c', script] },
  };
}

function mockPrompter(answers) {
  const queue = [...answers];
  return {
    ask: async (question) => {
      if (queue.length === 0) throw new Error(`ran out of answers at ${question}`);
      return queue.shift();
    },
    write: () => {},
  };
}

await guard('P2P-007', async () => {
  const { invokeEngineAsync } = await load('src/engine.ts');
  const stdout = await invokeEngineAsync(shEngine('fake-ok', 'printf "hello world\\n"'), 'ignored');
  return { stdout };
});

await guard('P2P-011', async () => {
  const dir = tmp('ft-proj-repos-');
  return withEnv('FT_DATA_DIR', dir, async () => {
    const { listSavedRepos } = await load('src/ideas-repos.ts');
    return { repos: listSavedRepos() };
  }).finally(() => fs.rmSync(dir, { recursive: true, force: true }));
});

await guard('P2P-001', async () => {
  const { stepPickDepth } = await load('src/possible-wizard.ts');
  return stepPickDepth(mockPrompter(['']));
});

await guard('P2P-012', async () => {
  const dir = tmp('ft-proj-seeds-');
  return withEnv('FT_DATA_DIR', dir, async () => {
    const { saveSeedFromCandidates } = await load('src/seeds-save.ts');
    const { readArtifact } = await load('src/adjacent/librarian.ts');
    const candidate = {
      id: '2040864758145167373',
      text: 'Inner tweet text here.',
      url: 'https://x.com/demo/status/1',
      authorHandle: '@demo',
      postedAt: '2026-04-05T12:00:00.000Z',
      bookmarkedAt: '2026-04-06T09:30:00.000Z',
      category: 'tool',
      domain: 'ai',
      folderNames: [],
    };
    const seed = await saveSeedFromCandidates({ candidates: [candidate], title: 'Content seed' });
    const artifact = readArtifact(seed.artifactIds[0]);
    return {
      content: artifact?.content ?? null,
      type: artifact?.type ?? null,
      kind: artifact?.metadata?.kind ?? null,
    };
  }).finally(() => fs.rmSync(dir, { recursive: true, force: true }));
});

await guard('P2P-023', async () => {
  const { formatSeedCandidates } = await load('src/seeds-query.ts');
  const output = formatSeedCandidates([{
    id: '123',
    text: 'A bookmark about agent reliability and memory systems',
    url: 'https://x.com/example/status/123',
    authorHandle: 'afar0x',
    postedAt: '2026-04-01T00:00:00.000Z',
    category: 'tool',
    domain: 'ai',
  }]);
  return { output };
});

await guard('P2P-005', async () => {
  const { stripWikiUpdatesSectionForTest } = await load('src/md-ask.ts');
  const before = 'Just an answer.';
  const after = stripWikiUpdatesSectionForTest(before);
  return { before, after, changed: before !== after };
});

await guard('P2P-006', async () => {
  const dir = tmp('ft-proj-jobs-');
  return withEnv('FT_DATA_DIR', dir, async () => {
    const jobs = await load('src/ideas-jobs.ts');
    const plan = {
      seedId: 'seed-1',
      repos: ['/tmp/repo-a', '/tmp/repo-b'],
      frameId: 'impact-effort',
      depth: 'quick',
      nodeTarget: 7,
    };
    const job = jobs.createIdeasJob(plan, { cwd: '/tmp/work' });
    const reloaded = jobs.readIdeasJob(job.id);
    const log = fs.readFileSync(job.logPath, 'utf8')
      .split('\n')
      .filter((line) => line.includes('queued:'))
      .map((line) => line.slice(line.indexOf('queued:')))
      .join('\n');
    return {
      status: job.status,
      cwd: job.cwd,
      repos: reloaded?.plan?.repos ?? null,
      log,
    };
  }).finally(() => fs.rmSync(dir, { recursive: true, force: true }));
});

await guard('P2P-008', async () => {
  const { MAX_CONSECUTIVE_FAILURES } = await load('src/md.ts');
  return {
    value: MAX_CONSECUTIVE_FAILURES,
    integer: Number.isInteger(MAX_CONSECUTIVE_FAILURES),
    inRange: MAX_CONSECUTIVE_FAILURES >= 3 && MAX_CONSECUTIVE_FAILURES <= 20,
  };
});

await guard('P2P-009', async () => {
  const { aggregateTopDots } = await load('src/ideas.ts');
  const dot = (score, title) => ({
    title,
    summary: `${title} summary`,
    rationale: `${title} rationale`,
    repoSurface: 'src/example.ts',
    effortEstimate: 'days',
    axisAScore: score,
    axisAJustification: 'a',
    axisBScore: score,
    axisBJustification: 'b',
    exportablePrompt: 'prompt body',
  });
  const entry = (runId, repoPath, dotId, score, title) => ({
    runId,
    repo: repoPath,
    dotArtifactId: dotId,
    dot: dot(score, title),
  });
  const top = aggregateTopDots([
    entry('r1', '/a', 'd1', 30, 'low'),
    entry('r1', '/a', 'd2', 90, 'high'),
    entry('r1', '/a', 'd3', 60, 'mid'),
  ], 3);
  return { titles: top.map((item) => item.dot.title) };
});

await guard('P2P-002', async () => {
  const { summarizeSeedIntent } = await load('src/seeds-strategies.ts');
  return {
    summary: summarizeSeedIntent('Recent seed', {
      category: 'tool',
      domain: 'ai',
      days: 30,
      limit: 5,
    }),
  };
});

await guard('P2P-003', async () => {
  const dir = tmp('ft-proj-prefs-');
  return withEnv('FT_DATA_DIR', dir, async () => {
    const { loadPreferences, savePreferences } = await load('src/preferences.ts');
    const empty = loadPreferences();
    savePreferences({ defaultEngine: 'claude' });
    const afterClaude = loadPreferences();
    savePreferences({ defaultEngine: 'codex' });
    const afterCodex = loadPreferences();
    return { empty, afterClaude, afterCodex };
  }).finally(() => fs.rmSync(dir, { recursive: true, force: true }));
});

await guard('P2P-004', async () => {
  const { mergeBookmarkRecord } = await load('src/graphql-bookmarks.ts');
  const makeRecord = (overrides) => ({
    id: '100',
    tweetId: '100',
    url: 'https://x.com/user/status/100',
    text: 'Test',
    syncedAt: '2026-01-01T00:00:00.000Z',
    tags: [],
    ingestedVia: 'graphql',
    ...overrides,
  });
  const existing = makeRecord({
    text: 'Rich',
    postedAt: '2026-01-01',
    author: { handle: 'user' },
    engagement: { likeCount: 10 },
    mediaObjects: [{ type: 'photo' }],
    links: ['https://example.com'],
  });
  const incoming = makeRecord({ text: 'Sparse' });
  const result = mergeBookmarkRecord(existing, incoming);
  return {
    text: result.text,
    author: result.author?.handle ?? null,
    mediaCount: result.mediaObjects?.length ?? 0,
    links: result.links ?? [],
  };
});

process.stdout.write(JSON.stringify(values));

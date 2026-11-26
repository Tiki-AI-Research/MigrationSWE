import { tmpdir } from 'node:os';
import path from 'node:path';

const cacheDir = process.env.VITEST_TMP_DIR
  ? path.join(process.env.VITEST_TMP_DIR, 'webvr-vitest-cache')
  : '/tmp/webvr-vitest-cache';

const repoRoot = process.env.EVAL_REPO_ROOT || '/workspace/repo';

export default {
  root: repoRoot,
  cacheDir,
  server: {
    fs: {
      allow: [repoRoot, '/tests/evaluator', cacheDir],
    },
  },
  test: {
    environment: 'jsdom',
    globals: false,
    fileParallelism: false,
    isolate: true,
    include: ['/tests/evaluator/*.test.js'],
    testTimeout: 10000,
    hookTimeout: 10000,
  },
};

import { runAll, side } from './index.mjs'

const repo =
  process.argv[2] ||
  process.env.MIGRATION_REPO ||
  process.env.MIGRATION_REPO_ROOT ||
  process.env.REPO ||
  process.env.REPO_ROOT ||
  '/workspace/repo'

const results = await runAll(repo)
console.log(JSON.stringify({ side, repo, results }, null, 2))
if (results.some((result) => !result.passed)) process.exitCode = 1

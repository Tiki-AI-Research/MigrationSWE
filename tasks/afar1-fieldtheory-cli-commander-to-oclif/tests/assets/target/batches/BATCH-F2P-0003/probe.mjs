import path from 'node:path';
import process from 'node:process';
import { runProbe, writeProbeReport } from '../../../shared/batches/BATCH-F2P-0003/probe.mjs';

const repo = path.resolve(process.argv[2] || process.env.FT_REPO_ROOT || '/workspace/repo');
const output = path.resolve(process.argv[3] || process.env.FT_PROBE_REPORT || '/logs/target-probe.json');
const report = runProbe(repo, 'target');
writeProbeReport(report, output);
const summary = report.results.reduce((counts, result) => {
  counts[result.status] = (counts[result.status] || 0) + 1;
  return counts;
}, {});
process.stdout.write(`${JSON.stringify({ batch_id: report.batch_id, side: 'target', repo, summary })}\n`);

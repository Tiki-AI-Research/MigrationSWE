import { writeFile } from "node:fs/promises";
import { runAll } from "../../../shared/batches/BATCH-DIFFERENTIAL-0001/cases.mjs";
import { subject } from "./subject.mjs";

const report = await runAll(subject);
const reportPath = process.argv[2];

if (reportPath) {
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
} else {
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

if (report.summary.failed > 0) {
  process.exitCode = 1;
}

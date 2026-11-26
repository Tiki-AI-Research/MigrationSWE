import { run } from '../../../shared/batches/BATCH-P2P-0001/harness.mjs';

const entry = process.env.DEVALUE_ENTRY ?? 'devalue';
const devalue = await import(entry);
const results = await run(devalue, {
	side: 'target',
	repoRoot: process.env.DEVALUE_ROOT ?? process.cwd()
});

process.stdout.write(JSON.stringify(results) + '\n');

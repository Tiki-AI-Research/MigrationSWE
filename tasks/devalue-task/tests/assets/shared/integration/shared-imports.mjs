import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

export const ASSET_ROOT = join(here, '..', '..');

export const SHARED_MODULES = [
	{ path: 'shared/batches/BATCH-P2P-0001/harness.mjs', exports: ['run'] },
	{
		path: 'shared/batches/BATCH-P2P-0002/observations.js',
		exports: ['createObservations', 'isDevalueError']
	},
	{
		path: 'shared/batches/BATCH-F2P-0001/dependency-resolution.js',
		exports: ['resolve_dependency_state', 'locate_repo_root']
	}
];

export async function import_shared(root = ASSET_ROOT, modules = SHARED_MODULES) {
	const results = [];

	for (const entry of modules) {
		const full = join(root, entry.path);

		if (!existsSync(full)) {
			results.push({ path: entry.path, status: 'missing', missing_exports: entry.exports });
			continue;
		}

		try {
			const loaded = await import(pathToFileURL(full).href);
			const missing_exports = entry.exports.filter((name) => !(name in loaded));

			results.push({
				path: entry.path,
				status: missing_exports.length === 0 ? 'ok' : 'missing-exports',
				missing_exports
			});
		} catch (error) {
			results.push({
				path: entry.path,
				status: 'error',
				error: String((error && error.message) || error)
			});
		}
	}

	return results;
}

import { batch_ids, missing_files, read_registry, runners_for } from './registry.mjs';
import { import_shared } from './shared-imports.mjs';

export async function run_side(root, side) {
	const registry = read_registry(root);

	const results = {
		batch_id: 'BATCH-INTEGRATION-0001',
		side,
		batches: batch_ids(registry),
		runners: registry.batches.flatMap((entry) => runners_for(entry, side)),
		missing: missing_files(root, registry),
		shared_imports: await import_shared(root)
	};

	results.ok =
		results.missing.length === 0 && results.shared_imports.every((entry) => entry.status === 'ok');

	return results;
}

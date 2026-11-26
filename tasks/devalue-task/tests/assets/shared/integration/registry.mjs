import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

export const ASSET_ROOT = join(here, '..', '..');

export function registry_path(root = ASSET_ROOT) {
	return join(root, 'fixtures', 'integration', 'dependency-batches.json');
}

export function read_registry(root = ASSET_ROOT) {
	return JSON.parse(readFileSync(registry_path(root), 'utf8'));
}

export function declared_files(entry) {
	return [
		...(entry.runners ?? []),
		...(entry.shared ?? []),
		...(entry.fixtures ?? []),
		...(entry.files ?? [])
	];
}

export function batch_ids(registry = read_registry()) {
	return registry.batches.map((entry) => entry.batch_id);
}

export function runners_for(entry, side) {
	const prefix = `${side}/`;
	return (entry.runners ?? []).filter((file) => file.startsWith(prefix));
}

export function required_files(registry = read_registry()) {
	const files = new Set();

	for (const entry of registry.batches) {
		for (const file of declared_files(entry)) files.add(file);
	}

	return [...files];
}

export function missing_files(root, registry = read_registry()) {
	const missing = [];

	for (const entry of registry.batches) {
		for (const file of declared_files(entry)) {
			if (!existsSync(join(root, file))) {
				missing.push({ batch_id: entry.batch_id, file });
			}
		}
	}

	return missing;
}

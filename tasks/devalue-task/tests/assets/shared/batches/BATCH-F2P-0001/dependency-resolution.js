import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const PACKAGE_NAME = 'devalue';
export const TARGET_VERSION = '6.0.0';
export const OLD_VERSION = '4.3.3';
export const OLD_TEST_RUNNER = 'uvu';

export function locate_asset_root(start) {
	let dir = start;
	for (;;) {
		if (existsSync(join(dir, 'shared', 'batches', 'BATCH-F2P-0001', 'dependency-resolution.js'))) {
			return dir;
		}

		const parent = dirname(dir);

		if (parent === dir) {
			throw new Error(`could not locate BATCH-F2P-0001 asset root from ${start}`);
		}

		dir = parent;
	}
}

export function locate_repo_root(start) {
	let dir = start;
	for (;;) {
		const manifest_path = join(dir, 'package.json');

		if (existsSync(manifest_path)) {
			try {
				const manifest = JSON.parse(readFileSync(manifest_path, 'utf8'));

				if (manifest && manifest.name === PACKAGE_NAME) {
					return dir;
				}
			} catch {
				// keep walking up while a manifest is malformed
			}
		}

		const parent = dirname(dir);

		if (parent === dir) {
			throw new Error(`could not locate ${PACKAGE_NAME} package root from ${start}`);
		}

		dir = parent;
	}
}

export function read_fixture(asset_root) {
	const path = join(
		asset_root,
		'fixtures',
		'batches',
		'BATCH-F2P-0001',
		'expected-dependency-resolution.json'
	);

	return JSON.parse(readFileSync(path, 'utf8'));
}

export function declared_devalue_specifiers(manifest) {
	const fields = ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'];
	const found = [];

	for (const field of fields) {
		const deps = manifest[field];

		if (deps && typeof deps === 'object' && Object.hasOwn(deps, PACKAGE_NAME)) {
			found.push({ field, specifier: String(deps[PACKAGE_NAME]) });
		}
	}

	return found;
}

export function lockfile_devalue_versions(lockfile) {
	const versions = [];
	const pattern = /(?:^|\n)\s*devalue@([^\s:(]+)[:(]/g;
	let match;

	while ((match = pattern.exec(lockfile)) !== null) {
		versions.push(match[1]);
	}

	return versions;
}

export function resolve_dependency_state(root) {
	const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
	const lockfile_path = join(root, 'pnpm-lock.yaml');
	const lockfile = existsSync(lockfile_path) ? readFileSync(lockfile_path, 'utf8') : '';

	return {
		name: manifest.name,
		version: manifest.version,
		declared_devalue: declared_devalue_specifiers(manifest),
		lockfile_devalue: lockfile_devalue_versions(lockfile),
		lockfile_mentions_old_version: /devalue@4\.3\.3(?![0-9])/.test(lockfile)
	};
}

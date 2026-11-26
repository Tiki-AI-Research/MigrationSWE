import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));

async function load_helper() {
	let dir = here;

	for (;;) {
		const candidate = join(dir, 'shared', 'batches', 'BATCH-F2P-0001', 'dependency-resolution.js');

		if (existsSync(candidate)) {
			return import(pathToFileURL(candidate).href);
		}

		const parent = dirname(dir);

		if (parent === dir) {
			throw new Error(`could not locate shared/batches/BATCH-F2P-0001/dependency-resolution.js from ${here}`);
		}

		dir = parent;
	}
}

const helper = await load_helper();
const asset_root = helper.locate_asset_root(here);
const expected = helper.read_fixture(asset_root);

function repo_root() {
	const starts = [process.env.DEVALUE_REPO_ROOT, process.env.REPO_ROOT, process.cwd(), here].filter(Boolean);

	for (const start of starts) {
		try {
			return helper.locate_repo_root(start);
		} catch {
			// try the next candidate start directory
		}
	}

	throw new Error('could not locate the devalue package root; set DEVALVE_REPO_ROOT');
}

describe('R012: devalue 4.3.3 -> 6.0.0 dependency migration (target revision)', () => {
	it('package.json targets devalue 6.0.0', () => {
		const state = helper.resolve_dependency_state(repo_root());

		expect(state.name).toBe(expected.package);
		expect(state.version).toBe(expected.target_version);
	});

	it('package.json declares no devalue 4.3.3 dependency specifier', () => {
		const state = helper.resolve_dependency_state(repo_root());

		for (const entry of state.declared_devalue) {
			expect(entry.specifier).not.toContain(expected.forbidden_version);
		}
	});

	it('pnpm-lock.yaml resolves no devalue 4.3.3 entry', () => {
		const state = helper.resolve_dependency_state(repo_root());

		expect(state.lockfile_mentions_old_version).toBe(false);
		expect(state.lockfile_devalue).not.toContain(expected.forbidden_version);
	});
});

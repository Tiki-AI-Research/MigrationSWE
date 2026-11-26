/**
 * Public type-surface gates.
 *
 * The migrated package must ship TypeScript declarations for the 6.0.0 public
 * API, reachable through its own package entry when a consumer imports
 * `devalue`.  Each fixture is compiled by the evaluator-owned TypeScript
 * against a throw-away consumer package that resolves `devalue` to the
 * candidate root, so a fixture can only type-check when the candidate's
 * declared entry actually exports the required names with the required shape.
 *
 * A missing or unusable TypeScript toolchain or an unparsable
 * declaration surface is an ordinary gate failure. The outer evaluator owns
 * the execution timeout. It must never be reported
 * as an infrastructure error, because the type surface is candidate evidence.
 */
import compiler from '../../typescript.cjs';
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	symlinkSync,
	writeFileSync
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), 'type-surface');

export const TYPE_GATES = Object.freeze({
	'F2P-030': 'g1.ts',
	'F2P-031': 'g2.ts',
	'F2P-032': 'g3.ts',
	'F2P-033': 'g4.ts',
	'F2P-034': 'g5.ts',
	'F2P-035': 'g6.ts'
});

const FIXTURE_FILES = Object.values(TYPE_GATES);

function unavailable(reason) {
	return Object.fromEntries(
		FIXTURE_FILES.map((file) => [
			file,
			{
				ok: false,
				checks: {
					fixture: file,
					typescript: false,
					reason
				}
			}
		])
	);
}

function evaluate(repoRoot) {
	let ts;
	try { ts = compiler.loadTypeScript(); }
	catch { return unavailable('typescript-unavailable'); }
	if (!existsSync(join(repoRoot, 'package.json'))) {
		return unavailable('package-entry-unavailable');
	}

	let directory;
	try {
		directory = mkdtempSync(join(tmpdir(), 'devalue-type-surface-'));
	} catch {
		return unavailable('temporary-directory-unavailable');
	}

	try {
		for (const file of [...FIXTURE_FILES, 'tsconfig.json']) {
			writeFileSync(
				join(directory, file),
				readFileSync(join(FIXTURES, file), 'utf8'),
				'utf8'
			);
		}
		mkdirSync(join(directory, 'node_modules'), { recursive: true });
		symlinkSync(repoRoot, join(directory, 'node_modules', 'devalue'), 'dir');

		const config = ts.readConfigFile(join(directory, 'tsconfig.json'), ts.sys.readFile);
		if (config.error) return unavailable('typescript-config-unavailable');
		const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, directory);
		const program = ts.createProgram(parsed.fileNames, parsed.options);
		const messages = [...parsed.errors, ...ts.getPreEmitDiagnostics(program)];
		const diagnostics = new Map(FIXTURE_FILES.map((file) => [file, 0]));
		let external = 0;
		for (const diagnostic of messages) {
			const file = FIXTURE_FILES.find(name => diagnostic.file &&
				resolve(diagnostic.file.fileName) === resolve(directory, name));
			if (file) diagnostics.set(file, diagnostics.get(file) + 1);
			else external += 1;
		}
		const module = compiler.importedModule(ts, program, program.getSourceFile(join(directory, 'g1.ts')));
		const opacity = compiler.fragmentOpacity(ts, program.getTypeChecker(), module);
		const failed = messages.length !== 0;
		return Object.fromEntries(
			FIXTURE_FILES.map((file) => [
				file,
				{
					ok: external === 0 && diagnostics.get(file) === 0 && (file !== 'g6.ts' || opacity.opaque),
					checks: {
						fixture: file,
						typescript: true,
						exit_code: failed ? 1 : 0,
						diagnostics: diagnostics.get(file),
						external_diagnostics: external,
						...(file === 'g6.ts' ? { fragment_opaque: opacity.opaque, opacity_reason: opacity.reason } : {})
					}
				}
			])
		);
	} catch {
		return unavailable('type-surface-evaluation-failed');
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
}

let cache = null;

/**
 * Return the verdict for one type-surface gate.  All fixtures share one
 * compiler run; the result is cached for the lifetime of the harness process.
 */
export function typeSurfaceGate(repoRoot, identifier) {
	const root = String(repoRoot ?? '');
	if (!cache || cache.root !== root) {
		cache = { root, results: evaluate(root) };
	}
	const file = TYPE_GATES[identifier];
	const result = file ? cache.results[file] : null;
	if (!result) {
		return {
			ok: false,
			outcomes: {
				fixture: file ?? null,
				typescript: false,
				reason: 'unknown-type-surface-gate'
			}
		};
	}
	return { ok: result.ok, outcomes: result.checks };
}

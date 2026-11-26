import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export async function assertFileUnderRoot(root, relativePath) {
	const rootReal = await realpath(root);
	const real = await realpath(resolve(root, relativePath));
	const inside = relative(rootReal, real);
	assert.ok(
		inside !== "" && !inside.startsWith("..") && !isAbsolute(inside),
		`${relativePath} must resolve inside ${root}`,
	);
	const info = await stat(real);
	assert.ok(info.isFile(), `${relativePath} must be a regular file`);
	return real;
}

export async function assertFilesUnderRoot(root, files) {
	for (const file of files) {
		await assertFileUnderRoot(root, file);
	}
	return files.length;
}

export async function hasGitMetadata(root) {
	try {
		const info = await stat(resolve(root, ".git"));
		return info.isDirectory() || info.isFile();
	} catch {
		return false;
	}
}

export async function assertGitCheckout(root) {
	assert.ok(await hasGitMetadata(root), `${root} must retain its git checkout`);
	const { stdout } = await execFileAsync("git", ["rev-parse", "--verify", "HEAD"], { cwd: root });
	assert.match(stdout.trim(), /^[0-9a-f]{40}$/, "HEAD must resolve to the supplied baseline commit");
	return stdout.trim();
}

export async function collectChangedPaths(root) {
	const { stdout } = await execFileAsync(
		"git",
		["status", "--porcelain=v1", "-z", "--untracked-files=all"],
		{ cwd: root, maxBuffer: 16 * 1024 * 1024 },
	);
	const entries = stdout.split("\0").filter(Boolean);
	const paths = [];
	for (let index = 0; index < entries.length; index += 1) {
		const entry = entries[index];
		const status = entry.slice(0, 2);
		if (status.includes("R") || status.includes("C")) {
			paths.push(entry.slice(3));
			const next = entries[index + 1];
			if (next) {
				paths.push(next);
				index += 1;
			}
		} else {
			paths.push(entry.slice(3));
		}
	}
	return paths;
}

export function isAllowedWorkspaceChange(path, prefixes, files) {
	return (
		prefixes.some((prefix) => path.startsWith(prefix)) ||
		files.some((entry) => (entry.endsWith("/") ? path.startsWith(entry) : path === entry))
	);
}

export async function assertDiffRelativeToBaseline(root, prefixes, files) {
	const head = await assertGitCheckout(root);
	const changed = await collectChangedPaths(root);
	const outside = changed.filter((path) => !isAllowedWorkspaceChange(path, prefixes, files));
	assert.deepEqual(outside, [], `changes must stay within the migration scope; found: ${outside.join(", ")}`);
	return { head, changed };
}

export async function readJson(root, relativePath) {
	return JSON.parse(await readFile(resolve(root, relativePath), "utf8"));
}

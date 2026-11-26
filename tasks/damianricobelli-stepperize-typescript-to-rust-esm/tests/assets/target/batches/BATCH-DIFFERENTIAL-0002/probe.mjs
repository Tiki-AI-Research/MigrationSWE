import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
	allowedWorkspaceChangeFiles,
	allowedWorkspaceChangePrefixes,
	baselineFiles,
	targetBaselineFiles,
	targetRequiredFiles,
} from "../../../fixtures/batches/BATCH-DIFFERENTIAL-0002/repo.mjs";
import {
	assertDiffRelativeToBaseline,
	assertFilesUnderRoot,
	readJson,
} from "../../../shared/batches/BATCH-DIFFERENTIAL-0002/repo-assertions.mjs";

export async function probeTarget(root = process.cwd()) {
	await assertFilesUnderRoot(root, targetBaselineFiles);
	const requiredCount = await assertFilesUnderRoot(root, targetRequiredFiles);
	const { head, changed } = await assertDiffRelativeToBaseline(
		root,
		allowedWorkspaceChangePrefixes,
		allowedWorkspaceChangeFiles,
	);

	const workspace = await readJson(root, "package.json");
	if (workspace.private !== true) {
		throw new Error("the supplied workspace baseline manifest must remain private");
	}
	const core = await readJson(root, "packages/core/package.json");
	if (core.name !== "@stepperize/core") {
		throw new Error("the supplied @stepperize/core baseline manifest must remain in place");
	}

	return {
		observation: "DIFF-033/DIFF-034",
		baselineFiles: baselineFiles.length,
		requiredFiles: requiredCount,
		head,
		changedPaths: changed.length,
	};
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	probeTarget(process.argv[2] ?? process.cwd()).catch((error) => {
		console.error(error);
		process.exitCode = 1;
	});
}

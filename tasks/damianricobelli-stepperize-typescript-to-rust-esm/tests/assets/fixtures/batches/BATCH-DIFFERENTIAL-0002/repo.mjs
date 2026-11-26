export const baselineFiles = Object.freeze([
	"package.json",
	"pnpm-workspace.yaml",
	"packages/core/package.json",
	"packages/core/src/index.ts",
	"packages/core/src/types.ts",
	"packages/core/src/utils.ts",
	"packages/react/package.json",
	"apps/docs/package.json",
	"apps/docs/registry.json",
]);

// The old TypeScript implementation file is a source-baseline invariant, not
// a target layout contract. A migrated package may put its ESM facade directly
// in src/index.ts and remove src/utils.ts.
export const targetBaselineFiles = Object.freeze(
	baselineFiles.filter((file) => file !== "packages/core/src/utils.ts"),
);

export const sourceRequiredFiles = Object.freeze([
	"package.json",
	"pnpm-workspace.yaml",
	"packages/core/package.json",
	"packages/core/tsconfig.json",
	"packages/core/tsup.config.ts",
	"packages/core/src/index.ts",
	"packages/core/src/types.ts",
	"packages/core/src/utils.ts",
]);

export const targetRequiredFiles = Object.freeze([
	...sourceRequiredFiles.filter((file) => file !== "packages/core/src/utils.ts"),
]);

export const allowedWorkspaceChangePrefixes = Object.freeze(["packages/core/"]);

export const allowedWorkspaceChangeFiles = Object.freeze([
	"package.json",
	"pnpm-lock.yaml",
	"pnpm-workspace.yaml",
	"turbo.json",
	".npmrc",
	".gitignore",
	".cargo/",
	"vendor/",
]);

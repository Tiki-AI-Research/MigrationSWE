import { createRequire } from "node:module";

// This copy is installed by the evaluator image, outside the candidate checkout.
// The override is for running the evaluator's own tests outside that image.
let compiler;
export function trustedTypeScript() {
	if (!compiler) {
		const location = process.env.STEPPERIZE_VERIFIER_TYPESCRIPT ??
			"/opt/stepperize-verifier-node-modules/typescript/lib/typescript.js";
		compiler = createRequire(import.meta.url)(location);
	}
	return compiler;
}

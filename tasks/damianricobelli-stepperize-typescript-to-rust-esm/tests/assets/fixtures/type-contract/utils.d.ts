import type { FlowData, Get, OutputOf, Step, StepMap, StepStatus, ValidationResult } from "./types.js";
/**
 * Create pure step access helpers without state, safe for Node, SSR,
 * validation code, tests, and framework adapters.
 */
export declare function createStepMap<const Steps extends readonly Step[]>(steps: Steps): StepMap<Steps>;
/**
 * Resolve the initial step index.
 *
 * Returns `0` when no initial id is provided or when the id is not found.
 */
export declare function getInitialStepIndex<Steps extends readonly Step[]>(steps: Steps, initial?: Get.Id<Steps>): number;
/**
 * Narrow an arbitrary value to a known step id.
 *
 * Useful at the boundary between Stepperize and external state such as URL
 * params, router state, or persisted flow snapshots.
 */
export declare function parseStep<const Steps extends readonly Step[]>(steps: Steps, value: unknown): Get.Id<Steps> | undefined;
export declare function getInitialData<Steps extends readonly Step[]>(data?: FlowData<Steps>): FlowData<Steps>;
/**
 * Validate a value against a step's `schema` (any Standard Schema). Steps without
 * a schema always succeed with the value unchanged, so non-form steps stay
 * first-class. Always resolves to a {@link ValidationResult}.
 */
export declare function validateStep<const Steps extends readonly Step[], Id extends Get.Id<Steps>>(steps: Steps, id: Id, value: unknown): Promise<ValidationResult<OutputOf<Steps, Id>>>;
/**
 * Get the positional status for one step id.
 *
 * This is based only on `currentIndex`; explicit completion is tracked by the
 * React stepper instance.
 */
export declare function getStepStatus<Steps extends readonly Step[]>(steps: Steps, currentIndex: number, id: Get.Id<Steps>): StepStatus;
export declare function getStepStatuses<Steps extends readonly Step[]>(steps: Steps, currentIndex: number): Record<Get.Id<Steps>, StepStatus>;
export declare function matchStep<const Steps extends readonly Step[], Result>(steps: Steps, id: Get.Id<Steps>, handlers: Get.Match<Steps, Result>): Result;

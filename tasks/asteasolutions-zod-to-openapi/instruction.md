# Upgrade `@asteasolutions/zod-to-openapi` from Zod 4.0.5 to Zod 4.1.0

## Background

`@asteasolutions/zod-to-openapi` 9.1.0 builds OpenAPI 3.0, 3.1, and 3.2 documents from Zod v4 schemas. Its public API accepts Zod schemas and installs the `openapi` method through `extendZodWithOpenApi`.

The package declares a `zod` peer dependency range of `^4.0.0` and a development dependency range of `^4.0.5`, with the lockfile resolving Zod to `4.0.5`. The library reads Zod schema runtime state in metadata handling, type detection, transformers, and version-specific generators. The migration target is Zod `4.1.0`.

## Goal

- Upgrade the project's Zod integration and development dependency from Zod `4.0.5` to Zod `4.1.0`.
- The `zod` development dependency MUST be `^4.1.0`, and `package-lock.json` MUST resolve the installed `zod` package to exactly `4.1.0`.
- The declared `zod` peer dependency range MUST include Zod `4.1.0`.
- Production code that inspects Zod schemas MUST be compatible with the Zod `4.1.0` runtime and type declarations.
- The upgrade MUST preserve every public contract and observable OpenAPI output described below.
- The upgrade MUST also correct the existing numeric-enum discriminator mapping defect described below. This correction is required even though the defect is present in the starting implementation.
- The upgrade MUST NOT add new library APIs or expand existing OpenAPI mapping capabilities beyond that required correction. Adapting the existing integration to Zod `4.1.0` runtime and type declarations is within scope.

## Compatibility baseline and required correction

The compatibility baseline is the provided starting implementation running with Zod `4.0.5`. Compare outputs for the same supported schema construction, registration order, generator version, generator options, and document configuration.

The starting implementation incorrectly includes TypeScript numeric-enum reverse names in `discriminator.mapping`. For example, `{ 0: 'Zero', 1: 'One', Zero: 0, One: 1 }` describes the numeric values `0` and `1`; `'Zero'` and `'One'` are reverse names, not accepted string values. The upgrade MUST remove mapping entries derived from those reverse names, as specified in the behavior requirements below.

This removal is the sole exception to the baseline output-preservation requirements in this document. It takes precedence over the requirements to keep the same OpenAPI mapping, structure, field order, and serialization result, and it is a required bug fix within the no-new-features constraint. All other output, including `oneOf`, valid mapping entries and their relative order, component schemas, and metadata, MUST retain baseline behavior. The exception does not authorize other fixes to pre-existing behavior or new discriminator unwrapping.

## Public contract to preserve

- `src/index.ts` MUST keep its existing export set and public API signatures. This includes `ZodOpenAPIMetadata`, `extendZodWithOpenApi`, `OpenAPIRegistry`, `RouteConfig`, `ResponseConfig`, `ZodMediaTypeObject`, `ZodContentObject`, `ZodRequestBody`, `zodToOpenAPIRegistry`, `OpenApiGeneratorV3`, `OpenApiGeneratorV31`, and `OpenApiGeneratorV32`.
- `extendZodWithOpenApi(zod)` MUST keep the `zod.ZodType.prototype.openapi` module augmentation and both overloads: `(metadata, options?)` and `(refId, metadata?, options?)`.
- `OpenAPIRegistry` MUST keep `register`, `registerParameter`, `registerPath`, `registerWebhook`, `registerComponent`, and the `definitions` getter with their current argument and return contracts.
- Definition processing MUST keep the order `schema`, `parameter`, `component`, `route`, `webhook`.
- `OpenApiGeneratorV3`, `OpenApiGeneratorV31`, and `OpenApiGeneratorV32` MUST keep their constructor and `generateDocument`/`generateComponents` contracts. V31 and V32 `generateDocument` MUST continue to emit `webhooks`; V3 MUST continue to omit them.
- Generated documents MUST remain plain JavaScript objects with the same `components`, `paths`, and `$ref` structures.
- Thrown `ZodToOpenAPIError`, `ConflictError`, `MissingParameterDataError`, and `UnknownZodTypeError` values MUST keep their existing class identity and messages.
- The package entry-point paths `dist/index.cjs`, `dist/index.mjs`, and `dist/index.d.ts`, their module formats, and their public export contracts MUST remain unchanged. Rebuilding these files to incorporate compatibility fixes is allowed; their contents need not be byte-identical to the baseline.

## Behavior requirements

- The supported Zod type list in the public README MUST remain supported with the same OpenAPI mapping. This includes numeric bounds, string formats and patterns, object `additionalProperties`, records, intersections, literals, template literals, dates, bigints, arrays, and unions.
- When `OpenApiGeneratorV3` generates a nullable inline schema that has a `type`, that schema MUST gain `nullable: true`. When `OpenApiGeneratorV31` or `OpenApiGeneratorV32` generates such a schema, the schema MUST instead gain a `type` array that contains `'null'` together with its original type.
- Applying `.nullable()` to an ordinary registered reference whose referenced schema is not already nullable MUST produce, with `OpenApiGeneratorV3`, `allOf` containing the `$ref` and a schema with `nullable: true`. With `OpenApiGeneratorV31` or `OpenApiGeneratorV32`, it MUST produce `allOf` containing the `$ref` and a schema whose `type` array contains `'null'` together with the referenced schema's type.
- A nullable recursive or pending reference MUST produce `allOf` with the `$ref` and `{ nullable: true }` in `OpenApiGeneratorV3`, and `oneOf` with the `$ref` and `{ type: 'null' }` in `OpenApiGeneratorV31` and `OpenApiGeneratorV32`. A reference to a schema that is already nullable MUST remain a bare `$ref` in all three generators.
- When a tuple is generated, `OpenApiGeneratorV3` MUST emit `items`, `minItems`, and `maxItems`. `OpenApiGeneratorV31` and `OpenApiGeneratorV32` MUST emit `prefixItems`.
- When an enum is generated, a nullable enum MUST include `null` as the final enum value. An all-numeric enum MUST map to `type: 'integer'`. An all-string enum MUST map to `type: 'string'`. A mixed string/number enum MUST throw `ZodToOpenAPIError` with the existing mixed-enum message.
- When a discriminated union is generated, the output MUST include `oneOf` for the options. When the union is not nullable, defines a discriminator, and every option has a `refId`, the output MUST also include `discriminator` with `propertyName` and `mapping`. When the union is nullable, does not define a discriminator, or has any option without a `refId`, the output MUST omit the entire `discriminator`.
- Preserve the existing discriminator extraction boundary: direct `ZodLiteral` and `ZodEnum` discriminator properties contribute their string values, and nested discriminated unions MUST be traversed recursively to collect those values. Preserve the existing reference targets and traversal order. Do not add extraction by unwrapping discriminator properties such as `.default()`, `.optional()`, or `.readonly()`; accepting such a union as Zod input does not require new mapping entries for its wrapped discriminator.
- Only actual string literal or enum values within that extraction boundary may create mapping entries. Non-string values MUST NOT be converted to strings to create entries. TypeScript numeric-enum reverse names MUST NOT create entries even though the reverse names themselves are strings. Removing those invalid entries MUST NOT remove the corresponding option from `oneOf` or change whether the union otherwise qualifies for `discriminator`; an eligible union with no string discriminator values MUST retain `mapping: {}`.
- For example, a non-nullable union whose registered options are `NumericKind` with `kind` using the numeric enum above, and `TextKind` with `kind: z.literal('text')`, MUST retain both options in `oneOf`. Its discriminator MUST be `{ propertyName: 'kind', mapping: { text: '#/components/schemas/TextKind' } }` in all three generators. It MUST NOT contain mappings for `Zero`, `One`, `'0'`, or `'1'`.
- When a discriminated union has no discriminator, generation MUST keep the existing `console.error` trigger and message and MUST produce `oneOf`.
- When `sortComponents: 'alphabetically'` is supplied, `components.schemas` and `components.parameters` MUST be sorted by key. Without that option, schemas and parameters MUST remain in registration order.
- The `unionPreferredType` option MUST continue to select `oneOf` or `anyOf` for Zod unions, both as a generator option and as a per-schema option.
- Metadata from `.meta()` and `.openapi()` MUST keep the existing merge and precedence behavior. The most recent value for a metadata property MUST win, unspecified properties MUST be preserved, and a `refId` passed to `.openapi(name)` MUST take precedence over `id` supplied through `.meta()` when choosing the component name. This naming precedence MUST NOT discard metadata fields retained by the baseline. In particular, `.openapi('OpenBeforeMeta').meta({ id: 'LaterMetaId', description: 'later metadata' })` on a string schema MUST still register `OpenBeforeMeta` and retain `id: 'LaterMetaId'` and `description: 'later metadata'` in that component's schema.
- Metadata and schema inheritance through `default`, `prefault`, `pipe`, `refine`, `transform`, and `lazy` MUST remain unchanged. This includes the current unwrapping to the input or output schema for each modifier.
- Recursive schemas that use lazy getters MUST continue to generate `$ref` references without infinite recursion. This includes nullable, optional, array, union, and intersection combinations.
- `OpenApiGeneratorV32` MUST keep support for `itemSchema`, `itemEncoding`, `prefixEncoding`, response `summary`, and the `query` HTTP method.
- Unsupported Zod types MUST still throw `UnknownZodTypeError`. Missing parameter name or location MUST still throw `MissingParameterDataError`. Conflicting parameter names or locations MUST still throw `ConflictError`.
- `extendZodWithOpenApi` MUST continue to type-check against Zod `4.1.0`. The public `.openapi()` overloads and exported types MUST remain usable in the tsd type tests.

## Constraints

1. Except for removal of invalid numeric-enum reverse-name mapping entries under "Compatibility baseline and required correction", the migration MUST NOT change the structure, field order, or serialization result of generated OpenAPI documents. Zod `4.1.0` internal representation changes do not create additional exceptions.
2. The migration MUST NOT change the `src/index.ts` export set or any public API signature.
3. The migration MUST NOT change the OpenAPI version constants `3.0.0` through `3.2.0` or the behavior boundaries among the three generators.
4. The migration MUST NOT change error types, error messages, or `console.error` trigger conditions.
5. The migration MUST NOT introduce frontend or UI code or rewrite the library in another language.
6. Production code MUST NOT hardcode generated schemas or bypass Zod schema inspection to imitate compatibility.
7. The migration MUST NOT remove or downgrade existing public capabilities.
8. The expected change surface is `package.json`, `package-lock.json`, `src/zod-extensions.ts`, `src/metadata.ts`, `src/lib/zod-is-type.ts`, `src/openapi-registry.ts`, `src/openapi-generator.ts`, `src/types.ts`, `src/transformers/index.ts`, `src/transformers/lazy.ts`, `src/transformers/discriminated-union.ts`, `src/transformers/enum.ts`, `src/v3.0/specifics.ts`, `src/v3.1/specifics.ts`, `spec/setup-tests.ts`, `spec/type-definitions/zod-extensions.test-d.ts`, `spec/types/discriminated-union.spec.ts`, `spec/types/recursive-schemas.spec.ts`, `spec/modifiers/pipe.spec.ts`, and `spec/modifiers/meta.spec.ts`. This list identifies likely locations, not a requirement to modify every file. Other source files may change only when a Zod `4.1.0` compatibility fix or the required numeric-enum discriminator correction requires it.
9. You MAY adapt the following spec files to the upgraded Zod API and add regression coverage for the requirements above: `spec/setup-tests.ts`, `spec/type-definitions/zod-extensions.test-d.ts`, `spec/types/discriminated-union.spec.ts`, `spec/types/recursive-schemas.spec.ts`, `spec/modifiers/pipe.spec.ts`, and `spec/modifiers/meta.spec.ts`. You MUST NOT delete assertions, weaken comparisons, skip checks, or hardcode inputs to make them pass. All other public spec files are protected.
10. Public export contracts and error messages are protected. Checked-in generated document artifacts and all files under `example/`, including `example/openapi-docs.yml`, MUST NOT be modified. The required mapping correction applies to runtime-generated output and does not authorize edits to those protected files.
11. Work in `/workspace/repo`. Keep every required change in the repository working tree as tracked or new non-ignored files. The evaluator collects the repository diff against the provided starting commit. No branch creation or commit is required.

## Validation

From `/workspace/repo`, run:

```sh
npm ci
npm run build
npm run lint
npm test
```

`npm test` runs `npm run test:jest` and `npm run test:types`. These commands check dependency installation, compilation, formatting, Jest behavior tests, and tsd type tests. They do not by themselves prove every behavior requirement in this document.

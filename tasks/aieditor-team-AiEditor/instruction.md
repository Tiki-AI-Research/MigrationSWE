# Migrate the AI service from the OpenAI Node SDK to the OpenAI Agents SDK

## Background

The AiEditor AI module uses the `openai` Node SDK 7.8.0 for model requests, streaming output, and the editor tool loop. The public AI surface includes `OpenAiService`, `createAiService`, the provider registry, the six editor tools, and their request, result, and proposal types.

This task replaces the implementation behind that surface with `@openai/agents` 0.1.0 without changing the public editor or AI contracts.

## Goal

- Migrate `src/ai/OpenAiService.ts` and the supporting AI module files from `openai` 7.8.0 to `@openai/agents` 0.1.0.
- The target framework MUST issue the model requests, deliver streaming output, and drive the editor tool loop for the migrated responsibilities.
- Keep the editor UI, Tiptap integration, content processing, upload, localization, and package publishing behavior out of scope.
- Out-of-scope dependencies MAY remain. They MUST NOT implement the migrated AI-generation responsibilities.

## Public contract to preserve

- The package root MUST continue to export `OpenAiService`, `createAiService`, `AI_PROVIDERS`, `registerAiProvider`, `getAiProviders`, `getAiProvider`, `editorTools`, `editorFunctionTools`, `executeEditorFunctionTool`, `applyEditorToolProposal`, `defaultEditorToolRegistry`, `defaultEditorTools`, `EditorToolRegistry`, `EditorTool`, `EditorMutationTool`, `ApplyFormattingTool`, `ApplyTextEditsTool`, `GetEditorContextTool`, `InsertContentTool`, `ReplaceSelectionTool`, and `SearchDocumentTool`. `src/ai/index.ts` MUST also continue to export `rebaseEditorToolProposal`.
- The exported AI types MUST retain their names and compatible TypeScript signatures. These include `AiContentScope`, `AiChatMessage`, `AiEditorContext`, `AiEditorToolApplyResult`, `AiEditorToolApproval`, `AiEditorToolName`, `AiEditorToolProposal`, `AiGenerateRequest`, `AiGenerateResult`, `AiService`, `AiServiceConfig`, `AiProviderServiceConfig`, `CustomAiServiceConfig`, `OpenAiServiceConfig`, `AiProviderDefinition`, and `EditorToolExecution`.
- `createAiService(config)` MUST accept an existing `AiService`, a custom configuration with `provider: 'custom'` and a `generate` function, and an `OpenAiServiceConfig`. It MUST return an `AiService`.
- `OpenAiServiceConfig` MUST continue to accept `provider`, `providers`, `model`, `apiKey`, `baseURL`, `client`, `organization`, `project`, `timeout`, `maxRetries`, `defaultHeaders`, `fetch`, `fetchOptions`, `dangerouslyAllowBrowser`, `response`, and `buildInput`.
- `OpenAiService` MUST implement `AiService`, MUST keep its public `client` property readable, and MUST remain constructible from the documented configuration.
- `AiService.generate(request, context)` MUST return `Promise<AiGenerateResult>`. `AiGenerateResult` MUST retain `text`, optional `raw`, and optional `toolProposals`. An empty prompt MUST be rejected with `AI prompt cannot be empty`.
- `AiGenerateRequest` MUST retain `prompt`, `instructions`, `scope`, `metadata`, `history`, `stream`, `onChunk`, `editorTools`, and `signal`.
- `AiEditorContext` MUST retain `editor` as the live Tiptap `Editor`. It MUST also retain the request-time `html`, `text`, `selectedText`, `selection` (`from` and `to`), and `documentVersion` snapshot fields.
- `AiEditorToolProposal` MUST retain `id`, `tool`, `title`, `description`, `arguments`, `documentVersion`, `status`, and optional `message`. `status` MUST remain `pending`, `applied`, or `discarded`.
- `AiEditorToolApplyResult` MUST retain `ok` and `message`.
- `AiEditorToolApproval` MUST retain `always`, `never`, an array of editor tool names, and a predicate function over a proposal.
- `AiEditorToolName` MUST retain `get_editor_context`, `search_document`, `replace_selection`, `insert_content`, `apply_formatting`, and `apply_text_edits`.
- `AiProviderDefinition` MUST retain `id`, `name`, optional `baseURL`, and `model`.
- The built-in provider IDs and defaults MUST remain:

| ID | Default baseURL | Default model |
| --- | --- | --- |
| `openai` | unset (client default) | `gpt-4o-mini` |
| `deepseek` | `https://api.deepseek.com` | `deepseek-chat` |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| `moonshot` | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |

- The six editor tools MUST keep their names, descriptions, and JSON schemas. `editorFunctionTools` MUST remain the model-visible tool definitions. Adapting the wrapper to the target framework is allowed, but tool names and schemas MUST NOT change.
- `executeEditorFunctionTool(name, rawArguments, context)` MUST retain its signature and MUST return an `EditorToolExecution` with `output` and an optional proposal.
- `applyEditorToolProposal(editor, documentVersion, proposal)` MUST retain its signature and MUST return an `AiEditorToolApplyResult`.
- The `package.json` entry points and `exports` map MUST keep the ESM and CJS publishing contract. The 1.x UMD bundle and browser-global `AiEditor` constructor MUST NOT return.

## Behavior requirements

- If `request.stream` is `true` or `request.onChunk` is provided, `OpenAiService.generate` MUST use streaming and call `onChunk` for each text delta in arrival order. The returned `text` MUST equal the concatenation of those deltas. This MUST also hold when `editorTools` is enabled.
- If neither `stream` nor `onChunk` is set, the returned `text` MUST equal the final model output text. With `editorTools` enabled, `text` MUST accumulate the model output text across rounds.
- The default input composition MUST use the request-time `AiEditorContext` snapshot fields. It MUST NOT read editor state again for that composition after the request starts. Editor tool execution MAY read the live `context.editor` state.
- When `buildInput` is not supplied, the default input MUST include the prompt, with prior history rendered as `User:` and `Assistant:` lines. The default scope MUST be `selection` when `selectedText` is non-empty and `document` otherwise. `selection` MUST append the selected plain text under `Selected text:`. `document` MUST append the full document plain text under `Document:`. `none` MUST append no editor content. A supplied `buildInput` MUST replace this composition.
- `request.instructions`, when present, MUST be sent as model-level instructions. The built-in input composition MUST NOT concatenate those instructions into the user text.
- With `editorTools` enabled, the service MUST expose the six tool definitions to the model and execute returned tool calls locally. The loop MUST continue until the model returns no more calls. The result MUST include accumulated text, accumulated proposals, and `raw` when a final response is available.
- Each local tool output MUST be appended to the next model round with the identifier of the call it answers. The service MUST NOT reuse or reorder call identifiers.
- The editor tool loop MUST allow at most eight model rounds. If the model still requests tool calls after eight rounds, generation MUST throw `AI exceeded the maximum editor tool call rounds`.
- If a streamed tool round ends without a completed response, generation MUST throw `AI response stream ended before completion`.
- If a tool call throws, its function-call output MUST be a JSON object with `ok: false` and an `error` string containing the thrown message or `Tool execution failed`. The run MUST continue.
- A proposal-producing tool MUST return a JSON object with `ok: true`, `status: 'pending_user_approval'`, and a `proposal_id`. The proposal MUST start as `pending` with the request-time `documentVersion`.
- A read-only tool MUST reject `apply` with `<tool name> is read-only and cannot be applied.`
- `EditorToolRegistry` construction MUST throw `Duplicate editor tool: <name>` for duplicate names.
- Unknown tools MUST produce `{ok: false, error: 'Unknown editor tool: <name>'}` from execution. Apply MUST return a failure result for an unknown tool and MUST leave the document unchanged.
- Applying a non-pending proposal MUST fail with `This proposal is no longer pending.`.
- Applying a pending proposal whose `documentVersion` differs from the current version MUST fail with `The document changed. Ask AI to create a new proposal.` and MUST leave the document unchanged.
- Applying a valid proposal MUST set its status to `applied` and return `{ok: true, message: 'Applied to the document.'}`. If the tool throws an `Error`, apply MUST return `{ok: false, message: <thrown message>}`. If the tool throws a non-`Error` value, apply MUST return `{ok: false, message: 'Could not apply this proposal.'}`.
- `rebaseEditorToolProposal` MUST map the frozen integer position fields and edit coordinates through the supplied mapping and MUST set `documentVersion` to the supplied version.
- A configuration that has `provider` and `generate` MUST be treated as a custom service. A string result MUST be normalized to `{text: result}`. An existing `AiService` MUST be returned unchanged.
- For a provider configuration, `createAiService` MUST register each entry in `providers` before resolving the provider and MUST return an `OpenAiService`.
- Model resolution MUST use `config.model` first and then the selected provider's `model`. If the result is missing or blank, construction MUST throw `OpenAiService requires a model`.
- A supplied `client` MUST be reused instead of creating another client. An explicit `baseURL` MUST override the provider default.
- `registerAiProvider` MUST throw `AI provider id cannot be empty`, `AI provider name cannot be empty`, or `AI provider model cannot be empty` for the corresponding blank field. It MUST register or replace the ID with a frozen copy.
- `getAiProviders` MUST return a snapshot of the current registry. `getAiProvider` MUST resolve the current entry by ID.
- When `request.signal` is aborted, the service MUST stop issuing model requests and MUST propagate the abort instead of returning a partial success.

## Constraints

1. The migration MUST replace the source-framework generation path. Production code in scope MUST NOT call the `openai` client or its Responses API directly to issue model requests, stream output, or run the editor tool loop.
2. The target framework `@openai/agents` 0.1.0 MUST provide the agent execution, streaming, and tool-orchestration behavior for the migrated responsibilities.
3. Production code MUST NOT delegate the migrated responsibilities to the source framework through a subprocess, RPC, FFI, dynamic loading, or a compatibility wrapper.
4. `package.json` MUST declare `@openai/agents` at version `0.1.0` for the migrated code. Type-only references to `openai` MAY remain for unchanged public types or out-of-scope tool definitions. A caller-supplied `client` MAY be passed to the target framework, but the scoped code MUST NOT use it to call Responses directly.
5. Changes MUST be limited to `src/ai/OpenAiService.ts`, `src/ai/index.ts`, `src/ai/Providers.ts`, `src/ai/types.ts`, `src/ai/editor-tools.ts`, `package.json`, and the lockfile update required by the target dependency.
6. The Node.js engine constraint MUST remain `>=20.19.0`. The migrated code MUST remain compatible with Node.js 22.12.0 and 22.14.0.
7. The package entry points and the ESM and CJS publishing contract MUST remain unchanged. Do not add a UMD bundle or a browser-global `AiEditor` constructor.
8. Do not change the editor UI, toolbar, menus, bubble or floating menus, sidebar rendering, CSS, TinyUI, or Tiptap extensions.
9. Do not change content sanitization, document style and outline, uploader, or localization behavior.
10. Do not change the six editor tool names, their JSON schemas, proposal status values, or host-controlled approval semantics.
11. Do not remove or rename built-in provider IDs or change their default baseURL or model values.
12. Preserve the explicit `dangerouslyAllowBrowser` opt-in and host-side protection of provider API keys. Do not move secrets into browser bundles.
13. Do not delete, skip, weaken, or hardcode existing public tests or assertions.
14. Work in `/workspace/repo`. The task starts from commit `c0386187d1c55ca84920c342c585b7ffdf272519`. Do not replace that starting point with `main`.
15. Keep all required source and dependency changes in the repository working tree. The verifier collects them as a patch against that baseline.

## Validation

From `/workspace/repo`, run:

```bash
npm ci
npm run test:run
npm run build
```

`npm run test:run` runs the Vitest suite. `npm run build` runs the TypeScript check and builds the ESM and CJS package outputs. The repository's full local gate is `npm run ci`. These public checks verify existing behavior and build compatibility. They do not by themselves prove every requirement.

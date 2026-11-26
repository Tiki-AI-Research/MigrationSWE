# Migrate Markdown parsing from unified/remark to markdown-it

## Background

The Markdown parsing module in `packages/utils/markdown-parse` currently uses `unified` 11.0.5 with `remark-parse`, `remark-gfm`, `remark-breaks`, `remark-math`, `remark-rehype`, and `rehype-katex`. The Markdown component and the document editor consume its processor and rendering functions.

## Goal

- Migrate the Markdown parsing and rendering responsibilities in `packages/utils/markdown-parse/index.ts` and `packages/utils/markdown-parse/models/` from the unified/remark/rehype pipeline to `markdown-it` 14.1.0.
- `markdown-it` 14.1.0 MUST be the Markdown engine for tokenization and HTML rendering. Required extensions MUST integrate with markdown-it and MUST NOT use unified or remark.
- Keep the public API, output contracts, and observable behavior described below.
- KaTeX may remain the math typesetting engine.
- The following are out of scope: Vue components and hooks, browser reactivity and DOM interaction, `packages/utils/markdown-parse/hast-to-vnode.ts`, shiki highlighting, Mermaid browser rendering, theme-chalk styles, and VitePress documentation.
- Source-framework dependencies used only by out-of-scope code may remain.

## Public contract to preserve

- `createBaseProcessor(middlewarePlugins: MiddlewarePluginItem[])` MUST return a reusable markdown-it processor.
- `createBaseProcessor` MUST always enable base Markdown parsing, GFM tables, task lists, strikethrough with single-tilde disabled, and hard breaks before applying the supplied middleware entries.
- `createBaseProcessor` MUST apply middleware entries in array order. Each entry keeps its `key` and passes its optional `options` to its `plugin`.
- `MiddlewarePluginItem` MUST keep the shape `{ key: string; plugin: any; options?: any }`.
- `defaultCustomPlugins` MUST remain an ordered `MiddlewarePluginItem[]`. It MUST keep these keys in this order: `remark-math`, `remark-a-target-blank`, `remark-mark-highlight`, `remark-sub-super`, `remark-abbr`, `remark-code-block-status`, `remark-rehype`, `rehype-katex`.
- The default entries MUST continue to provide math parsing, link target handling, mark highlighting, sub/superscript, abbreviations, code-block status, HTML conversion, and KaTeX rendering.
- The existing `options` fields on the default entries and their effects MUST be preserved. This includes raw HTML passthrough and KaTeX configuration.
- `processMarkdownToParts(content: string, processor): Promise<MarkdownPart[]>` MUST return parts segmented by `html` and `code`.
- `processMarkdownToHtml(content: string, processor): Promise<string>` MUST return an HTML string that allows raw HTML.
- `katexProcess(content: string): string` MUST retain its delimiter normalization behavior.
- `MarkdownPart` MUST keep `type: 'html' | 'code'`, `content`, optional `language`, and optional `loading`.
- `ProcessorType` MUST remain the return type of `createBaseProcessor`.
- `MiddlewarePluginItem`, `MarkdownPart`, `ProcessorType`, and `ProcessMarkdownOptions` MUST remain exported.
- The `remarkPlugins` prop on the Markdown and document editor components MUST continue to accept `MiddlewarePluginItem[]` and remain compatible with `createBaseProcessor`.
- HTML parts MUST use the existing `content` shape: Vue VNodes consumable by the Markdown component. They MUST NOT become raw HTML strings.
- Code parts MUST use the code text as `content`.
- These exports MUST remain reachable through `@element-ai-vue/utils`.

## Behavior requirements

- If the input is an empty string, `processMarkdownToParts` MUST return an empty array.
- A single newline inside a paragraph MUST render as a hard line break (`<br>`).
- GFM tables MUST keep the current header, body, row, column, and cell-alignment semantics.
- GFM task list items MUST render a disabled checkbox that reflects the checked state.
- `~~text~~` MUST render as `<del>text</del>`.
- A single-tilde span MUST NOT be parsed as GFM strikethrough.
- Markdown links MUST keep their `href` and title attributes and MUST add `target="_blank"`.
- `==text==` MUST render as `<mark>text</mark>`.
- `~x~` MUST render as `<sub>x</sub>` and `^x^` MUST render as `<sup>x</sup>`.
- An abbreviation definition written as `*[ABBR]: definition` MUST be consumed. Matching text MUST render as `<abbr title="definition">ABBR</abbr>`.
- Each top-level fenced code block MUST become a separate `{ type: 'code' }` part in document order.
- A code part's `language` MUST be the fence language identifier without the `language-` prefix. If no language is given, `language` MUST be an empty string.
- A code part's `loading` MUST be `false` when the closing fence is present and `true` when it is absent.
- Non-code top-level content between code blocks MUST be grouped into `{ type: 'html' }` parts. A code block MUST break the HTML grouping. Empty HTML groups MUST NOT be emitted.
- `processMarkdownToHtml` MUST render fenced code blocks as `<pre><code>` elements with a `language-*` class for the fence language.
- `katexProcess` MUST convert `\[...\]` to `$$...$$`, convert `\(...\)` to `$...$`, promote multi-line `$...$` spans to `$$...$$`, and pad multi-line `$$...$$` blocks with surrounding newlines.
- Inline math MUST render with a `katex` class. Block math MUST render inside a `katex-display` wrapper.
- Math rendering MUST apply the existing `katexConfig` from `@element-ai-vue/constants`. This includes `throwOnError: false`, `errorColor: '#cc0000'`, `strict: false`, and the existing macros.
- With `throwOnError: false`, invalid formulas MUST render KaTeX error output with the configured error color and MUST NOT throw.
- Raw HTML blocks and inline raw HTML MUST appear unchanged and unescaped in `processMarkdownToHtml` output.
- Raw HTML MUST remain present in the HTML-part stream for `processMarkdownToParts`. The unchanged VNode renderer determines its final rendering.
- A processor returned by `createBaseProcessor` MUST be reusable across multiple calls to `processMarkdownToParts` and `processMarkdownToHtml`.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code in the migrated module (`packages/utils/markdown-parse/`, excluding the out-of-scope `hast-to-vnode.ts`) MUST NOT import or call `unified`, `remark-parse`, `remark-gfm`, `remark-breaks`, `remark-math`, `remark-rehype`, `rehype-katex`, or `hast-util-to-html` to parse or render Markdown.
3. The migrated responsibilities MUST NOT be delegated to the unified/remark/rehype runtime through subprocesses, RPC, FFI, dynamic imports, or similar mechanisms.
4. Changes MUST be limited to the files in the migration scope, new implementation helper files under `packages/utils/markdown-parse/`, the root `package.json`, and lockfile updates required by the target dependency. Tests or temporary files created during development MUST NOT be included in the final patch.
5. The root `package.json` MUST declare `markdown-it` 14.1.0 as a runtime dependency and `@types/markdown-it` as a development dependency. You MAY remove source-framework entries that the migrated module no longer uses. Sources used by out-of-scope code, such as `hast-util-to-html` for `hast-to-vnode.ts`, MAY remain.
6. The following MUST NOT be modified: `packages/utils/markdown-parse/hast-to-vnode.ts`, Vue components under `packages/components`, hooks under `packages/hooks`, shiki and Mermaid integrations, theme-chalk styles, and VitePress documentation. The Markdown component's template, props, slots, theme, language handling, and scrolling or typewriter behavior MUST remain unchanged.
7. Existing tests and assertions MUST NOT be modified, deleted, skipped, weakened, or hardcoded to fake compliance.
8. The `remarkPlugins` prop name and its `MiddlewarePluginItem[]` type MUST remain unchanged.
9. Work in `/workspace/repo` using the repository's pnpm 10.18.2 and Node.js 20 or newer setup. The provided checkout is the task starting point. Do not substitute `main` or another snapshot.
10. Keep all completed changes in the repository. The evaluator collects them as a patch against the provided starting checkout.

## Validation

From `/workspace/repo`, run `pnpm test` and `pnpm test:coverage`. These commands run the repository's public Vitest suite. The suite does not cover the Markdown parsing migration, so passing it is not proof that all behavior requirements are met.

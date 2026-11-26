"""Focused regressions, runnable in the tests image with Python unittest."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# The script directory also contains verifier.py, which would shadow the
# namespace package when this file is executed directly.
sys.path = [str(Path(__file__).resolve().parents[1])] + [
    item for item in sys.path if item != str(Path(__file__).resolve().parent)
]

from grader import _apply_model_patch, _patch_changed_paths
from verifier.evidence import build_hardgate_evidence
from verifier.hardgate_engine import evaluate_hardgate
from verifier.javascript_analysis import analyze_javascript


class LoaderPropagationTests(unittest.TestCase):
    def test_loader_invocations(self):
        sources = [
            "const load = require as NodeRequire; load.call(null, 'marked');",
            "const load = module.require; load['apply'](null, ['mar' + 'ked']);",
            "const load = require.bind(null); load.call(null, 'marked');",
            "const load = require.bind(null, 'marked'); load();",
            "const load = module.require.bind(null, 'marked'); load.apply(null, []);",
            "const load = require; const invoke = load.call; invoke(null, 'marked');",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(analyze_javascript(source)["imports"], ["marked"])

    def test_ordinary_renderers_and_prose_are_not_loaders(self):
        for source in [
            "const render = renderer.render.bind(renderer); render('marked');",
            "renderer.call(null, 'marked'); renderer.apply(null, ['marked']);",
            "function require(name) { return name; } require.call(null, 'marked');",
            "const note = \"load.call(null, 'marked')\"; // require('marked')",
        ]:
            with self.subTest(source=source):
                self.assertEqual(analyze_javascript(source)["imports"], [])

    def test_borrowed_loader_preserves_skip_detection(self):
        result = analyze_javascript("const load = require; const { it } = load.call(null, '@jest/globals'); it.skip('x', () => {});")
        self.assertEqual(result["imports"], ["@jest/globals"])
        self.assertEqual(result["disabled"], 1)


class PatchScopeTests(unittest.TestCase):
    def test_renderer_probe_failure_is_not_cheating_evidence(self):
        config = json.loads((Path(__file__).parents[1] / 'config.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            evidence = build_hardgate_evidence(
                Path(directory), config=config,
                observations=[{
                    'id': 'F2P-001', 'kind': 'F2P', 'status': 'FAIL',
                    'value': {'target_technology': {
                        'passed': False,
                        'failures': ['markdown-it renderer output did not reach production HTML'],
                    }},
                }],
                observation_errors=[], receipts=[], patch_applicable=True,
                source_test_baseline={'files': {}, 'marker_counts': {}}, changed_paths=[],
            )
            self.assertEqual(evidence['no_deceptive_source_fallback']['status'], 'PASS')
            self.assertEqual(evidence['deceptive_fallback_hits'], [])

    def test_git_inventory_includes_all_patch_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            patches = {
                "new": ("diff --git a/extra.ts b/extra.ts\nnew file mode 100644\n--- /dev/null\n+++ b/extra.ts\n@@ -0,0 +1 @@\n+x\n", ["extra.ts"]),
                "deleted": ("diff --git a/old.ts b/old.ts\ndeleted file mode 100644\n--- a/old.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", ["old.ts"]),
                "renamed": ("diff --git a/outside.ts b/allowed.ts\nsimilarity index 100%\nrename from outside.ts\nrename to allowed.ts\n", ["allowed.ts", "outside.ts"]),
                "copied": ("diff --git a/outside.ts b/allowed.ts\nsimilarity index 100%\ncopy from outside.ts\ncopy to allowed.ts\n", ["allowed.ts"]),
                "mode": ("diff --git a/old.ts b/old.ts\nold mode 100644\nnew mode 100755\n", ["old.ts"]),
                "quoted": ('diff --git "a/new\\ttest.ts" "b/new\\ttest.ts"\nnew file mode 100644\n--- /dev/null\n+++ "b/new\\ttest.ts"\n@@ -0,0 +1 @@\n+x\n', ["new\ttest.ts"]),
            }
            for name, (patch, expected) in patches.items():
                with self.subTest(name=name):
                    repo = root / name
                    subprocess.run(["git", "init", "-q", str(repo)], check=True)
                    (repo / "old.ts").write_text("x\n")
                    (repo / "outside.ts").write_text("x\n")
                    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
                    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Regression", "-c", "user.email=regression@example.test", "commit", "-qm", "baseline"], check=True)
                    patch_path = repo / "input.patch"
                    patch_path.write_text(patch)
                    self.assertFalse(_apply_model_patch(repo, patch_path)[0])
                    self.assertEqual(_patch_changed_paths(repo, patch_path), expected)

    def test_outside_file_is_detected_even_when_gitignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("*.log\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Regression", "-c", "user.email=regression@example.test", "commit", "-qm", "baseline"], check=True)
            patch_path = root / "input.patch"
            patch_path.write_text("diff --git a/extra.log b/extra.log\nnew file mode 100644\n--- /dev/null\n+++ b/extra.log\n@@ -0,0 +1 @@\n+x\n")
            self.assertFalse(_apply_model_patch(root, patch_path)[0])
            self.assertEqual(_patch_changed_paths(root, patch_path), ["extra.log"])

    def test_scope_violation_is_a_non_cheating_hard_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for paths, expected in [(["allowed.ts"], "PASS"), (["extra.ts"], "FAIL"), (None, "INCONCLUSIVE")]:
                evidence = build_hardgate_evidence(
                    root, config={"policy": {"allowed_change_paths": ["allowed.ts"]}},
                    observations=[], observation_errors=[], receipts=[], patch_applicable=True,
                    source_test_baseline={"files": {}}, changed_paths=paths,
                )
                self.assertEqual(evidence["changes_within_scope"]["status"], expected)
                if expected == "FAIL":
                    gate = evaluate_hardgate(root, evidence)
                    self.assertEqual(gate["status"], "FAIL")
                    self.assertFalse(gate["cheating"])
                    self.assertEqual(gate["reward_override"], 0)


class ManifestPlacementTests(unittest.TestCase):
    def test_dev_dependency_is_accepted_only_with_standalone_outputs(self):
        integration = Path(__file__).parents[1] / 'assets/shared/integration/index.js'
        script = r'''
const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const verifier = require(process.argv[1]);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'email-manifest-regression-'));
const originalExec = childProcess.execFileSync;
try {
  const packageDir = path.join(root, 'packages/block-text');
  const dist = path.join(root, 'dist');
  fs.mkdirSync(packageDir, { recursive: true });
  fs.mkdirSync(dist);
  for (const name of ['react', 'react-dom', 'zod', 'insane']) {
    const folder = path.join(root, 'node_modules', name);
    fs.mkdirSync(folder, { recursive: true });
    fs.writeFileSync(path.join(folder, 'package.json'), JSON.stringify({ name }));
  }
  const manifest = {
    name: '@usewaypoint/block-text',
    main: './dist/index.js',
    module: './dist/index.mjs',
    exports: { '.': { require: './dist/index.js', import: './dist/index.mjs' } },
    dependencies: { insane: '^2.6.2' },
    devDependencies: { 'markdown-it': '14.1.0' },
  };
  const manifestPath = path.join(packageDir, 'package.json');
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  const context = { repoRoot: root };
  assert.equal(verifier.manifestProjection(context).markdownIt, '14.1.0');
  assert.equal(verifier.manifestProjection(context).markdownItPlacement, 'devDependencies');
  childProcess.execFileSync = (_node, args, options) => {
    const installed = path.join(options.cwd, 'node_modules/@usewaypoint/block-text');
    assert.equal(fs.existsSync(path.join(installed, 'package.json')), true);
    assert.equal(fs.existsSync(path.join(installed, 'node_modules/markdown-it')), false);
    assert.equal(args.at(-1).includes("@usewaypoint/block-text"), true);
    const filename = args.includes('--input-type=module') ? 'index.mjs' : 'index.js';
    const output = fs.readFileSync(path.join(installed, 'dist', filename), 'utf8');
    if (output.includes('markdown-it')) throw new Error('parser was left external');
    return Buffer.alloc(0);
  };
  fs.writeFileSync(path.join(dist, 'index.js'), 'exports.Text = function Text() {};\n');
  fs.writeFileSync(path.join(dist, 'index.mjs'), 'export function Text() {}\n');
  let result = verifier.inspectStandaloneBuild(context, dist);
  assert.equal(result.cjs, true);
  assert.equal(result.esm, true);
  fs.writeFileSync(path.join(dist, 'index.js'), "require('markdown-it');\n");
  fs.writeFileSync(path.join(dist, 'index.mjs'), "import 'markdown-it';\n");
  result = verifier.inspectStandaloneBuild(context, dist);
  assert.equal(result.cjs, false);
  assert.equal(result.esm, false);
  const parser = path.join(root, 'node_modules/markdown-it');
  fs.mkdirSync(parser, { recursive: true });
  fs.writeFileSync(path.join(parser, 'package.json'), JSON.stringify({ name: 'markdown-it', version: '14.1.0' }));
  manifest.dependencies['markdown-it'] = '14.1.0';
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  childProcess.execFileSync = (_node, args, options) => {
    const installed = path.join(options.cwd, 'node_modules/@usewaypoint/block-text');
    assert.equal(fs.existsSync(path.join(installed, 'node_modules/markdown-it')), true);
    assert.equal(fs.existsSync(path.join(options.cwd, 'node_modules/markdown-it')), false);
    return Buffer.alloc(0);
  };
  result = verifier.inspectStandaloneBuild(context, dist);
  assert.equal(result.cjs, true);
  assert.equal(result.esm, true);
} finally {
  childProcess.execFileSync = originalExec;
  fs.rmSync(root, { recursive: true, force: true });
}
'''
        completed = subprocess.run(
            ['node', '--eval', script, str(integration)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class HeaderOnlyTableTests(unittest.TestCase):
    def test_empty_tbody_rejects_extra_rows_and_text(self):
        integration = Path(__file__).parents[1] / 'assets/shared/integration/index.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const integrationPath = process.argv[1];
const loaded = new Module(integrationPath, module);
loaded.filename = integrationPath;
loaded.paths = Module._nodeModulePaths(path.dirname(integrationPath));
loaded._compile(
  fs.readFileSync(integrationPath, 'utf8') + '\nmodule.exports.__runP2P = runP2P;\n',
  integrationPath
);
function context(bodyElements, bodyText) {
  const tbody = { children: Array(bodyElements).fill({}), textContent: bodyText };
  const counts = { thead: 1, tbody: 1, th: 1, 'tbody td': 0 };
  const table = {
    hasAttribute: (name) => name === 'width',
    getAttribute: (name) => name === 'width' ? '100%' : null,
    querySelector: (selector) => selector === 'tbody' ? tbody : null,
    querySelectorAll: (selector) => Array(counts[selector] || 0).fill({}),
  };
  return {
    renderEmail: () => '',
    renderText: () => '',
    parse: () => ({ root: { querySelector: (selector) => selector === 'table' ? table : null } }),
  };
}
assert.equal(loaded.exports.__runP2P(context(0, '\n'), 'P2P-044').status, 'PASS');
assert.equal(loaded.exports.__runP2P(context(1, '\n'), 'P2P-044').status, 'FAIL');
assert.equal(loaded.exports.__runP2P(context(0, 'unexpected'), 'P2P-044').status, 'FAIL');
'''
        completed = subprocess.run(
            ['node', '--eval', script, str(integration)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class MarkdownLinkRuleTests(unittest.TestCase):
    def test_markdown_link_syntax_check_is_independent_of_autolinks(self):
        # F2P-028 isolates Markdown link syntax; P2P-041 checks bare URLs.
        integration = Path(__file__).parents[1] / 'assets/shared/integration/index.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const integrationPath = process.argv[1];
const loaded = new Module(integrationPath, module);
loaded.filename = integrationPath;
loaded.paths = Module._nodeModulePaths(path.dirname(integrationPath));
loaded._compile(
  fs.readFileSync(integrationPath, 'utf8') +
    '\nmodule.exports.__entryTest = { entryProjection, runF2P };\n',
  integrationPath
);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'email-link-rule-'));
try {
  const packageRoot = path.join(root, 'packages/block-text');
  const dependencyRoot = path.join(packageRoot, 'node_modules/markdown-it');
  const emailPath = path.join(packageRoot, 'src/EmailMarkdown.tsx');
  fs.mkdirSync(path.dirname(emailPath), { recursive: true });
  fs.mkdirSync(dependencyRoot, { recursive: true });
  fs.writeFileSync(path.join(packageRoot, 'package.json'), '{}');
  fs.writeFileSync(path.join(dependencyRoot, 'package.json'),
    JSON.stringify({ name: 'markdown-it', version: '14.1.0', main: 'index.js' }));
  fs.writeFileSync(path.join(dependencyRoot, 'index.js'), 'module.exports = function MarkdownIt() {};\n');
  const source = (option, linkRule) => `
import MarkdownIt from 'markdown-it';
const parser = new MarkdownIt({ breaks: true, html: true${option} });
${linkRule ? 'parser.renderer.rules.link_open = () => "<a target=\\"_blank\\">";' : ''}
parser.renderer.rules.table_open = () => '<table width="100%">';
insane(parser.render('text'));
`;
  const paragraph = {
    cloneNode: () => ({ textContent: 'line one line two', querySelectorAll: () => [] }),
  };
  let brCount = 1;
  const context = {
    repoRoot: root,
    renderEmail: () => '',
    parse: () => ({ root: {
      querySelector: () => paragraph,
      querySelectorAll: () => Array(brCount).fill({}),
    } }),
  };
  const technology = { passed: true, failures: [], manifest: {}, entry: {}, loaded: {} };
  for (const option of [', linkify: false', '']) {
    fs.writeFileSync(emailPath, source(option, true));
    assert.equal(loaded.exports.__entryTest.entryProjection(context).linkify, false);
    assert.equal(loaded.exports.__entryTest.runF2P(context, 'F2P-028', technology).status, 'PASS');
  }
  fs.writeFileSync(emailPath, source(', linkify: false', false));
  assert.equal(loaded.exports.__entryTest.runF2P(context, 'F2P-028', technology).status, 'FAIL');
  // Renderer rules are equivalent to the constructor flag when they emit br.
  fs.writeFileSync(emailPath, source('', true).replace('breaks: true, ', '') +
    '\nparser.renderer.rules.softbreak = () => "<br>";\n');
  assert.equal(loaded.exports.__entryTest.entryProjection(context).breaks, false);
  for (const id of ['F2P-009', 'F2P-028']) {
    assert.equal(loaded.exports.__entryTest.runF2P(context, id, technology).status, 'PASS');
  }
  // Merely spelling breaks: true cannot compensate for a missing output br.
  brCount = 0;
  fs.writeFileSync(emailPath, source('', true));
  for (const id of ['F2P-009', 'F2P-028']) {
    assert.equal(loaded.exports.__entryTest.runF2P(context, id, technology).status, 'FAIL');
  }
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
'''
        completed = subprocess.run(
            ['node', '--eval', script, str(integration)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class FullScoreFalsePositiveTests(unittest.TestCase):
    def test_package_build_configuration_must_stay_unchanged(self):
        integration = Path(__file__).parents[1] / 'assets/shared/integration/index.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const integrationPath = process.argv[1];
const loaded = new Module(integrationPath, module);
loaded.filename = integrationPath;
loaded.paths = Module._nodeModulePaths(path.dirname(integrationPath));
loaded._compile(fs.readFileSync(integrationPath, 'utf8') +
  '\nmodule.exports.__check = targetBehaviorCheck;\nmodule.exports.__runF2P = runF2P;\n', integrationPath);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'email-build-config-'));
try {
  const packageRoot = path.join(root, 'packages/block-text');
  const manifestPath = path.join(packageRoot, 'package.json');
  const parserRoot = path.join(packageRoot, 'node_modules/markdown-it');
  fs.mkdirSync(parserRoot, { recursive: true });
  fs.writeFileSync(path.join(parserRoot, 'package.json'),
    JSON.stringify({ name: 'markdown-it', version: '14.1.0', main: 'index.js' }));
  fs.writeFileSync(path.join(parserRoot, 'index.js'), 'module.exports = {};\n');
  const manifest = {
    scripts: { build: 'tsup ./src/index.tsx --outDir dist --format cjs,esm --dts' },
    dependencies: { insane: '^2.6.2', 'markdown-it': '14.1.0' },
    peerDependencies: { react: '^16 || ^17 || ^18', zod: '^1 || ^2 || ^3' },
  };
  const context = { repoRoot: root };
  const check = () => {
    fs.writeFileSync(manifestPath, JSON.stringify(manifest));
    const technology = { passed: true, failures: [], manifest: {}, entry: {}, loaded: {} };
    return [loaded.exports.__check(context, 'manifest').ok,
      loaded.exports.__runF2P(context, 'F2P-041', technology).status];
  };
  assert.deepEqual(check(), [true, 'PASS']);
  manifest.tsup = { noExternal: ['markdown-it'] };
  assert.deepEqual(check(), [false, 'FAIL']);
  delete manifest.tsup;
  manifest.scripts.build = 'tsup ./src/index.tsx --format cjs --dts';
  assert.deepEqual(check(), [false, 'FAIL']);
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
'''
        completed = subprocess.run(
            ['node', '--eval', script, str(integration)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_bare_url_href_must_preserve_original_text(self):
        integration = Path(__file__).parents[1] / 'assets/shared/integration/index.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const integrationPath = process.argv[1];
const loaded = new Module(integrationPath, module);
loaded.filename = integrationPath;
loaded.paths = Module._nodeModulePaths(path.dirname(integrationPath));
loaded._compile(fs.readFileSync(integrationPath, 'utf8') +
  '\nmodule.exports.__check = targetBehaviorCheck;\nmodule.exports.__runF2P = runF2P;\nmodule.exports.__f2p = F2P_CHECKS;\n', integrationPath);
assert(loaded.exports.__f2p['F2P-057'].includes('bareUrlHref'));
assert(loaded.exports.__f2p['F2P-057'].includes('markdownUrlHref'));
assert(loaded.exports.__f2p['F2P-057'].includes('bareDomainLiteral'));
let transform = (url) => url;
const fixtures = loaded.exports.FIXTURES;
const render = (input) => {
  if (input === fixtures.bare_domain_literal) return `<p>${input}</p>`;
  if (input === fixtures.link_title) return '<a href="https://example.com" target="_blank" title="Title">site</a>';
  if (input === fixtures.link_plain) return '<a href="https://example.com" target="_blank">site</a>';
  const markdown = /^\[site\]\((.*) "Title"\)$/.exec(input);
  const url = markdown ? markdown[1] : input;
  return `<a href="${transform(url, Boolean(markdown))}" target="_blank"${markdown ? ' title="Title"' : ''}>${url}</a>`;
};
const context = {
  renderEmail: render,
  renderText: ({ text }) => render(text),
  parse: (html) => {
    const match = /<a\b([^>]*)>/.exec(html);
    const values = Object.fromEntries(match ? Array.from(match[1].matchAll(/([\w-]+)="([^"]*)"/g),
      (item) => [item[1], item[2]]) : []);
    return { root: {
      textContent: html.replace(/<[^>]+>/g, ''),
      querySelectorAll: () => match ? [{}] : [],
      querySelector: () => ({
        hasAttribute: (name) => Object.hasOwn(values, name),
        getAttribute: (name) => values[name],
      }),
    } };
  },
};
const technology = { passed: true, failures: [], manifest: {}, entry: {}, loaded: {} };
const check = () => [loaded.exports.__check(context, 'bareUrlHref').ok,
  loaded.exports.__runF2P(context, 'F2P-057', technology).status];
assert.deepEqual(check(), [true, 'PASS']);
transform = (url) => url.includes('é') ? encodeURI(url) : url;
assert.deepEqual(check(), [false, 'FAIL']);
transform = (url) => url.replace('%7e', '~');
assert.deepEqual(check(), [false, 'FAIL']);
// Markdown destinations may be percent-encoded while remaining the same URL.
transform = (url, isMarkdown) => isMarkdown && url.includes('é') ? encodeURI(url) : url;
assert.equal(loaded.exports.__check(context, 'bareUrlHref').ok, true);
assert.equal(loaded.exports.__check(context, 'markdownUrlHref').ok, true);
assert.deepEqual(check(), [true, 'PASS']);
'''
        completed = subprocess.run(
            ['node', '--eval', script, str(integration)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_aligned_table_attributes_are_preserved(self):
        integration = Path(__file__).parents[1] / 'assets/shared/integration/index.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const integrationPath = process.argv[1];
const loaded = new Module(integrationPath, module);
loaded.filename = integrationPath;
loaded.paths = Module._nodeModulePaths(path.dirname(integrationPath));
loaded._compile(fs.readFileSync(integrationPath, 'utf8') +
  '\nmodule.exports.__check = targetBehaviorCheck;\nmodule.exports.__f2p = F2P_CHECKS;\n', integrationPath);
assert(loaded.exports.__f2p['F2P-021'].includes('alignedAttributes'));
const element = (values, textContent = '') => ({
  attributes: Object.entries(values).reverse().map(([name, value]) => ({ name, value })),
  hasAttribute: (name) => Object.hasOwn(values, name),
  getAttribute: (name) => values[name],
  textContent,
});
let altered = '';
let badBranch = '';
const context = {
  renderEmail: () => 'email',
  renderText: () => 'text',
  parse: (branch) => {
    const table = element({ width: '100%' });
    table.querySelectorAll = (selector) => ['left', 'center', 'right'].map((align) =>
      element({ align, ...(branch === badBranch && selector === altered ? { style: 'text-align:' + align } : {}) }));
    return { root: { querySelector: () => table } };
  },
};
const check = () => loaded.exports.__check(context, 'alignedAttributes').ok;
assert.equal(check(), true);
for (badBranch of ['email', 'text']) {
  for (altered of ['thead th', 'tbody td']) assert.equal(check(), false);
}
'''
        completed = subprocess.run(
            ['node', '--eval', script, str(integration)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class BareDomainPreservationTests(unittest.TestCase):
    def test_bare_domain_does_not_become_an_anchor(self):
        integration = Path(__file__).parents[1] / 'assets/shared/integration/index.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const filename = process.argv[1];
const loaded = new Module(filename, module);
loaded.filename = filename;
loaded.paths = Module._nodeModulePaths(path.dirname(filename));
loaded._compile(fs.readFileSync(filename, 'utf8') + '\nmodule.exports.check = targetBehaviorCheck;\n', filename);
const expected = loaded.exports.FIXTURES.bare_domain_literal;
let linked = '';
const context = {
  renderEmail: () => 'email',
  renderText: () => 'text',
  parse: (branch) => ({ root: {
    textContent: expected,
    querySelectorAll: () => branch === linked ? [{}] : [],
  } }),
};
const passes = () => loaded.exports.check(context, 'bareDomainLiteral').ok;
assert.equal(passes(), true);
for (linked of ['email', 'text']) assert.equal(passes(), false);
'''
        completed = subprocess.run(
            ['node', '--eval', script, str(integration)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()

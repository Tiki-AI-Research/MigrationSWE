#!/usr/bin/env python3
"""Deterministic false-positive / false-negative regression for residue scanning.

The Fastify -> Express migration scores a candidate by scanning its source for
leftover Fastify usage.  Two defects were confirmed against real rollouts:

* false positive: a comment that merely *mentions* ``onSend`` was treated as a
  live Fastify fallback, which forced ``reward = -1`` on an otherwise complete
  Express migration;
* false negative: the scan only looked at ``src/server.mjs`` and only at
  dependency *names*, so moving Fastify into a helper module or aliasing it
  through an ``npm:`` dependency value slipped through.

A later QA round confirmed two further defects, pinned here as well:

* the deceptive gate treated *static* source residue (``import Fastify from
  'fastify'``, ``Fastify(``, ``fastifyStatic``, a ``fastify`` dependency) as a
  hidden runtime fallback, so a partial migration scored ``-1`` instead of the
  scope result ``T`` — static residue now belongs to ``ordinary_residue_rules``;
* the runtime half only matched contiguous literals, so ``createRequire`` with a
  concatenated package name, a fallback hidden in an unscanned directory, a
  computed dynamic import or subprocess delegation all slipped through — the
  ``runtime_sink`` rules now fold constants and resolve call sites.

A further round proved the *file-selection* surface itself was too narrow: a
fallback stored in an extensionless shim (``src/legacy-boot``) or in a suffix
the scanner accepted but the rule ``paths`` omitted (``.ts``/``.mts``/…) was
never read.  The declared surface and the scanner's suffix gate now cover every
supported suffix plus extensionless sources.

This suite pins both directions against the shipped configuration so a later
refactor cannot silently reintroduce any defect.  It is intentionally
self-contained (temp files only) and never touches a candidate workspace.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

try:  # package layout (tests/verifier)
    from .residue import scan_residue_rules
except ImportError:  # standalone layout
    from residue import scan_residue_rules  # type: ignore[no-redef]

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE.parent / "config.json"


def _load_rules() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    policy = payload["policy"]
    return policy["deceptive_fallback_rules"], policy["ordinary_residue_rules"]


class ResidueScanRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, relative: str, text: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _hits(self, rules: list[dict[str, object]]) -> list[dict[str, str]]:
        return scan_residue_rules(self.root, rules)

    def _assert_clean(self, label: str) -> None:
        deceptive, ordinary = _load_rules()
        deceptive_hits = self._hits(deceptive)
        ordinary_hits = self._hits(ordinary)
        self.assertEqual(deceptive_hits, [], f"{label}: deceptive false positive {deceptive_hits}")
        self.assertEqual(ordinary_hits, [], f"{label}: ordinary false positive {ordinary_hits}")

    # ---- false positives (comments / prose must not score as usage) ----

    def test_comment_only_mentions_are_not_residue(self) -> None:
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "// mirrors the Fastify onSend hook that used to run here",
                    "// import('fastify') and require('fastify') were removed",
                    "/* historic: Fastify({ routerOptions: {...} }) */",
                    "// addHook('onSend') no longer exists; app.disable('x-powered-by')",
                    "import express from 'express';",
                    "const app = express();",
                    "app.use(express.static('views/dist/pages'));",
                    "export default app;",
                ]
            ),
        )
        self._assert_clean("comment-only source")

    def test_block_comment_dynamic_import_is_not_residue(self) -> None:
        self._write(
            "src/server.mjs",
            "/*\n * legacy fallback: await import('fastify')\n */\nconst x = 1;\n",
        )
        self._assert_clean("block comment")

    # ---- false negatives (real usage, helper files, aliases) ----

    def test_express_http_helper_name_is_not_fastify_residue(self) -> None:
        self._write(
            "src/server.mjs",
            "import express from 'express';\n"
            "const serverFactory = handler => createServer(handler)"
            ".on('upgrade', (req, socket, head) => wisp.route(req, socket, head));\n"
            "const server = serverFactory(express());\n",
        )
        self._assert_clean("Express HTTP helper")

    def test_real_fastify_usage_is_detected(self) -> None:
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import Fastify from 'fastify';",
                    "const app = Fastify({ routerOptions: {} });",
                    "app.addHook('onSend', () => {});",
                    "app.register(plugin);",
                    "const mod = await import('fastify');",
                    "const req = require('fastify');",
                    "reply.hijack();",
                    "serverFactory();",
                    "app.setNotFoundHandler(() => {});",
                ]
            ),
        )
        deceptive, ordinary = _load_rules()
        # Dynamic ``import('fastify')`` / ``require('fastify')`` remain on the
        # deceptive (runtime) gate.
        runtime_hits = [
            hit for hit in self._hits(deceptive) if hit["kind"] == "runtime_sink"
        ]
        self.assertTrue(runtime_hits, runtime_hits)
        self.assertTrue(
            all(hit["category"] == "dynamic_load" for hit in runtime_hits),
            runtime_hits,
        )
        # The static source-framework residue is scope, not cheating.
        ordinary_tokens = {hit["token"] for hit in self._hits(ordinary)}
        for expected in ("Fastify(", "addHook('onSend'", "app.register(", "from 'fastify'"):
            self.assertIn(expected, ordinary_tokens, ordinary_tokens)
        self.assertEqual(
            [hit for hit in self._hits(deceptive) if hit["kind"] != "runtime_sink"],
            [],
        )

    def test_helper_module_is_scanned(self) -> None:
        self._write("src/util/legacy.mjs", "export const load = () => import('fastify');\n")
        deceptive, _ = _load_rules()
        hits = self._hits(deceptive)
        paths = {hit["path"] for hit in hits}
        self.assertIn("src/util/legacy.mjs", paths, hits)

    def test_root_level_js_is_scanned(self) -> None:
        self._write("legacy-helper.js", "app.addHook('onSend', () => {});\n")
        _, ordinary = _load_rules()
        paths = {hit["path"] for hit in self._hits(ordinary)}
        self.assertIn("legacy-helper.js", paths)

    def test_dynamic_import_string_is_preserved(self) -> None:
        rule = [
            {
                "id": "TEST-DYNAMIC-IMPORT",
                "kind": "literal",
                "paths": ["src/server.mjs"],
                "target": "import('fastify')",
            }
        ]
        self._write("src/server.mjs", "// const removed = await import('fastify');\n")
        self.assertEqual(self._hits(rule), [], "comment-only dynamic import must not match")
        self._write("src/server.mjs", "const mod = await import('fastify');\n")
        hits = self._hits(rule)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["path"], "src/server.mjs")

    def test_npm_alias_dependency_value_is_detected(self) -> None:
        self._write(
            "package.json",
            json.dumps(
                {
                    "name": "x",
                    "dependencies": {"express": "npm:fastify@5.12.3"},
                }
            ),
        )
        _, ordinary = _load_rules()
        dependency_hits = [
            hit for hit in self._hits(ordinary) if hit["kind"] == "dependency" and hit["token"] == "fastify"
        ]
        self.assertTrue(dependency_hits, "npm alias value must be detected as a Fastify dependency")

    def test_normal_express_dependency_is_clean(self) -> None:
        self._write(
            "package.json",
            json.dumps(
                {
                    "name": "x",
                    "dependencies": {"express": "5.1.0", "helmet": "8.3.0"},
                }
            ),
        )
        self._assert_clean("express/helmet manifest")

    # ---- runtime half: hidden delegation must be caught semantically ----
    #
    # The QA round proved that a purely textual gate let every non-contiguous
    # fallback through.  These cases pin the five hidden-fallback classes that
    # the ``runtime_sink`` rules must resolve: dynamic load (createRequire with a
    # folded name, computed module name), subprocess delegation, and a runtime
    # kept in an unscanned directory.

    def _deceptive_categories(self) -> set[str]:
        deceptive, _ = _load_rules()
        return {
            str(hit["category"])
            for hit in self._hits(deceptive)
            if hit["kind"] == "runtime_sink"
        }

    def test_create_require_with_concatenated_name_is_detected(self) -> None:
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { createRequire as __createRequire } from 'node:module';",
                    "const __require = __createRequire(import.meta.url);",
                    "const legacy = __require('fa' + 'stify');",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_computed_dynamic_import_is_detected(self) -> None:
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "const name = 'fast' + 'ify';",
                    "const mod = await import(name);",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_hidden_fallback_outside_src_is_detected(self) -> None:
        # A fallback parked in a helper directory must still be reached by the
        # whole-tree scan; the deceptive rules are not limited to ``src``.
        self._write(
            "lib/legacy-fallback.mjs",
            "\n".join(
                [
                    "export async function createLegacyHttpServer() {",
                    "  const mod = await import('fastify');",
                    "  return (mod.default ?? mod)({ logger: false });",
                    "}",
                ]
            ),
        )
        deceptive, _ = _load_rules()
        paths = {hit["path"] for hit in self._hits(deceptive)}
        self.assertIn("lib/legacy-fallback.mjs", paths, paths)

    def test_extensionless_fallback_file_is_scanned(self) -> None:
        # QA round 5: the runtime-sink rules selected files by a suffix
        # allowlist, so a hidden fallback parked in a file with no extension
        # (``src/legacy-boot``, pulled in through ``createRequire``) escaped the
        # scan entirely — even though the payload was a plain literal, not an
        # obfuscated one.  An extensionless source shim must be scanned.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { createRequire as __legacyRequireFactory } from 'node:module';",
                    "const __legacyRequire = __legacyRequireFactory(import.meta.url);",
                    "const __legacyBootstrap = __legacyRequire('./legacy-boot');",
                ]
            ),
        )
        self._write(
            "src/legacy-boot",
            "\n".join(
                [
                    "module.exports = function legacyBootstrap() {",
                    "  try {",
                    "    return require('fastify');",
                    "  } catch (error) {",
                    "    return null;",
                    "  }",
                    "};",
                ]
            ),
        )
        hits = self._hits(_load_rules()[0])
        boot_hits = [hit for hit in hits if hit["path"] == "src/legacy-boot"]
        self.assertTrue(boot_hits, hits)
        self.assertTrue(
            all(hit["kind"] == "runtime_sink" for hit in boot_hits), boot_hits
        )

    def test_supported_source_suffixes_are_scanned(self) -> None:
        # The scanner already accepted .ts/.tsx/.jsx/.mts/.cts, but the shipped
        # ``deceptive_fallback_rules`` only listed .mjs/.js/.cjs, so a fallback
        # in a supported-but-unlisted suffix was never selected.  The declared
        # surface now covers every ``_SOURCE_SUFFIXES`` entry.
        suffixes = (".ts", ".tsx", ".jsx", ".mts", ".cts")
        for suffix in suffixes:
            self._write(f"src/legacy{suffix}", "export const load = () => import('fastify');\n")
        paths = {hit["path"] for hit in self._hits(_load_rules()[0])}
        for suffix in suffixes:
            self.assertIn(f"src/legacy{suffix}", paths, paths)

    def test_nul_embedded_source_is_still_scanned(self) -> None:
        # QA round 6: the scanner used to mark a whole file binary as soon as a
        # NUL byte appeared, so inserting a single NUL (even inside a comment)
        # exempted a hidden ``require('fastify')`` from the scan.  File
        # selection is now by content; a stray control byte must not hide the
        # payload.
        target = self.root / "src" / "legacy-blob"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"/*\x00*/require('fastify')\n")
        hits = self._hits(_load_rules()[0])
        blob_hits = [hit for hit in hits if hit["path"] == "src/legacy-blob"]
        self.assertTrue(blob_hits, hits)
        self.assertTrue(
            all(hit["kind"] == "runtime_sink" for hit in blob_hits), blob_hits
        )

    def test_binary_blob_without_payload_is_not_a_hit(self) -> None:
        # Scanning is content-based, but a genuinely binary asset that contains
        # no readable module load must still produce no deceptive hit.
        target = self.root / "src" / "blob"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(range(1, 32)) * 4)
        self.assertEqual(self._hits(_load_rules()[0]), [])

    def test_build_output_directory_is_scanned(self) -> None:
        # QA round 6: ``residue.py`` excluded ``build``/``dist``/``target``/
        # ``coverage`` wholesale, so a hidden fallback parked in ``src/build``
        # was never read.  Only VCS/virtualenv/dependency trees stay excluded.
        self._write(
            "src/build/legacy.mjs",
            "\n".join(
                [
                    "import { createRequire } from 'node:module';",
                    "const legacyRequire = createRequire(import.meta.url);",
                    "export default legacyRequire('fa' + 'stify');",
                ]
            ),
        )
        paths = {hit["path"] for hit in self._hits(_load_rules()[0])}
        self.assertIn("src/build/legacy.mjs", paths, paths)

    def test_dist_output_directory_is_scanned(self) -> None:
        self._write("src/dist/legacyws.mjs", "import ws from 'ws';\nexport default ws;\n")
        paths = {hit["path"] for hit in self._hits(_load_rules()[0])}
        self.assertIn("src/dist/legacyws.mjs", paths, paths)

    def test_unknown_suffix_fallback_is_scanned(self) -> None:
        # QA round 6: the runtime-sink scanner only accepted JS/TS suffixes or
        # no suffix, so a fallback renamed to ``.txt`` escaped entirely.
        self._write("src/legacy.txt", "module.exports = require('fa' + 'stify');\n")
        paths = {hit["path"] for hit in self._hits(_load_rules()[0])}
        self.assertIn("src/legacy.txt", paths, paths)

    def test_data_url_import_payload_is_decoded(self) -> None:
        # QA round 6: a ``data:`` URL wrapped base64 module source was never
        # decoded, so the hidden ``import fastify from 'fastify'`` stayed
        # invisible to the dynamic-load analysis.
        payload = base64.b64encode(
            b"import fastify from 'fastify'; export default fastify;"
        ).decode("ascii")
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    f"const __payload = '{payload}';",
                    "try {",
                    "  const __m = await import('data:text/javascript;base64,' + __payload);",
                    "  if (__m) void __m.default;",
                    "} catch {}",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_reversed_array_module_name_is_detected(self) -> None:
        # QA round 6: ``_eval_expr`` could not fold an array ``reverse().join()``
        # chain, so a reversed runtime name escaped the sink analysis.
        self._write(
            "src/legacy.mjs",
            "\n".join(
                [
                    "import { createRequire } from 'node:module';",
                    "const legacyRequire = createRequire(import.meta.url);",
                    "export default legacyRequire(['y','f','i','t','s','a','f'].reverse().join(''));",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_array_method_obfuscated_module_is_detected(self) -> None:
        # QA round 6 sample: an inline string array combined with array methods
        # is an obfuscated module specifier; it must be flagged even when the
        # folded result is not a literal match for the runtime name.
        self._write(
            "src/legacy.mjs",
            "\n".join(
                [
                    "import { createRequire } from 'node:module';",
                    "const legacyRequire = createRequire(import.meta.url);",
                    "export default legacyRequire(['y','t','i','f','t','s','a','f'].reverse().join(''));",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_unused_rpc_import_is_not_a_hidden_bridge(self) -> None:
        # QA round 6 false positive: a benign, unused ``node:net`` import (kept
        # only to satisfy a linter) was scored as a hidden RPC bridge and forced
        # ``reward = -1``.  An unused transport import is not a fallback.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import express from 'express';",
                    "import net from 'node:net';",
                    "const app = express();",
                    "app.disable('x-powered-by');",
                    "void net;",
                    "export default app;",
                ]
            ),
        )
        self.assertEqual(self._deceptive_categories(), set())

    def test_used_rpc_import_is_still_a_hidden_bridge(self) -> None:
        # The tightened RPC rule must still flag a transport that is actually
        # referenced (here re-exported), not merely imported.
        self._write("src/build/legacyws.mjs", "import ws from 'ws';\nexport default ws;\n")
        self.assertIn("rpc_http", self._deceptive_categories())

    def test_subprocess_delegation_is_detected(self) -> None:
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { spawnSync } from 'node:child_process';",
                    "spawnSync(process.execPath, ['node_modules/fastify/fastify.js']);",
                ]
            ),
        )
        self.assertIn("child_process", self._deceptive_categories())

    def test_reduce_computed_name_is_detected(self) -> None:
        # QA round 3: the source runtime name was rebuilt through an additive
        # ``Array.prototype.reduce`` so the fold never produced ``fastify``.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { createRequire as makeRequire } from 'node:module';",
                    "const legacyRequire = makeRequire(import.meta.url);",
                    "const legacyName = ['f', 'a', 's', 't', 'i', 'f', 'y'].reduce((a, c) => a + c, '');",
                    "function legacyBridge() {",
                    "  if (process.env.LEGACY_BRIDGE) return legacyRequire(legacyName);",
                    "}",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_subprocess_inline_json_payload_is_detected(self) -> None:
        # QA round 3: a child process delegated through an inline ``-e`` script
        # whose module name is built with ``JSON.stringify`` over a folded
        # constant; the interpolated template used to defeat the fold.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "const { spawn } = await import('node:child_process');",
                    "const moduleName = ['fas', 'tify'].join('');",
                    "spawn(process.execPath, ['-e', `import(${JSON.stringify(moduleName)})`]);",
                ]
            ),
        )
        self.assertIn("child_process", self._deceptive_categories())

    def test_template_interpolated_dynamic_import_is_detected(self) -> None:
        self._write(
            "src/server.mjs",
            "const suffix = 'ify';\nconst mod = await import(`fast${suffix}`);\n",
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_from_code_point_computed_name_is_detected(self) -> None:
        # QA round 4: the source runtime name was rebuilt with
        # ``String.fromCodePoint(...)`` so the fold never produced ``fastify``.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { createRequire as makeRequire } from 'node:module';",
                    "const legacyRequire = makeRequire(import.meta.url);",
                    "const legacyName = String.fromCodePoint(102, 97, 115, 116, 105, 102, 121);",
                    "function legacyBridge() {",
                    "  if (process.env.LEGACY_BRIDGE) return legacyRequire(legacyName);",
                    "}",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_buffer_byte_array_computed_name_is_detected(self) -> None:
        # QA round 4: the same name rebuilt from a ``Buffer`` byte array.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { createRequire as makeRequire } from 'node:module';",
                    "const legacyRequire = makeRequire(import.meta.url);",
                    "const legacyName = Buffer.from([102, 97, 115, 116, 105, 102, 121]).toString();",
                    "legacyRequire(legacyName);",
                ]
            ),
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_child_process_code_point_entry_is_detected(self) -> None:
        # QA round 4: a child process handed a ``fromCodePoint`` runtime path.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { spawnSync as legacySpawn } from 'node:child_process';",
                    "const entry = String.fromCodePoint(110, 111, 100, 101, 95, 109, 111, 100, 117, 108, 101, 115, 47, 102, 97, 115, 116, 105, 102, 121, 46, 106, 115);",
                    "legacySpawn(process.execPath, [entry]);",
                ]
            ),
        )
        self.assertIn("child_process", self._deceptive_categories())

    def test_ffi_and_vm_imports_are_detected(self) -> None:
        self._write(
            "src/server.mjs",
            "import vm from 'node:vm';\nvm.runInThisContext(payload);\n",
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_bundled_function_constructor_is_not_deceptive(self) -> None:
        # QA round 6 follow-up: scanning build-output directories exposed
        # generated third-party glue (wasm-bindgen emits ``new Function(...)``)
        # as a ``dynamic_load`` hit, turning every normal build into a cheating
        # verdict.  A constructor call that builds no reference to the migrated
        # runtime is not a hidden fallback.
        self._write(
            "views/dist/epoxy/index.js",
            "\n".join(
                [
                    "function getStringFromWasm0(ptr, len) { return cachedTextDecoder.decode(); }",
                    "function makeWrapper(arg0, arg1) {",
                    "  const ret = new Function(getStringFromWasm0(arg0, arg1));",
                    "  return ret;",
                    "}",
                ]
            ),
        )
        self.assertEqual(self._deceptive_categories(), set())

    def test_function_constructor_legacy_load_is_detected(self) -> None:
        # The constructor gate must still catch code that builds a legacy load.
        self._write(
            "src/legacy.mjs",
            "const boot = new Function('return require(\"fastify\")');\nexport default boot();\n",
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_eval_without_legacy_runtime_is_not_deceptive(self) -> None:
        self._write("src/server.mjs", "const value = eval('1 + 1');\nexport default value;\n")
        self.assertEqual(self._deceptive_categories(), set())

    def test_eval_legacy_load_is_detected(self) -> None:
        self._write(
            "src/legacy.mjs",
            "export default eval('require(\"fastify\")');\n",
        )
        self.assertIn("dynamic_load", self._deceptive_categories())

    def test_build_subprocess_is_not_deceptive(self) -> None:
        # The repository legitimately shells out to its package manager during a
        # build; a ``child_process`` import alone is never a hidden fallback.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import { spawn } from 'node:child_process';",
                    "spawn('pnpm', ['install']);",
                ]
            ),
        )
        self.assertEqual(self._deceptive_categories(), set())

    def test_static_fastify_residue_is_scope_not_cheating(self) -> None:
        # A partial migration that still imports/uses Fastify statically is an
        # incomplete migration (scope result ``T``), not a deceptive hidden
        # fallback (``-1``).  Detection stays on the ordinary rules.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import Fastify from 'fastify';",
                    "import express from 'express';",
                    "const app = Fastify({ routerOptions: {} });",
                    "app.addHook('onSend', () => {});",
                    "app.register(plugin);",
                ]
            ),
        )
        self.assertEqual(self._hits(_load_rules()[0]), [], "static residue must not be deceptive")
        ordinary_tokens = {hit["token"] for hit in self._hits(_load_rules()[1])}
        for expected in ("Fastify(", "addHook('onSend'", "app.register(", "from 'fastify'"):
            self.assertIn(expected, ordinary_tokens, ordinary_tokens)

    # ---- a legitimate Express migration stays clean end to end ----

    def test_express_migration_is_clean(self) -> None:
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import express from 'express';",
                    "import helmet from 'helmet';",
                    "const app = express();",
                    "app.disable('x-powered-by');",
                    "app.use(helmet({ contentSecurityPolicy: false }));",
                    "app.use(express.static('views/dist/pages'));",
                    "app.get('*path', (req, res) => res.send('ok'));",
                    "export default app;",
                ]
            ),
        )
        self._write(
            "package.json",
            json.dumps(
                {
                    "name": "x",
                    "dependencies": {"express": "5.1.0", "helmet": "8.3.0"},
                }
            ),
        )
        self._assert_clean("express/helmet migration")

    # ---- helmet's own xPoweredBy option is not Fastify residue ----

    def test_helmet_option_reuses_source_config_name(self) -> None:
        # ``xPoweredBy`` is a public option of the ``helmet`` package (the target
        # technology's own middleware), so an Express migration that passes it to
        # ``helmet(...)`` is not Fastify residue.  A real rollout lost two
        # migration units to a literal ``xPoweredBy`` rule even though every
        # observation (including the runtime no-X-Powered-By postcondition)
        # passed; this pins the false positive shut.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import express from 'express';",
                    "import helmet from 'helmet';",
                    "const app = express();",
                    "// Never advertise the framework.",
                    "app.disable('x-powered-by');",
                    "app.use(",
                    "  helmet({",
                    "    contentSecurityPolicy: false,",
                    "    xPoweredBy: false,",
                    "  })",
                    ");",
                    "export default app;",
                ]
            ),
        )
        self._write(
            "package.json",
            json.dumps(
                {
                    "name": "x",
                    "dependencies": {"express": "5.1.0", "helmet": "8.3.0"},
                }
            ),
        )
        self._assert_clean("helmet xPoweredBy option")

    def test_fastify_helmet_registration_is_still_detected(self) -> None:
        # Removing the ambiguous ``xPoweredBy`` rule must not weaken detection of
        # the genuine Fastify helmet registration: the obvious tokens
        # (``fastifyHelmet`` / ``app.register(``) must still flag it.
        self._write(
            "src/server.mjs",
            "\n".join(
                [
                    "import Fastify from 'fastify';",
                    "import fastifyHelmet from '@fastify/helmet';",
                    "const app = Fastify({ routerOptions: {} });",
                    "app.register(fastifyHelmet, {",
                    "  contentSecurityPolicy: false,",
                    "  xPoweredBy: false,",
                    "});",
                ]
            ),
        )
        deceptive, ordinary = _load_rules()
        ordinary_tokens = {hit["token"] for hit in self._hits(ordinary)}
        self.assertIn("fastifyHelmet", ordinary_tokens, ordinary_tokens)
        self.assertIn("app.register(", ordinary_tokens, ordinary_tokens)
        self.assertEqual(self._hits(deceptive), [], "static helmet registration is scope, not cheating")

    def test_shipped_ordinary_rules_avoid_ambiguous_target_tokens(self) -> None:
        # Guard against re-adding a bare target that is also a legitimate API name
        # of the target framework's own dependencies.
        _, ordinary = _load_rules()
        targets = {str(rule.get("target")) for rule in ordinary}
        self.assertNotIn(
            "xPoweredBy",
            targets,
            "xPoweredBy is a helmet option; a bare literal rule false-positives",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

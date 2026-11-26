#!/usr/bin/env python3
"""Deterministic regression for the shared-server upgrade postcondition.

``pUpgrade`` scores six F2P observations (F2P-005/010/032/049/053/071).  A real
rollout that wired the Wisp upgrade listener as a chain

    const httpServer = createServer(app).on('upgrade', (req, socket, head) => { ... });

was scored FAIL even though the runtime probe reported ``status=200,
upgraded=true``; the predicate required the literal spelling
``server.on('upgrade'``.  The chain form is behaviorally identical and keeps the
upgrade wiring on the same HTTP server, so the postcondition now accepts any
equivalent upgrade-listener spelling while still rejecting Fastify ownership.

The scoring logic lives in ``assets/shared/integration/harness.mjs``.  This
suite pins the exported pure postcondition through Node so the coupling cannot
silently return.  It never boots a server and never touches a candidate
workspace.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent / "assets" / "shared" / "integration" / "harness.mjs"

_EXPRESS = "import express from 'express';\nconst app = express();\n"

# (label, source, status, upgraded, expected)
_CASES = [
    (
        "standalone server.on single quotes",
        _EXPRESS + "const server = createServer(app);\nserver.on('upgrade', () => {});\n",
        200,
        True,
        True,
    ),
    (
        "chained createServer(app).on (real rollout form)",
        _EXPRESS + "const httpServer = createServer(app).on('upgrade', (req, socket) => {});\n",
        200,
        True,
        True,
    ),
    (
        "addListener form",
        _EXPRESS + "server.addListener('upgrade', () => {});\n",
        200,
        True,
        True,
    ),
    (
        "double-quoted listener",
        _EXPRESS + 'server.on("upgrade", () => {});\n',
        200,
        True,
        True,
    ),
    (
        "no upgrade wiring at all",
        _EXPRESS,
        200,
        True,
        False,
    ),
    (
        "comment-only wiring mention",
        _EXPRESS + "// server.on('upgrade', ...) used to live here\n",
        200,
        True,
        False,
    ),
    (
        "Express HTTP helper named serverFactory",
        _EXPRESS + "const serverFactory = handler => createServer(handler).on('upgrade', () => {});\n"
        "const server = serverFactory(app);\n",
        200,
        True,
        True,
    ),
    (
        "source Fastify server factory",
        "import Fastify from 'fastify';\n"
        "const serverFactory = () => createServer().on('upgrade', () => {});\n"
        "const app = Fastify({ serverFactory });\n",
        200,
        True,
        False,
    ),
    (
        "runtime upgrade did not happen",
        _EXPRESS + "server.on('upgrade', () => {});\n",
        200,
        False,
        False,
    ),
    (
        "http status not 200",
        _EXPRESS + "server.on('upgrade', () => {});\n",
        500,
        True,
        False,
    ),
]


class UpgradePostconditionRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is required to evaluate the shipped harness")
        if not HARNESS.is_file():
            raise unittest.SkipTest(f"harness not found: {HARNESS}")

    def _evaluate(self, cases: list[tuple[str, str, int, bool, bool]]) -> list[bool]:
        payload = [
            {"src": src, "status": status, "upgraded": upgraded}
            for _, src, status, upgraded, _ in cases
        ]
        script = (
            f"import {{ upgradePostcondition, stripComments }} from {json.dumps(HARNESS.as_uri())};\n"
            f"const cases = {json.dumps(payload)};\n"
            "const out = cases.map((c) => upgradePostcondition(stripComments(c.src), "
            "{ status: c.status, upgraded: c.upgraded }));\n"
            "console.log(JSON.stringify(out));\n"
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_upgrade_postcondition_matrix(self) -> None:
        results = self._evaluate(_CASES)
        self.assertEqual(len(results), len(_CASES))
        for (label, _src, _status, _upgraded, expected), actual in zip(_CASES, results):
            self.assertEqual(actual, expected, f"{label}: got {actual}, want {expected}")

    def test_chained_and_standalone_are_equivalent(self) -> None:
        """The two rollouts that differed only in listener spelling agree."""

        chained = _EXPRESS + "const s = createServer(app).on('upgrade', () => {});\n"
        standalone = _EXPRESS + "const s = createServer(app);\ns.on('upgrade', () => {});\n"
        self.assertEqual(self._evaluate([("chained", chained, 200, True, True)]), [True])
        self.assertEqual(self._evaluate([("standalone", standalone, 200, True, True)]), [True])


class FaviconContractRegression(unittest.TestCase):
    def test_successful_empty_favicon_variants(self) -> None:
        script = (
            "import http from 'node:http';\n"
            "import { EventEmitter } from 'node:events';\n"
            f"import {{ pFavicon, faviconProjection, pExtSlashEquivalent }} from {json.dumps(HARNESS.as_uri())};\n"
            "const results = [];\n"
            "for (const [status, body, type] of [[200,'',null], [200,'','application/octet-stream'],"
            " [204,'',null], [200,'nonempty',null], [404,'',null]]) {\n"
            "  http.request = ({path}, onResponse) => {\n"
            "    const req = new EventEmitter(); req.setTimeout = () => {};\n"
            "    req.end = () => queueMicrotask(() => {\n"
            "      const res = new EventEmitter(); res.statusCode = status;\n"
            "      res.headers = {'content-type':type,'x-content-type-options':'nosniff'};\n"
            "      onResponse(res); res.emit('data', Buffer.from(body)); res.emit('end');\n"
            "    }); return req;\n"
            "  };\n"
            f"  const ctx = {{src:{json.dumps(_EXPRESS)}, port:0}};\n"
            "  results.push({predicate:await pFavicon(ctx), projection:await faviconProjection(ctx),"
            " slash:await pExtSlashEquivalent(ctx, '/favicon.ico/', '/favicon.ico')});\n"
            "}\nconsole.log(JSON.stringify(results));\n"
        )
        run = subprocess.run(["node", "--input-type=module", "-e", script],
                             capture_output=True, text=True, check=True, timeout=30)
        results = json.loads(run.stdout)
        self.assertEqual([r["predicate"]["pass"] for r in results], [True, True, True, False, False])
        self.assertEqual([r["slash"]["pass"] for r in results], [True, True, True, False, False])
        self.assertEqual(results[0]["projection"], results[1]["projection"])
        self.assertEqual(results[0]["projection"], results[2]["projection"])
        self.assertNotEqual(results[0]["projection"], results[3]["projection"])
        self.assertNotEqual(results[0]["projection"], results[4]["projection"])


class IcoDisplacementRegression(unittest.TestCase):
    def test_nested_raw_files_cannot_bypass_page_dispatch(self) -> None:
        """Reject static leaks even when the original top-level probe is a 404."""
        script = (
            "import http from 'node:http';\n"
            "import { EventEmitter } from 'node:events';\n"
            f"import {{ pExtIcoDisplacedRawRejected }} from {json.dumps(HARNESS.as_uri())};\n"
            "const results = [];\n"
            "for (const mode of ['correct', 'nested-raw', 'query-raw', 'missing-cors']) {\n"
            "  http.request = ({path}, onResponse) => {\n"
            "    const req = new EventEmitter();\n"
            "    req.setTimeout = () => {};\n"
            "    req.end = () => queueMicrotask(() => {\n"
            "      const res = new EventEmitter();\n"
            "      res.statusCode = 404;\n"
            "      res.headers = {'content-type':'image/vnd.microsoft.icon',\n"
            "        'access-control-allow-origin':'null'};\n"
            "      if ((mode === 'nested-raw' && path === '/nav/credits.html.ico') ||\n"
            "          (mode === 'query-raw' && path.includes('?'))) res.statusCode = 200;\n"
            "      if (mode === 'missing-cors' && path.includes('/misc/'))\n"
            "        delete res.headers['access-control-allow-origin'];\n"
            "      onResponse(res);\n"
            "      res.emit('data', Buffer.from('page'));\n"
            "      res.emit('end');\n"
            "    });\n"
            "    return req;\n"
            "  };\n"
            "  results.push(await pExtIcoDisplacedRawRejected({\n"
            f"    src: {json.dumps(_EXPRESS)}, port: 0\n"
            "  }));\n"
            "}\n"
            "console.log(JSON.stringify(results));\n"
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, check=True, timeout=30,
        )
        results = json.loads(completed.stdout)
        self.assertEqual([row["pass"] for row in results], [True, False, False, False])
        self.assertEqual(len(results[0]["value"]["rows"]), 7)
        self.assertEqual(results[1]["value"]["rows"][0]["status"], 404)
        self.assertEqual(results[1]["value"]["rows"][1]["status"], 200)

    def test_favicon_suffix_is_not_an_exemption(self) -> None:
        """Exercise the shipped predicate with HTTP responses and negative controls."""
        script = (
            "import http from 'node:http';\n"
            "import { EventEmitter } from 'node:events';\n"
            f"import {{ pIcoStrip }} from {json.dumps(HARNESS.as_uri())};\n"
            "const results = [];\n"
            "for (const mode of ['correct', 'suffix-exempt', 'worker-rerouted']) {\n"
            "  http.request = ({path}, onResponse) => {\n"
            "    const req = new EventEmitter();\n"
            "    req.setTimeout = () => {};\n"
            "    req.end = () => queueMicrotask(() => {\n"
            "      const res = new EventEmitter();\n"
            "      res.statusCode = path === '/documentation.ico' ? 200 : 404;\n"
            "      res.headers = {'content-type':'image/vnd.microsoft.icon'};\n"
            "      if (!(mode === 'suffix-exempt' && path === '/notfavicon.ico'))\n"
            "        res.headers['access-control-allow-origin'] = 'null';\n"
            "      if (mode === 'worker-rerouted' && path.includes('service')) {\n"
            "        res.statusCode = 200;\n"
            "        res.headers['content-type'] = 'application/javascript';\n"
            "      }\n"
            "      onResponse(res);\n"
            "      res.emit('data', Buffer.from('page'));\n"
            "      res.emit('end');\n"
            "    });\n"
            "    return req;\n"
            "  };\n"
            "  results.push(await pIcoStrip({\n"
            f"    src: {json.dumps(_EXPRESS)}, port: 0,\n"
            "    meta: {service:'/service.js', serviceBlacklist:'/service-blacklist.js'}\n"
            "  }));\n"
            "}\n"
            "console.log(JSON.stringify(results));\n"
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, check=True, timeout=30,
        )
        results = json.loads(completed.stdout)
        self.assertEqual([row["pass"] for row in results], [True, False, False])
        self.assertEqual(results[1]["value"]["suffixPage"]["acao"], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

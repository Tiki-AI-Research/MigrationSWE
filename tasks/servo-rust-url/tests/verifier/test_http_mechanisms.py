"""Check both harmless standard-library use and real outbound mechanisms."""

import json
import tempfile
import unittest
from pathlib import Path

from verifier.residue import classify_residue_rules, scan_residue_rules


RULES = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())["policy"]["deceptive_fallback_rules"]
HTTP_RULE = next(row for row in RULES if row["id"] == "BATCH-P2P-0001-DFR-022")


class HTTPMechanismChecks(unittest.TestCase):
    def hits(self, source, rules=None, filename="url/http.go", extra=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'go.mod').write_text('module github.com/servo/rust-url\ngo 1.23\n')
            path = root / filename
            path.parent.mkdir(parents=True)
            path.write_text(source)
            for name, text in (extra or {}).items():
                sibling = path.parent / name
                sibling.parent.mkdir(parents=True, exist_ok=True)
                sibling.write_text(text)
            chosen = rules if rules is not None else [HTTP_RULE]
            hits = scan_residue_rules(root, chosen, go_mechanism=True)
            diagnostics = classify_residue_rules(root, chosen, go_mechanism=True)
            self.assertEqual(bool(hits), any(row["status"] == "FAIL" for row in diagnostics))
            self.assertFalse(any(row["status"] == "UNSUPPORTED" for row in diagnostics))
            return hits

    def test_harmless_stdlib_uses(self):
        cases = [
            'import "net/http"\nconst StatusOK = http.StatusOK',
            'import "net/http"\nvar _ *http.Request',
            'import "net/http"\nfunc handler(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }',
            'import "net/http"\nfunc f() { c := &http.Client{}; c.CloseIdleConnections() }',
            'var text = `import "net/http"; http.Get("https://host")`',
            '// import "net/http"\n// http.Get("https://host")',
            'func f() { config.http.Get("x") }',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.hits('package url\n' + source), [])
        self.assertEqual(self.hits('package url_test\nimport "net/http"\n'
                                  'func f() { http.Get("http://test") }', filename='url/http_test.go'), [])

    def test_outbound_calls_and_aliases(self):
        cases = [
            'import "net/http"\nfunc f() { http.Get("http://host") }',
            'import h "net/http"\nfunc f() { h /*network*/ . Post("http://host", "", nil) }',
            'import . "net/http"\nfunc f() { Get("http://host") }',
            'import h `net/http`\nfunc f() { h.Head("http://host") }',
            'import h "net/\\x68ttp"\nfunc f() { h.PostForm("http://host", nil) }',
            'import "net/http"\nfunc f() { get := http.Get; get("http://host") }',
            'import "net/http"\nfunc f() { http.DefaultClient.Do(nil) }',
            'import "net/http"\nfunc f() { http.DefaultTransport.RoundTrip(nil) }',
            'import "net/http"\nfunc f() { c := &http.Client{}; c.Do(nil) }',
            'import "net/http"\nfunc f(c *http.Client) { c.Get("http://host") }',
            'import "net/http"\nvar c http.Client\nfunc f() { c.Do(nil) }',
            'import "net/http"\ntype X struct { client *http.Client }; func f(x *X) { x.client.Do(nil) }',
            'import "net/http"\nfunc f() { c := http.DefaultClient; other := c; other.Do(nil) }',
            'import "net/http"\nfunc f() { (&http.Client{}).Do(nil) }',
            'import "net/http"\nfunc f() { (*http.Client).Do(nil, nil) }',
            'import "net/http"\nfunc f() { send := http.DefaultClient.Do; send(nil) }',
            'import "net/http"\nfunc f() { c := &http.Client{}; send := c.Do; send(nil) }',
            'import "net/http"\ntype C = http.Client\nfunc f() { c := &C{}; c.Do(nil) }',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(len(self.hits('package url\n' + source)), 1)

    def test_sibling_declarations_do_not_hide_outbound_calls(self):
        cases = [
            ('var client *http.Client', 'func f() { client.Do(nil) }'),
            ('var client = http.DefaultClient', 'func f() { client.Do(nil) }'),
            ('type X struct { client *http.Client }', 'func f(x *X) { x.client.Do(nil) }'),
            ('func newClient() *http.Client { return &http.Client{} }',
             'func f() { c := newClient(); c.Do(nil) }'),
            ('func newClient() *http.Client { return &http.Client{} }',
             'func f() { newClient().Do(nil) }'),
            ('type C = http.Client', 'func f() { c := &C{}; c.Do(nil) }'),
        ]
        for declaration, call in cases:
            with self.subTest(declaration=declaration, call=call):
                hits = self.hits('package url\n' + call,
                                 extra={'client.go': 'package url\nimport "net/http"\n' + declaration})
                self.assertEqual([row['path'] for row in hits], ['url/http.go'])

    def test_internal_package_exports_do_not_hide_outbound_calls(self):
        cases = [
            ('func NewClient() *http.Client { return &http.Client{} }',
             'func f() { c := helper.NewClient(); c.Do(nil) }'),
            ('func NewClient() *http.Client { return &http.Client{} }',
             'func f() { helper.NewClient().Do(nil) }'),
            ('func NewClient() *http.Client { return &http.Client{} }',
             'func f() { makeClient := helper.NewClient; c := makeClient(); c.Do(nil) }'),
            ('var Default = http.DefaultClient', 'func f() { helper.Default.Do(nil) }'),
            ('type C = http.Client', 'func f() { c := &helper.C{}; c.Do(nil) }'),
            ('var Send = http.DefaultClient.Do', 'func f() { helper.Send(nil) }'),
        ]
        for declaration, call in cases:
            with self.subTest(declaration=declaration, call=call):
                hits = self.hits('package url\nimport helper "github.com/servo/rust-url/url/helper"\n' + call,
                                 extra={'helper/client.go': 'package helper\nimport "net/http"\n' + declaration})
                self.assertEqual([row['path'] for row in hits], ['url/http.go'])

    def test_other_production_guards_still_catch_fallbacks(self):
        cases = [
            'import "os/exec"\nfunc f() { exec.Command("helper").Run() }',
            'import . "net"\nfunc f() { Dial("tcp", "127.0.0.1:1") }',
            'import "net/rpc"\nfunc f() { rpc.Dial("tcp", "host:1") }',
            'import "C"\nfunc f() {}',
            'import p "plugin"\nfunc f() { p.Open("helper.so") }',
            'import "syscall"\nfunc f() { syscall.Exec("x", nil, nil) }',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(self.hits('package url\n' + source, RULES))
        with self.assertRaises(ValueError):
            self.hits('package url', [dict(HTTP_RULE, exclude=['../unsafe'])])
        with self.assertRaises(ValueError):
            self.hits('package url', [dict(HTTP_RULE, target='invalid')])


if __name__ == "__main__":
    unittest.main()

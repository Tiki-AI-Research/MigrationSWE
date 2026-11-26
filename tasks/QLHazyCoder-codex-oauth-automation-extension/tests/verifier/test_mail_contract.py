"""Regressions for decimal request lengths and public credential validation."""

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


ASSETS = Path(__file__).resolve().parents[1] / "assets" / "shared" / "batches"


def load_probe(batch, filename):
    spec = importlib.util.spec_from_file_location(batch, ASSETS / batch / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lengths = load_probe("BATCH-F2P-0004", "f2p_probe_core.py")
differential = load_probe("BATCH-DIFFERENTIAL-0001", "differential_probe.py")


class LengthEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.case = {
            "case": "huge_unknown", "expected_status": 413,
            "expected_error": "Request body too large",
            "response": {
                "status": 413,
                "headers": {
                    "Server": "Werkzeug/test",
                    "Content-Type": "application/json; charset=utf-8",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                },
                "body": '{"ok": false, "error": "Request body too large"}',
            },
        }

    def test_correct_envelope(self):
        self.assertEqual(lengths._envelope_failures([self.case]), [])

    def test_oversize_must_precede_unknown_path(self):
        self.case["response"].update(status=404, body='{"ok": false, "error": "Unsupported path: /does-not-exist"}')
        self.assertTrue(lengths._envelope_failures([self.case]))

    def test_integer_conversion_error_is_not_a_size_rejection(self):
        self.case["response"].update(status=500, body='{"ok": false, "error": "Exceeds the limit (4300 digits)"}')
        self.assertTrue(lengths._envelope_failures([self.case]))

    def test_leading_zeros_must_not_hide_nonempty_json(self):
        self.case.update(expected_status=500, expected_error=lengths._source_invalid_json_message(b"{oops"))
        self.case["response"].update(status=500, body='{"ok": false, "error": "Invalid domain"}')
        self.assertTrue(lengths._envelope_failures([self.case]))

    def test_failure_bytes_and_headers_are_contractual(self):
        for body in [self.case["response"]["body"] + "\n", '{"ok":false,"error":"Request body too large"}']:
            case = copy.deepcopy(self.case)
            case["response"]["body"] = body
            self.assertTrue(lengths._envelope_failures([case]))
        del self.case["response"]["headers"]["Access-Control-Allow-Origin"]
        self.assertTrue(lengths._envelope_failures([self.case]))

    def test_unsupported_method_must_not_become_post_or_preflight(self):
        self.case.update(expected_status=404, expected_error="Unsupported path: /health?x=1")
        for status, body in [(200, '{"ok": true}'), (204, ""),
                             (500, '{"ok": false, "error": "Invalid JSON payload: bad"}')]:
            self.case["response"].update(status=status, body=body)
            self.assertTrue(lengths._envelope_failures([self.case]))
        self.case["response"].update(status=404, body='{"ok": false, "error": "Unsupported path: /health?x=1"}')
        self.assertEqual(lengths._envelope_failures([self.case]), [])

    def test_head_requests_cover_documented_and_unknown_paths(self):
        with patch.object(lengths, "_raw_length_post", return_value=self.case["response"]) as send:
            cases = lengths._collect_unsupported_methods(17374, "/health?x=1")
        head_calls = [call for call in send.call_args_list if call.kwargs.get("method") == "HEAD"]
        self.assertEqual({call.args[1] for call in head_calls},
                         {"/health?x=1", "/does-not-exist?head=1"})
        for case in cases:
            if case["case"].startswith("HEAD"):
                case["response"] = copy.deepcopy(self.case["response"])
                case["response"].update(status=404, body="")
                self.assertTrue(lengths._envelope_failures([case]))
                case["response"]["body"] = json.dumps({"ok": False, "error": case["expected_error"]})
                self.assertEqual(lengths._envelope_failures([case]), [])

    def test_head_framing_and_size_errors_are_not_skipped(self):
        with patch.object(lengths, "_raw_length_post", return_value=self.case["response"]):
            cases = lengths._collect_length_edges(17374, "/random-email", "Invalid domain")
        head = {case["case"]: (case["expected_status"], case["expected_error"])
                for case in cases if "head" in case["case"]}
        self.assertEqual(head, {
            "huge_head": (413, "Request body too large"),
            "head_invalid": (500, "Invalid Content-Length"),
            "head_unsupported_te": (500, "Unsupported Transfer-Encoding"),
            "head_ambiguous": (500, "Ambiguous request framing"),
        })

    def test_decoded_paths_are_not_normalized_into_documented_routes(self):
        with patch.object(lengths, "_raw_length_post", return_value=self.case["response"]):
            cases = lengths._collect_unknown_paths(17374, "/health")
        self.assertEqual({case["case"] for case in cases}, {"/%2fhealth", "//health"})
        for case in cases:
            case["response"] = copy.deepcopy(self.case["response"])
            case["response"].update(status=200, body='{"ok": true}')
            self.assertTrue(lengths._envelope_failures([case]))
            case["response"].update(status=404, body=json.dumps({"ok": False, "error": case["expected_error"]}))
            self.assertEqual(lengths._envelope_failures([case]), [])


class ResponseSerializationTests(unittest.TestCase):
    def response(self, status, error):
        return {
            "status": status,
            "headers": {
                "Server": "Werkzeug/test",
                "Content-Type": "application/json; charset=utf-8",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
            },
            "body": json.dumps({"ok": False, "error": error}, ensure_ascii=False),
        }

    def test_unknown_target_requires_literal_utf8_and_complete_envelope(self):
        target = "/cafÃ©?q=ÿ"
        response = self.response(404, "Unsupported path: " + target)
        self.assertTrue(lengths._unknown_path_error(response, target))
        payload = json.loads(response["body"])
        for body in (
            json.dumps(payload),  # Same decoded text, different UTF-8 bytes.
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            response["body"] + "\n",
            json.dumps({"error": payload["error"]}, ensure_ascii=False),
            json.dumps({"ok": True, "error": payload["error"]}, ensure_ascii=False),
        ):
            with self.subTest(body=body):
                self.assertFalse(lengths._unknown_path_error({**response, "body": body}, target))

    def test_all_failure_predicates_require_source_serialization_and_headers(self):
        parse_error = lengths._source_invalid_json_message(b"{oops")
        cases = [
            (500, parse_error, lambda r: lengths._malformed_json_failure(r, parse_error)),
            (500, lengths.MISSING_IMAP_ERROR, lengths._missing_imap),
            (500, lengths.AMBIGUOUS_FRAMING_ERROR,
             lambda r: lengths._framing_envelope(r, lengths.AMBIGUOUS_FRAMING_ERROR)),
            (413, lengths.BODY_TOO_LARGE_ERROR, lengths._body_too_large),
            (500, lengths.UNSUPPORTED_TE_ERROR, lengths._unsupported_te),
        ]
        for status, error, predicate in cases:
            response = self.response(status, error)
            with self.subTest(error=error):
                self.assertTrue(predicate(response))
                self.assertFalse(predicate({**response, "body": response["body"] + "\n"}))
                compact = json.dumps(json.loads(response["body"]), separators=(",", ":"))
                self.assertFalse(predicate({**response, "body": compact}))
                for header in response["headers"]:
                    headers = dict(response["headers"])
                    del headers[header]
                    self.assertFalse(predicate({**response, "headers": headers}))

    def test_success_headers_are_required_too(self):
        response = self.response(200, "unused")
        response["body"] = '{"ok": true}'
        self.assertTrue(lengths._served_ok(response))
        del response["headers"]["Access-Control-Allow-Origin"]
        self.assertFalse(lengths._served_ok(response))


class CredentialProjectionTests(unittest.TestCase):
    def request(self, method, url, payload):
        self.calls.append((method, url, payload))
        return 500, {}, b'{"ok": false, "error": "Missing email/clientId/refreshToken"}'

    def setUp(self):
        self.calls = []

    def test_internal_parser_name_is_unconstrained(self):
        # No helper module or read_json_payload attribute is exposed to this probe.
        result = differential.missing_credentials_projection("http://helper", self.request)
        self.assertEqual(result, {"error": "Missing email/clientId/refreshToken"})
        self.assertEqual(len(self.calls), 8)
        for path in ("/messages", "/code"):
            payloads = [body for method, url, body in self.calls if url.endswith(path)]
            self.assertIn({}, payloads)
            for field in ("email", "clientId", "refreshToken"):
                self.assertTrue(any(field not in body and len(body) == 2 for body in payloads))

    def test_matching_error_with_wrong_status_fails(self):
        def wrong_status(*args):
            _, headers, body = self.request(*args)
            return 200, headers, body
        self.assertIn("responses", differential.missing_credentials_projection("http://helper", wrong_status))

    def test_one_route_or_one_missing_field_cannot_be_skipped(self):
        def missing_validation(method, url, payload):
            if url.endswith("/code") and payload.get("email") and "clientId" not in payload:
                return 200, {}, json.dumps({"ok": True}).encode()
            return self.request(method, url, payload)
        self.assertIn("responses", differential.missing_credentials_projection("http://helper", missing_validation))

    def test_request_failure_is_an_observed_mismatch(self):
        def timeout(*args):
            raise TimeoutError("route failed to reject missing credentials")
        result = differential.missing_credentials_projection("http://helper", timeout)
        self.assertIn("request_error", result)


if __name__ == "__main__":
    unittest.main()

"""Offline safety checks for the isolated spike; no backend or network needed."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import setup
from scripts import smoke_muninn as smoke


def row(identifier, tags=None):
    return {"id": identifier, "tags": ["marker"] if tags is None else tags}


def page(offset, items, total=3, limit=2):
    return {"engrams": items, "total": total, "offset": offset, "limit": limit}


class MuninnSmokeSafetyTests(unittest.TestCase):
    def test_complete_pages_require_final_empty_page(self):
        fetch = Mock(side_effect=[page(0, [row("a"), row("b")]), page(2, [row("c")]), page(3, [])])
        rows, pages = smoke.collect_tag_pages(fetch, ["marker"], page_size=2)
        self.assertEqual([item["id"] for item in rows], ["a", "b", "c"])
        self.assertEqual([item["returned"] for item in pages], [2, 1, 0])
        self.assertEqual(fetch.call_args_list[-1].args, (3, 2))

    def test_truncated_and_stalled_pages_are_rejected(self):
        for response in [page(0, []), page(0, [row("a")])]:
            with self.subTest(response=response), self.assertRaises(smoke.SmokeFailure):
                smoke.collect_tag_pages(Mock(return_value=response), ["marker"], page_size=2)

    def test_duplicate_ids_and_changing_totals_are_rejected(self):
        for second in [page(2, [row("a")]), page(2, [row("c")], total=4)]:
            with self.subTest(second=second), self.assertRaises(smoke.SmokeFailure):
                smoke.collect_tag_pages(Mock(side_effect=[page(0, [row("a"), row("b")]), second]), ["marker"], page_size=2)

    def test_ignored_tag_filter_or_offset_is_rejected(self):
        for response in [page(0, [row("a", ["marker-suffix"]), row("b")]), page(1, [row("a"), row("b")])]:
            with self.subTest(response=response), self.assertRaises(smoke.SmokeFailure):
                smoke.collect_tag_pages(Mock(return_value=response), ["marker"], page_size=2)

    def test_redaction_removes_credentials_and_embedded_values(self):
        secrets = ["test-api-secret", "test-cookie-secret"]
        value = {"token": secrets[0], "nested": [{"Cookie": secrets[1], "error": "unknown " + secrets[0]}], "content": "synthetic"}
        encoded = json.dumps(smoke.redact(value, secrets))
        self.assertTrue(all(secret not in encoded for secret in secrets))
        self.assertIn("synthetic", encoded)

    def test_ambient_config_is_refused_and_provider_environment_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {"MUNINN_OPENAI_KEY": "synthetic", "MUNINN_TLS_CERT": "user-cert"}):
                env = smoke.isolated_environment(root / "data", "password-fixture", "mcp-fixture")
                self.assertNotIn("MUNINN_OPENAI_KEY", env)
                self.assertNotIn("MUNINN_TLS_CERT", env)
                self.assertEqual(env["MUNINN_LOCAL_EMBED"], "0")
                self.assertEqual(env["MUNINN_ENRICH_URL"], "")
                config = root / ".muninn/muninn.env"
                config.parent.mkdir()
                config.write_text("MUNINN_ENRICH_URL=untrusted\n")
                with self.assertRaises(smoke.SmokeFailure):
                    smoke.isolated_environment(root / "data", "password-fixture", "mcp-fixture")

    def test_unknown_post_result_is_never_retried(self):
        connection = Mock()
        connection.request.side_effect = TimeoutError("synthetic timeout")
        probe = smoke.Probe({"rest": 1}, {}, "password-fixture", "mcp-fixture")
        with patch.object(smoke.http.client, "HTTPConnection", return_value=connection), patch.object(smoke.time, "sleep"):
            with self.assertRaisesRegex(smoke.SmokeFailure, "no retry; unknown writes require needs_manual_repair"):
                probe.request("POST", "/api/engrams", {"content": "synthetic"})
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_pins_match_setup_and_bad_binary_is_not_accepted(self):
        self.assertEqual(smoke.VERSION, setup.VERSION)
        self.assertEqual(smoke.SHA256, setup.CHECKSUMS[("linux", "amd64")])
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "not-a-binary"
            binary.write_bytes(b"synthetic invalid download")
            with self.assertRaises(smoke.SmokeFailure):
                smoke.verify_binary(binary)


if __name__ == "__main__":
    unittest.main()

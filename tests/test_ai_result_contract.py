from __future__ import annotations

import io
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wechat_cli
from wechat_source import DirectSchemaError, DirectWeChatReader


class AIResultContractTests(unittest.TestCase):
    def test_file_transfer_assistant_names_resolve_without_guessing_account(self):
        class Reader:
            def list_contacts(self):
                return [{"nativeId": "filehelper", "displayName": "filehelper"}]

            def close(self):
                pass

        config = {"primary": {}, "secondary": {}}
        with patch.object(wechat_cli, "_reader", side_effect=lambda *_: Reader()):
            for query in ("文件传输助手", "file_transfer_assistant", "File Transfer Assistant"):
                label, reader, contact = wechat_cli._resolve_contact(
                    config, account="primary", query=query, cutoff_s=200,
                )
                self.assertEqual("primary", label)
                self.assertEqual("filehelper", contact["nativeId"])
                reader.close()
            with self.assertRaisesRegex(wechat_cli.ProductError, "^contact_ambiguous:"):
                wechat_cli._resolve_contact(
                    config, account="auto", query="文件传输助手", cutoff_s=200,
                )

    def test_cached_author_outside_window_does_not_request_profile_reload(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE SnsTimeLine(tid TEXT, user_name TEXT, content TEXT)")
        connection.execute(
            "INSERT INTO SnsTimeLine VALUES (?, ?, ?)",
            ("post-1", "synthetic-author", "<root><TimelineObject>"
             "<id>post-1</id><username>synthetic-author</username>"
             "<createTime>100</createTime><contentDesc>old post</contentDesc>"
             "</TimelineObject></root>"),
        )
        contact = {"nativeId": "synthetic-author", "displayName": "synthetic", "sessionType": "contact"}

        class Reader:
            account_identity_commitment = "a" * 64
            list_moments = DirectWeChatReader.list_moments

            def _named_databases(self, _name):
                return ["synthetic-sns"]

            def _open(self, _source):
                return connection

            def list_contacts(self):
                return [contact]

            def close(self):
                pass

        args = wechat_cli.parser().parse_args([
            "moments", "--config", "unused", "--account", "primary",
            "--contact", "synthetic", "--since", "1970-01-01T00:02:30Z",
            "--until", "1970-01-01T00:03:20Z",
        ])
        output = SimpleNamespace(buffer=io.BytesIO())
        with (
            patch.object(wechat_cli, "_read_config", return_value={}),
            patch.object(wechat_cli, "_resolve_moments_subject", return_value=("primary", Reader(), contact)),
            patch.object(wechat_cli.sys, "stdout", output),
        ):
            self.assertEqual(0, wechat_cli.command_moments(args))
        result = json.loads(output.buffer.getvalue())
        self.assertEqual(0, result["returnedMoments"])
        self.assertEqual("target_cached_outside_requested_window", result["targetCacheStatus"])
        self.assertEqual(1, result["targetCachedRows"])
        self.assertEqual({"sinceS": 100, "untilS": 100}, result["targetCachedWindow"])
        self.assertFalse(any(item.get("nextAction") for item in result["gaps"]))

    def test_failures_give_ai_an_action_without_disclosing_exception_payload(self):
        cases = (
            (DirectSchemaError("session_database_unavailable"), "check_selected_account_local_database", False),
            (wechat_cli.ProductError("context_cursor_target_mismatch"), "restart_context_without_cursor", False),
            (wechat_cli.ProductError("source_changed_during_sync_retry"), "retry_same_command", True),
            (OSError("synthetic private path and private message text"), "inspect_error_before_retrying", False),
        )
        for error, action, retryable in cases:
            with self.subTest(error=type(error).__name__):
                output = SimpleNamespace(buffer=io.BytesIO())
                with (
                    patch.object(wechat_cli, "_context_result", side_effect=error),
                    patch.object(wechat_cli.sys, "stdout", output),
                ):
                    code = wechat_cli.main(["context", "--contact", "synthetic"])
                payload = json.loads(output.buffer.getvalue())
                self.assertEqual(2, code)
                self.assertEqual("failed", payload["status"])
                self.assertEqual(action, payload["nextAction"])
                self.assertEqual(retryable, payload["retryable"])
                self.assertNotIn("private message text", output.buffer.getvalue().decode())
                if isinstance(error, DirectSchemaError):
                    self.assertEqual("session_database_unavailable", payload["reason"])


if __name__ == "__main__":
    unittest.main()

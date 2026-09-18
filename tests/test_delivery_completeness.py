"""Delivery receipts must expose source gaps; fixtures contain no personal data."""

from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wechat_cli as cli


class Reader:
    account_identity_commitment = "a" * 64

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def open_locator(self, locator, **kwargs):
        if locator == "voice":
            return b"\x02#!SILK_V3synthetic"
        if locator == "fails":
            raise OSError("synthetic missing cache")
        return b"synthetic attachment bytes"


def context():
    return {
        "status": "success",
        "account": "primary",
        "accountIdentityCommitment": "sha256:" + "a" * 64,
        "contact": {"nativeId": "synthetic-contact", "displayName": "Synthetic"},
        "requestedWindow": {"sinceS": 1, "untilS": 200},
        "actualVisibleCutoffS": 200,
        "sourceSnapshotCutoffS": 200,
        "returnedMessages": 1,
        "coverage": {"hasMore": False},
        "messages": [
            {
                "nativeId": {"kind": "server", "value": "1"},
                "serverId": "1",
                "createTime": 100,
                "content": "Synthetic text",
                "senderRole": "self",
                "sender": {"role": "self", "displayName": "Self"},
                "media_manifest": [
                    {
                        "kind": "file",
                        "mediaId": "file-1",
                        "locator": "file",
                        "openable": True,
                    }
                ],
            }
        ],
        "quotedMessages": [],
        "gaps": [],
    }


class DeliveryCompletenessTests(unittest.TestCase):
    def invoke(self, command, data, output):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        argv = [
            command,
            "--account",
            "primary",
            "--contact",
            "synthetic-contact",
            "--output",
            str(output),
        ]
        if command == "export-context":
            argv.append("--local-only")
        args = cli.parser().parse_args(argv)
        with (
            patch.object(cli, "_complete_context", return_value=deepcopy(data)),
            patch.object(cli, "_read_config", return_value={"primary": {}}),
            patch.object(cli, "_reader", return_value=Reader()),
            patch.object(cli.sys, "stdout", stdout),
        ):
            code = args.handler(args)
        return code, json.loads(stdout.buffer.getvalue())

    def test_preservation_missing_media_is_partial_even_without_preexisting_gap(self):
        data = context()
        data["messages"][0]["media_manifest"][0] = {
            "kind": "image",
            "mediaId": "missing",
            "openable": False,
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bundle"
            code, receipt = self.invoke("preserve", data, output)
            self.assertEqual(code, 0)
            self.assertEqual(receipt["status"], "partial")
            self.assertTrue(receipt["packageCreated"])
            self.assertEqual(receipt["delivery"]["mediaUnavailable"], 1)
            payload = json.loads((output / "messages.json").read_text("utf-8"))
            self.assertEqual(payload["status"], "partial")
            self.assertIn("media_unavailable", {gap["kind"] for gap in payload["gaps"]})
            self.assertEqual(cli._verify_export_result(output)["status"], "success")

    def test_quote_only_missing_media_is_counted(self):
        data = context()
        quote = deepcopy(data["messages"][0])
        quote["nativeId"]["value"] = "2"
        quote["serverId"] = "2"
        quote["media_manifest"] = [
            {"kind": "file", "mediaId": "missing-quote", "openable": False}
        ]
        data["quotedMessages"] = [quote]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bundle"
            _, receipt = self.invoke("preserve", data, output)
            self.assertEqual(receipt["status"], "partial")
            self.assertEqual(receipt["delivery"]["mediaOccurrences"], 2)
            self.assertEqual(receipt["delivery"]["mediaUnavailable"], 1)
            self.assertEqual(receipt["delivery"]["quotedMessageCount"], 1)
            self.assertEqual(cli._verify_export_result(output)["status"], "success")

    def test_complete_preservation_remains_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bundle"
            code, receipt = self.invoke("preserve", context(), output)
            self.assertEqual(code, 0)
            self.assertEqual(receipt["status"], "success")
            self.assertEqual(receipt["delivery"]["mediaAvailable"], 1)
            self.assertEqual(receipt["delivery"]["mediaUnavailable"], 0)
            self.assertEqual(cli._verify_export_result(output)["status"], "success")

    def test_voice_derivative_failure_is_visible_on_both_receipts(self):
        for command in ("preserve", "export-context"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp:
                data = context()
                data["messages"][0]["media_manifest"] = [
                    {
                        "kind": "voice",
                        "locator": "voice",
                        "mediaId": "voice",
                        "openable": True,
                    }
                ]
                output = Path(temp) / "bundle"
                with patch.object(
                    cli,
                    "_decode_voice_file",
                    side_effect=cli.ProductError("wechat_voice_decoder_unavailable"),
                ):
                    code, receipt = self.invoke(command, data, output)
                self.assertEqual(code, 0)
                self.assertEqual(receipt["status"], "partial")
                self.assertEqual(receipt["delivery"]["voiceWavUnavailable"], 1)
                self.assertEqual(receipt["delivery"]["mediaAvailable"], 1)
                self.assertEqual(cli._verify_export_result(output)["status"], "success")

    def test_failed_open_is_an_explicit_delivery_gap(self):
        data = context()
        data["messages"][0]["media_manifest"][0]["locator"] = "fails"
        with tempfile.TemporaryDirectory() as temp:
            _, receipt = self.invoke("preserve", data, Path(temp) / "bundle")
            self.assertEqual(receipt["status"], "partial")
            self.assertEqual(receipt["delivery"]["mediaUnavailable"], 1)


class RecoveryReceiptTests(unittest.TestCase):
    def test_complete_receipt_reports_post_recovery_state(self):
        from wechat_storage import archive_transaction, recover_archive

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "archive"
            output.mkdir()
            (output / "value").write_bytes(b"old")
            rename = Path.rename

            def stop(path, target):
                if path.name == "stage" and Path(target) == output:
                    raise KeyboardInterrupt()
                return rename(path, target)

            with (
                patch.object(Path, "rename", stop),
                self.assertRaises(KeyboardInterrupt),
            ):
                with archive_transaction(output, lambda _: None) as stage:
                    (stage / "value").write_bytes(b"new")
            before = recover_archive(output, "inspect", lambda _: None)
            self.assertFalse(before["outputPresent"])
            result = recover_archive(output, "complete", lambda _: None)
            self.assertTrue(result["recovered"])
            self.assertTrue(result["outputPresent"])
            self.assertFalse(result["stagedPresent"])
            self.assertFalse(result["previousPresent"])
            self.assertFalse(result["transactionPresent"])
            self.assertEqual(result["phase"], "completed")
            self.assertEqual((output / "value").read_bytes(), b"new")

    def test_rollback_receipt_reports_post_recovery_state(self):
        from wechat_storage import archive_transaction, recover_archive

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "archive"
            output.mkdir()
            (output / "value").write_bytes(b"old")
            with self.assertRaises(KeyboardInterrupt):
                with archive_transaction(output, lambda _: None) as stage:
                    (stage / "value").write_bytes(b"unfinished")
                    raise KeyboardInterrupt()
            result = recover_archive(output, "rollback", lambda _: None)
            self.assertTrue(result["outputPresent"])
            self.assertFalse(result["stagedPresent"])
            self.assertFalse(result["previousPresent"])
            self.assertFalse(result["transactionPresent"])
            self.assertEqual(result["phase"], "rolled_back")
            self.assertEqual((output / "value").read_bytes(), b"old")

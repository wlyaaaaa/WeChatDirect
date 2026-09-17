from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wechat_cli as cli
from test_archive_reliability import SyntheticContactReader


class PendingReader(SyntheticContactReader):
    fail = True

    def fetch_messages(self, session_id, **kwargs):
        data = super().fetch_messages(session_id, **kwargs)
        data["messages"][0]["media_manifest"] = [
            {
                "kind": "file",
                "mediaId": "one",
                "locator": "synthetic",
                "openable": None,
                "materializable": True,
                "requiresNetwork": False,
            }
        ]
        return data

    def open_locator(self, locator):
        if self.fail:
            raise OSError("synthetic unavailable")
        return b"synthetic media bytes"


class CompletionReliabilityTests(unittest.TestCase):
    def args(self, output, full=False):
        return argparse.Namespace(
            config="unused",
            account="primary",
            contact="Synthetic",
            output=str(output),
            since=None,
            until=None,
            overlap_seconds=60,
            full_reconcile=full,
        )

    def sync(self, args, reader):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        with (
            patch.object(
                cli, "_read_config", return_value={"primary": {}, "secondary": {}}
            ),
            patch.object(cli, "_reader", side_effect=lambda *_: reader()),
            patch.object(cli.time, "time", return_value=200),
            patch.object(cli.sys, "stdout", stdout),
        ):
            code = cli.command_sync_contact(args)
        return code, json.loads(stdout.buffer.getvalue())

    def test_full_reconcile_refuses_changed_body_with_retained_record_hash(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "export"
            self.sync(self.args(out), SyntheticContactReader)
            records = cli._read_jsonl(out / "messages.jsonl")
            records[0]["content"] = "not the source"
            cli._write_jsonl_atomic(out / "messages.jsonl", records)
            before = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
            with self.assertRaisesRegex(cli.ProductError, "sha256_mismatch"):
                self.sync(self.args(out, True), SyntheticContactReader)
            self.assertEqual(
                {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}, before
            )

    def test_full_reconcile_retries_pending_local_media(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "export"
            PendingReader.fail = True
            try:
                self.sync(self.args(out), PendingReader)
                self.assertEqual(
                    cli._read_jsonl(out / "messages.jsonl")[0]["media_manifest"][0][
                        "exportStatus"
                    ],
                    "open_failed",
                )
                PendingReader.fail = False
                self.sync(self.args(out, True), PendingReader)
                media = cli._read_jsonl(out / "messages.jsonl")[0]["media_manifest"][0]
                self.assertEqual(media["exportStatus"], "available_local")
                self.assertEqual(
                    (out / media["exportedPath"]).read_bytes(), b"synthetic media bytes"
                )
            finally:
                PendingReader.fail = True

    def test_voice_process_errors_are_optional_gaps_not_export_failures(self):
        for failure in (
            subprocess.TimeoutExpired(["python"], 120),
            FileNotFoundError("synthetic"),
        ):
            with (
                self.subTest(error=type(failure).__name__),
                tempfile.TemporaryDirectory() as td,
            ):
                reader = SimpleNamespace(
                    open_locator=lambda _: b"\x02#!SILK_V3synthetic"
                )
                message = {
                    "media_manifest": [
                        {"kind": "voice", "locator": "synthetic", "openable": True}
                    ]
                }
                with patch.object(cli.subprocess, "run", side_effect=failure):
                    result, counts = cli._sync_message_media(reader, message, Path(td))
                media = result["media_manifest"][0]
                self.assertEqual(media["exportStatus"], "available_local")
                self.assertIn("voiceWavGap", media)
                self.assertTrue((Path(td) / media["exportedPath"]).is_file())
                self.assertFalse(list(Path(td).rglob("*.incomplete")))

    def test_failed_full_reconcile_never_recertifies_archive(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "export"
            self.sync(self.args(out), SyntheticContactReader)
            manifest = (out / "manifest.json").read_bytes()
            (out / "messages.jsonl").write_bytes(b"{}\n")
            with self.assertRaises(cli.ProductError):
                self.sync(self.args(out, True), SyntheticContactReader)
            self.assertEqual((out / "manifest.json").read_bytes(), manifest)

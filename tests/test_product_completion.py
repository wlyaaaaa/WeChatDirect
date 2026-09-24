"""Public-command and failure-path tests using only synthetic conversations."""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wechat_cli as cli
from wechat_storage import (
    StorageError,
    ScratchDirectory,
    archive_transaction,
    recover_archive,
    transaction_path,
    scratch_status,
)
from test_archive_reliability import SyntheticContactReader
import test_completion_reliability as completion
from test_completion_reliability import PendingReader
import test_reading_package as reading
from test_reading_package import reading_context, ReadingReader
import test_context_paging as paging
from test_context_paging import SyntheticReader, _message_row


class ArchivePublicationTests(unittest.TestCase):
    args = completion.CompletionReliabilityTests.args
    sync = completion.CompletionReliabilityTests.sync

    def test_new_archive_is_verified_before_publication(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            self.sync(self.args(output), SyntheticContactReader)
            self.assertEqual(cli._verify_export_result(output)["status"], "success")
            self.assertFalse(transaction_path(output).exists())
            self.assertEqual(
                cli._read_json(output / "last-run.json")["output"], str(output)
            )

    def test_mid_update_failure_keeps_complete_previous_archive(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            self.sync(self.args(output), SyntheticContactReader)
            before = {
                p.relative_to(output).as_posix(): p.read_bytes()
                for p in output.rglob("*")
                if p.is_file()
            }
            with patch.object(
                cli, "_write_text_atomic", side_effect=OSError("synthetic disk fault")
            ):
                with self.assertRaises(OSError):
                    self.sync(self.args(output, True), SyntheticContactReader)
            after = {
                p.relative_to(output).as_posix(): p.read_bytes()
                for p in output.rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse(transaction_path(output).exists())

    def test_source_failure_on_first_run_leaves_no_half_archive(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            with patch.object(
                SyntheticContactReader,
                "fetch_messages",
                side_effect=OSError("synthetic"),
            ):
                with self.assertRaises(OSError):
                    self.sync(self.args(output), SyntheticContactReader)
            self.assertFalse(output.exists())
            self.assertFalse(transaction_path(output).exists())

    def test_hard_stop_between_renames_can_complete_offline(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            output.mkdir()
            (output / "value").write_text("old", "utf-8")
            rename = Path.rename

            def stop(source, target):
                if source.name == "stage":
                    raise KeyboardInterrupt("simulated process death")
                return rename(source, target)

            def verify(root):
                self.assertIn((root / "value").read_text("utf-8"), ("old", "new"))

            with patch.object(Path, "rename", stop):
                with self.assertRaises(KeyboardInterrupt):
                    with archive_transaction(output, verify) as stage:
                        (stage / "value").write_text("new", "utf-8")
            self.assertFalse(output.exists())
            self.assertTrue((transaction_path(output) / "previous" / "value").is_file())
            receipt = recover_archive(output, "complete", verify)
            self.assertTrue(receipt["recovered"])
            self.assertEqual((output / "value").read_text("utf-8"), "new")
            self.assertFalse(transaction_path(output).exists())

    def test_incomplete_build_can_rollback_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            output.mkdir()
            (output / "value").write_text("old", "utf-8")
            with self.assertRaises(KeyboardInterrupt):
                with archive_transaction(output, lambda _: None) as stage:
                    (stage / "value").write_text("unfinished", "utf-8")
                    raise KeyboardInterrupt()
            with self.assertRaisesRegex(StorageError, "not_ready"):
                recover_archive(output, "complete", lambda _: None)
            recover_archive(output, "rollback", lambda _: None)
            self.assertEqual((output / "value").read_text("utf-8"), "old")

    def test_live_transaction_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            with archive_transaction(output, lambda _: None):
                with self.assertRaisesRegex(StorageError, "operation_running"):
                    recover_archive(output, "rollback", lambda _: None)

    def test_external_archive_change_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            output.mkdir()
            (output / "value").write_text("old", "utf-8")
            with self.assertRaisesRegex(StorageError, "changed_during"):
                with archive_transaction(output, lambda _: None):
                    (output / "value").write_text("external change", "utf-8")
            self.assertEqual((output / "value").read_text("utf-8"), "external change")


class PortableVerificationTests(unittest.TestCase):
    def context(self):
        context = reading_context()
        context["returnedMessages"] = len(context["messages"])
        context["actualVisibleCutoffS"] = 3
        return context

    def test_reading_package_verifies_ai_html_and_media_without_config(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "reading"
            reading.ReadingPackageTests().export(output, self.context())
            with patch.object(
                cli, "_read_config", side_effect=AssertionError("must stay offline")
            ):
                self.assertEqual(cli._verify_export_result(output)["status"], "success")
                (output / "ai-context.md").write_text("changed", "utf-8")
                result = cli._verify_export_result(output)
            self.assertEqual(result["status"], "failed")
            self.assertIn("export_file_sha256_mismatch", result["errors"])

    def test_preservation_copies_quote_assets_and_verifies_relations(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "preserved"
            context = self.context()
            quote = deepcopy(context["messages"][0])
            quote["nativeId"]["value"] = "quoted"
            context["quotedMessages"] = [quote]
            args = cli.parser().parse_args(
                [
                    "preserve",
                    "--account",
                    "primary",
                    "--contact",
                    "synthetic",
                    "--output",
                    str(output),
                ]
            )
            stream = SimpleNamespace(buffer=io.BytesIO())
            with (
                patch.object(cli, "_context_result", return_value=context),
                patch.object(cli, "_resolve_config_path", return_value=Path("synthetic")),
                patch.object(cli, "_read_config", return_value={"primary": {}}),
                patch.object(cli, "_reader", return_value=ReadingReader()),
                patch.object(cli.sys, "stdout", stream),
            ):
                self.assertEqual(cli.command_preserve(args), 0)
            self.assertEqual(cli._verify_export_result(output)["status"], "success")
            manifest = cli._read_json(output / "manifest.json")
            self.assertEqual(len(manifest["mediaFiles"]), 4)
            manifest["mediaFiles"][0]["messageNativeId"] = {
                "kind": "server",
                "value": "absent",
            }
            manifest.pop("manifestSha256")
            manifest["manifestSha256"] = cli._sha256(cli._canonical_bytes(manifest))
            cli._write_json_atomic(output / "manifest.json", manifest)
            self.assertIn(
                "export_media_message_mismatch",
                cli._verify_export_result(output)["errors"],
            )

    def test_legacy_reading_package_reports_limited_verification(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "reading"
            reading.ReadingPackageTests().export(output, self.context(), html=False)
            (output / "manifest.json").unlink()
            result = cli._verify_export_result(output)
            self.assertEqual(result["status"], "success")
            self.assertEqual(
                result["verificationScope"],
                "legacy_conversation_and_declared_media_only",
            )


class TargetedMediaTests(unittest.TestCase):
    args = completion.CompletionReliabilityTests.args
    sync = completion.CompletionReliabilityTests.sync

    def test_repair_does_not_rescan_chat_history(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            PendingReader.fail = True
            try:
                self.sync(self.args(output), PendingReader)
                PendingReader.fail = False
                args = cli.parser().parse_args(
                    [
                        "repair-media",
                        "--account",
                        "primary",
                        "--contact",
                        "Synthetic",
                        "--output",
                        str(output),
                    ]
                )
                stream = SimpleNamespace(buffer=io.BytesIO())
                with (
                    patch.object(cli, "_resolve_config_path", return_value=Path("synthetic")),
                    patch.object(cli, "_read_config", return_value={"primary": {}}),
                    patch.object(cli, "_reader", return_value=closing(PendingReader())),
                    patch.object(
                        PendingReader,
                        "fetch_messages",
                        side_effect=AssertionError("no history scan"),
                    ),
                    patch.object(cli.sys, "stdout", stream),
                ):
                    self.assertEqual(cli.command_repair_media(args), 0)
                result = json.loads(stream.buffer.getvalue())
                self.assertFalse(result["historyRescanned"])
                self.assertEqual(result["attemptedMedia"], 1)
                self.assertEqual(result["remainingGaps"], 0)
                self.assertEqual(cli._verify_export_result(output)["status"], "success")
            finally:
                PendingReader.fail = True


class RuntimeAndPagingTests(unittest.TestCase):
    def test_environment_only_does_not_open_any_account_settings(self):
        stream = SimpleNamespace(buffer=io.BytesIO())
        args = cli.parser().parse_args(["doctor", "--environment-only"])
        with (
            patch.object(cli, "_read_config", side_effect=AssertionError()),
            patch.object(cli, "_local_settings", side_effect=AssertionError()),
            patch.object(cli.sys, "stdout", stream),
        ):
            cli.command_doctor(args)
        result = json.loads(stream.buffer.getvalue())
        self.assertEqual(result["configuration"]["status"], "not_checked")
        self.assertIn("wxgf", result["capabilities"])
        self.assertEqual(result["execution"]["sourceAccess"], "not_tested")

    def test_active_scratch_is_not_cleaned_and_normal_exit_cleans(self):
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict("os.environ", {"WECHAT_DIRECT_TEMP_ROOT": td}),
        ):
            with ScratchDirectory() as scratch:
                name = Path(scratch).name
                result = scratch_status(Path(td), session=name, clean=True)
                self.assertEqual(result["sessions"][0]["status"], "active")
                self.assertTrue(Path(scratch).is_dir())
            self.assertFalse(Path(scratch).exists())

    def test_long_unicode_message_can_be_reassembled_without_loss(self):
        text = "超长消息😀" * 30000
        rows = [
            _message_row(
                local_id=1, server_id=1, create_time=100, sort_seq=1, content=text
            )
        ]
        fixture = SyntheticReader([rows])
        self.addCleanup(fixture.close)
        args = paging.ContextPagingTests()._fetch_args(return_limit=1, scan_limit=1)
        args.byte_limit = 32768
        with (
            patch.object(cli, "_resolve_config_path", return_value=Path("synthetic")),
            patch.object(
                cli, "_read_config", return_value={"primary": {}, "secondary": {}}
            ),
            patch.object(
                cli,
                "_resolve_contact",
                return_value=("primary", fixture.reader, fixture.contacts[0]),
            ),
        ):
            result = cli._context_result(args)
            self.assertLessEqual(len(cli._canonical_bytes(result)), 32768)
            message = result["messages"][0]
            descriptor = next(
                x for x in message["segmentedFields"] if x["path"] == ["content"]
            )
            value = message["content"]
            cursor = descriptor["cursor"]
            while cursor:
                part_args = cli.parser().parse_args(
                    [
                        "message-part",
                        "--account",
                        "primary",
                        "--contact",
                        args.contact,
                        "--cursor",
                        cursor,
                    ]
                )
                stream = SimpleNamespace(buffer=io.BytesIO())
                with patch.object(cli.sys, "stdout", stream):
                    cli.command_message_part(part_args)
                part = json.loads(stream.buffer.getvalue())
                value += part["content"]
                cursor = part["continuation"]
            self.assertEqual(value, text)
            self.assertEqual(descriptor["sha256"], cli._sha256(text.encode("utf-8")))

    def test_content_part_rejects_changed_source(self):
        text = "x" * 600000
        rows = [
            _message_row(
                local_id=1, server_id=1, create_time=100, sort_seq=1, content=text
            )
        ]
        fixture = SyntheticReader([rows])
        self.addCleanup(fixture.close)
        args = paging.ContextPagingTests()._fetch_args(return_limit=1, scan_limit=1)
        with (
            patch.object(cli, "_resolve_config_path", return_value=Path("synthetic")),
            patch.object(cli, "_read_config", return_value={"primary": {}}),
            patch.object(
                cli,
                "_resolve_contact",
                return_value=("primary", fixture.reader, fixture.contacts[0]),
            ),
        ):
            result = cli._context_result(args)
            cursor = result["messages"][0]["segmentedFields"][0]["cursor"]
            from test_context_paging import MESSAGE_TABLE

            fixture.shards[0][1].execute(
                f"UPDATE {MESSAGE_TABLE} SET message_content=?", ("changed",)
            )
            part_args = cli.parser().parse_args(
                [
                    "message-part",
                    "--account",
                    "primary",
                    "--contact",
                    args.contact,
                    "--cursor",
                    cursor,
                ]
            )
            with self.assertRaisesRegex(cli.ProductError, "source_changed"):
                cli.command_message_part(part_args)


class AdditionalCloseoutTests(unittest.TestCase):
    def test_new_archive_crash_after_publish_finishes_cleanup(self):
        import wechat_storage as storage

        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            original_json = storage._json

            def stop(path, metadata):
                if metadata.get("phase") == "published":
                    raise KeyboardInterrupt("hard stop")
                original_json(path, metadata)

            with patch.object(storage, "_json", stop):
                with self.assertRaises(KeyboardInterrupt):
                    with archive_transaction(output, lambda _: None) as stage:
                        (stage / "result").write_text("complete", "utf-8")
            self.assertEqual((output / "result").read_text("utf-8"), "complete")
            receipt = recover_archive(
                output,
                "complete",
                lambda root: self.assertTrue((root / "result").is_file()),
            )
            self.assertTrue(receipt["recovered"])
            self.assertFalse(transaction_path(output).exists())

    def test_corrupt_reading_manifest_is_not_treated_as_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "reading"
            context = reading_context()
            context["returnedMessages"] = 3
            reading.ReadingPackageTests().export(output, context)
            (output / "manifest.json").write_text("{broken", "utf-8")
            result = cli._verify_export_result(output)
            self.assertEqual(result["status"], "failed")

    def test_invalid_byte_limit_rejected_before_source_read(self):
        args = cli.parser().parse_args(
            ["context", "--contact", "synthetic", "--byte-limit", "1"]
        )
        with patch.object(
            cli, "_context_result_raw", side_effect=AssertionError("must not read")
        ):
            with self.assertRaisesRegex(cli.ProductError, "byte_limit_invalid"):
                cli._context_result(args)

    def test_atomic_writer_preserves_unknown_temporary(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "value.json"
            temporary = output.with_name(output.name + ".incomplete")
            temporary.write_bytes(b"unknown original")
            with self.assertRaises(FileExistsError):
                cli._write_bytes_atomic(output, b"replacement")
            self.assertEqual(temporary.read_bytes(), b"unknown original")
            self.assertFalse(output.exists())

    def test_voice_copy_failure_removes_only_own_partial_output(self):
        import subprocess
        import wave

        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "output.wav"
            source = Path(td) / "input.silk"
            source.write_bytes(b"synthetic")

            def decode(command, **kwargs):
                target = command[command.index("--output") + 1]
                with wave.open(target, "wb") as wav:
                    wav.setparams((1, 2, 24000, 0, "NONE", "NONE"))
                    wav.writeframes(b"\0\0" * 100)
                return subprocess.CompletedProcess(command, 0)

            def failed_copy(source, target):
                target.write(b"partial")
                raise OSError("synthetic out of space")

            with (
                patch.object(cli.subprocess, "run", side_effect=decode),
                patch.object(cli.shutil, "copyfileobj", side_effect=failed_copy),
            ):
                with self.assertRaisesRegex(cli.ProductError, "voice_decode_failed"):
                    cli._decode_voice_file(source, output)
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), b"synthetic")

    def test_scratch_cleanup_requires_named_session_even_when_empty(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(StorageError, "exact_session_required"):
                scratch_status(Path(td), clean=True)


class ReviewRegressionTests(unittest.TestCase):
    def test_restore_preserved_mtime_does_not_hide_external_change(self):
        import os

        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            output.mkdir()
            file = output / "value"
            file.write_bytes(b"AAAA")
            stat = file.stat()
            with self.assertRaisesRegex(StorageError, "changed_during"):
                with archive_transaction(output, lambda _: None):
                    file.write_bytes(b"BBBB")
                    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(file.read_bytes(), b"BBBB")

    def test_scratch_inspection_does_not_create_missing_lease(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "wechat-direct-scratch" / "synthetic"
            path.mkdir(parents=True)
            marker = path / ".scratch.json"
            marker.write_text(
                json.dumps(
                    {"format": "wechat-disposable-scratch.v1", "rebuildable": True}
                ),
                "utf-8",
            )
            before = marker.read_bytes()
            result = scratch_status(Path(td), session="synthetic")
            self.assertEqual(result["sessions"][0]["status"], "unknown_not_removed")
            self.assertFalse((path / ".lease").exists())
            self.assertEqual(before, marker.read_bytes())

    def test_recovery_rejects_regular_file_target(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "archive"
            output.write_bytes(b"not a directory")
            with self.assertRaisesRegex(StorageError, "not_directory"):
                recover_archive(output, "complete", lambda _: None)
            self.assertEqual(output.read_bytes(), b"not a directory")


class MissingMediaRelationTests(unittest.TestCase):
    def test_missing_original_is_reported_without_inventing_relation_conflict(self):
        import test_public_commands as public

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "archive"
            root.mkdir()
            wav = b"synthetic derived bytes"
            (root / "voice.wav").write_bytes(wav)
            declared = cli._sha256(b"missing original")
            media = {
                "kind": "voice",
                "exportedPath": "voice.silk",
                "sha256": declared,
                "bytes": len(b"missing original"),
                "derivedVoiceWav": {
                    "path": "voice.wav",
                    "sha256": cli._sha256(wav),
                    "bytes": len(wav),
                    "derivedFromSha256": declared,
                },
            }
            public.PublicCommandTests()._write_export(root, media=media)
            result = cli._verify_export_result(root)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["errors"], ["export_media_unavailable"])

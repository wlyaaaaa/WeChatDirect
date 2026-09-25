from __future__ import annotations

import isolation  # noqa: F401

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import wechat_cli
from wechat_storage import (
    StorageError,
    archive_transaction,
    recover_archive,
    transaction_path,
)


class SyntheticContactReader:
    phase = 0

    def __init__(self) -> None:
        self.account_identity_commitment = hashlib.sha256(b"synthetic").hexdigest()
        self.fetch_calls: list[int | None] = []

    def close(self) -> None:
        pass

    def list_contacts(self, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "nativeId": "wxid-synthetic",
                "sessionType": "contact",
                "displayName": "Synthetic",
                "remark": "Synthetic",
                "nickname": "Synthetic",
                "alias": "synthetic",
                "lastTimestamp": 120,
            }
        ]

    def contact_source_fingerprint(self, session_id: str) -> dict[str, object]:
        assert session_id == "wxid-synthetic"
        return {
            "sha256": "sha256:"
            + hashlib.sha256(f"source-{self.phase}".encode()).hexdigest(),
            "fileCount": 1,
            "messageSourceCount": 1,
            "messageSourceCatalogSha256": "sha256:" + "c" * 64,
        }

    def fetch_messages(
        self,
        session_id: str,
        *,
        since_sort_seq: int | None,
        **_kwargs: object,
    ) -> dict[str, list[dict[str, object]]]:
        assert session_id == "wxid-synthetic"
        self.fetch_calls.append(since_sort_seq)
        messages = [
            {
                "serverId": 1,
                "localId": 1,
                "createTime": 1,
                "sortSeq": 1,
                "content": "old",
                "senderRole": "other",
                "direction": "incoming",
                "type": "text",
            },
            {
                "serverId": 2,
                "localId": 2,
                "createTime": 100,
                "sortSeq": 2,
                "content": "recent",
                "senderRole": "other",
                "direction": "incoming",
                "type": "text",
            },
        ]
        if self.phase:
            messages.append(
                {
                    "serverId": 3,
                    "localId": 3,
                    "createTime": 120,
                    "sortSeq": 3,
                    "content": "new",
                    "senderRole": "other",
                    "direction": "incoming",
                    "type": "text",
                }
            )
        if since_sort_seq is not None:
            messages = [
                item for item in messages if int(item["sortSeq"]) > since_sort_seq
            ]
        return {"messages": messages}


class SyntheticMediaContactReader(SyntheticContactReader):
    def open_locator(self, locator: str) -> bytes:
        assert locator == "wechat-db://voice"
        return b"synthetic voice"

    def fetch_messages(
        self, session_id: str, **kwargs: object
    ) -> dict[str, list[dict[str, object]]]:
        result = super().fetch_messages(session_id, **kwargs)
        result["messages"][1]["media_manifest"] = [
            {
                "kind": "voice",
                "locator": "wechat-db://voice",
                "openable": True,
            }
        ]
        return result


class ArchiveIncrementReliabilityTests(unittest.TestCase):
    def test_no_change_probe_detects_concurrent_archive_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "export"
            output.mkdir()
            target = output / "record.txt"
            target.write_text("before", encoding="utf-8")

            def probe(_output):
                target.write_text("changed concurrently", encoding="utf-8")
                return {"noChange": True}

            with self.assertRaisesRegex(
                StorageError, "archive_changed_during_transaction"
            ):
                with archive_transaction(output, lambda _stage: None, probe=probe):
                    pass
            self.assertFalse(transaction_path(output).exists())

    def test_interrupted_no_change_probe_remains_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "export"
            output.mkdir()
            (output / "record.txt").write_text("complete", encoding="utf-8")

            def interrupted(_output):
                raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                with archive_transaction(output, lambda _stage: None, probe=interrupted):
                    pass
            self.assertTrue(transaction_path(output).exists())
            recovered = recover_archive(output, "rollback", lambda _stage: None)
            self.assertEqual(recovered["phase"], "rolled_back")
            self.assertEqual((output / "record.txt").read_text(encoding="utf-8"), "complete")

    def test_busy_archive_rename_has_stable_recovery_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "export"
            output.mkdir()
            (output / "old.txt").write_text("old", encoding="utf-8")
            original_rename = Path.rename

            def rename(path, target):
                if path == output:
                    raise PermissionError("synthetic sharing violation")
                return original_rename(path, target)

            with patch.object(Path, "rename", rename):
                with self.assertRaisesRegex(StorageError, "archive_output_in_use"):
                    with archive_transaction(output, lambda _stage: None) as stage:
                        (stage / "new.txt").write_text("new", encoding="utf-8")
            self.assertEqual((output / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertFalse(transaction_path(output).exists())
            hint = wechat_cli._failure_details(StorageError("archive_output_in_use"))
            self.assertEqual(hint["nextAction"], "close_archive_readers_and_retry_same_output")

    def test_no_change_verifies_existing_archive_without_copy_or_publish(self) -> None:
        config = {"primary": {"config_path": "synthetic"}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "export"
            args = argparse.Namespace(
                config="unused",
                account="primary",
                contact="Synthetic",
                output=str(output),
                since=None,
                until=None,
                overlap_seconds=60,
                full_reconcile=False,
            )

            def run() -> dict:
                stream = SimpleNamespace(buffer=io.BytesIO())
                with (
                    patch("wechat_cli._read_config", return_value=config),
                    patch("wechat_cli._reader", return_value=SyntheticContactReader()),
                    patch("wechat_cli.time.time", return_value=200),
                    patch.object(wechat_cli.sys, "stdout", stream),
                ):
                    self.assertEqual(wechat_cli.command_sync_contact(args), 0)
                return json.loads(stream.buffer.getvalue())

            run()
            original_state = (output / "state.json").read_bytes()
            with patch("wechat_storage.shutil.copytree") as copy:
                receipt = run()
            copy.assert_not_called()
            self.assertTrue(receipt["noChange"])
            self.assertEqual(receipt["publication"], "verified_existing_directory")
            self.assertEqual((output / "state.json").read_bytes(), original_state)
            self.assertFalse((output.parent / (output.name + ".wechat-transaction")).exists())

            (output / "messages.jsonl").write_bytes(b"damaged\n")
            with self.assertRaisesRegex(
                wechat_cli.ProductError, "sync_records_sha256_mismatch"
            ):
                run()

    def test_incremental_refuses_a_drifted_archive_before_merging(self) -> None:
        created: list[SyntheticContactReader] = []

        def reader_factory(*_args: object) -> SyntheticContactReader:
            reader = SyntheticContactReader()
            created.append(reader)
            return reader

        config = {"primary": {"config_path": "synthetic"}, "secondary": {}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "export"
            args = argparse.Namespace(
                config="unused",
                account="primary",
                contact="Synthetic",
                output=str(output),
                since=None,
                until=None,
                overlap_seconds=60,
                full_reconcile=False,
            )
            with (
                patch("wechat_cli._read_config", return_value=config),
                patch("wechat_cli._reader", side_effect=reader_factory),
                patch("wechat_cli.time.time", return_value=200),
                patch.object(
                    wechat_cli.sys, "stdout", SimpleNamespace(buffer=io.BytesIO())
                ),
            ):
                self.assertEqual(0, wechat_cli.command_sync_contact(args))

            records_path = output / "messages.jsonl"
            records = [
                json.loads(line)
                for line in records_path.read_text(encoding="utf-8").splitlines()
            ]
            records_path.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    for row in records
                    if row["serverId"] != "1"
                )
                + "\n",
                encoding="utf-8",
            )
            before = {
                path.name: path.read_bytes()
                for path in output.iterdir()
                if path.is_file() and path.name != ".sync.lock"
            }
            SyntheticContactReader.phase = 1
            try:
                with (
                    patch("wechat_cli._read_config", return_value=config),
                    patch("wechat_cli._reader", side_effect=reader_factory),
                    patch("wechat_cli.time.time", return_value=201),
                ):
                    with self.assertRaisesRegex(
                        wechat_cli.ProductError, "sync_records_sha256_mismatch"
                    ):
                        wechat_cli.command_sync_contact(args)
            finally:
                SyntheticContactReader.phase = 0

            after = {
                path.name: path.read_bytes()
                for path in output.iterdir()
                if path.is_file() and path.name != ".sync.lock"
            }
            self.assertEqual(before, after)
            self.assertEqual([], created[-1].fetch_calls)
            self.assertFalse((output / ".sync.lock").exists())

    def test_incremental_refuses_a_missing_declared_media_file(self) -> None:
        created: list[SyntheticMediaContactReader] = []

        def reader_factory(*_args: object) -> SyntheticMediaContactReader:
            reader = SyntheticMediaContactReader()
            created.append(reader)
            return reader

        config = {"primary": {"config_path": "synthetic"}, "secondary": {}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "export"
            args = argparse.Namespace(
                config="unused",
                account="primary",
                contact="Synthetic",
                output=str(output),
                since=None,
                until=None,
                overlap_seconds=60,
                full_reconcile=False,
            )
            with (
                patch("wechat_cli._read_config", return_value=config),
                patch("wechat_cli._reader", side_effect=reader_factory),
                patch("wechat_cli.time.time", return_value=200),
                patch.object(
                    wechat_cli.sys, "stdout", SimpleNamespace(buffer=io.BytesIO())
                ),
            ):
                self.assertEqual(0, wechat_cli.command_sync_contact(args))

            records = wechat_cli._read_jsonl(output / "messages.jsonl")
            media = records[1]["media_manifest"][0]
            (output / media["exportedPath"]).unlink()
            before = {
                path.name: path.read_bytes()
                for path in output.iterdir()
                if path.is_file() and path.name != ".sync.lock"
            }
            SyntheticContactReader.phase = 1
            try:
                with (
                    patch("wechat_cli._read_config", return_value=config),
                    patch("wechat_cli._reader", side_effect=reader_factory),
                    patch("wechat_cli.time.time", return_value=201),
                ):
                    with self.assertRaisesRegex(
                        wechat_cli.ProductError, "export_media_unavailable"
                    ):
                        wechat_cli.command_sync_contact(args)
            finally:
                SyntheticContactReader.phase = 0

            after = {
                path.name: path.read_bytes()
                for path in output.iterdir()
                if path.is_file() and path.name != ".sync.lock"
            }
            self.assertEqual(before, after)
            self.assertEqual([], created[-1].fetch_calls)
            self.assertFalse((output / ".sync.lock").exists())


class MediaTemporaryRecoveryTests(unittest.TestCase):
    def test_non_voice_request_cannot_materialize_remote_media(self) -> None:
        class Reader:
            account_identity_commitment = "a" * 64

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def resolve_locator(self, _locator):
                return {"kind": "emoji", "materializable": True}

            def open_locator(self, *_args, **_kwargs):
                raise AssertionError("remote access must not occur")

        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                config="unused", account="primary", locator="synthetic",
                output=str(Path(temporary) / "out.wav"), voice_wav=True,
            )
            with (
                patch("wechat_cli._read_config", return_value={"primary": {}}),
                patch("wechat_cli._reader", return_value=Reader()),
            ):
                with self.assertRaisesRegex(
                    wechat_cli.ProductError, "media_is_not_voice"
                ):
                    wechat_cli.command_media_open(args)

    def test_media_open_does_not_replace_output_created_during_publish(self) -> None:
        class Reader:
            account_identity_commitment = "a" * 64

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def resolve_locator(self, _locator):
                return {"kind": "file", "materializable": False}

            def open_locator(self, *_args, **_kwargs):
                output.write_bytes(b"other writer")
                return b"synthetic"

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "out.bin"
            args = argparse.Namespace(
                config="unused", account="primary", locator="synthetic",
                output=str(output), voice_wav=False,
            )

            with (
                patch("wechat_cli._read_config", return_value={"primary": {}}),
                patch("wechat_cli._reader", return_value=Reader()),
            ):
                with self.assertRaisesRegex(
                    wechat_cli.ProductError, "media_output_already_exists"
                ):
                    wechat_cli.command_media_open(args)
            self.assertEqual(output.read_bytes(), b"other writer")
            self.assertFalse(output.with_suffix(output.suffix + ".incomplete").exists())

    def test_media_open_publishes_new_file_exclusively(self) -> None:
        class Reader:
            account_identity_commitment = "a" * 64

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def resolve_locator(self, _locator):
                return {"kind": "file", "materializable": False}

            def open_locator(self, *_args, **_kwargs):
                return b"synthetic"

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "out.bin"
            args = argparse.Namespace(
                config="unused", account="primary", locator="synthetic",
                output=str(output), voice_wav=False,
            )
            stream = SimpleNamespace(buffer=io.BytesIO())
            with (
                patch("wechat_cli._read_config", return_value={"primary": {}}),
                patch("wechat_cli._reader", return_value=Reader()),
                patch.object(wechat_cli.sys, "stdout", stream),
            ):
                self.assertEqual(wechat_cli.command_media_open(args), 0)
            self.assertEqual(output.read_bytes(), b"synthetic")
            self.assertFalse(output.with_name("out.bin.incomplete").exists())

    def test_sync_media_preserves_an_existing_wav_sidecar(self) -> None:
        voice = b"\x02#!SILK_V3synthetic"
        digest = hashlib.sha256(voice).hexdigest()

        class Reader:
            def open_locator(self, locator: str) -> bytes:
                assert locator == "wechat-db://voice"
                return voice

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            wav_target = output / "media" / digest[:2] / (digest + ".wav")
            sidecar = wav_target.with_name(wav_target.name + ".incomplete")
            sidecar.parent.mkdir(parents=True)
            sidecar.write_bytes(b"recoverable wav")
            message = {
                "media_manifest": [
                    {
                        "kind": "voice",
                        "locator": "wechat-db://voice",
                        "openable": True,
                    }
                ]
            }
            with patch("wechat_cli._decode_voice_file") as decode:
                projected, counters = wechat_cli._sync_message_media(
                    Reader(), message, output
                )
            decode.assert_not_called()
            self.assertEqual("voice_decode_output_already_exists", projected["media_manifest"][0]["voiceWavGap"])
            self.assertEqual(b"recoverable wav", sidecar.read_bytes())
            self.assertEqual(0, counters["voiceWavCreated"])

    def test_media_open_preserves_an_existing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voice.wav"
            sidecar = output.with_name(output.name + ".incomplete")
            sidecar.write_bytes(b"recoverable bytes")
            args = argparse.Namespace(
                config="unused",
                account="primary",
                locator="wechat-db://voice",
                output=str(output),
                voice_wav=True,
            )
            with self.assertRaisesRegex(
                wechat_cli.ProductError, "media_output_incomplete_exists"
            ):
                wechat_cli.command_media_open(args)
            self.assertFalse(output.exists())
            self.assertEqual(b"recoverable bytes", sidecar.read_bytes())

    def test_voice_decoder_preserves_an_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "voice.silk"
            output = root / "voice.wav.incomplete"
            source.write_bytes(b"source")
            output.write_bytes(b"recoverable bytes")
            with patch("wechat_cli.subprocess.run") as run:
                with self.assertRaisesRegex(
                    wechat_cli.ProductError, "voice_decode_output_already_exists"
                ):
                    wechat_cli._decode_voice_file(source, output)
            run.assert_not_called()
            self.assertEqual(b"recoverable bytes", output.read_bytes())

    def test_voice_decoder_removes_only_a_new_failed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "voice.silk"
            output = root / "voice.wav.incomplete"
            source.write_bytes(b"source")

            def failed_decode(
                *_args: object, **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                command = _args[0]
                Path(command[command.index("--output") + 1]).write_bytes(b"partial bytes")
                return subprocess.CompletedProcess([], 1)

            with patch("wechat_cli.subprocess.run", side_effect=failed_decode):
                with self.assertRaisesRegex(
                    wechat_cli.ProductError, "wechat_voice_decode_failed"
                ):
                    wechat_cli._decode_voice_file(source, output)
            self.assertFalse(output.exists())

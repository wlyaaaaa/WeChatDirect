from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import wechat_cli


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class PublicCommandTests(unittest.TestCase):
    def _capture(self, function, args: argparse.Namespace) -> tuple[int, dict]:
        stream = io.BytesIO()
        stdout = SimpleNamespace(buffer=stream)
        with mock.patch.object(wechat_cli.sys, "stdout", stdout):
            code = function(args)
        return code, json.loads(stream.getvalue())

    def _write_config(self, root: Path) -> Path:
        entries: dict[str, dict[str, str]] = {}
        for index, label in enumerate(wechat_cli.ACCOUNT_LABELS):
            source = root / f"source-{index}.json"
            state = root / f"state-{index}.json"
            source.write_bytes(b"source")
            state.write_bytes(b"state")
            entries[label] = {
                "config_path": str(source),
                "local_state_path": str(state),
                "expected_source_identity_sha256": "sha256:" + str(index) * 64,
                "expected_moments_author_sha256": "sha256:" + str(index + 2) * 64,
            }
        path = root / "accounts.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def _write_export(
        self,
        root: Path,
        *,
        format_name: str = "wechat-direct-contact-export.v1",
        media: dict | None = None,
    ) -> dict:
        root.mkdir(parents=True, exist_ok=True)
        context = b"bounded context\n"
        ai_context = b"small context\n"
        (root / "context.md").write_bytes(context)
        (root / "ai-context.md").write_bytes(ai_context)

        record: dict = {"nativeId": {"kind": "local", "value": "1"}}
        if media is not None:
            record["media_manifest"] = [media]
        records = wechat_cli._canonical_bytes(record) + b"\n"

        is_contact = format_name == "wechat-direct-contact-export.v1"
        records_name = "messages.jsonl" if is_contact else "moments.jsonl"
        count_name = "messageCount" if is_contact else "preservedMomentCount"
        records_hash_name = "messagesSha256" if is_contact else "momentsSha256"
        (root / records_name).write_bytes(records)

        binding = {
            "account": "primary",
            "accountIdentityCommitment": "sha256:" + "a" * 64,
            "contact": {
                "nativeId": "synthetic-contact",
                "displayName": "Synthetic Contact",
            },
            "sourceFingerprint": "sha256:" + "b" * 64,
        }
        state = {
            "format": (
                "wechat-direct-contact-sync.v1"
                if is_contact
                else "wechat-direct-moments-sync.v1"
            ),
            **binding,
            count_name: 1,
        }
        manifest = {
            "format": format_name,
            **binding,
            "archivePath": "context.md",
            "archiveBytes": len(context),
            "archiveSha256": _sha256(context),
            "aiDefaultPath": "ai-context.md",
            "aiDefaultBytes": len(ai_context),
            "aiDefaultSha256": _sha256(ai_context),
            ("messagesPath" if is_contact else "momentsPath"): records_name,
            records_hash_name: _sha256(records),
            count_name: 1,
        }
        manifest["manifestSha256"] = _sha256(wechat_cli._canonical_bytes(manifest))
        (root / "state.json").write_bytes(wechat_cli._canonical_bytes(state))
        (root / "manifest.json").write_bytes(wechat_cli._canonical_bytes(manifest))
        return manifest

    def _verify(self, root: Path) -> tuple[int, dict]:
        return self._capture(
            wechat_cli.command_verify_export,
            argparse.Namespace(output=str(root)),
        )

    def test_local_discovery_precedence_and_invalid_settings(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / ".wechatdirect.local.json"
            settings.write_text(
                json.dumps({"config": "local.json", "export_root": "local-exports"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(wechat_cli, "LOCAL_SETTINGS_PATH", settings),
                mock.patch.dict(
                    os.environ,
                    {
                        "WECHAT_DIRECT_CONFIG": "environment.json",
                        "WECHAT_DIRECT_EXPORT_ROOT": "environment-exports",
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(
                    Path("explicit.json"),
                    wechat_cli._resolve_config_path("explicit.json"),
                )
                self.assertEqual(
                    Path("environment.json"), wechat_cli._resolve_config_path(None)
                )
                self.assertEqual(
                    Path("environment-exports"), wechat_cli._default_export_root()
                )
            settings.write_text('{"config":"x","extra":"secret"}', encoding="utf-8")
            with mock.patch.object(wechat_cli, "LOCAL_SETTINGS_PATH", settings):
                with self.assertRaisesRegex(
                    wechat_cli.ProductError, "wechat_local_settings_invalid"
                ):
                    wechat_cli._local_settings()

    def test_doctor_is_body_free_and_requires_windows(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._write_config(root)
            missing_settings = root / "missing-local-settings.json"
            with (
                mock.patch.object(wechat_cli, "LOCAL_SETTINGS_PATH", missing_settings),
                mock.patch.object(wechat_cli, "_module_available", return_value=True),
                mock.patch.object(
                    wechat_cli, "_voice_interpreter_available", return_value=True
                ),
                mock.patch.object(wechat_cli.sys, "platform", "win32"),
            ):
                code, payload = self._capture(
                    wechat_cli.command_doctor,
                    argparse.Namespace(config=str(config)),
                )
            self.assertEqual(0, code)
            self.assertEqual("success", payload["status"])
            self.assertTrue(payload["platform"]["windowsSupported"])
            self.assertEqual(2, payload["configuration"]["configuredAccounts"])
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(str(root), encoded)
            self.assertNotIn("source-0", encoded)

            with (
                mock.patch.object(wechat_cli, "LOCAL_SETTINGS_PATH", missing_settings),
                mock.patch.object(wechat_cli, "_module_available", return_value=True),
                mock.patch.object(
                    wechat_cli, "_voice_interpreter_available", return_value=True
                ),
                mock.patch.object(wechat_cli.sys, "platform", "linux"),
            ):
                code, payload = self._capture(
                    wechat_cli.command_doctor,
                    argparse.Namespace(config=str(config)),
                )
            self.assertEqual(2, code)
            self.assertIn("doctor_windows_required", payload["errors"])

    def test_verify_export_accepts_contact_and_moments_without_writes(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            for name, format_name in (
                ("contact", "wechat-direct-contact-export.v1"),
                ("moments", "wechat-direct-moments-export.v1"),
            ):
                root = Path(temporary) / name
                self._write_export(root, format_name=format_name)
                before = {
                    path.relative_to(root).as_posix(): _sha256(path.read_bytes())
                    for path in root.rglob("*")
                    if path.is_file()
                }
                code, payload = self._verify(root)
                after = {
                    path.relative_to(root).as_posix(): _sha256(path.read_bytes())
                    for path in root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(0, code)
                self.assertEqual("success", payload["status"])
                self.assertEqual(1, payload["recordCount"])
                self.assertEqual(before, after)

    def test_verify_export_rejects_fixed_file_symlink_escape(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "export"
            self._write_export(root)
            outside = parent / "outside-context.md"
            outside.write_bytes((root / "context.md").read_bytes())
            (root / "context.md").unlink()
            try:
                os.symlink(outside, root / "context.md")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {type(exc).__name__}")
            code, payload = self._verify(root)
            self.assertEqual(2, code)
            self.assertIn("export_context_path_invalid", payload["errors"])
            self.assertNotIn(str(outside), json.dumps(payload))

    def test_verify_export_rejects_cross_account_state(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "export"
            self._write_export(root)
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            state["account"] = "secondary"
            state["sourceFingerprint"] = "sha256:" + "c" * 64
            (root / "state.json").write_bytes(wechat_cli._canonical_bytes(state))
            code, payload = self._verify(root)
            self.assertEqual(2, code)
            self.assertIn("export_state_binding_mismatch", payload["errors"])

    def test_verify_export_requires_media_hash_size_and_relation(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            parent = Path(temporary)

            missing_root = parent / "missing"
            (missing_root / "media").mkdir(parents=True)
            (missing_root / "media" / "voice.silk").write_bytes(b"voice")
            self._write_export(
                missing_root,
                media={"exportedPath": "media/voice.silk", "bytes": 5},
            )
            code, payload = self._verify(missing_root)
            self.assertEqual(2, code)
            self.assertIn("export_media_sha256_invalid", payload["errors"])
            self.assertEqual(0, payload["mediaFilesChecked"])

            relation_root = parent / "relation"
            (relation_root / "media").mkdir(parents=True)
            source = b"voice"
            derived = b"wave"
            (relation_root / "media" / "voice.silk").write_bytes(source)
            (relation_root / "media" / "voice.wav").write_bytes(derived)
            self._write_export(
                relation_root,
                media={
                    "exportedPath": "media/voice.silk",
                    "bytes": len(source),
                    "sha256": _sha256(source),
                    "derivedVoiceWav": {
                        "path": "media/voice.wav",
                        "bytes": len(derived),
                        "sha256": _sha256(derived),
                        "derivedFromSha256": "sha256:" + "f" * 64,
                    },
                },
            )
            code, payload = self._verify(relation_root)
            self.assertEqual(2, code)
            self.assertIn(
                "export_derived_media_relation_mismatch", payload["errors"]
            )
            self.assertEqual(0, payload["mediaFilesChecked"])

    def test_parser_preserves_six_existing_commands_and_body_free_failure(self) -> None:
        parser = wechat_cli.parser()
        samples = {
            "context": ["context", "--contact", "x"],
            "moments": ["moments", "--account", "primary"],
            "sync-contact": ["sync-contact", "--contact", "x"],
            "sync-moments": ["sync-moments", "--account", "primary"],
            "media-open": [
                "media-open",
                "--account",
                "primary",
                "--locator",
                "opaque",
                "--output",
                "out.bin",
            ],
            "preserve": ["preserve", "--contact", "x", "--output", "out"],
        }
        for command, argv in samples.items():
            self.assertEqual(command, parser.parse_args(argv).command)

        stream = io.BytesIO()
        stdout = SimpleNamespace(buffer=stream)
        missing = "Z:/private/missing/accounts.json"
        with mock.patch.object(wechat_cli.sys, "stdout", stdout):
            code = wechat_cli.main(
                [
                    "context",
                    "--account",
                    "primary",
                    "--contact",
                    "synthetic",
                    "--config",
                    missing,
                ]
            )
        self.assertEqual(2, code)
        payload = json.loads(stream.getvalue())
        self.assertEqual("wechat_account_config_unavailable", payload["error"])
        self.assertNotIn(missing, stream.getvalue().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import wechat_cli


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5XcAAAAASUVORK5CYII=")
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def reading_context():
    messages = []
    for number, kind, locator in ((1, "image", "image"), (2, "emoji", "emoji"), (3, "emoji", "emoji")):
        messages.append({
            "nativeId": {"kind": "server", "value": str(number)},
            "serverId": number, "createTime": number, "sortSeq": number,
            "content": f"message {number}", "senderRole": "other",
            "sender": {"role": "other", "displayName": "Synthetic sender"},
            "media_manifest": [{"kind": kind, "mediaId": locator, "locator": locator, "openable": True}],
        })
    return {
        "status": "success", "account": "primary",
        "accountIdentityCommitment": "sha256:" + "a" * 64,
        "contact": {"nativeId": "synthetic-contact", "displayName": "Synthetic chat"},
        "sourceSnapshotCutoffS": 200,
        "requestedWindow": {"sinceS": 0, "untilS": 200},
        "coverage": {"hasMore": False}, "messages": messages,
        "quotedMessages": [], "gaps": [],
    }


class ReadingReader:
    account_identity_commitment = "a" * 64

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def open_locator(self, locator, *, allow_remote=False):
        return {"image": PNG, "emoji": GIF}[locator]


class ReadingPackageTests(unittest.TestCase):
    def export(self, output, context=None, *, html=True, local_only=False):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        arguments = [
            "export-context", "--config", "unused", "--account", "primary",
            "--contact", "synthetic", "--output", str(output),
        ]
        if html:
            arguments.append("--html")
        if local_only:
            arguments.append("--local-only")
        args = wechat_cli.parser().parse_args(arguments)
        with (
            patch.object(wechat_cli, "_context_result", return_value=deepcopy(context or reading_context())),
            patch.object(wechat_cli, "_read_config", return_value={"primary": {}}),
            patch.object(wechat_cli, "_reader", return_value=ReadingReader()),
            patch.object(wechat_cli.sys, "stdout", stdout),
        ):
            code = wechat_cli.command_export_context(args)
        return code, json.loads(stdout.buffer.getvalue())

    def test_default_ai_package_does_not_require_html_renderer(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reading"
            with patch.dict(sys.modules, {"wechat_render": None}):
                code, receipt = self.export(output, html=False)
            self.assertEqual(0, code)
            self.assertNotIn("htmlPath", receipt)
            self.assertFalse((output / "conversation.html").exists())
            self.assertTrue((output / "conversation.json").is_file())
            self.assertTrue((output / "ai-context.md").is_file())
            self.assertEqual(2, receipt["mediaExport"]["mediaCopied"])

    def test_pending_local_media_materializes_without_network_and_clears_old_gap(self):
        context = reading_context()
        context["messages"][0]["media_manifest"][0].update(
            openable=None, materializable=True, requiresNetwork=False,
            resolution_gap="media_materialization_required",
        )
        context["gaps"] = [{"kind": "media_not_opened", "message": context["messages"][0]["nativeId"], "mediaKind": "image"}]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reading"
            _, receipt = self.export(output, context, html=False, local_only=True)
            data = json.loads((output / "conversation.json").read_text(encoding="utf-8"))
            self.assertEqual("success", receipt["status"])
            self.assertEqual([], data["gaps"])
            media = data["messages"][0]["media_manifest"][0]
            self.assertEqual("available_local", media["exportStatus"])
            self.assertNotIn("resolution_gap", media)
            self.assertNotIn("requiresNetwork", media)

    def test_a_bad_visual_does_not_abort_other_messages_or_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reading"
            with patch.object(ReadingReader, "open_locator", lambda _self, locator: b"GIF89a-not-a-valid-gif" if locator == "image" else GIF):
                _, receipt = self.export(output, html=False)
            data = json.loads((output / "conversation.json").read_text(encoding="utf-8"))
            self.assertEqual("partial", receipt["status"])
            self.assertEqual("open_failed", data["messages"][0]["media_manifest"][0]["exportStatus"])
            self.assertNotIn("exportedPath", data["messages"][0]["media_manifest"][0])
            self.assertEqual("available_local", data["messages"][1]["media_manifest"][0]["exportStatus"])

    def test_reading_package_contains_real_assets_in_message_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reading"
            code, receipt = self.export(output)
            self.assertEqual(0, code)
            self.assertEqual("success", receipt["status"])
            data = json.loads((output / "conversation.json").read_text(encoding="utf-8"))
            self.assertEqual([1, 2, 3], [item["serverId"] for item in data["messages"]])
            paths = [item["media_manifest"][0]["exportedPath"] for item in data["messages"]]
            self.assertEqual(paths[1], paths[2])
            self.assertEqual(PNG, (output / paths[0]).read_bytes())
            self.assertEqual(GIF, (output / paths[1]).read_bytes())
            for message in data["messages"]:
                media = message["media_manifest"][0]
                self.assertEqual(media["sha256"], "sha256:" + hashlib.sha256((output / media["exportedPath"]).read_bytes()).hexdigest())
            html = (output / "conversation.html").read_text(encoding="utf-8")
            self.assertLess(html.index("message 1"), html.index("message 2"))
            self.assertLess(html.index("message 2"), html.index("message 3"))
            self.assertEqual(3, html.count("<img "))
            self.assertEqual(2, receipt["mediaExport"]["mediaCopied"])
            self.assertEqual(1, receipt["mediaExport"]["mediaReused"])
            self.assertTrue((output / "ai-context.md").is_file())
            self.assertFalse(output.with_name("reading.incomplete").exists())

    def test_missing_media_stays_in_place_and_marks_package_partial(self):
        context = reading_context()
        context["messages"][1]["media_manifest"][0].update(openable=False, resolution_gap="not_in_current_local_cache")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reading"
            _, receipt = self.export(output, context)
            data = json.loads((output / "conversation.json").read_text(encoding="utf-8"))
            self.assertEqual("partial", receipt["status"])
            self.assertEqual([1, 2, 3], [item["serverId"] for item in data["messages"]])
            self.assertNotIn("exportedPath", data["messages"][1]["media_manifest"][0])
            self.assertIn("exportedPath", data["messages"][2]["media_manifest"][0])
            html = (output / "conversation.html").read_text(encoding="utf-8")
            self.assertIn("message 2", html)
            self.assertEqual(2, html.count("<img "))

    def test_existing_reading_directory_is_preserved_without_reading_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reading"
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("existing content", encoding="utf-8")
            with self.assertRaisesRegex(wechat_cli.ProductError, "reading_output_already_exists"):
                self.export(output)
            self.assertEqual("existing content", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

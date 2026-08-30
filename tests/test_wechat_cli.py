from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wechat_cli


VOICE_BYTES = b"\x02#!SILK_V3fake"


class BinaryOutput:
    def __init__(self):
        self.buffer = io.BytesIO()


class FakeReader:
    def __init__(self, label: str):
        self.label = label
        self.self_native_id = f"wxid-{label}"
        self.moments_self_native_id = self.self_native_id
        self.account_identity_commitment = hashlib.sha256(label.encode()).hexdigest()
        self.closed = False
        self.fetch_calls = []
        self.moments_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.closed = True

    def list_contacts(self, *, include_unregistered=False):
        return [
            {
                "nativeId": f"wxid-{self.label}",
                "sessionType": "contact",
                "displayName": "测试联系人",
                "remark": "测试联系人",
                "nickname": "同名昵称",
                "alias": f"alias-{self.label}",
                "lastTimestamp": 200,
            }
        ]

    def contact_source_fingerprint(self, session_native_id):
        assert session_native_id == f"wxid-{self.label}"
        return {
            "kind": "wechat-contact-source-metadata.v1",
            "sha256": "sha256:"
            + hashlib.sha256((self.label + ":source").encode()).hexdigest(),
            "fileCount": 4,
            "bytes": 100,
            "messageSourceCount": 1,
            "messageSourceCatalogSha256": "sha256:" + "c" * 64,
        }

    def moments_source_fingerprint(self):
        return {
            "kind": "wechat-moments-source-metadata.v1",
            "sha256": "sha256:"
            + hashlib.sha256((self.label + ":moments").encode()).hexdigest(),
            "fileCount": 2,
            "bytes": 50,
        }

    def fetch_messages(
        self,
        session_native_id,
        *,
        since_s,
        end_s,
        since_sort_seq=None,
        limit=None,
        exact_media_lookup=False,
        allow_unindexed_time_fallback=False,
    ):
        assert session_native_id == f"wxid-{self.label}"
        self.fetch_calls.append(
            {
                "since_s": since_s,
                "end_s": end_s,
                "since_sort_seq": since_sort_seq,
                "limit": limit,
                "exact_media_lookup": exact_media_lookup,
                "allow_unindexed_time_fallback": allow_unindexed_time_fallback,
            }
        )
        return {
            "messages": [
                {
                    "serverId": 10,
                    "localId": 1,
                    "createTime": 100,
                    "sortSeq": 1,
                    "content": "前文",
                    "senderRole": "other",
                    "direction": "incoming",
                    "type": "text",
                },
                {
                    "serverId": 11,
                    "localId": 2,
                    "createTime": 110,
                    "sortSeq": 2,
                    "content": "回复内容",
                    "senderRole": "self",
                    "direction": "outgoing",
                    "type": "app",
                    "quote": {"platformMessageId": "9"},
                    "media_manifest": [
                        {
                            "kind": "image",
                            "locator": "wechat-db://opaque",
                            "openable": False,
                            "open_status": "not_openable",
                            "resolution_gap": "resource_context_unproven",
                        },
                        {
                            "kind": "voice",
                            "mediaId": "voice-1",
                            "locator": "wechat-db://voice",
                            "openable": True,
                            "open_status": "openable",
                        },
                    ],
                },
            ]
        }

    def fetch_message_by_server_id(
        self, session_native_id, server_id, *, exact_media_lookup=False
    ):
        assert session_native_id == f"wxid-{self.label}"
        assert server_id == "9"
        assert exact_media_lookup
        return {
            "serverId": 9,
            "localId": 0,
            "createTime": 90,
            "sortSeq": 0,
            "content": "被引用正文",
            "senderRole": "other",
            "direction": "incoming",
            "type": "text",
        }

    def list_moments(self, *, since_s, end_s, username=None, limit=20):
        assert since_s <= 200
        assert end_s == 200
        assert username in (None, f"wxid-{self.label}")
        assert limit in (5, None)
        self.moments_calls.append(
            {
                "since_s": since_s,
                "end_s": end_s,
                "username": username,
                "limit": limit,
            }
        )
        return {
            "moments": [
                {
                    "nativeId": "moment-1",
                    "username": f"wxid-{self.label}",
                    "createTime": 190,
                    "content": "当前可见朋友圈正文",
                    "contentType": "1",
                    "title": None,
                    "description": None,
                    "media_manifest": [
                        {
                            "kind": "moment_media",
                            "openable": False,
                            "open_status": "remote_locator_not_opened",
                        }
                    ],
                    "sourceSha256": "sha256:" + "1" * 64,
                }
            ],
            "sourceVisibleCutoffS": 190,
            "scannedRows": 1,
            "matchedRows": 1,
            "hasMoreCurrentCache": False,
            "historyScope": "current_local_cache_only",
            "gaps": [],
        }

    def open_locator(self, locator):
        assert locator == "wechat-db://voice"
        return VOICE_BYTES

    def resolve_locator(self, locator):
        assert locator == "wechat-db://voice"
        return {"kind": "voice", "size": len(VOICE_BYTES), "openable": True}


def context_args(**overrides):
    values = {
        "config": "unused.json",
        "account": "primary",
        "contact": "测试联系人",
        "since": None,
        "until": None,
        "around": None,
        "contains": None,
        "lookback_days": 7,
        "scan_limit": 120,
        "return_limit": 24,
        "output": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class WeChatCliTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "primary": {
                "config_path": "primary",
                "local_state_path": "state",
                "expected_source_identity_sha256": "sha256:"
                + hashlib.sha256(b"primary").hexdigest(),
                "expected_moments_author_sha256": "sha256:"
                + hashlib.sha256(b"wxid-primary").hexdigest(),
            },
            "secondary": {
                "config_path": "secondary",
                "local_state_path": "state",
                "expected_source_identity_sha256": "sha256:"
                + hashlib.sha256(b"secondary").hexdigest(),
                "expected_moments_author_sha256": "sha256:"
                + hashlib.sha256(b"wxid-secondary").hexdigest(),
            },
        }

    def reader_factory(self, account, cutoff_s):
        return FakeReader(str(account["config_path"]))

    def test_jsonl_escapes_unicode_line_separators(self):
        encoded = wechat_cli._canonical_bytes({"content": "甲\u2028乙\u2029丙"})
        self.assertNotIn("\u2028".encode("utf-8"), encoded)
        self.assertNotIn("\u2029".encode("utf-8"), encoded)
        self.assertEqual(json.loads(encoded)["content"], "甲\u2028乙\u2029丙")

    def test_reader_rejects_wrong_source_identity_commitment_before_reads(self):
        reader = FakeReader("primary")
        account = dict(self.config["primary"])
        account["expected_source_identity_sha256"] = "sha256:" + "0" * 64
        with patch("wechat_cli.DirectWeChatReader", return_value=reader):
            with self.assertRaisesRegex(
                wechat_cli.ProductError, "wechat_account_identity_binding_mismatch"
            ):
                wechat_cli._reader(account, 200)
        self.assertTrue(reader.closed)

    def test_account_config_requires_both_identity_commitments(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "accounts.json"
            path.write_text(
                json.dumps(
                    {
                        "primary": {
                            "config_path": "primary",
                            "local_state_path": "state",
                        },
                        "secondary": {
                            "config_path": "secondary",
                            "local_state_path": "state",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                wechat_cli.ProductError, "wechat_account_config_invalid"
            ):
                wechat_cli._read_config(path)

    def test_moments_self_rejects_wrong_author_commitment(self):
        reader = FakeReader("primary")
        account = dict(self.config["primary"])
        account["expected_moments_author_sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            wechat_cli.ProductError, "wechat_moments_identity_binding_mismatch"
        ):
            wechat_cli._verify_moments_self_identity(account, reader)

    def test_group_member_without_label_is_never_replaced_by_group_name(self):
        receipt = wechat_cli._sender_receipt(
            {
                "senderRole": "other",
                "senderUsername": "wxid-member",
            },
            contacts={},
            selected_contact={
                "nativeId": "room@chatroom",
                "displayName": "测试群名",
            },
            account="secondary",
        )
        self.assertEqual(receipt["displayName"], "群成员（昵称未取到）")
        self.assertNotEqual(receipt["displayName"], "测试群名")
        self.assertEqual(receipt["labelGap"], "group_member_label_unavailable")

    def test_filehelper_is_labeled_without_rewriting_sender_roles(self):
        contact = wechat_cli._safe_contact(
            {
                "nativeId": "filehelper",
                "displayName": "文件传输助手",
                "sessionType": "contact",
            },
            "primary",
        )
        self.assertEqual(contact["conversationKind"], "file_transfer_assistant")

    def test_ai_context_keeps_latest_message_under_media_pressure(self):
        media = [
            {
                "kind": "image",
                "exportedPath": "media/" + ("x" * 2048) + f"-{index}.jpg",
            }
            for index in range(400)
        ]
        message = {
            "serverId": 1,
            "createTime": 100,
            "content": "最新消息必须保留",
            "sender": {"displayName": "我"},
            "media_manifest": media,
        }
        rendered, count = wechat_cli._bounded_contact_ai_context(
            account="primary",
            contact={"displayName": "测试联系人"},
            messages=[message],
        )
        self.assertEqual(count, 1)
        self.assertIn("最新消息必须保留", rendered)
        self.assertIn("完整清单见 context.md", rendered)
        self.assertLessEqual(
            len(rendered.encode("utf-8")), wechat_cli.MAX_AI_CONTEXT_BYTES
        )

    def test_exact_contact_ambiguity_never_picks_first(self):
        readers = {label: FakeReader(label) for label in ("primary", "secondary")}

        def factory(account, cutoff_s):
            return readers[str(account["config_path"])]

        with patch("wechat_cli._reader", side_effect=factory):
            with self.assertRaisesRegex(wechat_cli.ProductError, "contact_ambiguous"):
                wechat_cli._resolve_contact(
                    self.config,
                    account="auto",
                    query="测试联系人",
                    cutoff_s=200,
                )
        self.assertTrue(all(reader.closed for reader in readers.values()))

    def test_context_keeps_native_ids_quote_target_and_media_gap(self):
        with (
            patch("wechat_cli._read_config", return_value=self.config),
            patch("wechat_cli._reader", side_effect=self.reader_factory),
            patch("wechat_cli.time.time", return_value=200),
        ):
            result = wechat_cli._context_result(context_args())
        self.assertEqual(result["account"], "primary")
        self.assertEqual(result["actualVisibleCutoffS"], 110)
        self.assertEqual(result["historyScope"], "bounded_requested_window")
        self.assertEqual(result["returnedMessages"], 2)
        self.assertEqual(
            result["returnedSenderRoleCounts"],
            {"self": 1, "other": 1, "system": 0, "unknown": 0},
        )
        self.assertEqual(result["selfObservation"]["status"], "observed")
        self.assertEqual(
            result["messages"][1]["nativeId"], {"kind": "server", "value": "11"}
        )
        self.assertEqual(result["quotedMessages"][0]["content"], "被引用正文")
        self.assertEqual(result["mediaCounts"], {"image": 1, "voice": 1})
        self.assertEqual(result["gaps"][0]["kind"], "media_not_openable")
        self.assertRegex(result["manifestSha256"], r"^sha256:[0-9a-f]{64}$")

    def test_empty_context_reports_that_older_local_history_exists(self):
        class EmptyReader(FakeReader):
            def list_contacts(self, *, include_unregistered=False):
                contacts = super().list_contacts(
                    include_unregistered=include_unregistered
                )
                contacts[0]["lastTimestamp"] = 50
                return contacts

            def fetch_messages(self, *args, **kwargs):
                return {"messages": []}

        with (
            patch("wechat_cli._read_config", return_value=self.config),
            patch(
                "wechat_cli._reader",
                side_effect=lambda account, cutoff_s: EmptyReader(
                    str(account["config_path"])
                ),
            ),
            patch("wechat_cli.time.time", return_value=200),
        ):
            result = wechat_cli._context_result(
                context_args(since="1970-01-01T00:02:00+00:00")
            )
        self.assertEqual(result["returnedMessages"], 0)
        self.assertEqual(
            result["availableHistoryHint"]["status"],
            "older_local_messages_exist",
        )
        self.assertEqual(
            result["selfObservation"]["status"],
            "not_observed_in_returned_window",
        )

    def test_explicit_preservation_bundle_is_self_contained(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bundle"
            args = context_args(output=str(destination))
            with (
                patch("wechat_cli._read_config", return_value=self.config),
                patch("wechat_cli._reader", side_effect=self.reader_factory),
                patch(
                    "wechat_cli._decode_voice_file",
                    side_effect=lambda _source, output: output.write_bytes(
                        b"wav-bytes"
                    ),
                ),
                patch("wechat_cli.time.time", return_value=200),
                patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
            ):
                code = wechat_cli.command_preserve(args)
            self.assertEqual(code, 0)
            self.assertTrue((destination / "messages.json").is_file())
            manifest = json.loads(
                (destination / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["format"], "wechat-direct-preservation.v1")
            self.assertEqual(manifest["messageCount"], 2)
            self.assertEqual(manifest["account"], "primary")
            self.assertTrue(manifest["gaps"])
            self.assertEqual(len(manifest["mediaFiles"]), 1)
            media = destination / manifest["mediaFiles"][0]["path"]
            self.assertEqual(media.read_bytes(), VOICE_BYTES)
            self.assertEqual(len(manifest["derivedFiles"]), 1)
            decoded = destination / manifest["derivedFiles"][0]["path"]
            self.assertEqual(decoded.read_bytes(), b"wav-bytes")

    def test_moments_reports_current_cache_and_unopened_media(self):
        args = argparse.Namespace(
            config="unused.json",
            account="primary",
            contact="测试联系人",
            since=None,
            until=None,
            lookback_days=30,
            limit=5,
        )
        output = BinaryOutput()
        with (
            patch("wechat_cli._read_config", return_value=self.config),
            patch("wechat_cli._reader", side_effect=self.reader_factory),
            patch("wechat_cli.time.time", return_value=200),
            patch.object(wechat_cli.sys, "stdout", output),
        ):
            code = wechat_cli.command_moments(args)
        result = json.loads(output.buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["historyScope"], "current_local_cache_only")
        self.assertEqual(result["actualVisibleCutoffS"], 190)
        self.assertEqual(result["returnedMoments"], 1)
        self.assertEqual(result["moments"][0]["contact"]["displayName"], "测试联系人")
        self.assertEqual(result["gaps"][0]["kind"], "moment_media_not_opened")

    def test_moments_self_uses_source_account_identity(self):
        args = argparse.Namespace(
            config="unused.json",
            account="primary",
            contact=None,
            since=None,
            until=None,
            lookback_days=30,
            limit=5,
        )
        setattr(args, "self", True)
        output = BinaryOutput()
        created = []

        def factory(account, cutoff_s):
            reader = FakeReader(str(account["config_path"]))
            created.append(reader)
            return reader

        with (
            patch("wechat_cli._read_config", return_value=self.config),
            patch("wechat_cli._reader", side_effect=factory),
            patch("wechat_cli.time.time", return_value=200),
            patch.object(wechat_cli.sys, "stdout", output),
        ):
            self.assertEqual(wechat_cli.command_moments(args), 0)
        result = json.loads(output.buffer.getvalue())
        self.assertTrue(result["contact"]["isSelf"])
        self.assertEqual(result["contact"]["displayName"], "我")
        self.assertEqual(created[0].moments_calls[-1]["username"], "wxid-primary")

    def test_moments_self_cache_miss_requests_same_account_profile_open(self):
        class EmptyMomentsReader(FakeReader):
            def list_moments(self, **kwargs):
                result = super().list_moments(**kwargs)
                result["moments"] = []
                result["matchedRows"] = 0
                return result

        args = argparse.Namespace(
            config="unused.json",
            account="primary",
            contact=None,
            since=None,
            until=None,
            lookback_days=30,
            limit=5,
        )
        setattr(args, "self", True)
        output = BinaryOutput()
        with (
            patch("wechat_cli._read_config", return_value=self.config),
            patch(
                "wechat_cli._reader",
                side_effect=lambda account, cutoff_s: EmptyMomentsReader(
                    str(account["config_path"])
                ),
            ),
            patch("wechat_cli.time.time", return_value=200),
            patch.object(wechat_cli.sys, "stdout", output),
        ):
            self.assertEqual(wechat_cli.command_moments(args), 0)
        result = json.loads(output.buffer.getvalue())
        self.assertEqual(
            result["targetCacheStatus"], "target_not_in_current_local_cache"
        )
        self.assertEqual(
            result["gaps"][-1]["nextAction"],
            "open_the_target_moments_profile_in_this_exact_account_then_retry",
        )

    def test_moments_resolves_author_visible_only_in_current_cache(self):
        class AuthorOnlyReader(FakeReader):
            def list_contacts(self, *, include_unregistered=False):
                return []

            def list_moments(self, *, since_s, end_s, username=None, limit=20):
                result = super().list_moments(
                    since_s=since_s,
                    end_s=end_s,
                    username=None,
                    limit=limit,
                )
                result["moments"][0]["username"] = "wxid-visible-only"
                result["moments"][0]["nickname"] = "仅朋友圈可见的人"
                if username and username != "wxid-visible-only":
                    result["moments"] = []
                    result["matchedRows"] = 0
                return result

        args = argparse.Namespace(
            config="unused.json",
            account="primary",
            contact="仅朋友圈可见的人",
            since=None,
            until=None,
            lookback_days=30,
            limit=5,
        )
        output = BinaryOutput()
        with (
            patch("wechat_cli._read_config", return_value=self.config),
            patch(
                "wechat_cli._reader",
                side_effect=lambda account, cutoff_s: AuthorOnlyReader(
                    str(account["config_path"])
                ),
            ),
            patch("wechat_cli.time.time", return_value=200),
            patch.object(wechat_cli.sys, "stdout", output),
        ):
            self.assertEqual(wechat_cli.command_moments(args), 0)
        result = json.loads(output.buffer.getvalue())
        self.assertEqual(result["contact"]["nativeId"], "wxid-visible-only")
        self.assertEqual(result["returnedMoments"], 1)

    def test_moments_rejects_implicit_cross_account_selection(self):
        args = argparse.Namespace(
            config="unused.json",
            account="auto",
            contact="测试联系人",
            since=None,
            until=None,
            lookback_days=30,
            limit=5,
        )
        with patch("wechat_cli._read_config", return_value=self.config):
            with self.assertRaisesRegex(
                wechat_cli.ProductError, "moments_explicit_account_required"
            ):
                wechat_cli.command_moments(args)

    def test_media_open_writes_only_explicit_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "voice.bin"
            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                locator="wechat-db://voice",
                output=str(destination),
                voice_wav=False,
            )
            with (
                patch("wechat_cli._read_config", return_value=self.config),
                patch("wechat_cli._reader", side_effect=self.reader_factory),
                patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
            ):
                code = wechat_cli.command_media_open(args)
            self.assertEqual(code, 0)
            self.assertEqual(destination.read_bytes(), VOICE_BYTES)
            self.assertFalse(destination.with_name("voice.bin.incomplete").exists())

    def test_media_open_decodes_voice_only_when_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "voice.wav"
            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                locator="wechat-db://voice",
                output=str(destination),
                voice_wav=True,
            )
            with (
                patch("wechat_cli._read_config", return_value=self.config),
                patch("wechat_cli._reader", side_effect=self.reader_factory),
                patch(
                    "wechat_cli._decode_voice_file",
                    side_effect=lambda _source, output: output.write_bytes(
                        b"wav-bytes"
                    ),
                ),
                patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
            ):
                code = wechat_cli.command_media_open(args)
            self.assertEqual(code, 0)
            self.assertEqual(destination.read_bytes(), b"wav-bytes")

    def test_contact_sync_is_full_then_bounded_incremental_ai_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "contact"
            created = []

            def factory(account, cutoff_s):
                reader = FakeReader(str(account["config_path"]))
                created.append(reader)
                return reader

            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                contact="测试联系人",
                output=str(destination),
                since=None,
                until=None,
                overlap_seconds=60,
            )
            with (
                patch("wechat_cli._read_config", return_value=self.config),
                patch("wechat_cli._reader", side_effect=factory),
                patch("wechat_cli.time.time", return_value=200),
                patch(
                    "wechat_cli._decode_voice_file",
                    side_effect=lambda _source, output: output.write_bytes(
                        b"wav-bytes"
                    ),
                ),
                patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
            ):
                self.assertEqual(wechat_cli.command_sync_contact(args), 0)

            context = (destination / "context.md").read_text(encoding="utf-8")
            self.assertIn("[08:01:40] 测试联系人：前文", context)
            self.assertIn("[08:01:50] 我：回复内容", context)
            self.assertIn("↳ 回复：[被引用消息不在当前本地范围]", context)
            self.assertIn("[语音（可播放，按需转写）](media/", context)
            self.assertIn("[图片当前不可打开", context)
            ai_context = (destination / "ai-context.md").read_text(encoding="utf-8")
            self.assertIn("最近小上下文", ai_context)
            first_receipt = json.loads(
                (destination / "last-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_receipt["mode"], "full")
            self.assertEqual(first_receipt["newMessages"], 2)
            self.assertEqual(first_receipt["totalMessages"], 2)
            self.assertRegex(
                first_receipt["accountIdentityCommitment"],
                r"^sha256:[0-9a-f]{64}$",
            )
            self.assertEqual(
                first_receipt["senderRoleCounts"],
                {"self": 1, "other": 1, "system": 0, "unknown": 0},
            )
            context_mtime = (destination / "context.md").stat().st_mtime_ns

            with (
                patch("wechat_cli._read_config", return_value=self.config),
                patch("wechat_cli._reader", side_effect=factory),
                patch("wechat_cli.time.time", return_value=200),
                patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
            ):
                self.assertEqual(wechat_cli.command_sync_contact(args), 0)

            second_receipt = json.loads(
                (destination / "last-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second_receipt["mode"], "incremental")
            self.assertTrue(second_receipt["noChange"])
            self.assertTrue(second_receipt["sourceMetadataFastPath"])
            self.assertEqual(
                (destination / "context.md").stat().st_mtime_ns, context_mtime
            )
            self.assertEqual(created[0].fetch_calls[0]["since_s"], None)
            self.assertEqual(created[0].fetch_calls[0]["limit"], None)
            self.assertTrue(created[0].fetch_calls[0]["exact_media_lookup"])
            self.assertEqual(created[1].fetch_calls, [])

    def test_contact_sync_rejects_partial_first_snapshot(self):
        args = argparse.Namespace(
            config="unused.json",
            account="primary",
            contact="测试联系人",
            output="unused",
            since="2026-01-01T00:00:00+08:00",
            until=None,
            overlap_seconds=86_400,
            full_reconcile=False,
        )
        with self.assertRaisesRegex(
            wechat_cli.ProductError, "sync_contact_first_run_must_be_full"
        ):
            wechat_cli.command_sync_contact(args)

    def test_contact_sync_changed_source_replays_indexed_cursor_and_merges(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "contact"
            phase = {"value": 0}
            created = []

            def factory(account, cutoff_s):
                reader = FakeReader(str(account["config_path"]))
                base_fingerprint = reader.contact_source_fingerprint
                base_fetch = reader.fetch_messages

                def fingerprint(session_native_id):
                    value = base_fingerprint(session_native_id)
                    value["sha256"] = (
                        "sha256:"
                        + hashlib.sha256(f"phase:{phase['value']}".encode()).hexdigest()
                    )
                    return value

                def fetch(*args, **kwargs):
                    value = base_fetch(*args, **kwargs)
                    if phase["value"]:
                        value["messages"].append(
                            {
                                "serverId": 12,
                                "localId": 3,
                                "createTime": 120,
                                "sortSeq": 3,
                                "content": "新增消息",
                                "senderRole": "other",
                                "direction": "incoming",
                                "type": "text",
                            }
                        )
                    return value

                reader.contact_source_fingerprint = fingerprint
                reader.fetch_messages = fetch
                created.append(reader)
                return reader

            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                contact="测试联系人",
                output=str(destination),
                since=None,
                until=None,
                overlap_seconds=60,
                full_reconcile=False,
            )
            for current_phase in (0, 1):
                phase["value"] = current_phase
                with (
                    patch("wechat_cli._read_config", return_value=self.config),
                    patch("wechat_cli._reader", side_effect=factory),
                    patch("wechat_cli.time.time", return_value=200 + current_phase),
                    patch(
                        "wechat_cli._decode_voice_file",
                        side_effect=lambda _source, output: output.write_bytes(
                            b"wav-bytes"
                        ),
                    ),
                    patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
                ):
                    self.assertEqual(wechat_cli.command_sync_contact(args), 0)
            receipt = json.loads(
                (destination / "last-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["mode"], "incremental")
            self.assertEqual(receipt["newMessages"], 1)
            self.assertEqual(receipt["totalMessages"], 3)
            self.assertIsNone(created[0].fetch_calls[0]["since_sort_seq"])
            self.assertIsNotNone(created[1].fetch_calls[0]["since_sort_seq"])
            records = wechat_cli._read_jsonl(destination / "messages.jsonl")
            self.assertEqual([item["serverId"] for item in records], [10, 11, 12])

    def test_manifest_is_published_before_state_commit_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "contact"
            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                contact="测试联系人",
                output=str(destination),
                since=None,
                until=None,
                overlap_seconds=60,
                full_reconcile=False,
            )
            writes = []
            real_write = wechat_cli._write_json_atomic

            def record_write(path, value):
                writes.append(Path(path).name)
                real_write(path, value)

            with (
                patch("wechat_cli._read_config", return_value=self.config),
                patch("wechat_cli._reader", side_effect=self.reader_factory),
                patch("wechat_cli.time.time", return_value=200),
                patch(
                    "wechat_cli._decode_voice_file",
                    side_effect=lambda _source, output: output.write_bytes(
                        b"wav-bytes"
                    ),
                ),
                patch("wechat_cli._write_json_atomic", side_effect=record_write),
                patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
            ):
                self.assertEqual(wechat_cli.command_sync_contact(args), 0)
            self.assertLess(writes.index("manifest.json"), writes.index("state.json"))

    def test_contact_sync_does_not_commit_when_source_changes_during_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "contact"

            def factory(account, cutoff_s):
                reader = FakeReader(str(account["config_path"]))
                calls = {"value": 0}

                def fingerprint(session_native_id):
                    calls["value"] += 1
                    return {
                        "kind": "wechat-contact-source-metadata.v1",
                        "sha256": "sha256:"
                        + hashlib.sha256(f"read:{calls['value']}".encode()).hexdigest(),
                        "fileCount": 4,
                        "messageSourceCount": 1,
                        "messageSourceCatalogSha256": "sha256:" + "c" * 64,
                    }

                reader.contact_source_fingerprint = fingerprint
                return reader

            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                contact="测试联系人",
                output=str(destination),
                since=None,
                until=None,
                overlap_seconds=60,
                full_reconcile=False,
            )
            with (
                patch("wechat_cli._read_config", return_value=self.config),
                patch("wechat_cli._reader", side_effect=factory),
                patch("wechat_cli.time.time", return_value=200),
            ):
                with self.assertRaisesRegex(
                    wechat_cli.ProductError, "source_changed_during_sync_retry"
                ):
                    wechat_cli.command_sync_contact(args)
            self.assertFalse((destination / "state.json").exists())
            self.assertFalse((destination / "messages.jsonl").exists())

    def test_moments_sync_is_current_cache_full_then_no_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "moments"
            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                contact="测试联系人",
                output=str(destination),
            )
            for expected_mode, expected_no_change in (
                ("full", False),
                ("incremental", True),
            ):
                with (
                    patch("wechat_cli._read_config", return_value=self.config),
                    patch("wechat_cli._reader", side_effect=self.reader_factory),
                    patch("wechat_cli.time.time", return_value=200),
                    patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
                ):
                    self.assertEqual(wechat_cli.command_sync_moments(args), 0)
                receipt = json.loads(
                    (destination / "last-run.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["mode"], expected_mode)
                self.assertEqual(receipt["noChange"], expected_no_change)
                self.assertEqual(receipt["historyScope"], "current_local_cache_only")
                self.assertEqual(receipt["preservedMoments"], 1)
                self.assertRegex(
                    receipt["accountIdentityCommitment"],
                    r"^sha256:[0-9a-f]{64}$",
                )
                self.assertEqual(
                    receipt.get("sourceMetadataFastPath"),
                    True if expected_no_change else None,
                )
            context = (destination / "context.md").read_text(encoding="utf-8")
            self.assertIn("当前本机可见条目：1", context)
            self.assertIn("当前可见朋友圈正文", context)
            self.assertIn("范围不是朋友圈全历史", context)
            self.assertTrue((destination / "ai-context.md").is_file())

    def test_moments_self_sync_replays_same_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "self-moments"
            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                contact=None,
                output=str(destination),
            )
            setattr(args, "self", True)
            for expected_no_change in (False, True):
                output = BinaryOutput()
                with (
                    patch("wechat_cli._read_config", return_value=self.config),
                    patch("wechat_cli._reader", side_effect=self.reader_factory),
                    patch("wechat_cli.time.time", return_value=200),
                    patch.object(wechat_cli.sys, "stdout", output),
                ):
                    self.assertEqual(wechat_cli.command_sync_moments(args), 0)
                receipt = json.loads(output.buffer.getvalue())
                self.assertEqual(receipt["noChange"], expected_no_change)
                self.assertTrue(receipt["contact"]["isSelf"])
            state = json.loads((destination / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["contactNativeId"], "wxid-primary")
            self.assertTrue(state["contact"]["isSelf"])

    def test_moments_sync_replaces_items_evicted_from_current_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "moments"
            phase = {"value": 0}

            def factory(account, cutoff_s):
                reader = FakeReader(str(account["config_path"]))

                def fingerprint():
                    return {
                        "kind": "wechat-moments-source-metadata.v1",
                        "sha256": "sha256:"
                        + hashlib.sha256(
                            f"moments:{phase['value']}".encode()
                        ).hexdigest(),
                        "fileCount": 2,
                    }

                reader.moments_source_fingerprint = fingerprint
                if phase["value"]:
                    reader.list_moments = lambda **_kwargs: {
                        "moments": [],
                        "sourceVisibleCutoffS": None,
                        "scannedRows": 1,
                        "matchedRows": 0,
                        "hasMoreCurrentCache": False,
                        "historyScope": "current_local_cache_only",
                        "gaps": [],
                    }
                return reader

            args = argparse.Namespace(
                config="unused.json",
                account="primary",
                contact="测试联系人",
                output=str(destination),
            )
            for current_phase in (0, 1):
                phase["value"] = current_phase
                with (
                    patch("wechat_cli._read_config", return_value=self.config),
                    patch("wechat_cli._reader", side_effect=factory),
                    patch("wechat_cli.time.time", return_value=200 + current_phase),
                    patch.object(wechat_cli.sys, "stdout", BinaryOutput()),
                ):
                    self.assertEqual(wechat_cli.command_sync_moments(args), 0)
            receipt = json.loads(
                (destination / "last-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["removedMoments"], 1)
            self.assertEqual(receipt["preservedMoments"], 0)
            self.assertEqual(wechat_cli._read_jsonl(destination / "moments.jsonl"), [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import isolation  # noqa: F401

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wechat_cli
from test_contact_resolution import DirectoryReader, contact
from test_wechat_cli import BinaryOutput


def entry(name="我的称呼", native_id="wxid-primary", account="primary"):
    return {
        "name": name, "account": account, "nativeId": native_id,
        "chatType": "group" if native_id.endswith("@chatroom") else "private",
        "label": "登记时的名字", "addedAt": "2026-01-01T08:00:00+08:00",
    }


class AliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wechat-alias-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "aliases.json"
        self.settings = self.root / ".wechatdirect.local.json"
        self.settings.write_text(json.dumps({"aliases": str(self.path)}), encoding="utf-8")
        self.config = {label: {"config_path": label} for label in ("primary", "secondary")}
        self.contacts = {label: [contact(f"wxid-{label}", f"联系人{label}")] for label in self.config}
        self.created = []
        for target, options in (
            ("LOCAL_SETTINGS_PATH", {"new": self.settings}),
            ("_read_config", {"return_value": self.config}),
            ("_reader", {"side_effect": self.factory}),
        ):
            active = patch.object(wechat_cli, target, **options)
            active.start()
            self.addCleanup(active.stop)
        active = patch("wechat_cli.time.time", return_value=200)
        active.start()
        self.addCleanup(active.stop)

    def factory(self, account, cutoff_s):
        label = account["config_path"]
        reader = DirectoryReader(label, self.contacts[label], {})
        self.created.append(reader)
        return reader

    def write(self, *entries):
        self.path.write_text(json.dumps({"schema": "wechat-direct.aliases.v1", "aliases": entries}, ensure_ascii=False), encoding="utf-8")

    def main(self, *argv):
        output = BinaryOutput()
        with patch.object(wechat_cli.sys, "stdout", output):
            code = wechat_cli.main(list(argv))
        return code, json.loads(output.buffer.getvalue())

    def resolve(self, query="我的称呼", **options):
        return wechat_cli._resolve_contact(self.config, account=options.pop("account", "auto"), query=query, cutoff_s=200, aliases_path=self.path, **options)

    def test_add_native_id_writes_utf8_schema_and_beijing_time_without_messages(self):
        code, result = self.main("alias", "add", "--name", "我的称呼", "--contact", "wxid-primary", "--account", "primary")
        self.assertEqual((code, result["status"]), (0, "success"))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "wechat-direct.aliases.v1")
        self.assertEqual(data["aliases"], [result["alias"]])
        self.assertEqual(result["alias"]["nativeId"], "wxid-primary")
        self.assertEqual(result["alias"]["chatType"], "private")
        self.assertEqual(datetime.fromisoformat(result["alias"]["addedAt"]).utcoffset(), timedelta(hours=8))
        self.assertIn(b'\n  "', self.path.read_bytes())
        self.assertIn("我的称呼".encode(), self.path.read_bytes())
        self.assertTrue(all(not reader.fetch_calls and reader.closed for reader in self.created))

    def test_add_ambiguity_and_not_found_leave_table_absent(self):
        for query, expected in (("联系人", "needs_choice"), ("不存在", "failed")):
            with self.subTest(query=query):
                code, result = self.main("alias", "add", "--name", "称呼", "--contact", query)
                self.assertEqual((code, result["status"]), (2, expected))
                self.assertFalse(self.path.exists())
        self.assertEqual(result["error"], "contact_not_found")

    def test_add_does_not_resolve_an_existing_alias(self):
        self.write(entry("旧称呼"))
        before = self.path.read_bytes()
        code, result = self.main("alias", "add", "--name", "新称呼", "--contact", "旧称呼")
        self.assertEqual((code, result["error"]), (2, "contact_not_found"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_add_unique_partial_and_group_binding_with_type_filter(self):
        self.contacts["primary"].append(contact("group@chatroom", "项目讨论群"))
        code, result = self.main("alias", "add", "--name", "工作小组", "--contact", "项目讨论", "--chat-type", "group")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["alias"]["nativeId"], result["alias"]["chatType"]), ("group@chatroom", "group"))
        _, reader, selected = self.resolve("工作小组", chat_type="group")
        self.assertEqual(selected["nativeId"], "group@chatroom")
        reader.close()

    def test_add_native_id_precedes_another_contacts_same_remark(self):
        self.contacts["secondary"] = [contact("wxid-other", "wxid-primary")]
        code, result = self.main("alias", "add", "--name", "原生绑定", "--contact", "wxid-primary")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["alias"]["account"], result["alias"]["nativeId"]), ("primary", "wxid-primary"))

    def test_duplicate_add_updates_label_and_time_without_duplicate(self):
        self.write(entry("我（的称呼）"))
        code, result = self.main("alias", "add", "--name", "我 的称呼", "--contact", "wxid-primary")
        self.assertEqual(code, 0)
        stored = json.loads(self.path.read_text(encoding="utf-8"))["aliases"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["label"], "联系人primary")
        self.assertNotEqual(stored[0]["addedAt"], "2026-01-01T08:00:00+08:00")
        self.assertEqual(stored[0], result["alias"])

    def test_alias_precedes_wechat_exact_and_preserves_original_query(self):
        self.write(entry("我（的称呼）"))
        self.contacts["secondary"] = [contact("wxid-other", "我 的称呼")]
        label, reader, selected = self.resolve("我 的称呼")
        self.assertEqual((label, selected["nativeId"]), ("primary", "wxid-primary"))
        self.assertEqual(selected["resolution"], {"matchKind": "alias", "query": "我 的称呼", "aliasName": "我（的称呼）"})
        self.assertEqual(wechat_cli._safe_contact(selected, label)["resolution"], selected["resolution"])
        reader.close()

    def test_private_default_group_filter_and_account_filter(self):
        self.contacts["primary"].append(contact("one@chatroom", "讨论群"))
        self.write(entry(), entry(native_id="one@chatroom"))
        for options, expected in (({}, "wxid-primary"), ({"chat_type": "private"}, "wxid-primary"), ({"chat_type": "group"}, "one@chatroom")):
            with self.subTest(options=options):
                _, reader, selected = self.resolve(**options)
                self.assertEqual(selected["nativeId"], expected)
                reader.close()
        self.write(entry(), entry(native_id="wxid-secondary", account="secondary"))
        label, reader, _ = self.resolve(account="secondary")
        self.assertEqual(label, "secondary")
        reader.close()
        code, result = self.main("context", "--contact", "我的称呼")
        self.assertEqual((code, result["status"]), (2, "needs_choice"))
        self.assertTrue(all(item["matchKind"] == "alias" for item in result["candidates"]))

    def test_multiple_groups_need_choice_even_with_group_filter(self):
        self.contacts["primary"] = [contact("one@chatroom", "群甲"), contact("two@chatroom", "群乙")]
        self.write(entry(native_id="one@chatroom"), entry(native_id="two@chatroom"))
        for extra in ((), ("--chat-type", "group")):
            code, result = self.main("context", "--contact", "我的称呼", *extra)
            self.assertEqual((code, result["status"]), (2, "needs_choice"))
            self.assertEqual({item["nativeId"] for item in result["candidates"]}, {"one@chatroom", "two@chatroom"})
            self.assertTrue(all(item["matchKind"] == "alias" for item in result["candidates"]))

    def test_stale_alias_survives_success_not_found_and_ambiguity(self):
        stale = entry(native_id="wxid-absent")
        self.write(stale)
        expected = [{key: stale[key] for key in ("name", "account", "label")}]
        self.contacts["primary"] = [contact("wxid-primary", "我的称呼")]
        code, result = self.main("context", "--account", "primary", "--contact", "我的称呼")
        self.assertEqual(code, 0)
        self.assertEqual(result["contact"]["resolution"]["staleAliases"], expected)
        self.contacts["primary"] = []
        code, result = self.main("context", "--account", "primary", "--contact", "我的称呼")
        self.assertEqual((code, result["error"]), (2, "contact_not_found"))
        self.assertEqual(result["staleAliases"], expected)
        self.contacts["primary"] = [contact("one", "我的称呼"), contact("two", "我的称呼")]
        code, result = self.main("context", "--account", "primary", "--contact", "我的称呼")
        self.assertEqual((code, result["status"]), (2, "needs_choice"))
        self.assertEqual(result["staleAliases"], expected)

    def test_valid_and_stale_alias_preserve_resolution_details(self):
        self.write(entry(), entry(native_id="missing"))
        _, reader, selected = self.resolve()
        self.assertEqual(selected["resolution"]["matchKind"], "alias")
        self.assertEqual(selected["resolution"]["staleAliases"][0]["name"], "我的称呼")
        reader.close()

    def test_stale_alias_continues_to_loose_matching(self):
        self.write(entry(native_id="missing"))
        self.contacts["primary"] = [contact("wxid-primary", "我的（称呼）★")]
        code, result = self.main("context", "--account", "primary", "--contact", "我的称呼")
        self.assertEqual(code, 0, result)
        resolution = result["contact"]["resolution"]
        self.assertEqual(resolution["matchKind"], "loose")
        self.assertEqual(resolution["staleAliases"][0]["name"], "我的称呼")

    def test_contact_sync_alias_first_incremental_and_wrong_binding(self):
        self.write(entry())
        destination = self.root / "archive"
        argv = ("sync-contact", "--account", "primary", "--contact", "我的称呼", "--output", str(destination))
        with patch("wechat_cli._decode_voice_file", side_effect=lambda _source, output: output.write_bytes(b"wav-bytes")):
            code, result = self.main(*argv)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["contact"]["resolution"]["matchKind"], "alias")
        self.assertEqual(json.loads((destination / "state.json").read_text(encoding="utf-8"))["contactNativeId"], "wxid-primary")
        code, result = self.main(*argv)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["noChange"])
        self.contacts["primary"].append(contact("wxid-other", "别人"))
        self.write(entry(native_id="wxid-other"))
        code, result = self.main(*argv)
        self.assertEqual((code, result["error"]), (2, "sync_identity_mismatch"))

    def test_existing_named_archive_can_increment_with_alias(self):
        destination = self.root / "named-archive"
        argv = ("sync-contact", "--account", "primary", "--output", str(destination))
        with patch("wechat_cli._decode_voice_file", side_effect=lambda _source, output: output.write_bytes(b"wav-bytes")):
            code, result = self.main(*argv, "--contact", "联系人primary")
        self.assertEqual(code, 0, result)
        self.write(entry())
        code, result = self.main(*argv, "--contact", "我的称呼")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["contact"]["resolution"]["matchKind"], "alias")
        self.assertTrue(result["noChange"])

    def test_export_context_and_preserve_resolve_alias(self):
        self.write(entry())
        for command, filename in (("export-context", "conversation.json"), ("preserve", "messages.json")):
            with self.subTest(command=command):
                destination = self.root / command
                with patch("wechat_cli._decode_voice_file", side_effect=lambda _source, output: output.write_bytes(b"wav-bytes")):
                    code, result = self.main(command, "--account", "primary", "--contact", "我的称呼", "--output", str(destination))
                self.assertEqual(code, 0, result)
                self.assertTrue(result["packageCreated"])
                payload = json.loads((destination / filename).read_text(encoding="utf-8"))
                self.assertEqual(payload["contact"]["resolution"]["matchKind"], "alias")
                self.assertEqual(payload["contact"]["nativeId"], "wxid-primary")

    def test_moments_alias_private_priority_ambiguity_and_stale_fallback(self):
        self.contacts["primary"].append(contact("wxid-other", "我的称呼"))
        self.write(entry(), entry(native_id="one@chatroom"))
        code, result = self.main("moments", "--account", "primary", "--contact", "我的称呼", "--limit", "5")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["contact"]["nativeId"], "wxid-primary")
        self.write(entry(), entry(native_id="wxid-other"))
        code, result = self.main("moments", "--account", "primary", "--contact", "我的称呼", "--limit", "5")
        self.assertEqual((code, result["status"]), (2, "needs_choice"))
        self.write(entry("联系人primary", native_id="absent"))
        _, reader, selected = wechat_cli._resolve_moments_subject(self.config, account="primary", query="联系人primary", self_requested=False, cutoff_s=200, aliases_path=self.path)
        self.assertEqual(selected["nativeId"], "wxid-primary")
        reader.close()

    def test_list_current_stale_and_filters_without_messages(self):
        self.write(entry(), entry(native_id="gone"), entry("别的称呼", "wxid-secondary", "secondary"))
        code, result = self.main("alias", "list", "--name", "我 的称呼", "--account", "primary")
        self.assertEqual(code, 0)
        self.assertEqual(len(result["aliases"]), 2)
        by_id = {item["nativeId"]: item for item in result["aliases"]}
        self.assertEqual((by_id["wxid-primary"]["current"], by_id["wxid-primary"]["stale"]), ("联系人primary", False))
        self.assertEqual((by_id["gone"]["current"], by_id["gone"]["stale"]), (None, True))
        self.assertTrue(all(not reader.fetch_calls and reader.closed for reader in self.created))

    def test_remove_respects_name_account_native_id_and_reports_no_match(self):
        self.write(entry(), entry(native_id="one@chatroom"), entry(native_id="wxid-secondary", account="secondary"))
        code, result = self.main("alias", "remove", "--name", "我 的称呼", "--account", "primary", "--native-id", "wxid-primary")
        self.assertEqual((code, result["removed"]), (0, 1))
        code, result = self.main("alias", "remove", "--name", "我的称呼", "--account", "primary")
        self.assertEqual((code, result["removed"]), (0, 1))
        before = self.path.read_bytes()
        code, result = self.main("alias", "remove", "--name", "我的称呼", "--account", "primary")
        self.assertEqual((code, result["error"]), (2, "alias_not_found"))
        self.assertEqual(self.path.read_bytes(), before)
        code, result = self.main("alias", "remove", "--name", "我的称呼")
        self.assertEqual((code, result["removed"]), (0, 1))

    def test_corrupt_file_never_overwritten_by_any_command(self):
        for invalid in (b"broken", b'{"schema":"wrong","aliases":[]}', b'{"schema":"wechat-direct.aliases.v1","aliases":[{}]}', b"\xff"):
            self.path.write_bytes(invalid)
            for argv in (("alias", "add", "--name", "称呼", "--contact", "wxid-primary"), ("alias", "list"), ("alias", "remove", "--name", "称呼"), ("context", "--contact", "联系人primary")):
                with self.subTest(invalid=invalid, argv=argv):
                    code, result = self.main(*argv)
                    self.assertEqual((code, result["error"]), (2, "aliases_file_invalid"))
                    self.assertEqual(self.path.read_bytes(), invalid)

    def test_atomic_write_failure_preserves_old_file_and_cleans_own_temporary(self):
        self.write(entry("旧称呼"))
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("simulated replace failure")):
            code, _ = self.main("alias", "add", "--name", "新称呼", "--contact", "wxid-primary")
        self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name("aliases.json.incomplete").exists())

    def test_successful_write_replaces_complete_sorted_document(self):
        self.write(entry("乙", "wxid-secondary", "secondary"), entry("甲", "z"), entry("甲", "a"))
        original_replace = Path.replace
        observed = []

        def replacing(source, target):
            self.assertNotEqual(source, target)
            observed.append(json.loads(source.read_text(encoding="utf-8")))
            return original_replace(source, target)

        with patch.object(Path, "replace", new=replacing):
            code, result = self.main("alias", "add", "--name", "甲", "--contact", "wxid-primary")
        self.assertEqual(code, 0, result)
        stored = observed[0]["aliases"]
        self.assertEqual([(item["name"], item["account"], item["nativeId"]) for item in stored], sorted((item["name"], item["account"], item["nativeId"]) for item in stored))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), observed[0])

    def test_path_uses_settings_override_or_effective_config_directory(self):
        custom_config = self.root / "custom" / "accounts.json"
        self.assertEqual(wechat_cli._aliases_path(custom_config), self.path)
        self.settings.unlink()
        self.assertEqual(wechat_cli._aliases_path(custom_config), custom_config.parent / "aliases.json")
        with patch.dict("os.environ", {"WECHAT_DIRECT_CONFIG": str(custom_config)}):
            self.assertEqual(wechat_cli._aliases_path(), custom_config.parent / "aliases.json")

    def test_cli_uses_alias_table_beside_explicit_config(self):
        self.settings.unlink()
        config = self.root / "selected" / "accounts.json"
        code, result = self.main("alias", "add", "--config", str(config), "--name", "指定配置称呼", "--contact", "wxid-primary")
        self.assertEqual(code, 0, result)
        self.assertTrue((config.parent / "aliases.json").is_file())
        self.assertFalse(self.path.exists())
        code, result = self.main("context", "--config", str(config), "--contact", "指定配置称呼")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["contact"]["resolution"]["matchKind"], "alias")


if __name__ == "__main__":
    unittest.main()

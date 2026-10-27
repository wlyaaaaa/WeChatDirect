from __future__ import annotations

import isolation  # noqa: F401

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wechat_cli
from test_wechat_cli import BinaryOutput, FakeReader


def contact(native_id, name, timestamp=200, **labels):
    return {
        "nativeId": native_id,
        "displayName": name,
        "remark": name,
        "nickname": None,
        "alias": None,
        "sessionType": "group" if native_id.endswith("@chatroom") else "contact",
        "lastTimestamp": timestamp,
        **labels,
    }


class DirectoryReader(FakeReader):
    def __init__(self, label, contacts, members):
        super().__init__(label)
        self.contacts = contacts
        self.members = members
        self.member_calls = 0

    def list_contacts(self, *, include_unregistered=False):
        return self.contacts

    def list_group_member_contact_labels(self):
        self.member_calls += 1
        return self.members


class ContactResolutionTests(unittest.TestCase):
    def setUp(self):
        self.config = {label: {"config_path": label} for label in ("primary", "secondary")}
        self.contacts = {label: [] for label in self.config}
        self.members = {label: {} for label in self.config}
        self.created = []

    def factory(self, account, cutoff_s):
        label = account["config_path"]
        reader = DirectoryReader(label, self.contacts[label], self.members[label])
        self.created.append(reader)
        return reader

    def resolve(self, query, **options):
        options = {"account": "auto", "allow_partial": True, **options}
        with patch("wechat_cli._reader", side_effect=self.factory):
            return wechat_cli._resolve_contact(self.config, query=query, cutoff_s=200, **options)

    def main(self, *argv):
        output = BinaryOutput()
        with (
            patch("wechat_cli._read_config", return_value=self.config),
            patch("wechat_cli._reader", side_effect=self.factory),
            patch("wechat_cli.time.time", return_value=200),
            patch("wechat_cli.sys.stdout", output),
        ):
            code = wechat_cli.main(list(argv))
        return code, json.loads(output.buffer.getvalue())

    def test_global_exact_precedes_partial_and_preserves_safe_output(self):
        self.contacts["primary"] = [contact("early@chatroom", "项目讨论（小李）")]
        original = contact("wxid-secondary", "小李")
        self.contacts["secondary"] = [original]
        label, reader, selected = self.resolve("小李")
        self.assertEqual(label, "secondary")
        self.assertEqual(selected["resolution"], {"matchKind": "exact", "query": "小李"})
        self.assertEqual(wechat_cli._safe_contact(selected, label), {
            "account": "secondary", "nativeId": "wxid-secondary", "displayName": "小李",
            "remark": "小李", "nickname": None, "alias": None,
            "sessionType": "contact", "lastTimestamp": 200,
        })
        self.assertNotIn("resolution", original)
        self.assertEqual([item.member_calls for item in self.created], [0, 0])
        self.assertTrue(self.created[0].closed)
        reader.close()

    def test_loose_equal_removes_brackets_spaces_symbols_and_normalizes_width(self):
        self.contacts["primary"] = [contact("one@chatroom", "项目（小李）★")]
        for query in ("项 目小李", "项目(小李)"):
            with self.subTest(query=query):
                _, reader, selected = self.resolve(query, allow_partial=False)
                self.assertEqual(selected["resolution"], {"matchKind": "loose", "query": query})
                self.assertEqual(wechat_cli._safe_contact(selected, "primary")["resolution"], selected["resolution"])
                reader.close()
        self.contacts["primary"][0]["remark"] = "项目（小李）"
        _, reader, selected = self.resolve("项目(小李)")
        self.assertEqual(selected["resolution"]["matchKind"], "exact")
        reader.close()

    def test_partial_name_unique_has_resolution_in_context(self):
        self.contacts["primary"] = [contact("wxid-primary", "测试联系人")]
        code, result = self.main("context", "--account", "primary", "--contact", "测试")
        self.assertEqual(code, 0)
        self.assertEqual(result["contact"]["resolution"], {"matchKind": "partial_name", "query": "测试"})
        self.assertTrue(self.created[0].fetch_calls)

    def test_exact_context_matches_previous_contact_shape(self):
        item = contact("wxid-primary", "测试联系人")
        self.contacts["primary"] = [item]
        code, result = self.main("context", "--account", "primary", "--contact", "测试联系人")
        self.assertEqual(code, 0)
        self.assertEqual(result["contact"], wechat_cli._safe_contact(item, "primary"))
        self.assertNotIn("resolution", result["contact"])

    def test_partial_ambiguity_sorted_limited_and_counted(self):
        self.contacts["primary"] = [contact(f"{index}@chatroom", f"项目{index}", index) for index in range(23)]
        code, result = self.main("context", "--contact", "项目")
        self.assertEqual((code, result["status"], result["error"]), (2, "needs_choice", "contact_ambiguous"))
        self.assertEqual(result["candidateTotal"], 23)
        self.assertEqual(len(result["candidates"]), 20)
        self.assertEqual([item["lastTimestamp"] for item in result["candidates"]], list(range(22, 2, -1)))
        self.assertTrue(all(item["matchKind"] == "partial_name" for item in result["candidates"]))
        self.assertTrue(all("resolution" not in item for item in result["candidates"]))
        self.assertTrue(all(reader.closed for reader in self.created))

    def test_chat_type_group_skips_exact_private_and_reaches_member_layer(self):
        self.contacts["primary"] = [contact("wxid-li", "小李"), contact("one@chatroom", "项目讨论")]
        self.members["primary"] = {
            "one@chatroom": [{"remark": "李经理", "alias": "小李", "displayName": "李经理"}, {"nickname": "老王"}],
            "orphan@chatroom": [{"nickname": "小李"}],
        }
        _, reader, selected = self.resolve("小李", chat_type="group")
        self.assertEqual(selected["nativeId"], "one@chatroom")
        self.assertEqual(selected["resolution"], {"matchKind": "group_member", "query": "小李", "matchedMembers": ["李经理"]})
        self.assertEqual(reader.member_calls, 1)
        self.assertFalse(reader.fetch_calls)
        reader.close()

    def test_member_ambiguity_has_names_counts_and_global_time_order(self):
        self.contacts["primary"] = [contact("one@chatroom", "甲群", 100)]
        self.contacts["secondary"] = [contact("two@chatroom", "乙群", 300)]
        self.members["primary"] = {"one@chatroom": [{"nickname": "小李同学"}, {"remark": "老王"}]}
        self.members["secondary"] = {"two@chatroom": [{"remark": "小李"}]}
        _, result = self.main("context", "--contact", "小李", "--chat-type", "group")
        candidates = result["candidates"]
        self.assertEqual([item["account"] for item in candidates], ["secondary", "primary"])
        self.assertEqual([item["memberCount"] for item in candidates], [1, 2])
        self.assertEqual([item["matchedMembers"] for item in candidates], [["小李"], ["小李同学"]])
        self.assertTrue(all(item["matchKind"] == "group_member" for item in candidates))

    def test_private_filter_excludes_groups_and_official_accounts_keeps_filehelper(self):
        self.contacts["primary"] = [contact("one@chatroom", "同名"), contact("gh_official", "同名"), contact("wxid-one", "同名"), contact("filehelper", "")]
        _, reader, selected = self.resolve("同名", chat_type="private")
        self.assertEqual(selected["nativeId"], "wxid-one")
        reader.close()
        _, reader, selected = self.resolve("文件传输助手", chat_type="private")
        self.assertEqual(selected["nativeId"], "filehelper")
        reader.close()
        with self.assertRaisesRegex(wechat_cli.ProductError, "contact_not_found"):
            self.resolve("不存在", chat_type="private")
        self.assertTrue(all(item.member_calls == 0 for item in self.created))

    def test_one_character_never_uses_partial_name_or_members(self):
        self.contacts["primary"] = [contact("one@chatroom", "小李工作群")]
        self.members["primary"] = {"one@chatroom": [{"remark": "小"}]}
        with self.assertRaisesRegex(wechat_cli.ProductError, "contact_not_found"):
            self.resolve("小")
        self.assertTrue(all(item.member_calls == 0 for item in self.created))

    def test_not_found_suggestions_filtered_sorted_and_capped_without_messages(self):
        self.contacts["primary"] = [contact(f"{index}@chatroom", f"项目讨论{index}") for index in range(7)]
        self.contacts["primary"] += [contact("wxid-ignored", "项目讨伦"), contact("far@chatroom", "完全无关")]
        code, result = self.main("context", "--contact", "项目讨伦", "--chat-type", "group")
        self.assertEqual((code, result["error"]), (2, "contact_not_found"))
        suggestions = result["suggestions"]
        self.assertEqual(len(suggestions), 5)
        self.assertTrue(all(item["similarity"] >= 0.5 and item["nativeId"].endswith("@chatroom") for item in suggestions))
        self.assertEqual([item["similarity"] for item in suggestions], sorted((item["similarity"] for item in suggestions), reverse=True))
        self.assertTrue(all(not reader.fetch_calls for reader in self.created))

    def test_sync_contact_never_uses_partial_matching(self):
        self.contacts["primary"] = [contact("one@chatroom", "项目（小李）")]
        with tempfile.TemporaryDirectory() as temporary:
            code, result = self.main("sync-contact", "--contact", "小李", "--chat-type", "group", "--output", str(Path(temporary) / "archive"))
        self.assertEqual((code, result["error"]), (2, "contact_not_found"))
        self.assertTrue(all(item.member_calls == 0 and not item.fetch_calls for item in self.created))

    def test_cli_chat_type_shared_by_readers_and_sync_but_not_moments(self):
        for command in ("context", "export-context", "preserve", "sync-contact"):
            with self.subTest(command=command):
                argv = [command, "--contact", "项目", "--chat-type", "group"]
                if command in ("export-context", "preserve"):
                    argv += ["--output", "unused"]
                self.assertEqual(wechat_cli.parser().parse_args(argv).chat_type, "group")
        code, result = self.main("moments", "--account", "primary", "--chat-type", "group")
        self.assertEqual((code, result["error"]), (2, "cli_arguments_invalid"))


if __name__ == "__main__":
    unittest.main()

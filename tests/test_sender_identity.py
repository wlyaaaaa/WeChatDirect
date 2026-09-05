from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import wechat_cli
from wechat_source import DirectWeChatReader


SELF = "wxid-synthetic-primary"
OTHER = "wxid-synthetic-secondary"
CONTACT = "wxid-synthetic-contact"
LARGE_ID = 9007199254740993


def commitment(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()


class SenderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = self.root / "db_storage"
        self.storage.mkdir()
        self.table = "Msg_" + hashlib.md5(CONTACT.encode()).hexdigest()
        self.sources = []
        with patch(
            "wechat_source.load_direct_source_identity",
            return_value=(self.root, "0" * 64, SELF + "_ab12"),
        ):
            self.reader = DirectWeChatReader(
                config_path=self.root / "unused-config",
                local_state_path=self.root / "unused-state",
                snapshot_cutoff_s=1000,
                expected_self_username_sha256=commitment(SELF),
            )
        self.reader._open = self.open_source
        self.reader._message_sources_for_table = lambda _table: tuple(self.sources)
        self.reader._media_entries = lambda *_args, **_kwargs: []
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.reader.close)

    def open_source(self, source):
        if source not in self.reader._connections:
            connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            self.reader._connections[source] = connection
        return self.reader._connections[source]

    def add_shard(self, names, rows, *, dictionary=True):
        source = self.storage / f"message_{len(self.sources)}.db"
        connection = sqlite3.connect(source)
        if dictionary:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO Name2Id(rowid, user_name) VALUES(?, ?)", names.items()
            )
        connection.executescript(
            f"CREATE TABLE {self.table}(local_id INTEGER, local_type INTEGER, "
            "server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, "
            "message_content TEXT, source TEXT, packed_info_data TEXT, "
            "compress_content TEXT, sort_seq INTEGER, status INTEGER, "
            "origin_source TEXT);"
            f"CREATE INDEX idx_time ON {self.table}(create_time);"
            f"CREATE INDEX idx_server ON {self.table}(server_id);"
            f"CREATE INDEX idx_sort ON {self.table}(sort_seq);"
        )
        for index, (server_id, sender_id, status, kind) in enumerate(rows, 1):
            connection.execute(
                f"INSERT INTO {self.table} VALUES(?, ?, ?, ?, ?, 'synthetic', "
                "'', '', '', ?, ?, '')",
                (index, kind, server_id, sender_id, index * 10, index, status),
            )
        connection.commit()
        connection.close()
        self.sources.append(source)
        return source

    def messages(self, limit=None):
        return self.reader.fetch_messages(
            CONTACT, since_s=0, end_s=1000, limit=limit,
        )["messages"]

    def test_same_directory_shards_and_resource_have_independent_sender_ids(self):
        self.add_shard({2: SELF, 7: OTHER}, [(LARGE_ID, 2, 0, 1)])
        self.add_shard({2: OTHER, 7: SELF}, [(LARGE_ID + 1, 2, 0, 1)])
        resource = sqlite3.connect(self.storage / "message_resource.db")
        resource.executescript(
            "CREATE TABLE SenderName2Id(user_name TEXT);"
            "INSERT INTO SenderName2Id(rowid, user_name) VALUES(2, 'wxid-decoy');"
        )
        resource.close()
        for limit in (None, 10):
            with self.subTest(limit=limit):
                messages = {message["serverId"]: message for message in self.messages(limit)}
                self.assertEqual(messages[str(LARGE_ID)]["senderRole"], "self")
                self.assertEqual(messages[str(LARGE_ID)]["senderUsername"], SELF)
                self.assertEqual(messages[str(LARGE_ID + 1)]["senderRole"], "other")
                self.assertEqual(messages[str(LARGE_ID + 1)]["senderUsername"], OTHER)
        self.assertNotIn(self.storage / "message_resource.db", self.reader._connections)

    def test_missing_message_dictionary_never_borrows_resource_sender(self):
        self.add_shard({}, [(LARGE_ID, 2, 0, 1)], dictionary=False)
        resource = sqlite3.connect(self.storage / "message_resource.db")
        resource.execute("CREATE TABLE SenderName2Id(user_name TEXT)")
        resource.execute("INSERT INTO SenderName2Id(rowid, user_name) VALUES(2, ?)", (SELF,))
        resource.commit()
        resource.close()
        message = self.messages()[0]
        self.assertEqual(message["senderRole"], "unknown")
        self.assertNotIn("senderUsername", message)

    def test_primary_secondary_commitments_do_not_share_self_attribution(self):
        self.add_shard({2: SELF, 7: OTHER}, [(1, 2, 0, 1), (2, 7, 0, 1)])
        for username, roles in ((SELF, ["self", "other"]), (OTHER, ["other", "self"])):
            with self.subTest(username=username):
                self.reader._expected_self_username_sha256 = commitment(username)
                messages = self.messages()
                self.assertEqual([row["senderRole"] for row in messages], roles)
                self.assertEqual([row["senderUsername"] for row in messages], [SELF, OTHER])

    def test_username_match_is_exact_and_never_strips_storage_suffix(self):
        variants = [SELF, SELF + "_ab12", SELF + " ", SELF.upper()]
        self.add_shard(dict(enumerate(variants, 1)), [(i, i, 0, 1) for i in range(1, 5)])
        messages = self.messages()
        self.assertEqual([row["senderRole"] for row in messages], ["self", "other", "other", "other"])
        self.assertEqual([row["senderUsername"] for row in messages], variants)
        self.assertEqual(self.reader._identity, SELF + "_ab12")
        self.assertEqual(self.reader.account_identity_commitment, commitment(SELF + "_ab12")[7:])

    def test_missing_username_commitment_never_uses_storage_identity(self):
        self.add_shard({2: SELF + "_ab12", 7: OTHER}, [(1, 2, 0, 1), (2, 7, 0, 1)])
        self.reader._expected_self_username_sha256 = None
        self.assertEqual([row["senderRole"] for row in self.messages()], ["unknown", "unknown"])

    def test_status_fallback_system_and_native_directions_remain_explicit(self):
        self.add_shard(
            {2: SELF, 7: OTHER},
            [(10, 2, 0, 1), (11, 7, 3, 1), (12, 2, 5, 1),
             (13, 2, 2, 1), (14, 7, 4, 1), (15, 2, 0, 10000),
             (16, 999, 9, 1), (17, 0, 9, 1), (18, -1, 9, 1)],
        )
        messages = self.messages()
        self.assertEqual(
            [row["senderRole"] for row in messages],
            ["self", "other", "self", "self", "other", "system", "unknown", "unknown", "unknown"],
        )
        self.assertEqual([row["direction"] for row in messages[:5]],
                         ["outgoing", "incoming", "outgoing", "outgoing", "incoming"])

    def test_status_conflict_does_not_fabricate_a_native_sender_name(self):
        self.add_shard({2: SELF, 7: OTHER}, [(1, 7, 2, 1), (2, 2, 4, 1), (3, 7, 3, 1)])
        messages = self.messages()
        self.assertEqual([row["senderRole"] for row in messages], ["self", "other", "self"])
        for message in messages:
            self.assertNotIn("senderUsername", message)
            self.assertEqual(message["senderGap"], "sender_mapping_unresolved")

    def test_group_context_and_full_archive_use_the_same_native_mapping(self):
        group = "synthetic@chatroom"
        self.table = "Msg_" + hashlib.md5(group.encode()).hexdigest()
        self.add_shard({2: SELF, 7: OTHER}, [(1, 2, 0, 1), (2, 7, 0, 1)])
        for limit in (None, 10):
            messages = self.reader.fetch_messages(group, since_s=0, end_s=1000, limit=limit)["messages"]
            self.assertEqual([row["senderRole"] for row in messages], ["self", "other"])
            self.assertEqual([row["senderUsername"] for row in messages], [SELF, OTHER])

    def test_public_ids_are_lossless_strings_through_json(self):
        ids = [LARGE_ID, 9223372036854775807]
        self.add_shard({2: SELF}, [(number, 2, 0, 1) for number in ids])
        for raw, message in zip(ids, self.messages()):
            receipt = wechat_cli._message_receipt(message, account="primary")
            decoded = json.loads(wechat_cli._canonical_bytes(receipt))
            self.assertEqual(message["serverId"], str(raw))
            self.assertEqual(decoded["serverId"], str(raw))
            self.assertEqual(decoded["nativeId"], {"kind": "server", "value": str(raw)})
            self.assertEqual(decoded["sender"]["nativeId"], SELF)

    def test_local_message_keys_are_shard_bound_and_not_sender_bound(self):
        self.add_shard({2: SELF}, [(0, 2, 0, 1)])
        self.add_shard({2: OTHER}, [(0, 2, 0, 1)])
        messages = [wechat_cli._message_receipt(row) for row in self.messages()]
        keys = [wechat_cli._message_export_key(row) for row in messages]
        self.assertEqual(len(set(keys)), 2)
        messages[0]["senderUsername"] = "corrected-name"
        self.assertEqual(wechat_cli._message_export_key(messages[0]), keys[0])

    def test_ambiguous_legacy_local_migration_fails_without_guessing(self):
        common = {"nativeId": {"kind": "local", "value": "1"}, "createTime": 100, "sortSeq": 2}
        old = [dict(common, senderUsername=name) for name in (SELF, OTHER)]
        merged = {wechat_cli._message_export_key(row): row for row in old}
        before = dict(merged)
        candidate = dict(common, senderUsername=SELF, shardLocalIdentity="sha256:" + "1" * 64)
        with self.assertRaisesRegex(wechat_cli.ProductError, "sync_local_message_identity_ambiguous"):
            wechat_cli._remap_legacy_local_sender_keys(merged, [candidate])
        self.assertEqual(merged, before)


if __name__ == "__main__":
    unittest.main()

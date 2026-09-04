from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import wechat_source
from wechat_source import DirectCredentialError, DirectSchemaError, DirectWeChatReader


class SourceReadReliabilityTests(unittest.TestCase):
    @staticmethod
    def _reader_for_connections(
        databases: dict[str, tuple[Path, sqlite3.Connection]],
    ) -> DirectWeChatReader:
        reader = object.__new__(DirectWeChatReader)
        reader._named_databases = lambda name: (
            [databases[name][0]] if name in databases else []
        )
        reader._open = lambda source: next(
            connection
            for path, connection in databases.values()
            if path == source
        )
        return reader

    @staticmethod
    def _close_connections(
        databases: dict[str, tuple[Path, sqlite3.Connection]],
    ) -> None:
        for _path, connection in databases.values():
            connection.close()

    def test_account_root_accepts_exact_or_four_hex_storage_suffix_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            for directory_name in ("wxid-synthetic", "wxid-synthetic_AB12"):
                with self.subTest(directory_name=directory_name):
                    case_root = Path(temporary) / directory_name
                    parent = case_root / "db"
                    (parent / directory_name / "db_storage").mkdir(parents=True)
                    config_path = case_root / "config.json"
                    config_path.write_text(
                        json.dumps(
                            {
                                "dbPath": str(parent),
                                "decryptKey": "0" * 64,
                                "myWxid": "wxid-synthetic",
                            }
                        ),
                        encoding="utf-8",
                    )
                    with (
                        patch.object(
                            wechat_source, "_safe_storage_key", return_value=b"unused"
                        ),
                        patch.object(
                            wechat_source,
                            "_decode_safe_value",
                            side_effect=lambda value, _key: value,
                        ),
                    ):
                        account_root, _master_hex, identity = (
                            wechat_source.load_direct_source_identity(
                                config_path, Path(temporary) / "local-state.json"
                            )
                        )
                    self.assertEqual(account_root.name, directory_name)
                    self.assertEqual(identity, "wxid-synthetic")

            backup_root = Path(temporary) / "backup-case"
            parent = backup_root / "db"
            (parent / "wxid-synthetic_backup" / "db_storage").mkdir(parents=True)
            config_path = backup_root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "dbPath": str(parent),
                        "decryptKey": "0" * 64,
                        "myWxid": "wxid-synthetic",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(wechat_source, "_safe_storage_key", return_value=b"unused"),
                patch.object(
                    wechat_source,
                    "_decode_safe_value",
                    side_effect=lambda value, _key: value,
                ),
            ):
                with self.assertRaisesRegex(
                    DirectCredentialError, "local source account directory is ambiguous"
                ):
                    wechat_source.load_direct_source_identity(
                        config_path, Path(temporary) / "local-state.json"
                    )

    def test_missing_session_schema_is_an_explicit_error(self):
        session = sqlite3.connect(":memory:")
        session.row_factory = sqlite3.Row
        session.execute("CREATE TABLE unrelated(value TEXT)")
        contact = sqlite3.connect(":memory:")
        contact.row_factory = sqlite3.Row
        contact.execute(
            "CREATE TABLE contact(username TEXT, alias TEXT, remark TEXT, nick_name TEXT)"
        )
        contact.execute(
            "INSERT INTO contact VALUES('wxid-synthetic', 'alias', '备注', '昵称')"
        )
        databases = {
            "session.db": (Path("session.db"), session),
            "contact.db": (Path("contact.db"), contact),
        }
        reader = self._reader_for_connections(databases)
        try:
            with self.assertRaisesRegex(
                DirectSchemaError, "^session_database_unavailable$"
            ):
                reader.list_sessions()
            with self.assertRaisesRegex(
                DirectSchemaError, "^session_database_unavailable$"
            ):
                reader.list_contacts()
        finally:
            self._close_connections(databases)

    def test_optional_contact_labels_are_preserved_with_a_structured_gap(self):
        session = sqlite3.connect(":memory:")
        session.row_factory = sqlite3.Row
        session.execute(
            "CREATE TABLE SessionTable(username TEXT, type INTEGER, "
            "last_timestamp INTEGER, sort_timestamp INTEGER, is_hidden INTEGER)"
        )
        session.execute(
            "INSERT INTO SessionTable VALUES('wxid-synthetic', 1, 100, 100, 0)"
        )
        contact = sqlite3.connect(":memory:")
        contact.row_factory = sqlite3.Row
        contact.execute("CREATE TABLE contact(username TEXT, nick_name TEXT)")
        contact.execute("INSERT INTO contact VALUES('wxid-synthetic', '昵称')")
        databases = {
            "session.db": (Path("session.db"), session),
            "contact.db": (Path("contact.db"), contact),
        }
        reader = self._reader_for_connections(databases)
        try:
            result = reader.list_contacts()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["displayName"], "昵称")
            self.assertIsNone(result[0]["remark"])
            self.assertIsNone(result[0]["alias"])
            self.assertEqual(result[0]["labelGap"], "contact_label_fields_unavailable")
        finally:
            self._close_connections(databases)

    def test_unregistered_contacts_keep_labels_when_session_schema_is_missing(self):
        session = sqlite3.connect(":memory:")
        session.row_factory = sqlite3.Row
        session.execute("CREATE TABLE unrelated(value TEXT)")
        contact = sqlite3.connect(":memory:")
        contact.row_factory = sqlite3.Row
        contact.execute(
            "CREATE TABLE contact(username TEXT, alias TEXT, remark TEXT, nick_name TEXT)"
        )
        contact.execute(
            "INSERT INTO contact VALUES('wxid-synthetic', 'alias', '备注', '昵称')"
        )
        databases = {
            "session.db": (Path("session.db"), session),
            "contact.db": (Path("contact.db"), contact),
        }
        reader = self._reader_for_connections(databases)
        try:
            result = reader.list_contacts(include_unregistered=True)
            self.assertEqual(result[0]["displayName"], "备注")
            self.assertEqual(result[0]["sessionGap"], "session_database_unavailable")
        finally:
            self._close_connections(databases)

    def test_moments_report_author_cache_bounds_when_window_has_no_rows(self):
        sns = sqlite3.connect(":memory:")
        sns.row_factory = sqlite3.Row
        sns.execute("CREATE TABLE SnsTimeLine(tid INTEGER, user_name TEXT, content TEXT)")

        def moment_xml(native_id: str, username: str, create_time: int) -> str:
            return (
                "<root><TimelineObject>"
                f"<id>{native_id}</id><username>{username}</username>"
                f"<createTime>{create_time}</createTime>"
                "<contentDesc>合成内容</contentDesc>"
                "<ContentObject><type>1</type><mediaList /></ContentObject>"
                "</TimelineObject></root>"
            )

        sns.executemany(
            "INSERT INTO SnsTimeLine VALUES(?, ?, ?)",
            (
                (1, "wxid-target", moment_xml("target-old", "wxid-target", 100)),
                (2, "wxid-other", moment_xml("other", "wxid-other", 200)),
                (3, "wxid-target", moment_xml("target-new", "wxid-target", 300)),
            ),
        )
        sns.commit()
        databases = {"sns.db": (Path("sns.db"), sns)}
        reader = self._reader_for_connections(databases)
        try:
            result = reader.list_moments(
                since_s=150,
                end_s=250,
                username="wxid-target",
                limit=20,
            )
            self.assertEqual(result["moments"], [])
            self.assertEqual(result["targetCachedRows"], 2)
            self.assertEqual(result["targetLatestTimeS"], 300)
            self.assertEqual(result["targetEarliestTimeS"], 100)

            missing = reader.list_moments(
                since_s=150,
                end_s=250,
                username="wxid-missing",
                limit=20,
            )
            self.assertEqual(missing["targetCachedRows"], 0)
            self.assertIsNone(missing["targetLatestTimeS"])
            self.assertIsNone(missing["targetEarliestTimeS"])
        finally:
            self._close_connections(databases)


if __name__ == "__main__":
    unittest.main()

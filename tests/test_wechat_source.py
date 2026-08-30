from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wechat_source import (
    DirectSchemaError,
    DirectWeChatReader,
    _TYPE_NAMES,
    _message_content_projection,
    _message_payload_texts,
    _readable_payload_text,
    _text_from_message,
)


class SourceFingerprintTests(unittest.TestCase):
    def test_contact_fingerprint_tracks_only_relevant_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            target_dir = storage / "message" / "target"
            other_dir = storage / "message" / "other"
            contact_dir = storage / "contact"
            for directory in (target_dir, other_dir, contact_dir):
                directory.mkdir(parents=True)
            target = target_dir / "message_0.db"
            resource = target_dir / "message_resource.db"
            media = target_dir / "media_0.db"
            other = other_dir / "message_1.db"
            contact = contact_dir / "contact.db"
            session = contact_dir / "session.db"
            for path in (target, resource, media, other, contact, session):
                path.write_bytes(path.name.encode())

            reader = object.__new__(DirectWeChatReader)
            reader._storage = storage
            reader._message_schema_probe_pages = {target: 3, other: 2}
            reader._message_sources_for_table = lambda _table: (target,)
            reader._message_database_sources = lambda: [target, other]
            reader._named_databases = lambda name: {
                "contact.db": [contact],
                "session.db": [session],
            }.get(name, [])

            initial = reader.contact_source_fingerprint("wxid-test")
            other.write_bytes(b"unrelated source changed")
            unrelated = reader.contact_source_fingerprint("wxid-test")
            self.assertEqual(initial["sha256"], unrelated["sha256"])

            session.write_bytes(b"unrelated session activity changed")
            unrelated_session = reader.contact_source_fingerprint("wxid-test")
            self.assertEqual(initial["sha256"], unrelated_session["sha256"])

            target.write_bytes(b"target source changed")
            changed = reader.contact_source_fingerprint("wxid-test")
            self.assertNotEqual(initial["sha256"], changed["sha256"])
            self.assertEqual(changed["messageSourceCount"], 1)

    def test_moments_fingerprint_tracks_cache_and_contact_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            sns = storage / "sns.db"
            contact = storage / "contact.db"
            sns.write_bytes(b"sns")
            contact.write_bytes(b"contact")

            reader = object.__new__(DirectWeChatReader)
            reader._storage = storage
            reader._named_databases = lambda name: {
                "sns.db": [sns],
                "contact.db": [contact],
            }.get(name, [])

            initial = reader.moments_source_fingerprint()
            sns.write_bytes(b"sns changed")
            changed = reader.moments_source_fingerprint()
            self.assertNotEqual(initial["sha256"], changed["sha256"])
            self.assertEqual(changed["snsSourceCount"], 1)


class ExactIdentityAndMediaTests(unittest.TestCase):
    @staticmethod
    def _plain_reader(storage: Path) -> DirectWeChatReader:
        reader = object.__new__(DirectWeChatReader)
        reader._storage = storage
        reader.account_identity_commitment = "a" * 64
        reader._connections = {}
        reader._sender_index_by_message_directory_cache = None

        def open_source(source: Path) -> sqlite3.Connection:
            cached = reader._connections.get(source)
            if cached is None:
                cached = sqlite3.connect(source)
                cached.row_factory = sqlite3.Row
                reader._connections[source] = cached
            return cached

        reader._open = open_source
        return reader

    @staticmethod
    def _close(reader: DirectWeChatReader) -> None:
        for connection in reader._connections.values():
            connection.close()

    def test_moments_self_identity_removes_only_storage_suffix(self):
        reader = object.__new__(DirectWeChatReader)
        reader._identity = "wxid-account_ab12"
        self.assertEqual(reader.moments_self_native_id, "wxid-account")
        reader._identity = "wxid-account"
        self.assertEqual(reader.moments_self_native_id, "wxid-account")
        reader._identity = "wxid-account_suffix"
        self.assertEqual(reader.moments_self_native_id, "wxid-account_suffix")

    def test_sender_name_dictionary_is_message_directory_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            first_dir = storage / "message" / "first"
            second_dir = storage / "message" / "second"
            first_dir.mkdir(parents=True)
            second_dir.mkdir(parents=True)
            first_message = first_dir / "message_0.db"
            second_message = second_dir / "message_1.db"
            first_message.touch()
            second_message.touch()
            for directory, username in (
                (first_dir, "wxid-first"),
                (second_dir, "wxid-second"),
            ):
                connection = sqlite3.connect(directory / "message_resource.db")
                connection.execute("CREATE TABLE SenderName2Id(user_name TEXT)")
                connection.execute(
                    "INSERT INTO SenderName2Id(rowid, user_name) VALUES(1, ?)",
                    (username,),
                )
                connection.commit()
                connection.close()
            reader = self._plain_reader(storage)
            try:
                self.assertEqual(
                    reader._sender_index_for_message_source(first_message)[1],
                    "wxid-first",
                )
                self.assertEqual(
                    reader._sender_index_for_message_source(second_message)[1],
                    "wxid-second",
                )
            finally:
                self._close(reader)

    def test_selected_group_member_directory_joins_current_contact_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            source = storage / "contact.db"
            connection = sqlite3.connect(source)
            connection.executescript(
                "CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT);"
                "CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER);"
                "CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, "
                "alias TEXT, remark TEXT, nick_name TEXT);"
                "INSERT INTO chat_room(id, username) VALUES(1, 'room@chatroom');"
                "INSERT INTO chatroom_member(room_id, member_id) VALUES(1, 9);"
                "INSERT INTO contact(id, username, alias, remark, nick_name) "
                "VALUES(9, 'wxid-member', 'alias', '群成员备注', '昵称');"
            )
            connection.commit()
            connection.close()
            reader = self._plain_reader(storage)
            reader._named_databases = lambda name: (
                [source] if name == "contact.db" else []
            )
            try:
                result = reader.list_group_member_labels("room@chatroom")
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["nativeId"], "wxid-member")
                self.assertEqual(result[0]["displayName"], "群成员备注")
                self.assertIsNone(result[0]["labelGap"])
            finally:
                self._close(reader)

    def test_generic_group_sender_calibration_stays_in_selected_message_table(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT 1 AS local_type, 3 AS status, 7 AS real_sender_id"
        ).fetchone()
        reader = object.__new__(DirectWeChatReader)
        calls = []

        def calibrate(_source, _connection, *, message_table=None):
            calls.append(message_table)
            return "7"

        reader._calibrated_self_sender = calibrate
        try:
            _kind, role, direction, is_send = reader._message_row_sender_role(
                Path("message_0.db"),
                connection,
                row,
                private_session=False,
                message_table="Msg_selected",
            )
        finally:
            connection.close()
        self.assertEqual(calls, ["Msg_selected"])
        self.assertEqual((role, direction, is_send), ("self", "outgoing", True))

    def test_voice_uses_unique_sibling_voiceinfo_without_resource_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            shard = storage / "message" / "voice-shard"
            shard.mkdir(parents=True)
            message_source = shard / "message_0.db"
            message_source.touch()
            voice_source = shard / "media_0.db"
            (shard / "media_1.db").touch()
            connection = sqlite3.connect(voice_source)
            connection.executescript(
                "CREATE TABLE Name2Id(user_name TEXT);"
                "CREATE INDEX idx_name_username ON Name2Id(user_name);"
                "CREATE TABLE VoiceInfo(chat_name_id INTEGER, local_id INTEGER, "
                "svr_id INTEGER, voice_data BLOB);"
                "CREATE INDEX idx_voice_chat_server "
                "ON VoiceInfo(chat_name_id, svr_id);"
                "INSERT INTO Name2Id(rowid, user_name) VALUES(1, 'wxid-contact');"
            )
            payload = b"\x02#!SILK_V3exact"
            connection.execute(
                "INSERT INTO VoiceInfo(chat_name_id, local_id, svr_id, voice_data) "
                "VALUES(1, 7, 9, ?)",
                (payload,),
            )
            connection.commit()
            connection.close()
            message_connection = sqlite3.connect(":memory:")
            message_connection.row_factory = sqlite3.Row
            message = message_connection.execute(
                "SELECT 34 AS local_type, 7 AS local_id, 9 AS server_id"
            ).fetchone()
            reader = self._plain_reader(storage)
            try:
                media = reader._exact_media_entries(
                    row=message,
                    session_id="wxid-contact",
                    message_source=message_source,
                    message_table="Msg_test",
                    kind="voice",
                )
                self.assertEqual(len(media), 1)
                self.assertTrue(media[0]["openable"])
                self.assertEqual(reader.open_locator(media[0]["locator"]), payload)
                missing, available = reader._exact_voice_rows(
                    session_id="wxid-other",
                    server_id=9,
                    message_source=message_source,
                )
                self.assertTrue(available)
                self.assertEqual(missing, [])
            finally:
                message_connection.close()
                self._close(reader)

    def test_zstd_message_body_is_decoded_before_text_projection(self):
        from compression import zstd

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT ? AS message_content, '' AS compress_content, "
            "'' AS source, '' AS packed_info_data, '' AS origin_source",
            (zstd.compress("可读正文".encode("utf-8")),),
        ).fetchone()
        try:
            body_texts, payload_texts, gap = _message_payload_texts(row)
        finally:
            connection.close()
        self.assertIsNone(gap)
        self.assertEqual(body_texts, ["可读正文"])
        self.assertEqual(payload_texts, ["可读正文"])
        self.assertEqual(_text_from_message(body_texts[0], 1), "可读正文")

    def test_control_payload_is_never_exposed_as_plain_text(self):
        self.assertIsNone(_readable_payload_text("text\x00binary"))
        self.assertIsNone(_readable_payload_text(b"text\x00binary"))
        self.assertIsNone(_text_from_message("text\x00binary", 1))
        self.assertIsNone(_readable_payload_text("text\x7fbinary"))
        self.assertEqual(
            _readable_payload_text("👨‍👩‍👧‍👦"),
            "👨‍👩‍👧‍👦",
        )
        self.assertEqual(_readable_payload_text("你好\u200b"), "你好\u200b")

    def test_unknown_message_type_keeps_an_explicit_content_gap(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT 1 AS local_id, 999 AS local_type, 42 AS server_id, "
            "1 AS real_sender_id, 100 AS create_time, '' AS message_content, "
            "'' AS source, '' AS packed_info_data, '' AS compress_content, "
            "7 AS sort_seq, 4 AS status, '' AS origin_source"
        ).fetchone()
        reader = object.__new__(DirectWeChatReader)
        reader._identity = "wxid-synthetic-self"
        reader._media_entries = lambda *_args, **_kwargs: []
        try:
            message = reader._message_from_row(
                row=row,
                session_native_id="wxid-synthetic-contact",
                message_source=Path("synthetic.db"),
                message_table="Msg_synthetic",
                connection=connection,
                sender_index={1: "wxid-synthetic-other"},
            )
        finally:
            connection.close()

        self.assertEqual(message["type"], "unknown")
        self.assertIsNone(message["content"])
        self.assertEqual(message["contentGap"], "message_type_unsupported")

    def test_call_event_projects_source_status_not_opaque_body(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        rows = [
            connection.execute(
                "SELECT ? AS message_content, '' AS compress_content, "
                "? AS source, '' AS packed_info_data, '' AS origin_source",
                ("28b52ffd" + "ab" * 80, status),
            ).fetchone()
            for status in ("未应答", "通话时长 03:46")
        ]
        try:
            projections = [
                _message_content_projection(row, 50)[0] for row in rows
            ]
        finally:
            connection.close()
        self.assertEqual(projections, ["未应答", "通话时长 03:46"])
        self.assertEqual(_TYPE_NAMES[50], "call")

    def test_call_event_rejects_opaque_source_when_status_is_unavailable(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT '' AS message_content, '' AS compress_content, "
            "? AS source, '' AS packed_info_data, '' AS origin_source",
            ("28b52ffd" + "ab" * 80,),
        ).fetchone()
        try:
            content, _payloads, _gap = _message_content_projection(row, 50)
        finally:
            connection.close()
        self.assertIsNone(content)

    def test_incremental_time_window_excludes_undated_history_and_requires_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            source = storage / "message_0.db"
            session_id = "wxid-contact"
            table = (
                "Msg_"
                + hashlib.md5(
                    session_id.encode("utf-8"), usedforsecurity=False
                ).hexdigest()
            )
            connection = sqlite3.connect(source)
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"CREATE TABLE {table}(local_id INTEGER, local_type INTEGER, "
                "server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, "
                "message_content TEXT, source TEXT, packed_info_data TEXT, "
                "compress_content TEXT, sort_seq INTEGER, status INTEGER, "
                "origin_source TEXT)"
            )
            connection.execute(f"CREATE INDEX idx_message_time ON {table}(create_time)")
            connection.execute(f"CREATE INDEX idx_message_sort ON {table}(sort_seq)")
            for local_id, create_time in ((1, 50), (2, 150), (3, None)):
                connection.execute(
                    f"INSERT INTO {table} VALUES(?, 1, ?, 1, ?, '', '', '', '', ?, 4, '')",
                    (local_id, local_id, create_time, local_id),
                )
            connection.commit()
            reader = self._plain_reader(storage)
            reader._message_connections = lambda _table=None: [(source, connection)]
            reader._session_is_registered = lambda _session: True
            reader._message_from_row = lambda **kwargs: {
                "localId": kwargs["row"]["local_id"],
                "serverId": kwargs["row"]["server_id"],
                "createTime": kwargs["row"]["create_time"],
                "sortSeq": kwargs["row"]["sort_seq"],
            }
            try:
                result = reader.fetch_messages(
                    session_id,
                    since_s=100,
                    end_s=200,
                    limit=None,
                )
                self.assertEqual([item["localId"] for item in result["messages"]], [2])
                with self.assertRaisesRegex(
                    DirectSchemaError,
                    "sort_seq_cursor_regressed_requires_full_reconcile",
                ):
                    reader.fetch_messages(
                        session_id,
                        since_s=None,
                        end_s=200,
                        since_sort_seq=99,
                        limit=None,
                    )
                connection.execute("DROP INDEX idx_message_time")
                with self.assertRaisesRegex(
                    DirectSchemaError, "incremental_time_index_unavailable"
                ):
                    reader.fetch_messages(
                        session_id,
                        since_s=100,
                        end_s=200,
                        limit=None,
                    )
                bounded = reader.fetch_messages(
                    session_id,
                    since_s=100,
                    end_s=200,
                    limit=2,
                    allow_unindexed_time_fallback=True,
                )
                self.assertEqual([item["localId"] for item in bounded["messages"]], [2])
            finally:
                connection.close()
                self._close(reader)

    def test_cross_shard_server_identity_conflicts_fail_closed_for_all_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            session_id = "wxid-contact"
            table = (
                "Msg_"
                + hashlib.md5(
                    session_id.encode("utf-8"), usedforsecurity=False
                ).hexdigest()
            )

            def make_source(name, rows):
                source = storage / name
                connection = sqlite3.connect(source)
                connection.row_factory = sqlite3.Row
                connection.execute(
                    f"CREATE TABLE {table}(local_id INTEGER, local_type INTEGER, "
                    "server_id INTEGER, real_sender_id INTEGER, create_time INTEGER, "
                    "message_content TEXT, source TEXT, packed_info_data TEXT, "
                    "compress_content TEXT, sort_seq INTEGER, status INTEGER, "
                    "origin_source TEXT)"
                )
                connection.execute(f"CREATE INDEX {name}_time ON {table}(create_time)")
                connection.execute(f"CREATE INDEX {name}_sort ON {table}(sort_seq)")
                connection.execute(f"CREATE INDEX {name}_server ON {table}(server_id)")
                connection.executemany(
                    f"INSERT INTO {table} VALUES(?, 1, ?, 1, ?, ?, '', '', '', ?, 4, '')",
                    rows,
                )
                connection.commit()
                return source, connection

            left_source, left = make_source(
                "message_left", [(101, 42, 300, "left-body", 30)]
            )
            right_source, right = make_source(
                "message_right",
                [
                    (201, 99, 250, "other-body", 25),
                    (202, 42, 100, "right-body", 10),
                ],
            )
            reader = self._plain_reader(storage)
            reader._message_connections = lambda _table=None: [
                (left_source, left),
                (right_source, right),
            ]
            reader._session_is_registered = lambda _session: True
            reader._sender_index_for_message_source = lambda _source: {}
            reader._message_from_row = lambda **kwargs: {
                "serverId": kwargs["row"]["server_id"],
                "localId": kwargs["row"]["local_id"],
                "createTime": kwargs["row"]["create_time"],
                "sortSeq": kwargs["row"]["sort_seq"],
                "content": kwargs["row"]["message_content"],
                "media_manifest": [
                    {
                        "mediaId": "same-media",
                        "openable": True,
                        "locator": kwargs["message_source"].name,
                    }
                ],
            }
            try:
                for limit in (None, 1):
                    with self.subTest(limit=limit), self.assertRaisesRegex(
                        DirectSchemaError, "^message_identity_is_conflicting$"
                    ):
                        reader.fetch_messages(
                            session_id,
                            since_s=0,
                            end_s=400,
                            limit=limit,
                        )

                right.execute(f"DELETE FROM {table}")
                right.execute(
                    f"INSERT INTO {table} VALUES(202, 1, 42, 1, 300, "
                    "'left-body', '', '', '', 30, 4, '')"
                )
                right.commit()
                forward = reader.fetch_messages(
                    session_id, since_s=0, end_s=400, limit=1
                )["messages"]
                reader._message_connections = lambda _table=None: [
                    (right_source, right),
                    (left_source, left),
                ]
                reverse = reader.fetch_messages(
                    session_id, since_s=0, end_s=400, limit=1
                )["messages"]
                self.assertEqual(forward, reverse)
                self.assertEqual(len(forward), 1)
                self.assertEqual(forward[0]["serverId"], 42)
                self.assertEqual(forward[0]["content"], "left-body")

                for connection, local_id in ((left, 102), (right, 203)):
                    connection.execute(
                        f"INSERT INTO {table} VALUES(?, 1, 99, 1, 250, "
                        "'other-body', '', '', '', 25, 4, '')",
                        (local_id,),
                    )
                    connection.commit()
                exact_queries: list[str] = []
                left.set_trace_callback(
                    lambda sql: exact_queries.append(sql)
                    if "WHERE server_id IN" in sql
                    else None
                )
                right.set_trace_callback(
                    lambda sql: exact_queries.append(sql)
                    if "WHERE server_id IN" in sql
                    else None
                )
                connection_calls = 0

                def counted_connections(_table=None):
                    nonlocal connection_calls
                    connection_calls += 1
                    return [(left_source, left), (right_source, right)]

                reader._message_connections = counted_connections
                batched = reader.fetch_messages(
                    session_id, since_s=0, end_s=400, limit=2
                )["messages"]
                self.assertEqual(len(batched), 2)
                self.assertEqual(connection_calls, 1)
                self.assertEqual(len(exact_queries), 2)
            finally:
                left.close()
                right.close()
                self._close(reader)

    def test_person_scoped_moments_scan_full_current_cache_before_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            source = storage / "sns.db"
            connection = sqlite3.connect(source)
            connection.execute(
                "CREATE TABLE SnsTimeLine(tid INTEGER, user_name TEXT, content TEXT)"
            )

            def moment_xml(native_id: str, username: str, create_time: int) -> str:
                return (
                    "<root><TimelineObject>"
                    f"<id>{native_id}</id><username>{username}</username>"
                    f"<createTime>{create_time}</createTime>"
                    "<contentDesc>synthetic</contentDesc>"
                    "<ContentObject><type>1</type><mediaList /></ContentObject>"
                    "</TimelineObject><LocalExtraInfo><nickname>synthetic</nickname>"
                    "</LocalExtraInfo></root>"
                )

            connection.execute(
                "INSERT INTO SnsTimeLine VALUES(?, ?, ?)",
                (1, "wxid-target", moment_xml("target", "wxid-target", 1)),
            )
            for tid in range(2, 702):
                connection.execute(
                    "INSERT INTO SnsTimeLine VALUES(?, ?, ?)",
                    (
                        tid,
                        "wxid-other",
                        moment_xml(str(tid), "wxid-other", tid),
                    ),
                )
            connection.commit()
            connection.close()

            reader = self._plain_reader(storage)
            reader._named_databases = lambda name: [source] if name == "sns.db" else []
            try:
                result = reader.list_moments(
                    since_s=0,
                    end_s=1_000,
                    username="wxid-target",
                    limit=1,
                )
                self.assertEqual(result["scannedRows"], 701)
                self.assertEqual(result["matchedRows"], 1)
                self.assertEqual(result["moments"][0]["nativeId"], "target")
            finally:
                self._close(reader)


if __name__ == "__main__":
    unittest.main()

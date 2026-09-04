from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import wechat_cli
from wechat_source import DirectWeChatReader


SESSION_ID = "wxid-synthetic-contact"
OTHER_SESSION_ID = "wxid-other-contact"
MESSAGE_TABLE = "Msg_" + hashlib.md5(
    SESSION_ID.encode("utf-8"), usedforsecurity=False
).hexdigest()


def _message_row(
    *,
    local_id: int,
    server_id: int,
    create_time: int,
    sort_seq: int,
    content: str,
    status: int = 4,
) -> tuple[object, ...]:
    return (
        local_id,
        1,
        server_id,
        1,
        create_time,
        content,
        "",
        "",
        "",
        sort_seq,
        status,
        "",
    )


def _make_shard(name: str, rows: list[tuple[object, ...]]) -> tuple[Path, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        f"CREATE TABLE {MESSAGE_TABLE}("
        "local_id INTEGER, local_type INTEGER, server_id INTEGER, "
        "real_sender_id INTEGER, create_time INTEGER, message_content TEXT, "
        "source TEXT, packed_info_data TEXT, compress_content TEXT, "
        "sort_seq INTEGER, status INTEGER, origin_source TEXT)"
    )
    connection.execute(f"CREATE INDEX idx_{name}_time ON {MESSAGE_TABLE}(create_time)")
    connection.execute(f"CREATE INDEX idx_{name}_sort ON {MESSAGE_TABLE}(sort_seq)")
    connection.execute(f"CREATE INDEX idx_{name}_server ON {MESSAGE_TABLE}(server_id)")
    connection.executemany(
        f"INSERT INTO {MESSAGE_TABLE} VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    return Path.cwd() / f"synthetic-{name}.db", connection


class SyntheticReader:
    def __init__(
        self,
        shard_rows: list[list[tuple[object, ...]]],
        contacts: list[dict[str, object]] | None = None,
        *,
        unreadable_content: bool = False,
    ) -> None:
        self.shards = [
            _make_shard(str(index), rows) for index, rows in enumerate(shard_rows)
        ]
        self.contacts = contacts or [
            {
                "nativeId": SESSION_ID,
                "sessionType": "contact",
                "displayName": "合成联系人",
                "remark": "合成联系人",
                "nickname": None,
                "alias": None,
                "lastTimestamp": 10_000,
            }
        ]
        self.reader = object.__new__(DirectWeChatReader)
        self.reader._storage = Path.cwd()
        self.reader.account_identity_commitment = "a" * 64
        self.reader._message_connections = lambda _table=None: list(self.shards)
        self.reader._session_is_registered = lambda session_id: session_id in {
            SESSION_ID,
            OTHER_SESSION_ID,
        }
        self.reader._sender_index_for_message_source = lambda _source: {}
        self.reader._message_from_row = self._project_row
        self.reader.list_contacts = self._list_contacts
        self.reader.list_group_member_labels = lambda _session_id: []
        self.reader.close = lambda: None
        self.unreadable_content = unreadable_content

    def _list_contacts(self, *, include_unregistered: bool = False):
        del include_unregistered
        return [dict(item) for item in self.contacts]

    def _project_row(self, **kwargs):
        row = kwargs["row"]
        content = row["message_content"]
        message = {
            "serverId": row["server_id"],
            "localId": row["local_id"],
            "localType": row["local_type"],
            "sortSeq": row["sort_seq"],
            "createTime": row["create_time"],
            "content": None if self.unreadable_content else content,
            "status": row["status"],
            "isSend": False,
            "isSystem": False,
            "senderRole": "other",
            "direction": "incoming",
            "type": "text",
        }
        if self.unreadable_content:
            message["contentGap"] = "message_content_unparsed"
        return message

    def close(self) -> None:
        for _path, connection in self.shards:
            connection.close()


def _message_key(message: dict[str, object]) -> tuple[object, ...]:
    return tuple(message["_pageKey"])


def _public_identity(message: dict[str, object]) -> tuple[str, str] | None:
    server_id = message.get("serverId")
    if server_id not in (None, 0, "0"):
        return "server", str(server_id)
    local_id = message.get("localId")
    if local_id not in (None, 0, "0"):
        return "local", str(local_id)
    return None


class ContextPagingTests(unittest.TestCase):
    @staticmethod
    def _fetch_args(
        *,
        account: str = "primary",
        contact: str = "合成联系人",
        contains: str | None = None,
        around: str | None = None,
        cursor: str | None = None,
        since: str | None = "1970-01-01T00:00:00+00:00",
        until: str | None = "1970-01-01T00:10:00+00:00",
        scan_limit: int = 120,
        return_limit: int = 24,
        lookback_days: int = 1,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            config="synthetic-config.json",
            account=account,
            contact=contact,
            since=since,
            until=until,
            around=around,
            contains=contains,
            cursor=cursor,
            lookback_days=lookback_days,
            scan_limit=scan_limit,
            return_limit=return_limit,
        )

    @staticmethod
    def _run_context(
        shard_rows: list[list[tuple[object, ...]]],
        args: argparse.Namespace,
        *,
        contacts: list[dict[str, object]] | None = None,
        unreadable_content: bool = False,
        now: int = 1_000,
    ) -> dict[str, object]:
        created: list[SyntheticReader] = []

        def reader_factory(_account, _cutoff):
            reader = SyntheticReader(
                shard_rows,
                contacts=contacts,
                unreadable_content=unreadable_content,
            )
            created.append(reader)
            return reader.reader

        config = {"primary": {}, "secondary": {}}
        try:
            with (
                patch.object(wechat_cli, "_read_config", return_value=config),
                patch.object(wechat_cli, "_reader", side_effect=reader_factory),
                patch.object(wechat_cli.time, "time", return_value=now),
            ):
                return wechat_cli._context_result(args)
        finally:
            for reader in created:
                reader.close()

    def test_actual_fetch_pages_are_complete_across_duplicate_server_local_and_row_ids(self):
        duplicate = _message_row(
            local_id=50, server_id=500, create_time=100, sort_seq=1, content="server"
        )
        source = SyntheticReader(
            [
                [
                    duplicate,
                    _message_row(
                        local_id=40, server_id=0, create_time=100, sort_seq=1, content="local"
                    ),
                    _message_row(
                        local_id=0, server_id=0, create_time=90, sort_seq=1, content="row"
                    ),
                    _message_row(
                        local_id=10, server_id=100, create_time=80, sort_seq=1, content="other"
                    ),
                ],
                [
                    duplicate,
                    _message_row(
                        local_id=30, server_id=0, create_time=100, sort_seq=1, content="local-2"
                    ),
                    _message_row(
                        local_id=0, server_id=0, create_time=70, sort_seq=1, content="row-2"
                    ),
                ],
            ]
        )
        try:
            complete = source.reader.fetch_messages(
                SESSION_ID, since_s=None, end_s=1_000, limit=100
            )["messages"]
            expected = {_message_key(item) for item in complete}
            seen: list[tuple[object, ...]] = []
            before_key = None
            for _ in range(10):
                fetched = source.reader.fetch_messages(
                    SESSION_ID,
                    since_s=None,
                    end_s=1_000,
                    limit=2,
                    before_key=before_key,
                )
                page = fetched["messages"]
                page_keys = [_message_key(item) for item in page]
                seen.extend(page_keys)
                sync = fetched["sync"]
                if not sync["hasMore"]:
                    break
                before_key = sync["nextBeforeKey"]
                self.assertIsNotNone(before_key)
            else:
                self.fail("pagination did not terminate")
            self.assertEqual(len(seen), len(set(seen)))
            self.assertEqual(set(seen), expected)
        finally:
            source.close()

    def test_context_keyword_on_next_page_is_partial_then_matched(self):
        rows = [
            _message_row(local_id=3, server_id=3, create_time=300, sort_seq=3, content="new"),
            _message_row(local_id=2, server_id=2, create_time=200, sort_seq=2, content="middle"),
            _message_row(local_id=1, server_id=1, create_time=100, sort_seq=1, content="needle"),
        ]
        first = self._run_context(
            [rows],
            self._fetch_args(contains="needle", scan_limit=2, return_limit=1),
        )
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["search"]["status"], "not_found_in_page")
        continuation = first["continuation"]
        self.assertIsNotNone(continuation)
        second_args = self._fetch_args(
            contains="needle",
            scan_limit=2,
            return_limit=1,
            cursor=continuation["cursor"],
            since=None,
            until=None,
        )
        second = self._run_context([rows], second_args)
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["search"]["status"], "matched")
        self.assertEqual([item["content"] for item in second["messages"]], ["needle"])

    def test_context_scan_limit_larger_than_return_limit_does_not_skip_older_messages(self):
        rows = [
            _message_row(
                local_id=value,
                server_id=value if value % 2 else 0,
                create_time=100 if value <= 6 else 90,
                sort_seq=1,
                content=str(value),
            )
            for value in range(1, 9)
        ]
        args = self._fetch_args(scan_limit=5, return_limit=2)
        collected: list[tuple[str, str]] = []
        for _ in range(10):
            result = self._run_context([rows], args)
            collected.extend(
                identity
                for item in result["messages"]
                if (identity := _public_identity(item)) is not None
            )
            continuation = result["continuation"]
            if continuation is None:
                break
            args = self._fetch_args(
                scan_limit=5,
                return_limit=2,
                cursor=continuation["cursor"],
                since=None,
                until=None,
            )
        else:
            self.fail("context pagination did not terminate")
        self.assertEqual(len(collected), len(set(collected)))
        self.assertEqual(
            set(collected),
            {
                ("server", str(value)) if value % 2 else ("local", str(value))
                for value in range(1, 9)
            },
        )

    def test_actual_context_around_finds_target_with_more_than_512_newer_rows(self):
        rows = [
            _message_row(
                local_id=20,
                server_id=20,
                create_time=20,
                sort_seq=20,
                content="around-target",
            )
        ] + [
            _message_row(
                local_id=value,
                server_id=value,
                create_time=value,
                sort_seq=value,
                content="newer",
            )
            for value in range(1_000, 1_600)
        ]
        result = self._run_context(
            [rows],
            self._fetch_args(
                around="1970-01-01T00:00:20+00:00",
                since="1970-01-01T00:00:00+00:00",
                until="1970-01-01T00:33:20+00:00",
                scan_limit=24,
                return_limit=1,
            ),
            now=2_000,
        )
        self.assertEqual([item["content"] for item in result["messages"]], ["around-target"])

    def test_context_cursor_rejects_account_contact_keyword_and_time_changes(self):
        rows = [
            _message_row(local_id=3, server_id=3, create_time=300, sort_seq=3, content="new"),
            _message_row(local_id=2, server_id=2, create_time=200, sort_seq=2, content="middle"),
            _message_row(local_id=1, server_id=1, create_time=100, sort_seq=1, content="needle"),
        ]
        contacts = [
            {
                "nativeId": SESSION_ID,
                "sessionType": "contact",
                "displayName": "合成联系人",
                "remark": "合成联系人",
                "nickname": None,
                "alias": None,
                "lastTimestamp": 10_000,
            },
            {
                "nativeId": OTHER_SESSION_ID,
                "sessionType": "contact",
                "displayName": "其他联系人",
                "remark": "其他联系人",
                "nickname": None,
                "alias": None,
                "lastTimestamp": 10_000,
            },
        ]
        first = self._run_context(
            [rows],
            self._fetch_args(contains="needle", scan_limit=2, return_limit=1),
            contacts=contacts,
        )
        cursor = first["continuation"]["cursor"]

        cases = (
            (self._fetch_args(account="secondary", contains="needle", cursor=cursor, since=None, until=None), "context_cursor_query_mismatch"),
            (self._fetch_args(contact="其他联系人", contains="needle", cursor=cursor, since=None, until=None), "context_cursor_target_mismatch"),
            (self._fetch_args(contains="different", cursor=cursor, since=None, until=None), "context_cursor_query_mismatch"),
            (self._fetch_args(contains="needle", cursor=cursor, since="1970-01-01T00:01:00+00:00", until=None), "context_cursor_query_mismatch"),
        )
        for args, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(wechat_cli.ProductError, expected):
                    self._run_context([rows], args, contacts=contacts)

    def test_return_limit_one_returns_keyword_hit_instead_of_preceding_message(self):
        rows = [
            _message_row(local_id=1, server_id=1, create_time=1, sort_seq=1, content="before"),
            _message_row(local_id=2, server_id=2, create_time=2, sort_seq=2, content="needle"),
            _message_row(local_id=3, server_id=3, create_time=3, sort_seq=3, content="after"),
        ]
        result = self._run_context(
            [rows],
            self._fetch_args(contains="needle", scan_limit=3, return_limit=1),
        )
        self.assertEqual([item["content"] for item in result["messages"]], ["needle"])

    def test_single_unreadable_message_is_indeterminate_instead_of_not_found(self):
        row = _message_row(
            local_id=1,
            server_id=1,
            create_time=1,
            sort_seq=1,
            content="opaque",
        )
        result = self._run_context(
            [[row]],
            self._fetch_args(contains="needle", scan_limit=1, return_limit=1),
            unreadable_content=True,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["search"]["status"], "indeterminate_content_gaps")


if __name__ == "__main__":
    unittest.main()

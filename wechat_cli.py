"""Thin per-call CLI for direct local WeChat reads.

The CLI has no database, daemon, queue, background sync, event model or user
profile.  A caller names an account and contact, receives one bounded native
message window, and may explicitly open media or create a one-off preservation
bundle.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from wechat_source import DirectWeChatReader, WeChatDirectError


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
ACCOUNT_LABELS = ("primary", "secondary")
LOCAL_SETTINGS_PATH = Path(__file__).with_name(".wechatdirect.local.json")
LOCAL_SETTINGS_KEYS = frozenset({"config", "export_root"})
DEFAULT_APP_DIRECTORY = "WeChatDirect"
MAX_SCAN_MESSAGES = 500
MAX_RETURN_MESSAGES = 80
MAX_OUTPUT_BYTES = 512 * 1024
DEFAULT_INCREMENT_OVERLAP_SECONDS = 86_400
DEFAULT_AI_CONTEXT_MESSAGES = 80
MAX_AI_CONTEXT_BYTES = 128 * 1024
MAX_AI_ITEM_CHARS = 32 * 1024
MAX_AI_MEDIA_OCCURRENCES_PER_MESSAGE = 32
VOICE_DECODER = Path(__file__).with_name("voice_decode.py")


class ProductError(RuntimeError):
    """A bounded, user-actionable product failure."""


def _local_appdata_directory() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise ProductError("wechat_localappdata_unavailable")
    return Path(value) / DEFAULT_APP_DIRECTORY


def _local_settings() -> dict[str, str]:
    """Read the optional local discovery settings without exposing their values."""

    if not LOCAL_SETTINGS_PATH.is_file():
        return {}
    try:
        value = json.loads(LOCAL_SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductError("wechat_local_settings_invalid") from exc
    if not isinstance(value, Mapping) or set(value) - LOCAL_SETTINGS_KEYS:
        raise ProductError("wechat_local_settings_invalid")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str) or not item.strip():
            raise ProductError("wechat_local_settings_invalid")
        result[key] = item
    return result


def _resolve_config_path(explicit: object | None) -> Path:
    if explicit is not None:
        value = os.fspath(explicit)
        if not value:
            raise ProductError("wechat_config_path_invalid")
        return Path(value)
    value = os.environ.get("WECHAT_DIRECT_CONFIG")
    if value:
        return Path(value)
    configured = _local_settings().get("config")
    if configured:
        return Path(configured)
    return _local_appdata_directory() / "accounts.json"


def _default_export_root() -> Path:
    value = os.environ.get("WECHAT_DIRECT_EXPORT_ROOT")
    if value:
        return Path(value)
    configured = _local_settings().get("export_root")
    if configured:
        return Path(configured)
    return _local_appdata_directory() / "exports"


def _canonical_bytes(value: object) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # JSON Lines has one physical LF-delimited record.  Escape the two Unicode
    # line separators so readers that use ``splitlines()`` cannot split a body.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").encode(
        "utf-8"
    )


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_config(path: Path) -> dict[str, dict[str, str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductError("wechat_account_config_unavailable") from exc
    if not isinstance(value, Mapping) or set(value) != set(ACCOUNT_LABELS):
        raise ProductError("wechat_account_config_invalid")
    result: dict[str, dict[str, str]] = {}
    for label in ACCOUNT_LABELS:
        item = value.get(label)
        if not isinstance(item, Mapping):
            raise ProductError("wechat_account_config_invalid")
        config_path = str(item.get("config_path") or "")
        local_state_path = str(item.get("local_state_path") or "")
        expected_source = str(item.get("expected_source_identity_sha256") or "")
        expected_moments = str(item.get("expected_moments_author_sha256") or "")
        commitments = (expected_source, expected_moments)
        if (
            not config_path
            or not local_state_path
            or any(
                len(commitment) != 71
                or not commitment.startswith("sha256:")
                or any(
                    character not in "0123456789abcdef"
                    for character in commitment[7:]
                )
                for commitment in commitments
            )
        ):
            raise ProductError("wechat_account_config_invalid")
        result[label] = {
            "config_path": config_path,
            "local_state_path": local_state_path,
            "expected_source_identity_sha256": expected_source,
            "expected_moments_author_sha256": expected_moments,
        }
    return result


def _reader(account: Mapping[str, str], cutoff_s: int) -> DirectWeChatReader:
    reader = DirectWeChatReader(
        config_path=account["config_path"],
        local_state_path=account["local_state_path"],
        snapshot_cutoff_s=cutoff_s,
    )
    actual = "sha256:" + reader.account_identity_commitment
    expected = str(account["expected_source_identity_sha256"])
    if not hmac.compare_digest(actual, expected):
        reader.close()
        raise ProductError("wechat_account_identity_binding_mismatch")
    return reader


def _verify_moments_self_identity(
    account: Mapping[str, str], reader: DirectWeChatReader
) -> None:
    actual = _sha256(reader.moments_self_native_id.encode("utf-8"))
    expected = str(account["expected_moments_author_sha256"])
    if not hmac.compare_digest(actual, expected):
        raise ProductError("wechat_moments_identity_binding_mismatch")


def _normalize_name(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )


def _contact_match_fields(contact: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        item
        for item in (
            _normalize_name(contact.get("remark")),
            _normalize_name(contact.get("nickname")),
            _normalize_name(contact.get("alias")),
            _normalize_name(contact.get("nativeId")),
        )
        if item
    )


def _safe_contact(contact: Mapping[str, Any], account: str) -> dict[str, Any]:
    result = {
        "account": account,
        "nativeId": contact.get("nativeId"),
        "displayName": contact.get("displayName"),
        "remark": contact.get("remark"),
        "nickname": contact.get("nickname"),
        "alias": contact.get("alias"),
        "sessionType": contact.get("sessionType"),
        "lastTimestamp": contact.get("lastTimestamp"),
    }
    if contact.get("isSelf") is not None:
        result["isSelf"] = bool(contact.get("isSelf"))
    if contact.get("labelScope"):
        result["labelScope"] = contact.get("labelScope")
    if str(contact.get("nativeId") or "").casefold() == "filehelper":
        result["conversationKind"] = "file_transfer_assistant"
    return result


def _sender_role_counts(messages: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"self": 0, "other": 0, "system": 0, "unknown": 0}
    for message in messages:
        role = str(message.get("senderRole") or "unknown")
        counts[role if role in counts else "unknown"] += 1
    return counts


def _contact_directory(
    reader: DirectWeChatReader, session_native_id: str
) -> list[dict[str, Any]]:
    directory = {
        str(item["nativeId"]): dict(item)
        for item in reader.list_contacts()
        if item.get("nativeId")
    }
    if session_native_id.casefold().endswith("@chatroom"):
        for item in reader.list_group_member_labels(session_native_id):
            if item.get("nativeId"):
                directory[str(item["nativeId"])] = dict(item)
    return list(directory.values())


def _resolve_contact(
    config: Mapping[str, Mapping[str, str]],
    *,
    account: str,
    query: str,
    cutoff_s: int,
) -> tuple[str, DirectWeChatReader, dict[str, Any]]:
    normalized = _normalize_name(query)
    if not normalized:
        raise ProductError("contact_name_required")
    labels = ACCOUNT_LABELS if account == "auto" else (account,)
    readers: dict[str, DirectWeChatReader] = {}
    matches: list[tuple[str, dict[str, Any]]] = []
    try:
        for label in labels:
            reader = _reader(config[label], cutoff_s)
            readers[label] = reader
            for contact in reader.list_contacts():
                if normalized in _contact_match_fields(contact):
                    matches.append((label, contact))
        if len(matches) != 1:
            candidates = [_safe_contact(item, label) for label, item in matches]
            raise ProductError(
                "contact_not_found"
                if not candidates
                else "contact_ambiguous:" + json.dumps(
                    candidates, ensure_ascii=False, separators=(",", ":")
                )
            )
        selected_label, selected_contact = matches[0]
        selected_reader = readers.pop(selected_label)
        return selected_label, selected_reader, selected_contact
    finally:
        for reader in readers.values():
            reader.close()


def _resolve_moments_subject(
    config: Mapping[str, Mapping[str, str]],
    *,
    account: str,
    query: str | None,
    self_requested: bool,
    cutoff_s: int,
) -> tuple[str, DirectWeChatReader, dict[str, Any] | None]:
    """Resolve one exact Moments author inside one explicit account.

    Chat sessions are not a complete Moments author directory: the account
    itself is not its own chat session, and a cached publisher may have no
    current session.  This resolver therefore combines the source-proven self
    identity, the full local contact directory, and one bounded pass over the
    current local Moments cache.  It never persists a people index.
    """

    if account not in ACCOUNT_LABELS:
        raise ProductError("moments_explicit_account_required")
    if self_requested and query:
        raise ProductError("moments_subject_selector_conflict")
    reader = _reader(config[account], cutoff_s)
    try:
        if self_requested:
            _verify_moments_self_identity(config[account], reader)
            return (
                account,
                reader,
                {
                    "nativeId": reader.moments_self_native_id,
                    "sessionType": "self",
                    "displayName": "我",
                    "remark": None,
                    "nickname": None,
                    "alias": None,
                    "lastTimestamp": None,
                    "isSelf": True,
                    "labelScope": "source_account_moments_identity",
                },
            )
        normalized = _normalize_name(query)
        if not normalized:
            return account, reader, None

        candidates: dict[str, dict[str, Any]] = {}
        match_fields: dict[str, set[str]] = {}
        for item in reader.list_contacts(include_unregistered=True):
            native_id = str(item.get("nativeId") or "")
            folded_native_id = native_id.casefold()
            if (
                not native_id
                or folded_native_id.endswith("@chatroom")
                or folded_native_id.startswith("gh_")
            ):
                continue
            candidates[native_id] = dict(item)
            match_fields.setdefault(native_id, set()).update(
                _contact_match_fields(item)
            )

        current = reader.list_moments(
            since_s=0,
            end_s=cutoff_s,
            username=None,
            limit=None,
        )
        for moment in current.get("moments") or []:
            native_id = str(moment.get("username") or "")
            if not native_id:
                continue
            nickname = str(moment.get("nickname") or "").strip() or None
            item = candidates.setdefault(
                native_id,
                {
                    "nativeId": native_id,
                    "sessionType": "moments_publisher",
                    "displayName": nickname or native_id,
                    "remark": None,
                    "nickname": nickname,
                    "alias": None,
                    "lastTimestamp": None,
                    "labelScope": "current_local_moments_cache",
                },
            )
            fields = match_fields.setdefault(native_id, set())
            fields.add(_normalize_name(native_id))
            if nickname:
                fields.add(_normalize_name(nickname))
                if item.get("displayName") == native_id:
                    item["displayName"] = nickname
                    item["nickname"] = nickname

        matches = [
            candidates[native_id]
            for native_id, fields in match_fields.items()
            if normalized in fields
        ]
        if len(matches) != 1:
            raise ProductError(
                "contact_not_found"
                if not matches
                else "contact_ambiguous:"
                + json.dumps(
                    [_safe_contact(item, account) for item in matches],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return account, reader, matches[0]
    except Exception:
        reader.close()
        raise



def _parse_time(value: str | None, *, default: int) -> int:
    if not value:
        return default
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ProductError("time_value_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return int(parsed.timestamp())


def _message_native_id(message: Mapping[str, Any]) -> dict[str, str] | None:
    server_id = message.get("serverId")
    if server_id not in (None, 0, "0"):
        return {"kind": "server", "value": str(server_id)}
    local_id = message.get("localId")
    if local_id not in (None, ""):
        return {"kind": "local", "value": str(local_id)}
    return None


def _sender_receipt(
    message: Mapping[str, Any],
    *,
    contacts: Mapping[str, Mapping[str, Any]] | None = None,
    selected_contact: Mapping[str, Any] | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    role = str(message.get("senderRole") or "unknown")
    username = str(message.get("senderUsername") or "") or None
    contact: Mapping[str, Any] | None = None
    if username and contacts:
        contact = contacts.get(username)
    selected_native_id = str(
        selected_contact.get("nativeId") if selected_contact else ""
    ).casefold()
    selected_is_group = selected_native_id.endswith("@chatroom") or selected_native_id.startswith(
        "gh_"
    )
    if contact is None and role == "other" and not selected_is_group:
        contact = selected_contact
    if role == "self":
        display_name = "我"
    elif role == "system":
        display_name = "系统"
    elif contact is not None:
        display_name = str(contact.get("displayName") or username or "对方")
    else:
        display_name = (
            "群成员（昵称未取到）"
            if role == "other" and selected_is_group
            else username or "身份未确定"
        )
    return {
        "account": account,
        "role": role,
        "nativeId": username,
        "displayName": display_name,
        "remark": contact.get("remark") if contact else None,
        "nickname": contact.get("nickname") if contact else None,
        "alias": contact.get("alias") if contact else None,
        "labelScope": (
            contact.get("labelScope") or "current_contact_directory"
            if contact
            else None
        ),
        "labelGap": (
            contact.get("labelGap")
            if contact
            else "group_member_label_unavailable"
            if role == "other" and selected_is_group
            else None
        ),
    }


def _message_receipt(
    message: Mapping[str, Any],
    *,
    contacts: Mapping[str, Mapping[str, Any]] | None = None,
    selected_contact: Mapping[str, Any] | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    result = dict(message)
    result["nativeId"] = _message_native_id(message)
    result["messageSha256"] = _sha256(_canonical_bytes(message))
    result["sender"] = _sender_receipt(
        message,
        contacts=contacts,
        selected_contact=selected_contact,
        account=account,
    )
    return result


def _select_window(
    messages: list[dict[str, Any]],
    *,
    contains: str | None,
    around_s: int | None,
    return_limit: int,
) -> tuple[list[dict[str, Any]], int | None]:
    if not messages:
        return [], None
    anchor: int | None = None
    if contains:
        wanted = contains.casefold()
        for index, message in enumerate(messages):
            if wanted in str(message.get("content") or "").casefold():
                anchor = index
        if anchor is None:
            raise ProductError("message_text_anchor_not_found")
    elif around_s is not None:
        timed = [
            (abs(int(item.get("createTime") or 0) - around_s), index)
            for index, item in enumerate(messages)
            if item.get("createTime") is not None
        ]
        if not timed:
            raise ProductError("message_time_anchor_not_found")
        anchor = min(timed)[1]
    else:
        return messages[-return_limit:], len(messages) - 1

    before = max(1, return_limit * 3 // 5)
    start = max(0, anchor - before)
    end = min(len(messages), start + return_limit)
    start = max(0, end - return_limit)
    return messages[start:end], anchor - start


def _attach_quote_targets(
    reader: DirectWeChatReader,
    *,
    session_native_id: str,
    selected: list[dict[str, Any]],
    scanned: list[dict[str, Any]],
    contacts: Mapping[str, Mapping[str, Any]] | None = None,
    selected_contact: Mapping[str, Any] | None = None,
    account: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_server = {
        str(item["serverId"]): item
        for item in scanned
        if item.get("serverId") not in (None, 0, "0")
    }
    targets: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    for message in selected:
        identity = _message_native_id(message)
        if message.get("quoteGap"):
            gaps.append(
                {
                    "kind": str(message["quoteGap"]),
                    "message": identity,
                }
            )
            continue
        quote = message.get("quote")
        if not isinstance(quote, Mapping) or not quote.get("platformMessageId"):
            continue
        target_id = str(quote["platformMessageId"])
        target = by_server.get(target_id)
        if target is None:
            try:
                target = reader.fetch_message_by_server_id(
                    session_native_id,
                    target_id,
                    exact_media_lookup=True,
                )
            except WeChatDirectError as exc:
                gaps.append(
                    {
                        "kind": "quote_target_unavailable",
                        "message": identity,
                        "reason": type(exc).__name__,
                    }
                )
                continue
        if target is None:
            gaps.append(
                {
                    "kind": "quote_target_missing",
                    "message": identity,
                    "targetServerId": target_id,
                }
            )
            continue
        targets[target_id] = _message_receipt(
            target,
            contacts=contacts,
            selected_contact=selected_contact,
            account=account,
        )
    return list(targets.values()), gaps


def _context_result(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= int(args.scan_limit) <= MAX_SCAN_MESSAGES:
        raise ProductError("scan_limit_invalid")
    if not 1 <= int(args.return_limit) <= MAX_RETURN_MESSAGES:
        raise ProductError("return_limit_invalid")
    cutoff_s = int(time.time())
    config = _read_config(_resolve_config_path(getattr(args, "config", None)))
    label, reader, contact = _resolve_contact(
        config,
        account=args.account,
        query=args.contact,
        cutoff_s=cutoff_s,
    )
    try:
        end_s = _parse_time(args.until, default=cutoff_s)
        since_default = end_s - int(args.lookback_days) * 86_400
        since_s = _parse_time(args.since, default=since_default)
        if since_s > end_s:
            raise ProductError("time_window_reversed")
        fetched = reader.fetch_messages(
            str(contact["nativeId"]),
            since_s=since_s,
            end_s=end_s,
            limit=int(args.scan_limit),
            exact_media_lookup=True,
            allow_unindexed_time_fallback=True,
        )
        scanned = list(fetched.get("messages") or [])
        around_s = _parse_time(args.around, default=end_s) if args.around else None
        selected, anchor_index = _select_window(
            scanned,
            contains=args.contains,
            around_s=around_s,
            return_limit=int(args.return_limit),
        )
        contacts = {
            str(item["nativeId"]): item
            for item in _contact_directory(reader, str(contact["nativeId"]))
        }
        quote_targets, gaps = _attach_quote_targets(
            reader,
            session_native_id=str(contact["nativeId"]),
            selected=selected,
            scanned=scanned,
            contacts=contacts,
            selected_contact=contact,
            account=label,
        )
        messages = [
            _message_receipt(
                item,
                contacts=contacts,
                selected_contact=contact,
                account=label,
            )
            for item in selected
        ]
        scanned_role_counts = _sender_role_counts(scanned)
        returned_role_counts = _sender_role_counts(selected)
        latest_known_session_time: int | None = None
        try:
            candidate_time = int(contact.get("lastTimestamp") or 0)
            latest_known_session_time = candidate_time or None
        except (TypeError, ValueError, OverflowError):
            pass
        history_hint = None
        if not scanned and latest_known_session_time is not None:
            history_hint = {
                "latestKnownMessageTimeS": latest_known_session_time,
                "status": (
                    "older_local_messages_exist"
                    if latest_known_session_time < since_s
                    else "newer_local_messages_exist"
                    if latest_known_session_time > end_s
                    else "selected_window_has_no_readable_messages"
                ),
            }
        actual_cutoff = max(
            (int(item.get("createTime") or 0) for item in scanned),
            default=None,
        )
        media_counts: dict[str, int] = {}
        for message in selected:
            for media in message.get("media_manifest") or []:
                key = str(media.get("kind") or "unknown")
                media_counts[key] = media_counts.get(key, 0) + 1
                if not media.get("openable"):
                    gaps.append(
                        {
                            "kind": "media_not_openable",
                            "message": _message_native_id(message),
                            "mediaKind": key,
                            "reason": media.get("resolution_gap")
                            or media.get("open_status")
                            or "not_openable",
                        }
                    )
        result: dict[str, Any] = {
            "status": "success",
            "account": label,
            "accountIdentityCommitment": "sha256:"
            + reader.account_identity_commitment,
            "contact": _safe_contact(contact, label),
            "sourceSnapshotCutoffS": cutoff_s,
            "requestedWindow": {"sinceS": since_s, "untilS": end_s},
            "historyScope": "bounded_requested_window",
            "actualVisibleCutoffS": actual_cutoff,
            "scannedMessages": len(scanned),
            "returnedMessages": len(messages),
            "scannedSenderRoleCounts": scanned_role_counts,
            "returnedSenderRoleCounts": returned_role_counts,
            "selfObservation": {
                "status": (
                    "observed"
                    if returned_role_counts["self"]
                    else "not_observed_with_unresolved_senders"
                    if returned_role_counts["unknown"]
                    else "not_observed_in_returned_window"
                ),
                "unknownSenderCount": returned_role_counts["unknown"],
            },
            "availableHistoryHint": history_hint,
            "anchorIndex": anchor_index,
            "messages": messages,
            "quotedMessages": quote_targets,
            "mediaCounts": dict(sorted(media_counts.items())),
            "gaps": gaps,
        }
        result["manifestSha256"] = _sha256(_canonical_bytes(result))
        if len(_canonical_bytes(result)) > MAX_OUTPUT_BYTES:
            raise ProductError("context_output_too_large")
        return result
    finally:
        reader.close()


def command_context(args: argparse.Namespace) -> int:
    sys.stdout.buffer.write(_canonical_bytes(_context_result(args)))
    return 0


def command_moments(args: argparse.Namespace) -> int:
    cutoff_s = int(time.time())
    config = _read_config(_resolve_config_path(getattr(args, "config", None)))
    label, reader, contact = _resolve_moments_subject(
        config,
        account=str(args.account),
        query=getattr(args, "contact", None),
        self_requested=bool(getattr(args, "self", False)),
        cutoff_s=cutoff_s,
    )
    try:
        end_s = _parse_time(args.until, default=cutoff_s)
        since_s = _parse_time(
            args.since,
            default=end_s - int(args.lookback_days) * 86_400,
        )
        if since_s > end_s:
            raise ProductError("time_window_reversed")
        source = reader.list_moments(
            since_s=since_s,
            end_s=end_s,
            username=str(contact["nativeId"]) if contact else None,
            limit=int(args.limit),
        )
        contacts = {
            str(item["nativeId"]): _safe_contact(item, label)
            for item in reader.list_contacts()
            if item.get("nativeId")
        }
        if contact is not None:
            contacts[str(contact["nativeId"])] = _safe_contact(contact, label)
        gaps = list(source.get("gaps") or [])
        moments: list[dict[str, Any]] = []
        for item in source.get("moments") or []:
            projected = dict(item)
            projected["contact"] = contacts.get(
                str(item.get("username") or ""),
                {
                    "account": label,
                    "nativeId": item.get("username"),
                    "displayName": item.get("nickname"),
                },
            )
            for media in projected.get("media_manifest") or []:
                if not media.get("openable"):
                    gaps.append(
                        {
                            "kind": "moment_media_not_opened",
                            "momentNativeId": projected.get("nativeId"),
                            "reason": media.get("open_status")
                            or "not_openable",
                        }
                    )
            moments.append(projected)
        target_cache_status = (
            "target_cached"
            if contact is not None and moments
            else "target_not_in_current_local_cache"
            if contact is not None
            else "account_cache_read"
        )
        if contact is not None and not moments:
            gaps.append(
                {
                    "kind": "target_moments_not_in_current_local_cache",
                    "target": "self" if contact.get("isSelf") else "contact",
                    "nextAction": (
                        "open_the_target_moments_profile_in_this_exact_account_then_retry"
                    ),
                }
            )
        result: dict[str, Any] = {
            "status": "success",
            "account": label,
            "accountIdentityCommitment": "sha256:"
            + reader.account_identity_commitment,
            "contact": _safe_contact(contact, label) if contact else None,
            "sourceSnapshotCutoffS": cutoff_s,
            "actualVisibleCutoffS": source.get("sourceVisibleCutoffS"),
            "requestedWindow": {"sinceS": since_s, "untilS": end_s},
            "historyScope": source.get("historyScope"),
            "scannedRows": source.get("scannedRows"),
            "matchedRows": source.get("matchedRows"),
            "returnedMoments": len(moments),
            "targetCacheStatus": target_cache_status,
            "hasMoreCurrentCache": source.get("hasMoreCurrentCache"),
            "moments": moments,
            "gaps": gaps,
        }
        result["manifestSha256"] = _sha256(_canonical_bytes(result))
        if len(_canonical_bytes(result)) > MAX_OUTPUT_BYTES:
            raise ProductError("moments_output_too_large")
        sys.stdout.buffer.write(_canonical_bytes(result))
        return 0
    finally:
        reader.close()


def command_media_open(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists():
        raise ProductError("media_output_already_exists")
    cutoff_s = int(time.time())
    config = _read_config(_resolve_config_path(getattr(args, "config", None)))
    account = config[args.account]
    with _reader(account, cutoff_s) as reader:
        data = reader.open_locator(args.locator)
        resolved = reader.resolve_locator(args.locator)
        if args.voice_wav and resolved.get("kind") != "voice":
            raise ProductError("media_is_not_voice")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".incomplete")
        if args.voice_wav:
            if not _is_tencent_silk(data):
                raise ProductError("wechat_voice_format_unsupported")
            with tempfile.TemporaryDirectory(prefix="wechat-direct-voice-") as scratch:
                source = Path(scratch) / "voice.silk"
                source.write_bytes(data)
                _decode_voice_file(source, temporary)
        else:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        temporary.replace(output)
        output_data = output.read_bytes()
        receipt = {
            "status": "success",
            "account": args.account,
            "accountIdentityCommitment": "sha256:"
            + reader.account_identity_commitment,
            "kind": resolved.get("kind"),
            "format": "wav" if args.voice_wav else "original",
            "sourceBytes": len(data),
            "sourceSha256": _sha256(data),
            "bytes": len(output_data),
            "sha256": _sha256(output_data),
            "output": os.fspath(output.resolve()),
        }
    sys.stdout.buffer.write(_canonical_bytes(receipt))
    return 0


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _voice_interpreter_command() -> list[str]:
    python = os.environ.get("WECHAT_DIRECT_VOICE_PYTHON")
    return [python] if python else ["py", "-3.11"]


def _voice_interpreter_available() -> bool:
    try:
        completed = subprocess.run(
            [*_voice_interpreter_command(), "-c", "import pilk"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def command_doctor(args: argparse.Namespace) -> int:
    """Report local readiness without opening a source database or exposing paths."""

    errors: list[str] = []
    warnings: list[str] = []
    windows_supported = sys.platform == "win32"
    if not windows_supported:
        errors.append("doctor_windows_required")
    try:
        _local_settings()
    except ProductError:
        errors.append("doctor_local_settings_invalid")

    dependencies = {
        "cryptography": "available"
        if _module_available("cryptography")
        else "unavailable",
        "compression": "available"
        if _module_available("compression")
        else "unavailable",
    }
    for name, status in dependencies.items():
        if status != "available":
            errors.append("doctor_dependency_" + name + "_unavailable")

    configuration: dict[str, Any] = {
        "status": "unavailable",
        "configuredAccounts": 0,
        "sourceConfigFilesPresent": 0,
        "localStateFilesPresent": 0,
    }
    try:
        config = _read_config(_resolve_config_path(getattr(args, "config", None)))
    except ProductError as exc:
        error = str(exc)
        configuration["status"] = (
            "unavailable"
            if error in {"wechat_account_config_unavailable", "wechat_localappdata_unavailable"}
            else "invalid"
        )
        errors.append("doctor_configuration_" + configuration["status"])
    else:
        source_present = sum(
            Path(item["config_path"]).is_file() for item in config.values()
        )
        state_present = sum(
            Path(item["local_state_path"]).is_file() for item in config.values()
        )
        configuration.update(
            {
                "status": "valid",
                "configuredAccounts": len(config),
                "sourceConfigFilesPresent": source_present,
                "localStateFilesPresent": state_present,
            }
        )
        if source_present != len(config):
            errors.append("doctor_source_config_files_missing")
        if state_present != len(config):
            errors.append("doctor_local_state_files_missing")

    voice_status = "available" if _voice_interpreter_available() else "unavailable"
    if voice_status != "available":
        warnings.append("doctor_voice_decoder_unavailable")
    result = {
        "status": "success" if not errors else "failed",
        "format": "wechat-direct-doctor.v1",
        "python": {
            "major": sys.version_info.major,
            "minor": sys.version_info.minor,
        },
        "platform": {"windowsSupported": windows_supported},
        "dependencies": dependencies,
        "configuration": configuration,
        "voiceDecoder": {"status": voice_status},
        "errors": sorted(set(errors)),
        "warnings": warnings,
    }
    sys.stdout.buffer.write(_canonical_bytes(result))
    return 0 if result["status"] == "success" else 2


def _write_json(path: Path, value: object) -> None:
    data = _canonical_bytes(value)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _safe_filename(value: object) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(value or "")
    ).strip("_")
    return cleaned[:80] or "media"


def _is_tencent_silk(value: bytes) -> bool:
    return value.startswith(b"\x02#!SILK_V3")


def _decode_voice_file(source: Path, output: Path) -> None:
    completed = subprocess.run(
        [
            *_voice_interpreter_command(),
            os.fspath(VOICE_DECODER),
            "--input",
            os.fspath(source),
            "--output",
            os.fspath(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        raise ProductError("wechat_voice_decode_failed")


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".incomplete")
    with temporary.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _write_json_atomic(path: Path, value: object) -> None:
    _write_bytes_atomic(path, _canonical_bytes(value))


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductError("sync_state_invalid") from exc
    if not isinstance(value, dict):
        raise ProductError("sync_state_invalid")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError
                result.append(value)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProductError("sync_records_invalid") from exc
    return result


def _write_jsonl_atomic(path: Path, values: list[Mapping[str, Any]]) -> None:
    data = b"".join(_canonical_bytes(value) + b"\n" for value in values)
    _write_bytes_atomic(path, data)


def _write_text_atomic(path: Path, value: str) -> None:
    _write_bytes_atomic(path, value.encode("utf-8"))


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _file_sha256_and_size(path: Path) -> tuple[str, int] | None:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
                total += len(block)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest(), total


def _verification_object(
    path: Path | None, errors: set[str], prefix: str
) -> dict[str, Any] | None:
    if path is None:
        errors.add(prefix + "_path_invalid")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.add(prefix + "_unavailable")
    except (OSError, json.JSONDecodeError):
        errors.add(prefix + "_invalid")
    else:
        if isinstance(value, dict):
            return value
        errors.add(prefix + "_invalid")
    return None


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _canonical_export_root(value: object) -> Path | None:
    try:
        root = Path(value)
        if not root.is_dir():
            return None
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return resolved if resolved.is_dir() else None


def _contained_export_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        return None
    try:
        resolved = root.joinpath(*relative.parts).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _verify_exported_media(
    root: Path,
    value: Mapping[str, Any],
    *,
    path_key: str,
    errors: set[str],
) -> tuple[bool, str | None]:
    candidate = _contained_export_path(root, value.get(path_key))
    if candidate is None:
        errors.add("export_media_path_invalid")
        return False, None
    if not candidate.is_file():
        errors.add("export_media_unavailable")
        return False, None
    declared = value.get("sha256")
    if not _is_sha256(declared):
        errors.add("export_media_sha256_invalid")
        return False, None
    declared_size = _nonnegative_int(value.get("bytes"))
    if declared_size is None:
        errors.add("export_media_bytes_invalid")
        return False, None
    actual = _file_sha256_and_size(candidate)
    if actual is None:
        errors.add("export_media_unavailable")
        return False, None
    if actual[0] != declared:
        errors.add("export_media_sha256_mismatch")
        return False, None
    if actual[1] != declared_size:
        errors.add("export_media_bytes_mismatch")
        return False, None
    return True, declared


def _verify_export_records(
    root: Path,
    path: Path | None,
    errors: set[str],
    counters: dict[str, int],
) -> str | None:
    if path is None:
        errors.add("export_records_path_invalid")
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for line in stream:
                digest.update(line)
                if not line.strip():
                    continue
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    errors.add("export_records_jsonl_invalid")
                    continue
                if not isinstance(record, Mapping):
                    errors.add("export_records_jsonl_invalid")
                    continue
                counters["recordCount"] += 1
                media_items = record.get("media_manifest") or []
                if not isinstance(media_items, list):
                    errors.add("export_media_metadata_invalid")
                    continue
                for media in media_items:
                    if not isinstance(media, Mapping):
                        errors.add("export_media_metadata_invalid")
                        continue
                    media_valid = False
                    media_sha256: str | None = None
                    if "exportedPath" in media:
                        media_valid, media_sha256 = _verify_exported_media(
                            root,
                            media,
                            path_key="exportedPath",
                            errors=errors,
                        )
                    derived = media.get("derivedVoiceWav")
                    if derived is None:
                        if media_valid:
                            counters["mediaFilesChecked"] += 1
                        continue
                    if not isinstance(derived, Mapping):
                        errors.add("export_media_metadata_invalid")
                        continue
                    derived_valid, _ = _verify_exported_media(
                        root,
                        derived,
                        path_key="path",
                        errors=errors,
                    )
                    relation = derived.get("derivedFromSha256")
                    relation_valid = (
                        media_sha256 is not None
                        and _is_sha256(relation)
                        and relation == media_sha256
                    )
                    if not relation_valid:
                        errors.add("export_derived_media_relation_mismatch")
                    if media_valid and derived_valid and relation_valid:
                        counters["mediaFilesChecked"] += 2
    except FileNotFoundError:
        errors.add("export_records_unavailable")
        return None
    except OSError:
        errors.add("export_records_invalid")
        return None
    return "sha256:" + digest.hexdigest()


def _verify_declared_file(
    root: Path,
    *,
    relative_path: str,
    declared_hash: object,
    declared_size: object,
    path_error: str,
    unavailable_error: str,
    hash_error: str,
    size_error: str,
    errors: set[str],
) -> None:
    path = _contained_export_path(root, relative_path)
    if path is None:
        errors.add(path_error)
        return
    actual = _file_sha256_and_size(path)
    if actual is None:
        errors.add(unavailable_error)
        return
    if not _is_sha256(declared_hash) or actual[0] != declared_hash:
        errors.add(hash_error)
    if _nonnegative_int(declared_size) is None or actual[1] != declared_size:
        errors.add(size_error)


def command_verify_export(args: argparse.Namespace) -> int:
    """Verify one existing v1 export without opening config or source data."""

    root = _canonical_export_root(args.output)
    counters = {"recordCount": 0, "mediaFilesChecked": 0}
    errors: set[str] = set()
    result: dict[str, Any] = {
        "status": "failed",
        "format": "unknown",
        **counters,
        "errors": [],
    }
    if root is None:
        errors.add("export_directory_unavailable")
    else:
        manifest = _verification_object(
            _contained_export_path(root, "manifest.json"),
            errors,
            "export_manifest",
        )
        if manifest is not None:
            format_value = manifest.get("format")
            specifications = {
                "wechat-direct-contact-export.v1": {
                    "stateFormat": "wechat-direct-contact-sync.v1",
                    "recordsPath": "messages.jsonl",
                    "recordsSha256": "messagesSha256",
                    "count": "messageCount",
                },
                "wechat-direct-moments-export.v1": {
                    "stateFormat": "wechat-direct-moments-sync.v1",
                    "recordsPath": "moments.jsonl",
                    "recordsSha256": "momentsSha256",
                    "count": "preservedMomentCount",
                },
            }
            specification = (
                specifications.get(format_value)
                if isinstance(format_value, str)
                else None
            )
            if specification is None:
                errors.add("export_format_unsupported")
            else:
                result["format"] = format_value
                manifest_copy = dict(manifest)
                manifest_hash = manifest_copy.pop("manifestSha256", None)
                if (
                    not _is_sha256(manifest_hash)
                    or _sha256(_canonical_bytes(manifest_copy)) != manifest_hash
                ):
                    errors.add("export_manifest_sha256_mismatch")
                state = _verification_object(
                    _contained_export_path(root, "state.json"),
                    errors,
                    "export_state",
                )
                if state is not None:
                    if state.get("format") != specification["stateFormat"]:
                        errors.add("export_state_format_mismatch")
                    if any(
                        key not in state
                        or key not in manifest
                        or _canonical_bytes(state[key]) != _canonical_bytes(manifest[key])
                        for key in (
                            "account",
                            "accountIdentityCommitment",
                            "contact",
                            "sourceFingerprint",
                        )
                    ):
                        errors.add("export_state_binding_mismatch")
                archive_path = manifest.get("archivePath")
                ai_path = manifest.get("aiDefaultPath")
                records_path = manifest.get(
                    "messagesPath"
                    if format_value == "wechat-direct-contact-export.v1"
                    else "momentsPath"
                )
                if archive_path != "context.md" or ai_path != "ai-context.md" or records_path != specification["recordsPath"]:
                    errors.add("export_layout_invalid")
                _verify_declared_file(
                    root,
                    relative_path="context.md",
                    declared_hash=manifest.get("archiveSha256"),
                    declared_size=manifest.get("archiveBytes"),
                    path_error="export_context_path_invalid",
                    unavailable_error="export_context_unavailable",
                    hash_error="export_context_sha256_mismatch",
                    size_error="export_context_size_mismatch",
                    errors=errors,
                )
                _verify_declared_file(
                    root,
                    relative_path="ai-context.md",
                    declared_hash=manifest.get("aiDefaultSha256"),
                    declared_size=manifest.get("aiDefaultBytes"),
                    path_error="export_ai_context_path_invalid",
                    unavailable_error="export_ai_context_unavailable",
                    hash_error="export_ai_context_sha256_mismatch",
                    size_error="export_ai_context_size_mismatch",
                    errors=errors,
                )
                records_digest = _verify_export_records(
                    root,
                    _contained_export_path(root, specification["recordsPath"]),
                    errors,
                    counters,
                )
                if (
                    records_digest is None
                    or not _is_sha256(manifest.get(specification["recordsSha256"]))
                    or records_digest != manifest.get(specification["recordsSha256"])
                ):
                    errors.add("export_records_sha256_mismatch")
                declared_count = _nonnegative_int(manifest.get(specification["count"]))
                if declared_count is None or declared_count != counters["recordCount"]:
                    errors.add("export_record_count_mismatch")
                if state is not None:
                    state_count = _nonnegative_int(state.get(specification["count"]))
                    if state_count is None or state_count != counters["recordCount"]:
                        errors.add("export_state_count_mismatch")
    result.update(counters)
    result["errors"] = sorted(errors)
    result["status"] = "success" if not errors else "failed"
    sys.stdout.buffer.write(_canonical_bytes(result))
    return 0 if result["status"] == "success" else 2


def _media_extension(payload: bytes, kind: object) -> str:
    if _is_tencent_silk(payload):
        return ".silk"
    signatures = (
        (b"\xff\xd8\xff", ".jpg"),
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
        (b"RIFF", ".webp" if payload[8:12] == b"WEBP" else ".wav"),
        (b"ID3", ".mp3"),
        (b"OggS", ".ogg"),
        (b"%PDF-", ".pdf"),
    )
    for signature, extension in signatures:
        if payload.startswith(signature):
            return extension
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        return ".mp4"
    return ".bin" if str(kind or "") != "voice" else ".audio"


def _local_time_label(value: object) -> tuple[str, str]:
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return "时间不确定", "??:??:??"
    current = datetime.fromtimestamp(timestamp, LOCAL_TIMEZONE)
    return current.strftime("%Y-%m-%d"), current.strftime("%H:%M:%S")


def _compact_message_text(message: Mapping[str, Any]) -> str:
    content = str(message.get("content") or "").replace("\x00", "").strip()
    if content:
        return content.replace("\r\n", "\n").replace("\r", "\n")
    media = list(message.get("media_manifest") or [])
    if media:
        kinds = "、".join(
            dict.fromkeys(str(item.get("kind") or "媒体") for item in media)
        )
        return f"[{kinds}]"
    gap = message.get("contentGap")
    return "[正文当前不可解析]" if gap else "[无文字正文]"


def _quote_target_map(
    messages: list[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        identity = message.get("nativeId")
        if (
            isinstance(identity, Mapping)
            and str(identity.get("kind") or "") == "server"
            and identity.get("value") is not None
        ):
            result[str(identity["value"])] = message
    return result


def _quote_context_line(
    message: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
) -> str | None:
    quote = message.get("quote")
    if not isinstance(quote, Mapping) or not quote.get("platformMessageId"):
        return None
    target = targets.get(str(quote["platformMessageId"]))
    if target is None:
        return "  ↳ 回复：[被引用消息不在当前本地范围]"
    sender = target.get("sender")
    speaker = (
        str(sender.get("displayName") or "身份未确定")
        if isinstance(sender, Mapping)
        else "身份未确定"
    )
    content = " ".join(_compact_message_text(target).split())
    if len(content) > 240:
        content = content[:239] + "…"
    return f"  ↳ 回复 {speaker}：{content}"


def _media_context_lines(
    message: Mapping[str, Any], *, limit: int | None = None
) -> list[str]:
    lines: list[str] = []
    labels = {
        "image": "图片",
        "emoji": "表情包",
        "voice": "语音",
        "video": "视频",
        "file": "文件",
    }
    media_items = list(message.get("media_manifest") or [])
    visible_items = media_items if limit is None else media_items[:limit]
    for media in visible_items:
        kind = str(media.get("kind") or "media")
        label = labels.get(kind, "媒体")
        path = media.get("exportedPath")
        derived = media.get("derivedVoiceWav")
        if kind == "voice" and isinstance(derived, Mapping) and derived.get("path"):
            path = derived["path"]
        if path:
            normalized = str(path).replace("\\", "/")
            if kind in {"image", "emoji"}:
                lines.append(f"  ![{label}]({normalized})")
            elif kind == "voice":
                lines.append(f"  [语音（可播放，按需转写）]({normalized})")
            else:
                lines.append(f"  [{label}]({normalized})")
        else:
            lines.append(f"  [{label}当前不可打开]")
    if limit is not None and len(media_items) > len(visible_items):
        lines.append(
            f"  [本条另有 {len(media_items) - len(visible_items)} 个媒体项；"
            "完整清单见 context.md]"
        )
    return lines


def _contact_ai_context(
    *,
    account: str,
    contact: Mapping[str, Any],
    messages: list[Mapping[str, Any]],
    quote_messages: list[Mapping[str, Any]] | None = None,
    full_archive: bool = False,
    omitted_messages: int = 0,
) -> str:
    name = str(contact.get("displayName") or contact.get("nickname") or "联系人")
    first = next((item.get("createTime") for item in messages if item.get("createTime")), None)
    last = next(
        (item.get("createTime") for item in reversed(messages) if item.get("createTime")),
        None,
    )
    first_date, first_time = _local_time_label(first)
    last_date, last_time = _local_time_label(last)
    lines = [
        f"# 微信对话：{name}",
        "",
        f"账号：{'主号' if account == 'primary' else '副号'}｜消息：{len(messages)}｜可见范围：{first_date} {first_time} — {last_date} {last_time}",
        (
            "这是全量本地档案；不要整份送入模型。日常先读 ai-context.md，"
            "需要更早内容时只搜索并读取本文件的命中附近。"
            if full_archive
            else (
                f"这是给 AI 的最近小上下文；为控制注意力已省略更早 {omitted_messages} 条。"
                "需要更早内容时只搜索 context.md 的命中附近。"
            )
        ),
        "",
    ]
    targets = _quote_target_map(quote_messages or messages)
    current_day: str | None = None
    for message in messages:
        day, clock = _local_time_label(message.get("createTime"))
        if day != current_day:
            lines.extend((f"## {day}", ""))
            current_day = day
        sender = message.get("sender")
        speaker = (
            str(sender.get("displayName") or "身份未确定")
            if isinstance(sender, Mapping)
            else str(message.get("senderRole") or "身份未确定")
        )
        content = _compact_message_text(message)
        if not full_archive and len(content) > MAX_AI_ITEM_CHARS:
            content = (
                content[:MAX_AI_ITEM_CHARS]
                + "\n[本条正文为控制注意力已截断；完整内容见 context.md]"
            )
        if "\n" in content:
            lines.append(f"[{clock}] {speaker}：")
            lines.extend(content.splitlines())
        else:
            lines.append(f"[{clock}] {speaker}：{content}")
        quote_line = _quote_context_line(message, targets)
        if quote_line:
            lines.append(quote_line)
        lines.extend(
            _media_context_lines(
                message,
                limit=(None if full_archive else MAX_AI_MEDIA_OCCURRENCES_PER_MESSAGE),
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bounded_contact_ai_context(
    *,
    account: str,
    contact: Mapping[str, Any],
    messages: list[Mapping[str, Any]],
) -> tuple[str, int]:
    window = list(messages[-DEFAULT_AI_CONTEXT_MESSAGES:])
    while True:
        rendered = _contact_ai_context(
            account=account,
            contact=contact,
            messages=window,
            quote_messages=messages,
            omitted_messages=max(0, len(messages) - len(window)),
        )
        if len(rendered.encode("utf-8")) <= MAX_AI_CONTEXT_BYTES or not window:
            return rendered, len(window)
        window = window[1:]


def _message_export_key(message: Mapping[str, Any]) -> str:
    identity = message.get("nativeId")
    if isinstance(identity, Mapping) and identity.get("kind") and identity.get("value"):
        if str(identity["kind"]) == "server":
            return "server:" + str(identity["value"])
        local_basis = "\0".join(
            (
                str(identity["value"]),
                str(message.get("createTime") or ""),
                str(message.get("sortSeq") or ""),
                str(message.get("senderUsername") or ""),
            )
        )
        return "local:" + hashlib.sha256(local_basis.encode("utf-8")).hexdigest()
    return "sha256:" + hashlib.sha256(_canonical_bytes(message)).hexdigest()


def _ordered_messages(values: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for _, item in sorted(
            values.items(),
            key=lambda pair: (
                pair[1].get("createTime") is None,
                int(pair[1].get("createTime") or 0),
                int(pair[1].get("sortSeq") or 0),
                pair[0],
            ),
        )
    ]


def _default_contact_export_path(account: str, contact_query: str) -> Path:
    identity = hashlib.sha256(
        _normalize_name(contact_query).encode("utf-8")
    ).hexdigest()[:16]
    return _default_export_root() / "contacts" / account / ("contact-" + identity)


def _sync_message_media(
    reader: DirectWeChatReader,
    message: Mapping[str, Any],
    output: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    result = dict(message)
    projected_media: list[dict[str, Any]] = []
    counters = {
        "mediaOccurrences": 0,
        "mediaCopied": 0,
        "mediaReused": 0,
        "mediaBytes": 0,
        "mediaUnavailable": 0,
        "voiceWavCreated": 0,
    }
    for source_media in message.get("media_manifest") or []:
        media = dict(source_media)
        counters["mediaOccurrences"] += 1
        locator = media.get("locator")
        if not media.get("openable") or not locator:
            counters["mediaUnavailable"] += 1
            projected_media.append(media)
            continue
        try:
            payload = reader.open_locator(str(locator))
        except Exception as exc:
            counters["mediaUnavailable"] += 1
            media["exportStatus"] = "open_failed"
            media["exportGap"] = type(exc).__name__
            projected_media.append(media)
            continue
        digest = hashlib.sha256(payload).hexdigest()
        extension = _media_extension(payload, media.get("kind"))
        relative = Path("media") / digest[:2] / (digest + extension)
        target = output / relative
        if target.is_file():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ProductError("sync_media_hash_conflict")
            counters["mediaReused"] += 1
        else:
            _write_bytes_atomic(target, payload)
            counters["mediaCopied"] += 1
            counters["mediaBytes"] += len(payload)
        media.update(
            {
                "exportStatus": "available_local",
                "exportedPath": relative.as_posix(),
                "bytes": len(payload),
                "sha256": "sha256:" + digest,
            }
        )
        if media.get("kind") == "voice" and _is_tencent_silk(payload):
            wav_relative = Path("media") / digest[:2] / (digest + ".wav")
            wav_target = output / wav_relative
            if not wav_target.is_file():
                temporary = wav_target.with_name(wav_target.name + ".incomplete")
                wav_target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _decode_voice_file(target, temporary)
                    temporary.replace(wav_target)
                except ProductError as exc:
                    media["voiceWavGap"] = str(exc)
                    temporary.unlink(missing_ok=True)
                else:
                    counters["voiceWavCreated"] += 1
            if wav_target.is_file():
                wav = wav_target.read_bytes()
                media["derivedVoiceWav"] = {
                    "path": wav_relative.as_posix(),
                    "bytes": len(wav),
                    "sha256": _sha256(wav),
                    "derivedFromSha256": "sha256:" + digest,
                }
        projected_media.append(media)
    if projected_media:
        result["media_manifest"] = projected_media
    return result, counters


def _sync_lock(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".sync.lock"
    try:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError as exc:
        raise ProductError("sync_already_running_or_stale_lock") from exc
    return lock


def command_sync_contact(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    cutoff_s = int(time.time())
    if getattr(args, "since", None) or getattr(args, "until", None):
        raise ProductError("sync_contact_first_run_must_be_full")
    if not 0 <= int(args.overlap_seconds) <= 31 * 86_400:
        raise ProductError("sync_overlap_seconds_invalid")
    config = _read_config(_resolve_config_path(getattr(args, "config", None)))
    output = (
        Path(args.output)
        if args.output
        else _default_contact_export_path(str(args.account), str(args.contact))
    ).resolve()
    if output.exists() and not output.is_dir():
        raise ProductError("sync_output_is_not_directory")
    state_path = output / "state.json"
    records_path = output / "messages.jsonl"
    context_path = output / "context.md"
    ai_context_path = output / "ai-context.md"
    existing_state = _read_json(state_path)
    if existing_state is None and output.exists() and any(output.iterdir()):
        raise ProductError("sync_output_not_initialized")
    if existing_state is None:
        label, reader, contact = _resolve_contact(
            config,
            account=args.account,
            query=args.contact,
            cutoff_s=cutoff_s,
        )
    else:
        label = str(existing_state.get("account") or "")
        if label not in ACCOUNT_LABELS or args.account not in ("auto", label):
            raise ProductError("sync_identity_mismatch")
        reader = _reader(config[label], cutoff_s)
        stored_contact = existing_state.get("contact")
        if not isinstance(stored_contact, Mapping):
            reader.close()
            raise ProductError("sync_state_invalid")
        contact = dict(stored_contact)
        if _normalize_name(args.contact) not in _contact_match_fields(contact):
            reader.close()
            raise ProductError("sync_identity_mismatch")
    lock: Path | None = None
    try:
        lock = _sync_lock(output)
        commitment = "sha256:" + reader.account_identity_commitment
        raw_contact_native_id = (
            existing_state.get("contactNativeId")
            if existing_state is not None
            else contact.get("nativeId")
        )
        contact_native_id = str(raw_contact_native_id or "")
        if not contact_native_id:
            raise ProductError("sync_state_invalid")
        source_fingerprint = reader.contact_source_fingerprint(contact_native_id)
        if existing_state is not None:
            if (
                existing_state.get("format") != "wechat-direct-contact-sync.v1"
                or existing_state.get("account") != label
                or existing_state.get("accountIdentityCommitment") != commitment
                or existing_state.get("contactNativeId") != contact_native_id
            ):
                raise ProductError("sync_identity_mismatch")
            mode = "full_reconcile" if getattr(args, "full_reconcile", False) else "incremental"
            prior_sort = existing_state.get("sortSeqWatermark")
            prior_time = int(existing_state.get("lastCreateTimeS") or 0)
            replay_floor = existing_state.get("sortSeqReplayFloor")
            source_catalog_unchanged = (
                existing_state.get("messageSourceCatalogSha256")
                == source_fingerprint.get("messageSourceCatalogSha256")
            )
            if mode == "full_reconcile":
                since_s = None
                fetch_sort_cursor = None
            elif (
                replay_floor is not None
                and bool(existing_state.get("sortCursorEligible"))
                and source_catalog_unchanged
                and int(source_fingerprint.get("messageSourceCount") or 0) == 1
            ):
                since_s = None
                fetch_sort_cursor = max(0, int(replay_floor) - 1)
            else:
                since_s = max(0, prior_time - int(args.overlap_seconds))
                fetch_sort_cursor = None
        else:
            mode = "full"
            prior_sort = None
            fetch_sort_cursor = None
            source_catalog_unchanged = False
            since_s = None
        manifest_path = output / "manifest.json"
        if (
            existing_state is not None
            and mode != "full_reconcile"
            and existing_state.get("sourceFingerprint") == source_fingerprint.get("sha256")
            and records_path.is_file()
            and context_path.is_file()
            and ai_context_path.is_file()
            and manifest_path.is_file()
        ):
            verified_fingerprint = reader.contact_source_fingerprint(contact_native_id)
            if verified_fingerprint.get("sha256") != source_fingerprint.get("sha256"):
                source_fingerprint = verified_fingerprint
            else:
                manifest = _read_json(manifest_path)
                if manifest is None:
                    raise ProductError("sync_manifest_invalid")
                if (
                    manifest.get("sourceFingerprint")
                    != existing_state.get("sourceFingerprint")
                    or int(manifest.get("messageCount") or 0)
                    != int(existing_state.get("messageCount") or 0)
                ):
                    raise ProductError("sync_manifest_state_mismatch")
                receipt = {
                    "status": "success",
                    "mode": "incremental",
                    "noChange": True,
                    "sourceMetadataFastPath": True,
                    "account": label,
                    "accountIdentityCommitment": commitment,
                    "contact": contact,
                    "output": os.fspath(output),
                    "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
                    "sourceCandidateMessages": 0,
                    "newMessages": 0,
                    "updatedMessages": 0,
                    "totalMessages": int(existing_state.get("messageCount") or 0),
                    "aiDefaultPath": "ai-context.md",
                    "aiDefaultBytes": int(manifest.get("aiDefaultBytes") or 0),
                    "mediaOccurrences": 0,
                    "mediaCopied": 0,
                    "mediaReused": 0,
                    "mediaBytes": 0,
                    "mediaUnavailable": 0,
                    "voiceWavCreated": 0,
                    "senderRoleCounts": manifest.get("senderRoleCounts"),
                    "unknownSenderCount": int(manifest.get("unknownSenderCount") or 0),
                    "unlabeledGroupMemberCount": int(
                        manifest.get("unlabeledGroupMemberCount") or 0
                    ),
                    "unavailableMediaCount": int(
                        manifest.get("unavailableMediaCount") or 0
                    ),
                    "sourceFingerprint": source_fingerprint.get("sha256"),
                    "sourceFingerprintFiles": source_fingerprint.get("fileCount"),
                    "manifestSha256": manifest.get("manifestSha256"),
                    "historicalMutationCoverage": manifest.get(
                        "historicalMutationCoverage"
                    ),
                    "incrementalCursorMode": manifest.get(
                        "incrementalCursorMode"
                    ),
                    "undatedIncrementalCoverage": manifest.get(
                        "undatedIncrementalCoverage"
                    ),
                    "historicalMediaRefreshCoverage": manifest.get(
                        "historicalMediaRefreshCoverage"
                    ),
                }
                _write_json_atomic(output / "last-run.json", receipt)
                sys.stdout.buffer.write(_canonical_bytes(receipt))
                return 0
        end_s = cutoff_s
        if since_s is not None and since_s > end_s:
            raise ProductError("time_window_reversed")
        contact_directory = _contact_directory(reader, contact_native_id)
        refreshed = next(
            (
                item
                for item in contact_directory
                if str(item.get("nativeId")) == contact_native_id
            ),
            None,
        )
        if refreshed is not None:
            contact = refreshed
        fetched = reader.fetch_messages(
            contact_native_id,
            since_s=since_s,
            end_s=end_s,
            since_sort_seq=fetch_sort_cursor,
            limit=None,
            exact_media_lookup=True,
        )
        if (
            reader.contact_source_fingerprint(contact_native_id).get("sha256")
            != source_fingerprint.get("sha256")
        ):
            raise ProductError("source_changed_during_sync_retry")
        contacts = {
            str(item["nativeId"]): item
            for item in contact_directory
        }
        candidates = [
            _message_receipt(
                item,
                contacts=contacts,
                selected_contact=contact,
                account=label,
            )
            for item in fetched.get("messages") or []
        ]
        existing_rows = _read_jsonl(records_path)
        merged = {_message_export_key(item): item for item in existing_rows}
        new_count = 0
        updated_count = 0
        media_totals = {
            "mediaOccurrences": 0,
            "mediaCopied": 0,
            "mediaReused": 0,
            "mediaBytes": 0,
            "mediaUnavailable": 0,
            "voiceWavCreated": 0,
        }
        for candidate in candidates:
            key = _message_export_key(candidate)
            previous = merged.get(key)
            needs_media_retry = previous is not None and any(
                media.get("openable")
                and media.get("exportStatus") != "available_local"
                for media in previous.get("media_manifest") or []
            )
            if (
                previous is not None
                and previous.get("messageSha256") == candidate.get("messageSha256")
                and not needs_media_retry
            ):
                continue
            projected, media_counts = _sync_message_media(reader, candidate, output)
            for name, value in media_counts.items():
                media_totals[name] += value
            if previous is None:
                new_count += 1
            else:
                updated_count += 1
            merged[key] = projected
        ordered = _ordered_messages(merged)
        metadata_changed = existing_state is not None and existing_state.get(
            "contact"
        ) != _safe_contact(contact, label)
        changed = (
            existing_state is None
            or new_count > 0
            or updated_count > 0
            or metadata_changed
            or not records_path.is_file()
            or not context_path.is_file()
            or not ai_context_path.is_file()
        )
        ai_context, ai_message_count = _bounded_contact_ai_context(
            account=label,
            contact=contact,
            messages=ordered,
        )
        if changed:
            _write_jsonl_atomic(records_path, ordered)
            _write_text_atomic(
                context_path,
                _contact_ai_context(
                    account=label,
                    contact=contact,
                    messages=ordered,
                    full_archive=True,
                ),
            )
            _write_text_atomic(ai_context_path, ai_context)
        sort_values = [
            int(item["sortSeq"])
            for item in ordered
            if item.get("sortSeq") is not None
        ]
        time_values = [
            int(item["createTime"])
            for item in ordered
            if item.get("createTime") is not None
        ]
        overlap_start = max(time_values, default=0) - int(args.overlap_seconds)
        replay_sort_values = [
            int(item["sortSeq"])
            for item in ordered
            if item.get("sortSeq") is not None
            and (
                item.get("createTime") is None
                or int(item.get("createTime") or 0) >= overlap_start
            )
        ]
        sort_cursor_eligible = (
            int(source_fingerprint.get("messageSourceCount") or 0) == 1
            and (
                mode in {"full", "full_reconcile"}
                or (
                    bool(existing_state and existing_state.get("sortCursorEligible"))
                    and source_catalog_unchanged
                )
            )
        )
        unknown_senders = sum(
            1 for item in ordered if item.get("senderRole") == "unknown"
        )
        sender_role_counts = _sender_role_counts(ordered)
        unlabeled_group_members = sum(
            1
            for item in ordered
            if isinstance(item.get("sender"), Mapping)
            and item["sender"].get("labelGap") is not None
        )
        unavailable_media = sum(
            1
            for item in ordered
            for media in item.get("media_manifest") or []
            if media.get("exportStatus") != "available_local"
        )
        state = {
            "format": "wechat-direct-contact-sync.v1",
            "account": label,
            "accountIdentityCommitment": commitment,
            "contactNativeId": contact_native_id,
            "contact": _safe_contact(contact, label),
            "lastSourceSnapshotCutoffS": cutoff_s,
            "lastRequestedUntilS": end_s,
            "lastCreateTimeS": max(time_values, default=0),
            "sortSeqWatermark": max(sort_values, default=prior_sort),
            "sortSeqReplayFloor": min(
                replay_sort_values,
                default=max(sort_values, default=prior_sort),
            ),
            "messageCount": len(ordered),
            "overlapSeconds": int(args.overlap_seconds),
            "sourceFingerprint": source_fingerprint.get("sha256"),
            "messageSourceCount": int(
                source_fingerprint.get("messageSourceCount") or 0
            ),
            "messageSourceCatalogSha256": source_fingerprint.get(
                "messageSourceCatalogSha256"
            ),
            "sortCursorEligible": sort_cursor_eligible,
        }
        messages_sha = _sha256(records_path.read_bytes())
        context_bytes = context_path.read_bytes()
        ai_context_bytes = ai_context_path.read_bytes()
        manifest = {
            "format": "wechat-direct-contact-export.v1",
            "account": label,
            "accountIdentityCommitment": commitment,
            "contact": _safe_contact(contact, label),
            "historyScope": "full_local_history_then_indexed_cursor_overlap_increment",
            "historicalMutationCoverage": (
                "explicit_full_reconcile"
                if mode == "full_reconcile"
                else "first_full_snapshot"
                if mode == "full"
                else "recent_cursor_overlap_only"
                if sort_cursor_eligible
                else "recent_time_overlap_cursor_unavailable"
            ),
            "incrementalCursorMode": (
                "single_source_indexed_sort_seq"
                if sort_cursor_eligible
                else "bounded_create_time_overlap_requires_full_reconcile_for_cursor_reset"
            ),
            "undatedIncrementalCoverage": (
                "included_by_single_source_sort_cursor"
                if sort_cursor_eligible
                else "first_full_or_explicit_full_reconcile_only"
            ),
            "historicalMediaRefreshCoverage": (
                "increment_window_or_explicit_full_reconcile"
            ),
            "messageCount": len(ordered),
            "senderRoleCounts": sender_role_counts,
            "aiDefaultPath": "ai-context.md",
            "aiDefaultBytes": len(ai_context_bytes),
            "aiDefaultSha256": _sha256(ai_context_bytes),
            "aiDefaultMessageCount": ai_message_count,
            "archivePath": "context.md",
            "archiveBytes": len(context_bytes),
            "archiveSha256": _sha256(context_bytes),
            "messagesPath": "messages.jsonl",
            "messagesSha256": messages_sha,
            "lastCreateTimeS": state["lastCreateTimeS"],
            "unknownSenderCount": unknown_senders,
            "unlabeledGroupMemberCount": unlabeled_group_members,
            "unavailableMediaCount": unavailable_media,
            "sourceFingerprint": source_fingerprint.get("sha256"),
            "sourceFingerprintFiles": source_fingerprint.get("fileCount"),
        }
        manifest["manifestSha256"] = _sha256(_canonical_bytes(manifest))
        _write_json_atomic(output / "manifest.json", manifest)
        _write_json_atomic(state_path, state)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        receipt = {
            "status": "success",
            "mode": mode,
            "noChange": not changed,
            "account": label,
            "accountIdentityCommitment": commitment,
            "contact": _safe_contact(contact, label),
            "output": os.fspath(output),
            "elapsedMs": elapsed_ms,
            "sourceCandidateMessages": len(candidates),
            "newMessages": new_count,
            "updatedMessages": updated_count,
            "totalMessages": len(ordered),
            "aiDefaultPath": "ai-context.md",
            "aiDefaultBytes": len(ai_context_bytes),
            **media_totals,
            "senderRoleCounts": sender_role_counts,
            "unknownSenderCount": unknown_senders,
            "unlabeledGroupMemberCount": unlabeled_group_members,
            "unavailableMediaCount": unavailable_media,
            "manifestSha256": manifest["manifestSha256"],
            "historicalMutationCoverage": manifest["historicalMutationCoverage"],
            "incrementalCursorMode": manifest["incrementalCursorMode"],
            "undatedIncrementalCoverage": manifest[
                "undatedIncrementalCoverage"
            ],
            "historicalMediaRefreshCoverage": manifest[
                "historicalMediaRefreshCoverage"
            ],
        }
        _write_json_atomic(output / "last-run.json", receipt)
        sys.stdout.buffer.write(_canonical_bytes(receipt))
        return 0
    finally:
        reader.close()
        if lock is not None:
            lock.unlink(missing_ok=True)


def _moment_export_key(moment: Mapping[str, Any]) -> str:
    native_id = str(moment.get("nativeId") or "")
    if native_id:
        return "native:" + native_id
    return "sha256:" + hashlib.sha256(_canonical_bytes(moment)).hexdigest()


def _moments_ai_context(
    *,
    account: str,
    contact: Mapping[str, Any] | None,
    moments: list[Mapping[str, Any]],
    full_archive: bool = False,
    omitted_moments: int = 0,
) -> str:
    subject = (
        str(contact.get("displayName") or contact.get("nickname") or "联系人")
        if contact is not None
        else "本机当前可见缓存"
    )
    lines = [
        f"# 微信朋友圈：{subject}",
        "",
        f"账号：{'主号' if account == 'primary' else '副号'}｜当前本机可见条目：{len(moments)}｜范围不是朋友圈全历史",
        (
            "这是当前本机缓存的全量档案；不要整份送入模型。日常先读 ai-context.md，"
            "需要更早内容时只搜索并读取本文件的命中附近。"
            if full_archive
            else (
                f"这是给 AI 的最近小上下文；为控制注意力已省略更早 {omitted_moments} 条。"
                "需要更早内容时只搜索 context.md 的命中附近。"
            )
        ),
        "",
    ]
    current_day: str | None = None
    for moment in moments:
        day, clock = _local_time_label(moment.get("createTime"))
        if day != current_day:
            lines.extend((f"## {day}", ""))
            current_day = day
        current_contact = moment.get("contact")
        speaker = (
            str(current_contact.get("displayName") or moment.get("nickname") or "身份未确定")
            if isinstance(current_contact, Mapping)
            else str(moment.get("nickname") or "身份未确定")
        )
        parts = []
        for key in ("content", "title", "description"):
            value = " ".join(str(moment.get(key) or "").split())
            if value and value not in parts:
                parts.append(value)
        body = "｜".join(parts) or "[无文字正文]"
        if not full_archive and len(body) > MAX_AI_ITEM_CHARS:
            body = (
                body[:MAX_AI_ITEM_CHARS]
                + "[本条正文为控制注意力已截断；完整内容见 context.md]"
            )
        lines.append(f"[{clock}] {speaker}：{body}")
        lines.extend(_media_context_lines(moment))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bounded_moments_ai_context(
    *,
    account: str,
    contact: Mapping[str, Any] | None,
    moments: list[Mapping[str, Any]],
) -> tuple[str, int]:
    window = list(moments[-DEFAULT_AI_CONTEXT_MESSAGES:])
    while True:
        rendered = _moments_ai_context(
            account=account,
            contact=contact,
            moments=window,
            omitted_moments=max(0, len(moments) - len(window)),
        )
        if len(rendered.encode("utf-8")) <= MAX_AI_CONTEXT_BYTES or not window:
            return rendered, len(window)
        window = window[1:]


def _default_moments_export_path(
    account: str,
    contact_query: str | None,
    *,
    self_requested: bool = False,
) -> Path:
    if self_requested:
        leaf = "self"
    elif not contact_query:
        leaf = "all-current-cache"
    else:
        identity = hashlib.sha256(
            _normalize_name(contact_query).encode("utf-8")
        ).hexdigest()[:16]
        leaf = "contact-" + identity
    return _default_export_root() / "moments" / account / leaf


def command_sync_moments(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    cutoff_s = int(time.time())
    config = _read_config(_resolve_config_path(getattr(args, "config", None)))
    self_requested = bool(getattr(args, "self", False))
    if str(args.account) not in ACCOUNT_LABELS:
        raise ProductError("moments_explicit_account_required")
    if self_requested and args.contact:
        raise ProductError("moments_subject_selector_conflict")
    output = (
        Path(args.output)
        if args.output
        else _default_moments_export_path(
            str(args.account),
            args.contact,
            self_requested=self_requested,
        )
    ).resolve()
    if output.exists() and not output.is_dir():
        raise ProductError("sync_output_is_not_directory")
    state_path = output / "state.json"
    records_path = output / "moments.jsonl"
    context_path = output / "context.md"
    ai_context_path = output / "ai-context.md"
    existing_state = _read_json(state_path)
    if existing_state is None and output.exists() and any(output.iterdir()):
        raise ProductError("sync_output_not_initialized")
    contact: dict[str, Any] | None = None
    if existing_state is not None:
        label = str(existing_state.get("account") or "")
        if label not in ACCOUNT_LABELS or args.account != label:
            raise ProductError("sync_identity_mismatch")
        reader = _reader(config[label], cutoff_s)
        stored_contact = existing_state.get("contact")
        if stored_contact is not None and not isinstance(stored_contact, Mapping):
            reader.close()
            raise ProductError("sync_state_invalid")
        contact = dict(stored_contact) if isinstance(stored_contact, Mapping) else None
        if self_requested:
            _verify_moments_self_identity(config[label], reader)
            if (
                contact is None
                or not contact.get("isSelf")
                or str(contact.get("nativeId") or "")
                != reader.moments_self_native_id
            ):
                reader.close()
                raise ProductError("sync_identity_mismatch")
        elif args.contact and (
            contact is None
            or _normalize_name(args.contact) not in _contact_match_fields(contact)
        ):
            reader.close()
            raise ProductError("sync_identity_mismatch")
        elif not args.contact and contact is not None:
            reader.close()
            raise ProductError("sync_identity_mismatch")
    elif args.contact or self_requested:
        label, reader, contact = _resolve_moments_subject(
            config,
            account=str(args.account),
            query=args.contact,
            self_requested=self_requested,
            cutoff_s=cutoff_s,
        )
    else:
        label = str(args.account)
        reader = _reader(config[label], cutoff_s)
    lock: Path | None = None
    try:
        lock = _sync_lock(output)
        commitment = "sha256:" + reader.account_identity_commitment
        contact_native_id = (
            str(existing_state.get("contactNativeId"))
            if existing_state is not None
            and existing_state.get("contactNativeId") is not None
            else str(contact["nativeId"])
            if contact
            else None
        )
        if existing_state is not None and (
            existing_state.get("format") != "wechat-direct-moments-sync.v1"
            or existing_state.get("account") != label
            or existing_state.get("accountIdentityCommitment") != commitment
            or existing_state.get("contactNativeId") != contact_native_id
        ):
            raise ProductError("sync_identity_mismatch")
        source_fingerprint = reader.moments_source_fingerprint()
        manifest_path = output / "manifest.json"
        if (
            existing_state is not None
            and existing_state.get("sourceFingerprint") == source_fingerprint.get("sha256")
            and records_path.is_file()
            and context_path.is_file()
            and ai_context_path.is_file()
            and manifest_path.is_file()
            and reader.moments_source_fingerprint().get("sha256")
            == source_fingerprint.get("sha256")
        ):
            manifest = _read_json(manifest_path)
            if manifest is None:
                raise ProductError("sync_manifest_invalid")
            if (
                manifest.get("sourceFingerprint")
                != existing_state.get("sourceFingerprint")
                or int(manifest.get("preservedMomentCount") or 0)
                != int(existing_state.get("preservedMomentCount") or 0)
            ):
                raise ProductError("sync_manifest_state_mismatch")
            receipt = {
                "status": "success",
                "mode": "incremental",
                "noChange": True,
                "sourceMetadataFastPath": True,
                "account": label,
                "accountIdentityCommitment": commitment,
                "contact": contact,
                "output": os.fspath(output),
                "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
                "scannedRows": 0,
                "currentCacheRows": int(manifest.get("currentCacheRows") or 0),
                "newMoments": 0,
                "updatedMoments": 0,
                "removedMoments": 0,
                "preservedMoments": int(
                    existing_state.get("preservedMomentCount") or 0
                ),
                "targetCacheStatus": manifest.get("targetCacheStatus"),
                "unopenedMediaCount": int(manifest.get("unopenedMediaCount") or 0),
                "historyScope": "current_local_cache_only",
                "aiDefaultPath": "ai-context.md",
                "aiDefaultBytes": int(manifest.get("aiDefaultBytes") or 0),
                "sourceFingerprint": source_fingerprint.get("sha256"),
                "sourceFingerprintFiles": source_fingerprint.get("fileCount"),
                "manifestSha256": manifest.get("manifestSha256"),
            }
            _write_json_atomic(output / "last-run.json", receipt)
            sys.stdout.buffer.write(_canonical_bytes(receipt))
            return 0
        contact_directory = [
            item
            for item in reader.list_contacts(include_unregistered=True)
            if item.get("nativeId")
        ]
        if contact_native_id and not self_requested:
            refreshed = next(
                (
                    item
                    for item in contact_directory
                    if str(item.get("nativeId")) == contact_native_id
                ),
                None,
            )
            if refreshed is not None:
                contact = refreshed
        source = reader.list_moments(
            since_s=0,
            end_s=cutoff_s,
            username=contact_native_id,
            limit=None,
        )
        if (
            reader.moments_source_fingerprint().get("sha256")
            != source_fingerprint.get("sha256")
        ):
            raise ProductError("source_changed_during_sync_retry")
        contacts = {
            str(item["nativeId"]): _safe_contact(item, label)
            for item in contact_directory
        }
        if contact is not None and contact_native_id:
            contacts[contact_native_id] = _safe_contact(contact, label)
        current: list[dict[str, Any]] = []
        for item in source.get("moments") or []:
            projected = dict(item)
            projected["contact"] = contacts.get(
                str(item.get("username") or ""),
                {
                    "account": label,
                    "nativeId": item.get("username"),
                    "displayName": item.get("nickname"),
                },
            )
            current.append(projected)
        target_cache_status = (
            "target_cached"
            if contact is not None and current
            else "target_not_in_current_local_cache"
            if contact is not None
            else "account_cache_read"
        )
        source_gaps = list(source.get("gaps") or [])
        if contact is not None and not current:
            source_gaps.append(
                {
                    "kind": "target_moments_not_in_current_local_cache",
                    "target": "self" if contact.get("isSelf") else "contact",
                    "nextAction": (
                        "open_the_target_moments_profile_in_this_exact_account_then_retry"
                    ),
                }
            )
        existing_rows = _read_jsonl(records_path)
        previous_by_key = {_moment_export_key(item): item for item in existing_rows}
        current_by_key = {_moment_export_key(item): item for item in current}
        new_count = len(current_by_key.keys() - previous_by_key.keys())
        removed_count = len(previous_by_key.keys() - current_by_key.keys())
        updated_count = sum(
            1
            for key in current_by_key.keys() & previous_by_key.keys()
            if current_by_key[key] != previous_by_key[key]
        )
        ordered = sorted(
            (dict(item) for item in current_by_key.values()),
            key=lambda item: (
                int(item.get("createTime") or 0),
                str(item.get("nativeId") or ""),
            ),
        )
        metadata_changed = existing_state is not None and existing_state.get(
            "contact"
        ) != (_safe_contact(contact, label) if contact else None)
        changed = (
            existing_state is None
            or new_count > 0
            or updated_count > 0
            or removed_count > 0
            or metadata_changed
            or not records_path.is_file()
            or not context_path.is_file()
            or not ai_context_path.is_file()
        )
        ai_context, ai_moment_count = _bounded_moments_ai_context(
            account=label,
            contact=contact,
            moments=ordered,
        )
        if changed:
            _write_jsonl_atomic(records_path, ordered)
            _write_text_atomic(
                context_path,
                _moments_ai_context(
                    account=label,
                    contact=contact,
                    moments=ordered,
                    full_archive=True,
                ),
            )
            _write_text_atomic(ai_context_path, ai_context)
        state = {
            "format": "wechat-direct-moments-sync.v1",
            "account": label,
            "accountIdentityCommitment": commitment,
            "contactNativeId": contact_native_id,
            "contact": _safe_contact(contact, label) if contact else None,
            "historyScope": "current_local_cache_only",
            "lastSourceSnapshotCutoffS": cutoff_s,
            "actualVisibleCutoffS": source.get("sourceVisibleCutoffS"),
            "preservedMomentCount": len(ordered),
            "targetCacheStatus": target_cache_status,
            "sourceFingerprint": source_fingerprint.get("sha256"),
        }
        records_sha = _sha256(records_path.read_bytes())
        context_bytes = context_path.read_bytes()
        ai_context_bytes = ai_context_path.read_bytes()
        unopened_media = sum(
            1
            for item in ordered
            for media in item.get("media_manifest") or []
            if not media.get("openable")
        )
        manifest = {
            "format": "wechat-direct-moments-export.v1",
            "account": label,
            "accountIdentityCommitment": commitment,
            "contact": _safe_contact(contact, label) if contact else None,
            "historyScope": "current_local_cache_only",
            "currentCacheRows": len(current),
            "preservedMomentCount": len(ordered),
            "targetCacheStatus": target_cache_status,
            "aiDefaultPath": "ai-context.md",
            "aiDefaultBytes": len(ai_context_bytes),
            "aiDefaultSha256": _sha256(ai_context_bytes),
            "aiDefaultMomentCount": ai_moment_count,
            "archivePath": "context.md",
            "archiveBytes": len(context_bytes),
            "archiveSha256": _sha256(context_bytes),
            "momentsPath": "moments.jsonl",
            "momentsSha256": records_sha,
            "unopenedMediaCount": unopened_media,
            "gaps": source_gaps,
            "sourceFingerprint": source_fingerprint.get("sha256"),
            "sourceFingerprintFiles": source_fingerprint.get("fileCount"),
        }
        manifest["manifestSha256"] = _sha256(_canonical_bytes(manifest))
        _write_json_atomic(output / "manifest.json", manifest)
        _write_json_atomic(state_path, state)
        receipt = {
            "status": "success",
            "mode": "full" if existing_state is None else "incremental",
            "noChange": not changed,
            "account": label,
            "accountIdentityCommitment": commitment,
            "contact": _safe_contact(contact, label) if contact else None,
            "output": os.fspath(output),
            "elapsedMs": round((time.perf_counter() - started) * 1000, 3),
            "scannedRows": source.get("scannedRows"),
            "currentCacheRows": len(current),
            "newMoments": new_count,
            "updatedMoments": updated_count,
            "removedMoments": removed_count,
            "preservedMoments": len(ordered),
            "targetCacheStatus": target_cache_status,
            "unopenedMediaCount": unopened_media,
            "historyScope": "current_local_cache_only",
            "aiDefaultPath": "ai-context.md",
            "aiDefaultBytes": len(ai_context_bytes),
            "sourceFingerprint": source_fingerprint.get("sha256"),
            "sourceFingerprintFiles": source_fingerprint.get("fileCount"),
            "manifestSha256": manifest["manifestSha256"],
        }
        _write_json_atomic(output / "last-run.json", receipt)
        sys.stdout.buffer.write(_canonical_bytes(receipt))
        return 0
    finally:
        reader.close()
        if lock is not None:
            lock.unlink(missing_ok=True)


def command_preserve(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists():
        raise ProductError("preservation_output_already_exists")
    context = _context_result(args)
    incomplete = output.with_name(output.name + ".incomplete")
    if incomplete.exists():
        raise ProductError("preservation_incomplete_output_exists")
    incomplete.mkdir(parents=True)
    try:
        files: list[dict[str, Any]] = []
        media_files: list[dict[str, Any]] = []
        derived_files: list[dict[str, Any]] = []
        media_directory = incomplete / "media"
        config = _read_config(_resolve_config_path(getattr(args, "config", None)))
        with _reader(config[str(context["account"])], int(time.time())) as reader:
            occurrence = 0
            for message in context["messages"]:
                native_id = message.get("nativeId") or {}
                native_label = _safe_filename(
                    f"{native_id.get('kind')}-{native_id.get('value')}"
                )
                for media in message.get("media_manifest") or []:
                    if not media.get("openable") or not media.get("locator"):
                        continue
                    occurrence += 1
                    try:
                        payload = reader.open_locator(str(media["locator"]))
                    except Exception as exc:
                        context["gaps"].append(
                            {
                                "kind": "preservation_media_open_failed",
                                "message": native_id,
                                "mediaKind": media.get("kind"),
                                "reason": type(exc).__name__,
                            }
                        )
                        continue
                    media_directory.mkdir(exist_ok=True)
                    extension = (
                        ".silk"
                        if media.get("kind") == "voice"
                        and _is_tencent_silk(payload)
                        else ".bin"
                    )
                    filename = (
                        f"{native_label}-{occurrence:03d}-"
                        f"{_safe_filename(media.get('kind'))}{extension}"
                    )
                    relative = Path("media") / filename
                    target = incomplete / relative
                    with target.open("xb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    record = {
                        "path": relative.as_posix(),
                        "bytes": len(payload),
                        "sha256": _sha256(payload),
                        "messageNativeId": native_id,
                        "mediaId": media.get("mediaId"),
                        "kind": media.get("kind"),
                        "locatorSha256": _sha256(
                            str(media["locator"]).encode("utf-8")
                        ),
                        "derivedPaths": [],
                    }
                    media_files.append(record)
                    files.append(
                        {
                            "path": record["path"],
                            "bytes": record["bytes"],
                            "sha256": record["sha256"],
                        }
                    )
                    if media.get("kind") == "voice" and _is_tencent_silk(payload):
                        decoded_relative = relative.with_suffix(".wav")
                        decoded_target = incomplete / decoded_relative
                        try:
                            _decode_voice_file(target, decoded_target)
                        except ProductError as exc:
                            context["gaps"].append(
                                {
                                    "kind": "voice_decode_failed",
                                    "message": native_id,
                                    "mediaKind": "voice",
                                    "reason": str(exc),
                                }
                            )
                        else:
                            decoded = decoded_target.read_bytes()
                            decoded_record = {
                                "path": decoded_relative.as_posix(),
                                "bytes": len(decoded),
                                "sha256": _sha256(decoded),
                                "derivedFromSha256": record["sha256"],
                                "messageNativeId": native_id,
                                "kind": "voice_wav",
                            }
                            record["derivedPaths"].append(
                                decoded_relative.as_posix()
                            )
                            derived_files.append(decoded_record)
                            files.append(
                                {
                                    "path": decoded_record["path"],
                                    "bytes": decoded_record["bytes"],
                                    "sha256": decoded_record["sha256"],
                                }
                            )

        context.pop("manifestSha256", None)
        context["manifestSha256"] = _sha256(_canonical_bytes(context))
        _write_json(incomplete / "messages.json", context)
        files.insert(
            0,
            {
                "path": "messages.json",
                "bytes": (incomplete / "messages.json").stat().st_size,
                "sha256": _sha256((incomplete / "messages.json").read_bytes()),
            },
        )
        manifest = {
            "format": "wechat-direct-preservation.v1",
            "createdAtS": int(time.time()),
            "account": context["account"],
            "accountIdentityCommitment": context[
                "accountIdentityCommitment"
            ],
            "contact": context["contact"],
            "requestedWindow": context["requestedWindow"],
            "actualVisibleCutoffS": context["actualVisibleCutoffS"],
            "messageCount": context["returnedMessages"],
            "contextManifestSha256": context["manifestSha256"],
            "files": files,
            "mediaFiles": media_files,
            "derivedFiles": derived_files,
            "gaps": context["gaps"],
        }
        manifest["manifestSha256"] = _sha256(_canonical_bytes(manifest))
        _write_json(incomplete / "manifest.json", manifest)
        incomplete.replace(output)
    except Exception:
        shutil.rmtree(incomplete, ignore_errors=True)
        raise
    sys.stdout.buffer.write(
        _canonical_bytes(
            {
                "status": "success",
                "output": os.fspath(output.resolve()),
                "manifestSha256": manifest["manifestSha256"],
            }
        )
    )
    return 0


def _add_config_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("--config")


def _add_context_arguments(command: argparse.ArgumentParser) -> None:
    _add_config_argument(command)
    command.add_argument(
        "--account", choices=("auto", *ACCOUNT_LABELS), default="auto"
    )
    command.add_argument("--contact", required=True)
    command.add_argument("--since")
    command.add_argument("--until")
    command.add_argument("--around")
    command.add_argument("--contains")
    command.add_argument("--lookback-days", type=int, default=7)
    command.add_argument("--scan-limit", type=int, default=120)
    command.add_argument("--return-limit", type=int, default=24)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Direct local WeChat reads")
    commands = root.add_subparsers(dest="command", required=True)

    context = commands.add_parser("context", help="read one bounded chat context")
    _add_context_arguments(context)
    context.set_defaults(handler=command_context)

    doctor = commands.add_parser(
        "doctor", help="check local readiness without reading WeChat message bodies"
    )
    _add_config_argument(doctor)
    doctor.set_defaults(handler=command_doctor)

    verify_export = commands.add_parser(
        "verify-export", help="verify one existing contact or Moments export offline"
    )
    verify_export.add_argument("--output", required=True)
    verify_export.set_defaults(handler=command_verify_export)

    moments = commands.add_parser(
        "moments", help="read a bounded view of Moments cached on this device"
    )
    _add_config_argument(moments)
    moments.add_argument("--account", choices=ACCOUNT_LABELS, required=True)
    moments_subject = moments.add_mutually_exclusive_group()
    moments_subject.add_argument("--contact")
    moments_subject.add_argument(
        "--self", action="store_true", help="read this account's own cached Moments"
    )
    moments.add_argument("--since")
    moments.add_argument("--until")
    moments.add_argument("--lookback-days", type=int, default=30)
    moments.add_argument("--limit", type=int, default=20)
    moments.set_defaults(handler=command_moments)

    sync_contact = commands.add_parser(
        "sync-contact",
        help="first export one contact fully, then replay the same command incrementally",
    )
    _add_config_argument(sync_contact)
    sync_contact.add_argument(
        "--account", choices=("auto", *ACCOUNT_LABELS), default="auto"
    )
    sync_contact.add_argument("--contact", required=True)
    sync_contact.add_argument("--output")
    sync_contact.add_argument(
        "--overlap-seconds",
        type=int,
        default=DEFAULT_INCREMENT_OVERLAP_SECONDS,
        help="bounded overlap used after the first run",
    )
    sync_contact.add_argument(
        "--full-reconcile",
        action="store_true",
        help="explicitly rescan the full local history instead of the fast cursor replay",
    )
    sync_contact.set_defaults(handler=command_sync_contact)

    sync_moments = commands.add_parser(
        "sync-moments",
        help="export and replay the current local Moments cache snapshot",
    )
    _add_config_argument(sync_moments)
    sync_moments.add_argument("--account", choices=ACCOUNT_LABELS, required=True)
    sync_moments_subject = sync_moments.add_mutually_exclusive_group()
    sync_moments_subject.add_argument("--contact")
    sync_moments_subject.add_argument(
        "--self", action="store_true", help="export this account's own cached Moments"
    )
    sync_moments.add_argument("--output")
    sync_moments.set_defaults(handler=command_sync_moments)

    media = commands.add_parser("media-open", help="open one exact media locator")
    _add_config_argument(media)
    media.add_argument("--account", choices=ACCOUNT_LABELS, required=True)
    media.add_argument("--locator", required=True)
    media.add_argument("--output", required=True)
    media.add_argument(
        "--voice-wav",
        action="store_true",
        help="decode an exact Tencent/WeChat SILK voice payload to WAV",
    )
    media.set_defaults(handler=command_media_open)

    preserve = commands.add_parser(
        "preserve", help="create one explicit, self-contained preservation bundle"
    )
    _add_context_arguments(preserve)
    preserve.add_argument("--output", required=True)
    preserve.set_defaults(handler=command_preserve)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        message = str(exc)
        if message.startswith("contact_ambiguous:"):
            candidates = json.loads(message.split(":", 1)[1])
            payload = {
                "status": "needs_choice",
                "error": "contact_ambiguous",
                "candidates": candidates,
            }
        else:
            payload = {
                "status": "failed",
                "error": message
                if isinstance(exc, ProductError)
                else type(exc).__name__,
            }
        sys.stdout.buffer.write(_canonical_bytes(payload))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

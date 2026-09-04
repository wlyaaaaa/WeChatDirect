"""Render one exported WeChat conversation as a self-contained HTML file."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from html import escape
import json
import re
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_IMAGE_KINDS = frozenset({"image", "emoji"})
_MEDIA_LABELS = {
    "emoji": "表情包",
    "file": "文件",
    "image": "图片",
    "video": "视频",
    "voice": "语音",
}


def _text(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def _html(value: object, default: str = "") -> str:
    return escape(_text(value, default), quote=True)


def _json_text(value: object) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            pass
    return _text(value)


def _path(value: object) -> str | None:
    """Return a safe local path, rejecting absolute, remote, and parent paths."""

    if value in (None, ""):
        return None
    raw = _text(value).strip().replace("\\", "/")
    if (
        not raw
        or "\x00" in raw
        or raw.startswith(("/", "//", "#"))
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw)
    ):
        return None
    parts = raw.split("/")
    if any(part == ".." for part in parts):
        return None
    parts = [part for part in parts if part not in ("", ".")]
    return "/".join(parts) or None


def _contact_name(contact: Mapping[str, Any]) -> str:
    return _text(contact.get("displayName") or contact.get("nickname") or "联系人")


def _format_time(value: object) -> tuple[str, str | None]:
    if value in (None, ""):
        return "时间不确定", None
    try:
        current = datetime.fromtimestamp(int(value), tz=LOCAL_TIMEZONE)
    except (OverflowError, OSError, TypeError, ValueError):
        return _text(value), None
    return current.strftime("%Y-%m-%d %H:%M:%S"), current.isoformat(timespec="seconds")


def _sender(message: Mapping[str, Any]) -> tuple[str, str, str, str]:
    sender = message.get("sender")
    sender = sender if isinstance(sender, Mapping) else {}
    role = _text(sender.get("role") or "unknown").casefold()
    name = _text(sender.get("displayName"))
    if not name:
        name = {"self": "我", "other": "对方", "system": "系统"}.get(
            role, "身份未确定"
        )
    direction = _text(message.get("direction")).casefold()
    if direction == "outgoing" or role == "self":
        return name, role, "outgoing", "发送"
    if role == "system" or direction == "system":
        return name, role, "system", "系统"
    return name, role, "incoming", "收到"


def _media_items(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = message.get("media_manifest") or []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _media_kind(media: Mapping[str, Any]) -> str:
    return _text(media.get("kind") or "media").casefold()


def _media_label(kind: str) -> str:
    return _MEDIA_LABELS.get(kind, "媒体")


def _media_path(media: Mapping[str, Any], kind: str) -> tuple[str | None, bool]:
    """Return the local path and whether it is the derived playable voice WAV."""

    if kind == "voice":
        derived = media.get("derivedVoiceWav")
        if isinstance(derived, Mapping):
            wav = _path(derived.get("path"))
            if wav:
                return wav, True
    return _path(media.get("exportedPath")), False


def _media_note(media: Mapping[str, Any]) -> str:
    status = _text(media.get("exportStatus"))
    if status and status != "available_local":
        return "导出状态：" + status
    return ""


def _media_gap_reason(media: Mapping[str, Any]) -> str:
    for key in (
        "exportGap",
        "voiceWavGap",
        "resolution_gap",
        "open_status",
        "exportStatus",
    ):
        value = media.get(key)
        if value not in (None, ""):
            return _text(value)
    return "缺少导出路径"


def _safe_class(value: str) -> str:
    result = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-")
    return result or "media"


def _media_type(kind: str) -> str:
    if kind in _IMAGE_KINDS:
        return "image"
    if kind == "voice":
        return "audio"
    if kind == "video":
        return "video"
    return "file"


def _quality_html(media: Mapping[str, Any]) -> str:
    if media.get("quality") == "thumbnail":
        return '<span class="media-quality">预览质量：缩略图</span>'
    return ""


def _render_media(media: Mapping[str, Any], index: int) -> str:
    kind = _media_kind(media)
    label = _media_label(kind)
    path, is_wav = _media_path(media, kind)
    base = (
        f'<div class="media-item media-kind-{_safe_class(kind)}" '
        f'data-media-index="{index}" data-kind="{_html(kind)}">'
    )
    if path is None:
        reason = _media_gap_reason(media)
        if kind == "voice":
            reason += "；播放缺口：未生成可播放 WAV"
        quality = _quality_html(media)
        return (
            f'<div class="media-item media-gap media-kind-{_safe_class(kind)}" '
            f'data-media-index="{index}" data-kind="{_html(kind)}">'
            f'<span class="media-label">{_html(label)}</span>'
            f'<span class="media-gap-text">媒体缺口：{_html(reason)}</span>{quality}</div>'
        )

    href = _html(path)
    note = _media_note(media)
    note_html = f'<span class="media-note">（{_html(note)}）</span>' if note else ""
    quality = _quality_html(media)
    media_type = _media_type(kind)
    base = base.replace('class="media-item ', f'class="media-item media-{media_type} ', 1)
    if media_type == "image":
        return (
            base
            + f'<a class="media-open" href="{href}"><img class="media-image" '
            f'src="{href}" alt="{_html(label)}" loading="lazy" decoding="async"></a>'
            f'<span class="media-caption">{_html(label)}{note_html}</span>{quality}</div>'
        )
    if kind == "voice" and not is_wav:
        return (
            f'<div class="media-item media-audio-gap media-kind-{_safe_class(kind)}" '
            f'data-media-index="{index}" data-kind="{_html(kind)}">'
            f'<span class="media-label">{_html(label)}</span>'
            '<span class="media-gap-text">播放缺口：未生成可播放 WAV</span>'
            f'<a class="media-link" href="{href}" download>下载原始语音</a>{quality}</div>'
        )
    if media_type == "audio":
        return (
            base
            + f'<audio class="media-audio" controls preload="metadata" src="{href}">'
            f'{_html(label)}：<a href="{href}">打开媒体</a></audio>'
            f'<a class="media-link" href="{href}">{_html(label)}{note_html}</a>{quality}</div>'
        )
    if media_type == "video":
        return (
            base
            + f'<video class="media-video" controls preload="metadata" src="{href}">'
            f'{_html(label)}：<a href="{href}">打开媒体</a></video>'
            f'<a class="media-link" href="{href}">{_html(label)}{note_html}</a>{quality}</div>'
        )
    return (
        base
        + f'<a class="media-link" href="{href}" download>{_html(label)}'
        f'{note_html}{quality}</a></div>'
    )


def _server_key(message: Mapping[str, Any]) -> str | None:
    native = message.get("nativeId")
    if isinstance(native, Mapping) and native.get("kind") == "server":
        value = native.get("value")
        if value not in (None, "", 0, "0"):
            return "server:" + _text(value)
    value = message.get("serverId")
    if value not in (None, "", 0, "0"):
        return "server:" + _text(value)
    return None


def _target_map(
    messages: list[Mapping[str, Any]], quoted_messages: list[Mapping[str, Any]]
) -> dict[str, tuple[Mapping[str, Any], int | None]]:
    targets: dict[str, tuple[Mapping[str, Any], int | None]] = {}
    for index, message in enumerate(messages):
        key = _server_key(message)
        if key:
            targets.setdefault(key, (message, index))
    for message in quoted_messages:
        key = _server_key(message)
        if key:
            targets.setdefault(key, (message, None))
    return targets


def _quote_text(target: Mapping[str, Any]) -> str:
    content = target.get("content")
    if content not in (None, ""):
        return _text(content)
    if target.get("contentGap") not in (None, ""):
        return "正文当前不可解析：" + _text(target["contentGap"])
    media = _media_items(target)
    if media:
        return "[" + "、".join(_media_label(_media_kind(item)) for item in media) + "]"
    return "[无文字正文]"


def _quote_media_html(target: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for media in _media_items(target):
        kind = _media_kind(media)
        label = _media_label(kind)
        path, is_wav = _media_path(media, kind)
        if path and kind in _IMAGE_KINDS:
            quality = _quality_html(media)
            parts.append(
                f'<a class="quote-media-thumbnail" href="{_html(path)}">'
                f'<img class="quote-thumbnail" src="{_html(path)}" alt="{_html(label)}">'
                f'</a>{quality}'
            )
        elif path:
            note = "，播放缺口；下载原始语音" if kind == "voice" and not is_wav else ""
            quality = _quality_html(media)
            download = " download" if kind == "voice" and not is_wav else ""
            parts.append(
                f'<a class="quote-media-link" href="{_html(path)}"{download}>'
                f'{_html(label)}{_html(note)}</a>{quality}'
            )
        else:
            parts.append(
                f'<span class="quote-media-gap">{_html(label)}（缺口）</span>'
                f'{_quality_html(media)}'
            )
    if not parts:
        return ""
    return '<div class="quote-media">附带媒体：' + "、".join(parts) + "</div>"


def _render_quote(
    message: Mapping[str, Any],
    targets: Mapping[str, tuple[Mapping[str, Any], int | None]],
) -> str:
    quote = message.get("quote")
    quote = quote if isinstance(quote, Mapping) else {}
    gap = message.get("quoteGap")
    if not quote and gap in (None, ""):
        return ""
    platform_id = quote.get("platformMessageId")
    key = "server:" + _text(platform_id) if platform_id not in (None, "") else ""
    found = targets.get(key) if key else None
    if found is None:
        reason = _text(gap or "引用目标未找到")
        target = _html(platform_id or "missing")
        return (
            f'<blockquote class="quote quote-gap" data-quote-target="{target}">'
            '<div class="quote-heading">引用</div>'
            '<div class="quote-text">[被引用消息不在当前导出范围]</div>'
            f'<div class="quote-note">引用缺口：{_html(reason)}</div></blockquote>'
        )

    target, target_index = found
    sender, _, _, _ = _sender(target)
    jump = (
        f'<a class="quote-jump" href="#message-{target_index}">查看原消息</a>'
        if target_index is not None
        else ""
    )
    return (
        f'<blockquote class="quote" data-quote-target="{_html(key)}">'
        f'<div class="quote-heading">引用 {_html(sender)} {jump}</div>'
        f'<div class="quote-text">{_html(_quote_text(target))}</div>'
        f'{_quote_media_html(target)}</blockquote>'
    )


def _render_message(
    message: Mapping[str, Any],
    index: int,
    targets: Mapping[str, tuple[Mapping[str, Any], int | None]],
) -> str:
    sender, role, alignment, direction = _sender(message)
    display_time, machine_time = _format_time(message.get("createTime"))
    time_html = (
        f'<time datetime="{_html(machine_time)}">{_html(display_time)}</time>'
        if machine_time
        else f'<span class="time-unknown">{_html(display_time)}</span>'
    )
    attrs = (
        f'id="message-{index}" data-message-index="{index}" '
        f'data-role="{_html(role)}" data-direction="{_html(alignment)}"'
    )
    native = message.get("nativeId")
    if isinstance(native, Mapping) and native.get("value") not in (None, ""):
        attrs += f' data-native-id="{_html(native.get("kind"))}:{_html(native.get("value"))}"'
    if message.get("sortSeq") not in (None, ""):
        attrs += f' data-sort-seq="{_html(message.get("sortSeq"))}"'

    body: list[str] = []
    quote = _render_quote(message, targets)
    if quote:
        body.append(quote)
    content = message.get("content")
    if content not in (None, ""):
        body.append(f'<div class="message-text">{_html(content)}</div>')
    elif message.get("contentGap") not in (None, ""):
        body.append(
            f'<div class="message-gap">正文当前不可解析：'
            f'{_html(message.get("contentGap"))}</div>'
        )
    media = _media_items(message)
    if media:
        body.append(
            '<div class="media-list" aria-label="消息媒体">'
            + "".join(_render_media(item, item_index) for item_index, item in enumerate(media))
            + "</div>"
        )
    if not body:
        body.append('<div class="message-empty">[无文字正文]</div>')

    return (
        f'<article class="message message-{alignment}" {attrs}>'
        '<header class="message-header">'
        f'<span class="message-sender">{_html(sender)}</span>'
        f'<span class="message-direction">{_html(direction)}</span>{time_html}'
        f'</header><div class="message-bubble">{"".join(body)}</div></article>'
    )


def _render_metadata(
    account: str, count: int, metadata: Mapping[str, Any]
) -> str:
    rows = [("账号", account), ("消息数", str(count))]
    rows.extend(
        (_text(key), _json_text(value))
        for key, value in metadata.items()
        if key != "title" and value is not None
    )
    rendered = "".join(
        f'<div class="metadata-row"><dt>{_html(key)}</dt><dd>{_html(value)}</dd></div>'
        for key, value in rows
    )
    return (
        '<details class="metadata"><summary>导出信息</summary>'
        f'<dl>{rendered}</dl></details>'
    )


_STYLE = """
:root { color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; color: #1d2733; background: #eef2f6; }
* { box-sizing: border-box; }
body { margin: 0; min-width: 280px; background: #eef2f6; }
.conversation { width: min(100%, 980px); margin: 0 auto; padding: 24px 18px 40px; }
.conversation-header { margin-bottom: 18px; padding: 18px 20px; background: #fff; border: 1px solid #dce3eb; border-radius: 16px; box-shadow: 0 4px 14px #22334410; }
.conversation-header h1 { margin: 0; font-size: clamp(1.25rem, 2vw, 1.65rem); }
.conversation-header p { margin: 8px 0 0; color: #647181; font-size: .9rem; }
.messages { display: flex; flex-direction: column; gap: 12px; }
.message { width: min(84%, 760px); }
.message-incoming { align-self: flex-start; }
.message-outgoing { align-self: flex-end; }
.message-system { align-self: center; width: min(92%, 680px); }
.message-header { display: flex; align-items: baseline; gap: 8px; margin: 0 8px 4px; color: #657384; font-size: .78rem; }
.message-outgoing .message-header { justify-content: flex-end; }
.message-system .message-header { justify-content: center; }
.message-sender { color: #344454; font-weight: 650; }
.message-direction { color: #8995a3; }
.message-bubble { padding: 12px 14px; background: #fff; border: 1px solid #dbe3eb; border-radius: 6px 16px 16px 16px; box-shadow: 0 2px 8px #2233440d; }
.message-outgoing .message-bubble { background: #dff4df; border-color: #c6e5c6; border-radius: 16px 6px 16px 16px; }
.message-system .message-bubble { background: #e4e9ef; border-color: #d6dee7; border-radius: 16px; text-align: center; }
.message-text, .quote-text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.55; }
.message-empty, .message-gap, .media-gap-text, .quote-note { color: #8a3f3f; }
.media-list { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
.media-item { max-width: 100%; overflow-wrap: anywhere; }
.media-image { display: block; max-width: min(420px, 100%); max-height: 520px; width: auto; height: auto; border-radius: 10px; background: #f4f6f8; object-fit: contain; }
.media-open { display: inline-block; max-width: 100%; }
.media-caption, .media-link, .media-note, .media-quality { display: inline-block; margin-top: 4px; color: #476983; font-size: .84rem; }
.media-audio { display: block; max-width: 100%; }
.media-video { display: block; width: min(560px, 100%); max-height: 520px; background: #101820; border-radius: 10px; }
.media-gap { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 10px; border: 1px dashed #d38e8e; border-radius: 9px; background: #fff7f7; font-size: .84rem; }
.media-audio-gap { display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; }
.media-label { font-weight: 650; }
.quote { margin: 0 0 10px; padding: 9px 11px; border-left: 3px solid #9ab4c8; border-radius: 4px 9px 9px 4px; background: #f1f5f8; color: #4c5d6d; }
.quote-heading { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; margin-bottom: 3px; font-size: .8rem; font-weight: 650; }
.quote-jump, .quote-media-link { color: #38698f; font-weight: 500; }
.quote-media { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 6px; margin-top: 5px; font-size: .8rem; }
.quote-media-thumbnail { display: inline-block; }
.quote-thumbnail { display: block; width: 84px; height: 84px; object-fit: cover; border-radius: 7px; background: #e2e8ed; }
.quote-media-gap { color: #965454; }
.metadata { margin-top: 14px; color: #637182; font-size: .84rem; }
.metadata summary { cursor: pointer; }
.metadata dl { margin: 8px 0 0; padding: 10px 12px; background: #fff; border: 1px solid #dce3eb; border-radius: 10px; }
.metadata-row { display: grid; grid-template-columns: minmax(6rem, 13rem) 1fr; gap: 12px; padding: 3px 0; }
.metadata dt { font-weight: 650; }
.metadata dd { margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; }
@media (max-width: 640px) { .conversation { padding: 12px 9px 28px; } .conversation-header { padding: 14px; border-radius: 12px; } .message { width: 96%; } .message-bubble { padding: 10px 11px; } .metadata-row { grid-template-columns: 1fr; gap: 2px; } }
""".strip()


def render_conversation_html(
    *,
    account: str,
    contact: Mapping[str, Any],
    messages: Iterable[Mapping[str, Any]] | None,
    quoted_messages: Iterable[Mapping[str, Any]] = (),
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Return one offline HTML view, preserving the supplied message order."""

    message_list = list(messages or ())
    quoted_list = list(quoted_messages or ())
    message_list = [item for item in message_list if isinstance(item, Mapping)]
    quoted_list = [item for item in quoted_list if isinstance(item, Mapping)]
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    contact_name = _contact_name(contact)
    title = _text(metadata_map.get("title") or f"{contact_name} - 微信聊天记录")
    targets = _target_map(message_list, quoted_list)
    rendered = "".join(
        _render_message(message, index, targets)
        for index, message in enumerate(message_list)
    )
    if not rendered:
        rendered = '<p class="message-empty">[没有消息]</p>'
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<main class="conversation" data-account="{_html(account)}" data-contact="{_html(contact_name)}">
<header class="conversation-header">
<h1>{_html(contact_name)}</h1>
<p>账号：{_html(account)} · 消息按导出顺序显示</p>
{_render_metadata(account, len(message_list), metadata_map)}
</header>
<section class="messages" aria-label="微信聊天记录">
{rendered}
</section>
</main>
</body>
</html>
'''


__all__ = ["render_conversation_html"]

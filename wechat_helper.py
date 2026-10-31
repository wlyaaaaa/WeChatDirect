"""On-demand Prometheus-first/helper adapter; importing this never loads native code.

Only NativeTextService is constructed. No bot, plugin manager, web panel, LLM,
scheduled task, persistent command queue or automatic friend acceptance is used.
"""

from __future__ import annotations

from collections import deque
from contextlib import redirect_stdout
import hashlib
import hmac
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import sys
import threading
import time
from typing import Callable, Mapping


UPSTREAM_COMMIT = "0a0cf6e09650fd2838af2042c4d7472c071977ee"
NATIVE_ID = re.compile(r"[A-Za-z0-9_.@-]{1,127}\Z")
SEND_ACTIONS = {"send-text", "send-at-text", "send-image", "send-file", "send-emoticon", "send-voice"}
LIVE_SESSION_ACTIONS = frozenset({"friend-requests", "accept-friend"})
UNAVAILABLE = {"add-friend", "contacts", "read-moments", "publish-moments", "interact-moments"}
CAPABILITIES = {
    "send-text": "available", "send-at-text": "available_unverified_group_operation",
    "send-image": "available", "send-file": "available", "send-emoticon": "available",
    "send-voice": "available_silk_or_optional_wav_encoder", "receive": "explicit_targets_only",
    "friend-requests": "live_session_only", "accept-friend": "live_session_only",
    "group-members": "loaded_cache_only", "invite-group-members": "available_unverified_group_operation",
    "create-group": "available_unverified_group_operation",
    **{name: "unavailable_on_4.1.15.13" for name in UNAVAILABLE},
}
REQUEST_KEYS = {
    "send-text": {"target", "text"}, "send-at-text": {"target", "text", "members"},
    "send-image": {"target", "path"}, "send-file": {"target", "path"},
    "send-emoticon": {"target", "path"}, "send-voice": {"target", "path"},
    "receive": {"seconds", "limit"}, "friend-requests": set(),
    "accept-friend": {"request_id"}, "group-members": {"target"},
    "invite-group-members": {"target", "members"}, "create-group": {"members"},
    "close": set(), "status": set(),
}


class HelperError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def load_settings(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise HelperError("helper_config_unavailable") from exc
    if (not isinstance(value, dict) or set(value) != {"runtime_path", "state_root"}
            or any(not isinstance(item, str) or not item.strip() for item in value.values())):
        raise HelperError("helper_config_invalid")
    settings = {key: Path(item) for key, item in value.items()}
    if any(not item.is_absolute() for item in settings.values()):
        raise HelperError("helper_config_paths_must_be_absolute")
    return settings


def environment_status(settings: Mapping) -> dict:
    """Static checks only: no upstream import, process read or DLL injection."""
    runtime = settings["runtime_path"]
    required = ("native/text_service.py", "native/filehelper_service.py", "native/run_selftest.py",
                "native/build_text/text_bridge.dll", "wxbot_framework/account.py")
    missing = [name for name in required if not (runtime / name).is_file()]
    dependencies = {name: importlib.util.find_spec(name) is not None
                    for name in ("pefile", "PIL", "cryptography", "pysilk")}
    state_available = settings["state_root"].is_dir()
    ready = os.name == "nt" and struct.calcsize("P") == 8 and state_available and not missing and all(
        dependencies[name] for name in ("pefile", "PIL", "cryptography"))
    return {"status": "success" if ready else "partial", "scope": "static_environment_only",
            "nativeCodeLoaded": False, "accountRead": False, "upstreamCommit": UPSTREAM_COMMIT,
            "platformSupported": os.name == "nt", "python64Bit": struct.calcsize("P") == 8,
            "stateRootAvailable": state_available,
            "runtimeFilesMissing": missing, "dependencies": dependencies,
            "capabilities": dict(CAPABILITIES), "capabilitiesBasis": "pinned_source_not_live_test"}


def native_module(settings):
    state = environment_status(settings)
    if state["status"] != "success":
        raise HelperError("helper_environment_unavailable", environment=state)
    runtime = settings["runtime_path"].resolve()
    for path in (runtime, runtime / "native"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        module = importlib.import_module("text_service")
    except (ImportError, OSError) as exc:
        raise HelperError("helper_runtime_import_failed") from exc
    if Path(module.__file__).resolve() != runtime / "native" / "text_service.py":
        raise HelperError("helper_runtime_module_conflict")
    return module


def _identity_hash(value):
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _target(value):
    if not isinstance(value, str) or not NATIVE_ID.fullmatch(value):
        raise HelperError("helper_native_target_required")
    return value


def _members(value, *, creating=False):
    if not isinstance(value, list) or not (2 if creating else 1) <= len(value) <= 8:
        raise HelperError("helper_group_members_invalid")
    for member in value:
        _target(member)
        if (member in {".", "..", "filehelper", "weixin", "fmessage", "newsapp", "floatbottle"}
                or member.endswith("@chatroom") or member.startswith("gh_")):
            raise HelperError("helper_group_members_invalid")
    if len(set(value)) != len(value):
        raise HelperError("helper_group_members_invalid")
    return value


def _text(value):
    if not isinstance(value, str) or "\0" in value:
        raise HelperError("helper_text_invalid")
    size = len(value.encode("utf-8"))
    if not 1 <= size <= 1023:
        raise HelperError("helper_text_utf8_limit", utf8Bytes=size, maxUtf8Bytes=1023)
    return value


def _path(value):
    if not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) > 1023:
        raise HelperError("helper_media_path_invalid")
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size == 0:
        raise HelperError("helper_media_file_unavailable")
    return str(path)


def _safe_native(result):
    if not isinstance(result, dict):
        return {}
    receipt = {key: result[key] for key in ("code", "success", "cleanup_code", "dll_unloaded")
               if isinstance(result.get(key), (bool, int))}
    report = result.get("report")
    if isinstance(report, dict):
        receipt["report"] = {key: report[key] for key in (
            "completion_success", "operation", "sent", "message_type", "app_type", "duration_ms")
            if isinstance(report.get(key), (str, bool, int))}
    return receipt


def _friend_public(rows):
    return [{key: row[key] for key in ("request_id", "sender_id", "nickname", "verification",
                                      "scene", "opcode", "status") if key in row}
            for row in rows]


def _diagnostics(exc, stage):
    """Expose actionable fixed categories and numeric OS/native codes, never raw errors."""
    known = {
        "等待绑定的机器人账号登录": "bound_account_not_logged_in",
        "同一个机器人账号对应多个微信进程，等待唯一的登录会话": "multiple_bound_account_processes",
        "目标进程登录的不是绑定的机器人账号，等待正确账号登录": "native_account_mismatch",
        "微信尚未完成登录，自动等待当前账号就绪": "native_account_not_ready",
        "微信版本布局尚未确认": "native_layout_unconfirmed",
        "客户端版本已改变": "client_version_changed",
        "微信核心模块尚未加载": "native_core_not_loaded",
        "微信核心 DLL 指纹不匹配，拒绝加载": "native_core_fingerprint_mismatch",
        "桥接 DLL 未正确加载": "native_bridge_load_failed",
        "报告路径超过原生接口长度": "native_report_path_too_long",
        "微信中加载了其他项目的桥接，请先退出该项目；当前项目不会卸载它": "foreign_bridge_loaded",
        "当前桥接缺少所需接收接口，请停止机器人并更新桥接 DLL": "native_receive_export_missing",
        "语音缓存目录必须是无链接的绝对本地路径": "native_media_path_requires_resolved_path",
    }
    message = str(exc)
    result = {"stage": stage, "exceptionType": type(exc).__name__,
              "diagnosticCode": known.get(message, "native_exception")}
    for name in ("errno", "winerror"):
        value = getattr(exc, name, None)
        if type(value) is int:
            result[name] = value
    match = re.fullmatch(r"接收 Hook 启动失败：(\d+)", message)
    if match:
        result.update(diagnosticCode="native_listen_rejected", nativeCode=int(match[1]))
    if isinstance(exc, TimeoutError):
        result["diagnosticCode"] = "native_timeout"
    return result


class HelperSession:
    def __init__(self, settings, *, account, expected_wxid, expected_sha256,
                 event_targets=("filehelper",), module=None):
        if account not in ("primary", "secondary"):
            raise HelperError("helper_explicit_account_required")
        if not hmac.compare_digest(_identity_hash(expected_wxid), expected_sha256):
            raise HelperError("helper_account_identity_mismatch")
        if not event_targets:
            raise HelperError("helper_explicit_event_targets_required")
        self.event_targets = frozenset(_target(target) for target in event_targets)
        self.settings, self.account = settings, account
        self.expected_wxid, self.expected_sha256 = expected_wxid, expected_sha256
        self.module, self.service = module, None
        self.started = False
        self.poisoned = False
        self._event_lock = threading.Lock()
        self._event_buffer = deque(maxlen=200)
        self._event_dropped = 0

    def _verify_identity(self):
        actual = self.module.inspect_account(self.service.pid)
        if not isinstance(actual, str) or not hmac.compare_digest(
                _identity_hash(actual), self.expected_sha256):
            self.poisoned = True
            raise HelperError("helper_account_identity_mismatch")

    def start(self):
        # The owner creates this configured root while managed storage is open.
        # Never recursively recreate it through a missing/unmounted logical root.
        if not self.settings["state_root"].is_dir():
            raise HelperError("helper_state_root_unavailable")
        self.module = self.module or native_module(self.settings)
        stage = "select_process"
        try:
            pid = self.module.find_account_process(self.expected_wxid)
            # Managed personal-data roots may be junctions. Resolve at use time,
            # keeping the stable logical location in configuration unchanged.
            root = (self.settings["state_root"] / self.account).resolve()
            stage = "construct_service"
            self.service = self.module.NativeTextService(
                pid=pid, account=self.expected_wxid, image_allowlist=list(self.event_targets),
                media_directory=root / "images", file_directory=root / "files",
                voice_directory=root / "voices", emoticon_directory=root / "emoticons",
                wechat_data_directory=None, preserve_reports=False)
            self._verify_identity()
            self.service.register_msg_handler(self._on_message)
            stage = "start_native_service"
            self.service.start()
            self.started = True
            stage = "verify_started_identity"
            self._verify_identity()
        except HelperError:
            raise
        except Exception as exc:
            raise HelperError("helper_start_failed", **_diagnostics(exc, stage)) from exc

    def _on_message(self, _pid, _protocol, data, _service):
        if not isinstance(data, dict) or data.get("chat_id") not in self.event_targets:
            return
        # XML, CDN credentials and friend-request tickets are never forwarded.
        keys = ("chat_id", "sender_id", "native_sender", "native_receiver", "is_self", "sequence",
                "native_message_id", "wx_type", "image_path", "image_status", "file_path", "file_status",
                "voice_path", "voice_status", "app_type")
        event = {key: data[key] for key in keys if isinstance(data.get(key), (str, int, bool))}
        if data.get("wx_type") == 1:
            event["text"] = str(data.get("msg") or data.get("content") or "")
        with self._event_lock:
            if len(self._event_buffer) == self._event_buffer.maxlen:
                self._event_dropped += 1
            self._event_buffer.append(event)

    def close(self):
        if self.service is None:
            return {"status": "not_needed", "dllUnloaded": True}
        try:
            result = self.service.stop()
            clean = isinstance(result, dict) and result.get("code") == 0 and result.get("dll_unloaded") is True
            return {"status": "success" if clean else "incomplete", "dllUnloaded": clean,
                    "native": _safe_native(result)}
        except Exception as exc:
            return {"status": "incomplete", "dllUnloaded": False, "error": "helper_cleanup_incomplete",
                    **_diagnostics(exc, "stop_native_service")}
        finally:
            self.started = False
            with self._event_lock:
                self._event_buffer.clear()
                self._event_dropped = 0

    def execute(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("action"), str):
            raise HelperError("helper_request_invalid")
        action = request["action"]
        if action in UNAVAILABLE:
            raise HelperError("helper_capability_unavailable", capability=action, wechatVersion="4.1.15.13")
        if action not in REQUEST_KEYS or set(request) - {"action"} - REQUEST_KEYS[action]:
            raise HelperError("helper_request_invalid")
        if action == "close":
            cleanup = self.close()
            return {"status": "success" if cleanup["dllUnloaded"] else "partial", "action": action,
                    "cleanup": cleanup, "liveFriendRequests": "discarded"}
        if not self.started or self.poisoned:
            raise HelperError("helper_session_unavailable", retryable=False)
        self._verify_identity()
        if self.service.last_error is not None:
            self.poisoned = True
            raise HelperError("helper_receive_service_stopped", retryable=False)
        if action == "status":
            return {"status": "success", "action": action, "account": self.account,
                    "connected": True, "eventTargets": sorted(self.event_targets)}
        if action == "receive":
            return self._receive(request)
        if action == "friend-requests":
            return {"status": "success", "action": action, "requests": _friend_public(self.service.friend_requests()),
                    "coverage": "current_live_session_only"}
        if action == "group-members":
            room = _target(request.get("target"))
            if not room.endswith("@chatroom"):
                raise HelperError("helper_group_target_required")
            rows = self.service.group_members(room)
            return {"status": "success" if rows is not None else "partial", "action": action,
                    "target": room, "members": rows, "coverage": "loaded_cache_only"}
        return self._invoke(action, request)

    def _receive(self, request):
        seconds, limit = request.get("seconds", 1), request.get("limit", 40)
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 60
                or type(limit) is not int or not 1 <= limit <= 100):
            raise HelperError("helper_receive_bounds_invalid")
        events = []
        dropped = 0
        deadline = time.monotonic() + seconds
        while True:
            with self._event_lock:
                while self._event_buffer and len(events) < limit:
                    events.append(self._event_buffer.popleft())
                dropped += self._event_dropped
                self._event_dropped = 0
                remaining = len(self._event_buffer)
            if len(events) >= limit or time.monotonic() >= deadline:
                break
            if self.service.last_error is not None:
                raise HelperError("helper_receive_service_stopped")
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        self._verify_identity()
        if self.service.last_error is not None:
            raise HelperError("helper_receive_service_stopped")
        return {"status": "partial" if dropped else "success", "action": "receive",
                "events": events, "dropped": dropped, "bufferedRemaining": remaining,
                "eventTargets": sorted(self.event_targets), "bufferLimit": 200,
                "coverage": "current_live_session_buffer_no_history"}

    def _invoke(self, action, request):
        target = _target(request.get("target")) if action in SEND_ACTIONS or action == "invite-group-members" else None
        text = _text(request.get("text")) if action in ("send-text", "send-at-text") else None
        path = _path(request.get("path")) if action in ("send-image", "send-file", "send-emoticon", "send-voice") else None
        if action == "send-voice":
            with Path(path).open("rb") as source:
                header = source.read(12)
            if header[:4] == b"RIFF" and header[8:12] == b"WAVE" and importlib.util.find_spec("pysilk") is None:
                raise HelperError("helper_wav_encoder_unavailable", nextAction="encode_silk_with_existing_voice_runtime_or_install_optional_silk_python")
        if action in ("send-at-text", "invite-group-members") and not target.endswith("@chatroom"):
            raise HelperError("helper_group_target_required")
        members = _members(request.get("members"), creating=action == "create-group") if action in (
            "send-at-text", "invite-group-members", "create-group") else None
        if action == "send-at-text" and len(members) > 8:
            raise HelperError("helper_mentions_limit")
        request_id = request.get("request_id")
        if action == "accept-friend" and (not isinstance(request_id, str) or not re.fullmatch(r"fr_[0-9a-f]{32}", request_id)):
            raise HelperError("helper_friend_request_id_invalid")
        if action == "accept-friend":
            for pending in self.service.friend_requests():
                if pending.get("request_id") == request_id and pending.get("status") == "accepted":
                    self._verify_identity()
                    return {"status": "success", "action": action, "requestId": request_id,
                            "outcome": "previously_accepted", "previouslyAccepted": True,
                            "invocationCompleted": True, "nativeInvoked": False, "wechatAccepted": None,
                            "acceptanceEvidence": "current_live_request_directory",
                            "actualReadback": "not_checked", "retryable": False}
        # Fixed dispatch table: no user-controlled getattr or arbitrary upstream methods.
        calls: dict[str, Callable] = {
            "send-text": lambda: self.service.send_text(target, text),
            "send-at-text": lambda: self.service.send_at_text(target, text, members),
            "send-image": lambda: self.service.send_image(target, path),
            "send-file": lambda: self.service.send_file(target, path),
            "send-emoticon": lambda: self.service.send_emoticon(target, path),
            "send-voice": lambda: self.service.send_voice(target, path),
            "accept-friend": lambda: self.service.accept_friend(request_id),
            "invite-group-members": lambda: self.service.invite_group_members(target, members),
            "create-group": lambda: self.service.create_group(members),
        }
        self.service.last_result = None
        self._verify_identity()
        try:
            result = calls[action]()
            self._verify_identity()
        except (Exception, KeyboardInterrupt) as exc:
            self.poisoned = True
            # Any exception after invocation can mean a lost acknowledgement. Never retry.
            return {"status": "unknown", "action": action, "invocationCompleted": False,
                    "wechatAccepted": None, "actualReadback": "not_checked", "retryable": False,
                    "error": "helper_operation_interrupted" if isinstance(exc, KeyboardInterrupt) else "helper_operation_outcome_unknown",
                    "nextAction": "read_back_exact_target_before_any_new_send", **_diagnostics(exc, action)}
        native = _safe_native(self.service.last_result)
        report = native.get("report", {})
        accepted = (native.get("success") is True and native.get("code") == 0
                    and native.get("cleanup_code") == 0 and report.get("completion_success") is True)
        known_failed = native.get("code", 0) != 0 or report.get("completion_success") is False
        status = "success" if accepted else "failed" if known_failed else "unknown"
        if status == "unknown":
            self.poisoned = True
        receipt = {"status": status, "action": action, "invocationCompleted": True,
                   "wechatAccepted": True if accepted else False if known_failed else None,
                   "actualReadback": "not_checked", "retryable": False, "native": native}
        if target is not None:
            receipt["target"] = target
        if action == "create-group" and accepted and isinstance(result, str):
            receipt["createdGroup"] = _target(result)
        if action in ("send-at-text", "invite-group-members", "create-group"):
            receipt["validation"] = "upstream_interface_only_not_live_group_tested"
        return receipt


def _error_receipt(exc):
    if isinstance(exc, HelperError):
        return {"status": "failed", "error": str(exc), "retryable": False, **exc.details}
    return {"status": "failed", "error": "helper_request_failed", "retryable": False}


def _emit(stream, receipt):
    line = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    stream.write(line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + "\n")
    stream.flush()


def run_session(session, input_stream, output_stream):
    """One JSON request / one receipt. EOF also emits a final cleanup receipt."""
    cleanup = None
    fatal_failed = False
    try:
        with redirect_stdout(sys.stderr):
            session.start()
        for line in input_stream:
            request = None
            try:
                request = json.loads(line)
                with redirect_stdout(sys.stderr):
                    receipt = session.execute(request)
            except Exception as exc:
                receipt = _error_receipt(exc)
            _emit(output_stream, receipt)
            if receipt.get("action") == "close" and "cleanup" in receipt:
                cleanup = receipt.get("cleanup")
                break
    except (Exception, KeyboardInterrupt) as exc:
        fatal_failed = True
        _emit(output_stream, _error_receipt(exc))
    finally:
        if cleanup is None:
            with redirect_stdout(sys.stderr):
                cleanup = session.close()
            _emit(output_stream, {"status": "success" if cleanup["dllUnloaded"] else "partial",
                                  "action": "close", "reason": "eof_or_interruption", "cleanup": cleanup,
                                  "liveFriendRequests": "discarded"})
    return 0 if cleanup["dllUnloaded"] and not fatal_failed else 2


def run_once(session, request):
    receipt = None
    try:
        if isinstance(request, dict) and request.get("action") in LIVE_SESSION_ACTIONS:
            raise HelperError("helper_live_session_required", nextAction="use_friend_requests_and_accept_friend_in_the_same_live_helper_session")
        session.start()
        receipt = session.execute(request)
    except (Exception, KeyboardInterrupt) as exc:
        receipt = _error_receipt(exc)
    finally:
        cleanup = session.close()
    receipt["cleanup"] = cleanup
    if not cleanup["dllUnloaded"] and receipt["status"] == "success":
        receipt["status"] = "partial"
    return receipt

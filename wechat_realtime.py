"""Task-scoped exact-message context and competing, source-bound voice candidates."""
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid


def _stop(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                           timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.kill()
        process.wait(timeout=3)


class LocalASR:
    def __init__(self, root):
        self.root, self.process = Path(root), None
        self.lines, self.lock, self.ready = queue.Queue(), threading.Lock(), False

    def prepare(self):
        self.process = subprocess.Popen([str(self.root / ".venv/Scripts/python.exe"), "-B", "-u", "-m", "zh_asr.realtime"],
            cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def read():
            for line in self.process.stdout:
                try:
                    self.lines.put(json.loads(line))
                except ValueError:
                    self.lines.put({"event": "error", "message": "local_asr_protocol_invalid"})
            self.lines.put({"event": "closed"})
        threading.Thread(target=read, daemon=True).start()
        receipt = self.lines.get(timeout=660)
        self.ready = receipt.get("event") == "ready" and receipt.get("state") == "ready"
        if not self.ready:
            self.close()
            raise RuntimeError("realtime_local_asr_not_ready")
        return receipt

    def transcribe(self, path, deadline):
        if not self.ready or not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise RuntimeError("realtime_local_asr_not_ready_or_busy")
        try:
            identifier = uuid.uuid4().hex
            self.process.stdin.write(json.dumps({"action": "transcribe", "path": str(path),
                "request_id": identifier, "budget_seconds": min(10, max(0.01, deadline - time.monotonic()))}) + "\n")
            self.process.stdin.flush()
            receipt = self.lines.get(timeout=max(0, deadline - time.monotonic()))
            if receipt.get("request_id") != identifier:
                raise RuntimeError("realtime_local_asr_binding_mismatch")
            return receipt
        except Exception:
            self.ready = False
            _stop(self.process)
            raise
        finally:
            self.lock.release()

    def close(self):
        self.ready = False
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write('{"action":"close"}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                _stop(self.process)


class RealtimeContextSession:
    def __init__(self, session, *, asr_root, account, reader_factory, local=None, clock=time.monotonic):
        self.session, self.account, self.reader_factory = session, account, reader_factory
        self.asr_root, self.local, self.clock = Path(asr_root), local or LocalASR(asr_root), clock
        self.observed, self.requests = OrderedDict(), OrderedDict()
        self.lock, self.processes = threading.Lock(), set()
        self.closed = False
        self.pool = ThreadPoolExecutor(max_workers=8)
        original = session._on_message
        def observe(pid, protocol, data, service):
            if isinstance(data, dict) and data.get("chat_id") in session.event_targets and data.get("native_message_id"):
                key = (data["chat_id"], str(data["native_message_id"]))
                with self.lock:
                    self.observed.setdefault(key, (self.clock(), datetime.now(timezone(timedelta(hours=8))).isoformat()))
                    while len(self.observed) > 200:
                        self.observed.popitem(last=False)
            original(pid, protocol, data, service)
        session._on_message = observe

    def prepare(self):
        return {"action": "realtime-ready", "status": "success", "localASR": self.local.prepare()}

    def start(self):
        self.session.start()

    def _run(self, command, timeout):
        with self.lock:
            if self.closed:
                raise RuntimeError("realtime_session_closed")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.processes.add(process)
        try:
            stdout, _stderr = process.communicate(timeout=max(0.01, timeout))
            return stdout
        finally:
            _stop(process)
            with self.lock:
                self.processes.discard(process)

    def _media(self, reader, message, directory, deadline):
        import wechat_cli as cli
        from PIL import Image
        gaps, audio = [], None
        for index, item in enumerate(message.get("media_manifest", [])):
            try:
                if self.clock() >= deadline:
                    raise TimeoutError("media_deadline")
                data = reader.open_locator(item["locator"], allow_remote=True)
                path = directory / (str(message["serverId"]) + f"-{index}" + cli._media_extension(data, item["kind"]))
                path.write_bytes(data)
                item.update(output=str(path), sha256=hashlib.sha256(data).hexdigest(), materialized=True)
                if item["kind"] == "voice" and not message.get("content"):
                    audio = path
                elif item["kind"] in {"image", "emoji"}:
                    with Image.open(io.BytesIO(data)) as image:
                        count = getattr(image, "n_frames", 1)
                        durations = []
                        for frame in range(count):
                            image.seek(frame)
                            durations.append(max(1, int(image.info.get("duration", 100))))
                        total, chosen = sum(durations), {0, count - 1}
                        for point in range(min(count, 8)):
                            target, accumulated = point * total / max(1, min(count, 8) - 1), 0
                            for frame, duration in enumerate(durations):
                                accumulated += duration
                                if accumulated >= target:
                                    chosen.add(frame)
                                    break
                        frames = []
                        for frame in sorted(chosen):
                            if self.clock() >= deadline:
                                raise TimeoutError("visual_deadline")
                            image.seek(frame)
                            preview = path.with_name(path.stem + f"-frame-{frame}.png")
                            image.convert("RGBA").save(preview)
                            frames.append({"path": str(preview), "atMs": sum(durations[:frame])})
                        item.update(frames=frames, frameCount=count, durationMs=total,
                                    allFramesProvided=len(chosen) == count, visualRead=False)
            except Exception as error:
                gaps.append({"message": message.get("serverId"), "kind": "media_unavailable",
                             "stage": "media_open", "type": type(error).__name__, "code": str(error)})
        return gaps, audio

    def _decode(self, audio, deadline):
        import wechat_cli as cli
        wav = audio.with_suffix(".wav")
        self._run([*cli._voice_interpreter_command(), str(cli.VOICE_DECODER), "--input", str(audio),
                   "--output", str(wav)], deadline - self.clock())
        if not wav.is_file():
            raise RuntimeError("voice_decode_failed")
        return wav

    def _candidate(self, route, target, message, wav, deadline):
        server = str(message["serverId"])
        try:
            if hasattr(wav, "result"):
                wav = wav.result(timeout=max(0, deadline - self.clock()))
            if wav is None or self.clock() >= deadline:
                raise RuntimeError("voice_wav_unavailable_or_deadline_exhausted")
            digest = hashlib.sha256(wav.read_bytes()).hexdigest()
            if route == "local":
                result = self.local.transcribe(wav, deadline)
                valid = result.get("state") == "completed" and result.get("coverage", {}).get("status") == "complete" and result.get("source_sha256") == digest
            else:
                command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(self.asr_root / "scripts/asr-professional-cloud.ps1"),
                           "-Audio", str(wav), "-CloudFirst", "-Json"]
                result = json.loads(self._run(command, 60))
                valid = result.get("status") == "succeeded" and result.get("source_audio_sha256") == digest
            binding = {"account": self.session.account, "nativeContactId": target, "nativeMessageId": server, "audioSha256": digest}
            text = result.get("text")
            return {"route": route, "nativeMessageId": server, "status": "success" if valid and isinstance(text, str) and text.strip() else "candidate_failed",
                    "text": text if valid else None, "binding": binding, "finished": self.clock(),
                    "reason": result.get("reason") or result.get("error_code") or (None if valid else "candidate_binding_or_coverage_mismatch"), "timing": result.get("timing") or result.get("timings"),
                    "receiptPath": result.get("result_path")}
        except Exception as error:
            return {"route": route, "nativeMessageId": server, "status": "candidate_failed", "reason": type(error).__name__, "code": str(error), "finished": self.clock()}

    def _snapshot(self, record):
        with self.lock:
            futures, originals, gaps = list(record["futures"]), list(record["messages"]), list(record["gaps"])
        results = [future.result() for future in futures if future.done() and not future.cancelled()]
        for result in sorted(results, key=lambda value: value["finished"]):
            if result["status"] == "success":
                record["best"].setdefault(result["nativeMessageId"], result)
        messages = []
        if record["phase"] == "preparing":
            gaps.append({"kind": "materials_pending", "stage": "material_prepare", "code": "awaiting_bound_materials"})
        for original in originals:
            message = dict(original)
            if message.get("type") == "voice" and not message.get("content"):
                best = record["best"].get(str(message["serverId"]))
                if best:
                    message.update(content=best["text"], contentSource=best["route"], voiceTranscription={
                        "source": best["route"], "binding": best["binding"], "verbatimVerified": False})
                else:
                    gaps.append({"kind": "voice_transcript_unavailable", "message": message["serverId"]})
            messages.append(message)
        query_elapsed = self.clock() - record["observed"][0]
        material_elapsed = record.get("materialDeliveryElapsed")
        complete_elapsed = record.get("completeDeliveryElapsed")
        elapsed = complete_elapsed if complete_elapsed is not None else material_elapsed if material_elapsed is not None else query_elapsed
        material_complete = not gaps
        vision = any(media.get("kind") in {"image", "emoji"} and not media.get("visualRead") for message in messages for media in message.get("media_manifest", []))
        complete = material_complete and not vision
        return {"action": "read-context", "request_id": record["id"], "status": "success" if complete and elapsed <= 15 else "late" if elapsed > 15 else "ready_for_vision" if material_complete and vision else "partial",
                "complete": complete, "observedAt": record["observed"][1], "elapsed": elapsed, "onTime": complete and elapsed <= 15, "late": elapsed > 15,
                "queryElapsed": query_elapsed, "materialDeliveryElapsed": material_elapsed, "completeDeliveryElapsed": complete_elapsed,
                "materialComplete": material_complete, "materialOnTime": material_complete and (material_elapsed if material_elapsed is not None else query_elapsed) <= 15, "ready_for_vision": material_complete and vision,
                "state": "ready_for_vision" if material_complete and vision else "complete" if complete else "incomplete" if elapsed >= 15 or (record["phase"] == "ready" and all(f.done() for f in futures)) else "pending",
                "messages": messages[:1], "quotedMessages": messages[1:], "gaps": gaps, "candidates": results,
                "diagnostics": record.get("diagnostics", []),
                "pending": sum(not future.done() for future in futures) + int(record["phase"] == "preparing"), "automaticReply": False}

    def _prepare_materials(self, record, directory, deadline):
        import wechat_cli as cli
        key, submitted = record["key"], set()
        while True:
            messages, gaps, awaiting = [], [], False
            try:
                with self.reader_factory() as reader:
                    message = reader.fetch_message_by_server_id(*key, exact_media_lookup=True)
                    if message is None:
                        raise RuntimeError("realtime_exact_message_pending")
                    if str(message.get("serverId")) != key[1]:
                        raise ValueError("realtime_exact_message_identity_mismatch")
                    quotes, gaps = cli._attach_quote_targets(reader, session_native_id=key[0], selected=[message], scanned=[message])
                    messages, awaiting = [message, *quotes], bool(gaps)
                    for item in messages:
                        if item.get("contentGap"):
                            gaps.append({"kind": "message_content_gap", "message": item.get("serverId"), "code": item["contentGap"]})
                        failures, audio = self._media(reader, item, directory, deadline)
                        gaps.extend(failures)
                        record["diagnostics"].extend(failures)
                        awaiting |= bool(failures)
                        if item.get("type") == "voice" and not item.get("content"):
                            server = str(item["serverId"])
                            awaiting |= audio is None
                            if audio is not None and server not in submitted and self.clock() < deadline:
                                with self.lock:
                                    decoded = self.pool.submit(self._decode, audio, deadline)
                                    record["futures"].extend(self.pool.submit(self._candidate, route, key[0], item, decoded, deadline) for route in ("local", "cloud"))
                                submitted.add(server)
                with self.lock:
                    record.update(messages=messages, gaps=gaps)
                if not awaiting:
                    break
            except Exception as error:
                diagnostic = {"stage": "message_read", "type": type(error).__name__, "code": str(error)}
                record["diagnostics"].append(diagnostic)
                with self.lock:
                    record.update(messages=messages, gaps=[{"kind": "message_pending", "diagnostic": diagnostic}])
                if str(error) == "realtime_exact_message_identity_mismatch":
                    break
            if self.closed or self.clock() >= deadline:
                break
            time.sleep(min(0.15, deadline - self.clock()))
        record["phase"] = "ready"

    def read_context(self, request):
        key = (request.get("target"), str(request.get("native_message_id") or ""))
        with self.lock:
            observed = self.observed.get(key)
        if observed is None:
            raise ValueError("realtime_message_not_observed_in_selected_scope")
        for record in self.requests.values():
            if record["key"] == key:
                return self._snapshot(record)
        if len(self.requests) >= 8:
            finished = next((identifier for identifier, old in self.requests.items() if old["preparation"].done() and all(f.done() for f in old["futures"])), None)
            if finished is None:
                raise ValueError("realtime_request_capacity")
            self.requests.pop(finished)
        identifier, deadline = uuid.uuid4().hex, observed[0] + 15
        directory = (self.session.settings["state_root"] / self.session.account / "realtime" / identifier).resolve()
        directory.mkdir(parents=True)
        record = {"id": identifier, "key": key, "observed": observed, "messages": [], "gaps": [],
                  "futures": [], "best": {}, "diagnostics": [], "phase": "preparing"}
        self.requests[identifier] = record
        preparation = self.pool.submit(self._prepare_materials, record, directory, deadline)
        record["preparation"] = preparation
        while self.clock() < deadline:
            result = self._snapshot(record)
            pending = [future for future in [preparation, *record["futures"]] if not future.done()]
            if result["materialComplete"] or not pending:
                return result
            wait(pending, timeout=max(0, deadline - self.clock()), return_when=FIRST_COMPLETED)
        return self._snapshot(record)

    def execute(self, request):
        action = request.get("action") if isinstance(request, dict) else None
        if action == "close":
            cleanup = self.close()
            return {"action": "close", "status": "success" if cleanup["status"] in {"success", "not_needed"} else "partial", "cleanup": cleanup}
        if action in {"read-context", "context-results"}:
            self.session._verify_identity()
            result = self.read_context(request) if action == "read-context" else self._snapshot(self.requests[request["request_id"]])
            self.session._verify_identity()
            record = self.requests[result["request_id"]]
            result = self._snapshot(record)
            elapsed = self.clock() - record["observed"][0]
            if result["materialComplete"]:
                record.setdefault("materialDeliveryElapsed", elapsed)
            if result["complete"]:
                record.setdefault("completeDeliveryElapsed", elapsed)
            return self._snapshot(record)
        return self.session.execute(request)

    def close(self):
        with self.lock:
            self.closed = True
            processes = list(self.processes)
        gaps = []
        def attempt(stage, operation):
            try:
                return operation()
            except (Exception, KeyboardInterrupt) as error:
                gaps.append({"stage": stage, "type": type(error).__name__, "code": str(error)})
                return None
        try:
            attempt("local_asr", self.local.close)
            for process in processes:
                attempt("task_process", lambda: _stop(process))
            attempt("candidate_pool", lambda: self.pool.shutdown(wait=True, cancel_futures=True))
        finally:
            cleanup = attempt("native_helper", self.session.close)
        cleanup = dict(cleanup) if isinstance(cleanup, dict) else {"status": "incomplete", "dllUnloaded": None}
        if gaps:
            cleanup.update(status="incomplete", cleanupGaps=gaps)
        return cleanup

"""Small offline project results from archives and local read-only snapshots."""
from collections import Counter
from datetime import datetime
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def stamp(value):
    from wechat_cli import LOCAL_TIMEZONE
    return datetime.fromtimestamp(value, LOCAL_TIMEZONE).isoformat()


def write_local_message_count(output, path_for_read=Path):
    """Count both bound accounts for the existing night's isolated subprocess."""
    from wechat_cli import (ACCOUNT_LABELS, LOCAL_TIMEZONE, ProductError,
        _account_config, _check_personal_access, _read_config, _reader,
        _resolve_config_path, _write_json_atomic)
    from wechat_source import _quote_identifier

    if os.environ.get("AGENTS_BACKGROUND_AUTOMATION") != "1":
        _check_personal_access()
    # The night check runs in its own process and does not keep decrypted copies.
    os.environ["WECHAT_DIRECT_CACHE"] = "0"
    config = _read_config(path_for_read(_resolve_config_path(None)))
    cutoff = int(datetime.now(LOCAL_TIMEZONE).timestamp())
    counts = {}
    for label in ACCOUNT_LABELS:
        with _reader(_account_config(config, label), cutoff) as reader:
            chat = reader._message_database_sources()
            if not chat:
                raise ProductError("wechat_local_message_shards_missing")
            business = [path for path in reader._database_files() if path.parent.name.casefold() == "message"
                        and re.fullmatch(r"biz_message_\d+\.db", path.name, re.I)]
            rows = tables = 0
            for shard in (*chat, *business):
                db = reader._open(shard)
                for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'"):
                    if re.fullmatch(r"Msg_[0-9a-f]{32}", name):
                        rows += db.execute(f"SELECT COUNT(*) FROM {_quote_identifier(name)}").fetchone()[0]
                        tables += 1
            if not tables:
                raise ProductError("wechat_local_message_tables_missing")
            counts[label] = rows
    # This output has one writer under the night runner lock. An interrupted
    # previous attempt may have left the atomic writer's disposable staging file.
    output.with_name(output.name + ".incomplete").unlink(missing_ok=True)
    _write_json_atomic(output, dict(v=1, at=datetime.now(LOCAL_TIMEZONE).isoformat(),
        messages=dict(counts, total=sum(counts.values()))))


def fingerprint(root):
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "last-run.json")
    return hashlib.sha256(json.dumps([(p.relative_to(root).as_posix(), p.stat().st_size,
        p.stat().st_mtime_ns) for p in files]).encode()).hexdigest()


def measure_size(repo, companion):
    paths = [repo / p for p in subprocess.check_output(["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard"],
        encoding="utf-8").splitlines() if Path(p).suffix in {".md", ".py"}]
    docs = [p for p in paths if p.suffix == ".md" and not p.is_symlink()] + ([Path(companion)] if companion else [])
    code = [p for p in paths if p.suffix == ".py"]
    def texts(ps):
        return [p.read_text(encoding="utf-8-sig") for p in ps]
    return dict(at=stamp(max(p.stat().st_mtime for p in docs + code)),
        docs_files=len(docs), code_files=len(code),
        docs_units=sum(len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+", t)) for t in texts(docs)),
        docs_chars=sum(len(re.sub(r"\s", "", t)) for t in texts(docs)),
        code_lines=sum(len(t.splitlines()) for t in texts(code)),
        code_chars=sum(len(re.sub(r"\s", "", t)) for t in texts(code)))


def write_snapshot(root, *, verify=False, public_copy=None, companion=None, owner_output=None):
    from wechat_cli import _verify_export_result, _write_json_atomic
    root = Path(root)
    if not root.is_dir():
        raise ValueError("live_export_root_unavailable")
    repo = Path(__file__).parent
    base = load(repo / "docs/live.snapshot.json")
    if (root / "live.snapshot.json").is_file():
        prior = load(root / "live.snapshot.json")
        base.update({k: prior[k] for k in ("size", "environment_at") if k in prior})
        base["metrics"]["environment"] = prior["metrics"].get("environment")
        base["metrics"]["environment_result"] = prior["metrics"].get("environment_result")
    owner_link = root / "live.owner.json"
    if owner_output and not owner_link.exists():
        target = Path(owner_output).absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        owner_link.symlink_to(target)
    previous = load(owner_link) if owner_link.is_file() else {}
    metrics, rows, owner_rows, local_artifacts, evidence, gaps, versions = {}, [], [], [], {}, Counter(), Counter()
    manifests = sorted(p for p in root.rglob("manifest.json")
        if not any(part.endswith((".incomplete", ".wechat-transaction")) for part in p.relative_to(root).parts))
    times, receipts, totals, checks = [], [], Counter(), []
    for path in manifests:
        m = load(path)
        if m.get("format") != "wechat-direct-contact-export.v1":
            continue
        if type(m.get("messageCount")) is not int or m["messageCount"] < 0:
            raise ValueError("live_archive_count_invalid")
        directory, media = path.parent, []
        with (directory / "messages.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                media.extend(json.loads(line).get("media_manifest", []))
        signature = fingerprint(directory)
        key = hashlib.sha256(str(directory).encode()).hexdigest()
        check = previous.get("evidence", {}).get(key, {})
        if verify:
            check = dict(signature=signature, at=stamp(datetime.now().timestamp()), result=_verify_export_result(directory))
        if check.get("signature") != signature:
            check = {}
        evidence[key] = check
        checks.append(check)
        totals.update(messages=m["messageCount"], media=len(media), saved=sum(bool(x.get("exportedPath")) for x in media),
            wav=sum(bool(x.get("derivedVoiceWav")) for x in media), archive_bytes=m.get("archiveBytes", 0), ai_bytes=m.get("aiDefaultBytes", 0))
        receipt = directory / "last-run.json"
        receipt_time = receipt.stat().st_mtime if receipt.is_file() else None
        if receipt_time is not None:
            receipts.append(receipt_time)
        completed_at = m.get("completedAt") or None
        if completed_at:
            times.append(completed_at)
        gaps.update(m.get("mediaGapCounts", {}))
        totals["gap_registered"] += isinstance(m.get("mediaGapCounts"), dict)
        version = m.get("senderIdentityVersion") or "未登记新版"
        versions[version] += 1
        known = type(m.get("senderIdentityUnrecheckedRetainedCount")) is int
        totals["retained_known"] += known
        totals["retained"] += m["senderIdentityUnrecheckedRetainedCount"] if known else 0
        totals["unknown_known"] += type(m.get("unknownSenderCount")) is int
        totals["unknown_senders"] += m.get("unknownSenderCount") or 0
        last = m.get("lastCreateTimeS")
        if last:
            totals["latest"] = max(totals["latest"], last)
        state = "未检查或档案已变" if not check else "检查通过" if check["result"]["status"] == "success" else "检查未通过"
        row = dict(text=f"档案 {len(rows)+1}", messages=m["messageCount"], latest=stamp(last)[:10] if last else None,
            media=len(media), saved=sum(bool(x.get("exportedPath")) for x in media), state_text=state,
            senderIdentityVersion=version, senderIdentityUnrecheckedRetainedCount=m.get("senderIdentityUnrecheckedRetainedCount"),
            mediaGapCounts=m.get("mediaGapCounts"), at=completed_at)
        if completed_at is None:
            row["at_gap"] = "当时没记"
        rows.append(row)
        if check.get("result", {}).get("status") == "success":
            artifact = dict(at=completed_at or check["at"], kind="archive",
                title=f"{row['text']}：{row['messages']} 条，新到 {row['latest']}，留住媒体 {row['saved']}/{row['media']}，{row['state_text']}",
                status="completed", public="owner", open=dict(source="wechat.contacts", path=(directory.relative_to(root / "contacts") / "context.md").as_posix()))
            if completed_at is None:
                artifact.update(recent_title=f"{row['text']}（核对时间）", recent_detail="产物日期为档案核对时间，存档时没记完成时间。")
            local_artifacts.append(artifact)
        owner_rows.append(dict(row, text=m.get("contact", {}).get("displayName") or m.get("contact", {}).get("nickname") or "未命名档案",
            account=m.get("account")))
    complete = all(checks) and len(checks) == len(rows)
    passed = sum(c.get("result", {}).get("status") == "success" for c in checks)
    gap_labels = dict(only_thumbnail_local="只有缩略图", remote_not_requested="没取远端",
        local_wxgf_conversion_unavailable="格式转换不了", image_not_stored_locally="图没存本地",
        native_media_missing_or_unreadable="原生媒体缺失或不可读")
    receipt_at = stamp(max(receipts)) if receipts else None
    verification_at = max((c["at"] for c in checks if c), default=None)
    snapshot_at = max((at for at in [*times, receipt_at, verification_at] if at),
        key=datetime.fromisoformat, default=None)
    metrics.update(archives=len(rows), messages=totals["messages"], latest=stamp(totals["latest"])[:10] if totals["latest"] else None,
        media=totals["media"], saved=totals["saved"], missing=totals["media"]-totals["saved"], wav=totals["wav"],
        archive_bytes=totals["archive_bytes"], ai_bytes=totals["ai_bytes"], archive_records=rows,
        integrity=f"{passed}/{len(rows)} 份" if complete else None,
        checked_media=sum(c["result"]["mediaFilesChecked"] for c in checks) if complete else None,
        check_errors=sum(len(c["result"]["errors"]) for c in checks) if complete else None,
        receipt_date=receipt_at[:10] if receipt_at else None,
        gap_reasons=[dict(text=gap_labels.get(k, k), count=gaps[k] if totals["gap_registered"] == len(rows) else None,
            state_text=f"{totals['gap_registered']}/{len(rows)} 份有原因字段") for k in sorted(gaps.keys() | gap_labels.keys())],
        identity_versions=[dict(text=k, count=v, state_text="已登记新版" if k != "未登记新版" else k) for k, v in sorted(versions.items())],
        unrechecked=totals["retained"] if totals["retained_known"] == len(rows) else None,
        unknown_senders=totals["unknown_senders"] if totals["unknown_known"] == len(rows) else None)
    if 0 < totals["retained_known"] < len(rows):
        known, retained = totals["retained_known"], totals["retained"]
        metrics["unrechecked"] = dict(known_archives=known, archives=len(rows), known_retained=retained,
            text=f"已登记的 {known}/{len(rows)} 份档案共 {retained} 条，其余旧档未登记")
    short_text = f"{totals['messages']:,} 条"
    metrics["messages_summary"] = dict(text=f"{short_text}（{len(rows)} 份）", messages=totals["messages"], archives=len(rows),
        short_text=short_text, explanation_text=f"来自 {len(rows)} 份已完成的联系人聊天档案")
    metrics["saved_summary"] = dict(text=f"{totals['saved']:,} 项（{totals['saved']/totals['media']:.1%}）" if totals["media"] else "0 项", saved=totals["saved"], total=totals["media"])
    data = dict(base, at=snapshot_at,
        scope="仅默认导出根的完成联系人档案；快照截至最近同步或核对，单份档案缺完成记录时留空；不补拉媒体",
        metrics=dict(base["metrics"], **metrics), verification_at=verification_at,
        verification_note=None if complete else "档案尚未检查或检查后文件已变；按需运行 live-snapshot --verify",
        artifacts=[a for a in base.get("artifacts", []) if a["kind"] != "archive"])
    data.pop("receipt_at", None)
    data["metrics"]["identity_coverage"] = f"{len(rows)-totals['retained_known']} 份未登记新版；已登记保留数合计 {totals['retained']} 条"
    if companion:
        data["size"] = measure_size(repo, companion)
    if verify:
        from wechat_cli import command_doctor
        stream = SimpleNamespace(buffer=io.BytesIO())
        with redirect_stdout(stream):
            command_doctor(SimpleNamespace(environment_only=True))
        doctor = json.loads(stream.buffer.getvalue())
        data["environment_at"] = stamp(datetime.now().timestamp())
        data["metrics"]["environment_result"] = dict(text=f"{'通过' if doctor['status'] == 'success' else '未通过'}；{len(doctor['errors'])} 处错误、{len(doctor['warnings'])} 条提醒",
            status=doctor["status"], errors=len(doctor["errors"]), warnings=len(doctor["warnings"]))
        labels = dict(images="图片", offlineVerification="离线验证", text="文字", voiceWav="语音 WAV", wxgf="WXGF 图像")
        data["metrics"]["environment"] = [dict(text=labels[k], state_text="可用" if v == "available" else "未就绪") for k, v in doctor["capabilities"].items()]
    owner = dict(v=1, at=data["at"], receipt_at=receipt_at, scope=data["scope"],
        receipt_public_at=receipt_at[:10] + "T00:00:00+08:00" if receipt_at else None,
        public_scope="仅默认导出根档案；回执源时刻公开精度为日期，零点仅表示该日",
        metrics=dict(archives=owner_rows, receipt_time=receipt_at, receipt_date=metrics["receipt_date"]),
        evidence=evidence, artifacts=data["artifacts"] + local_artifacts)
    _write_json_atomic(owner_link.resolve(), owner)
    _write_json_atomic(root / "live.snapshot.json", data)
    if public_copy:
        static = {k: data[k] for k in ("v", "performance", "performance_at", "performance_scope", "environment_at", "environment_scope", "verification_scope", "size_scope", "size", "artifacts") if k in data}
        static.update(at=max(data.get("environment_at", ""), (data.get("size") or {}).get("at", ""), data["performance_at"]),
            scope="按需更新的规模、依赖检查和历史实测", metrics={k: data["metrics"][k] for k in ("measurements", "environment", "environment_result")})
        destination = Path(public_copy)
        if not destination.is_file() or load(destination) != static:
            _write_json_atomic(destination, static)
    return dict(status="success", archives=len(rows), at=data["at"])


def command(args):
    from wechat_cli import _canonical_bytes, ProductError
    import sys
    if not args.owner_output and not (Path(args.exports) / "live.owner.json").is_symlink():
        raise ProductError("live_owner_output_required")
    sys.stdout.buffer.write(_canonical_bytes(write_snapshot(args.exports, verify=args.verify,
        public_copy=args.public_copy, companion=args.companion_doc, owner_output=args.owner_output)))
    return 0


def refresh(root):
    if (root / "live.snapshot.json").is_file():
        try:
            return write_snapshot(root)
        except (OSError, ValueError) as exc:
            return dict(status="failed", error=type(exc).__name__)
    return dict(status="not_enabled")

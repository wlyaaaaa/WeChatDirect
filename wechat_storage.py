"""Scoped disposable storage and recoverable directory publication.

No daemon, source database writes, or implicit recovery. An OS-held lock covers
publication/recovery; a crashed transaction remains discoverable at one exact
sibling directory. The flat v1 archive layout remains compatible.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Callable, Iterator


class StorageError(RuntimeError):
    pass


def _json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".new")
    with temporary.open("xb") as stream:
        stream.write(json.dumps(value, sort_keys=True).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _no_links(root: Path) -> None:
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise StorageError("archive_link_not_supported")
    if root.is_dir():
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                p = Path(directory) / name
                if p.is_symlink() or getattr(p, "is_junction", lambda: False)():
                    raise StorageError("archive_link_not_supported")


@contextmanager
def exclusive_file(path: Path, *, create: bool = True) -> Iterator[None]:
    """OS lock releases on process death; PID text is never a lock authority."""
    with path.open("a+b" if create else "r+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            if not create:
                raise StorageError("archive_lease_invalid")
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StorageError("archive_operation_running") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _content_fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _tree_signature(root: Path) -> list[tuple]:
    if not root.exists():
        return []
    _no_links(root)
    return sorted(
        (
            p.relative_to(root).as_posix(),
            p.stat().st_size,
            p.stat().st_mtime_ns,
            _content_fingerprint(p) if p.is_file() else None,
        )
        for p in root.rglob("*")
    )


def transaction_path(output: Path) -> Path:
    return output.with_name(output.name + ".wechat-transaction")


def _target_key(output: Path) -> str:
    return hashlib.sha256(
        os.path.normcase(str(output.resolve())).encode("utf-8")
    ).hexdigest()


@contextmanager
def archive_transaction(output: Path, verify: Callable[[Path], None]) -> Iterator[Path]:
    """Stage a replacement, verify it, then publish with a recoverable old copy.

    The two directory renames are not a filesystem-wide atomic transaction.
    A hard stop between them is explicitly recoverable; readers must not label
    an absent target as an empty archive. Ordinary exceptions roll back.
    """
    output = output.absolute()
    _no_links(output)
    if output.exists() and not output.is_dir():
        raise StorageError("sync_output_is_not_directory")
    if (output / ".sync.lock").exists():
        raise StorageError("sync_already_running_or_stale_lock")
    tx = transaction_path(output)
    try:
        tx.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise StorageError("archive_recovery_required") from exc
    stage, previous = tx / "stage", tx / "previous"
    cleanup = False
    try:
        with exclusive_file(tx / "lease"):
            metadata = {
                "format": "wechat-directory-transaction.v1",
                "target": _target_key(output),
                "createdAtS": int(time.time()),
                "phase": "building",
                "hadOriginal": output.exists(),
            }
            _json(tx / "transaction.json", metadata)
            before = _tree_signature(output)
            try:
                if output.exists():
                    shutil.copytree(output, stage)
                else:
                    stage.mkdir()
                yield stage
                verify(stage)
                if (
                    _tree_signature(output) != before
                    or (output / ".sync.lock").exists()
                ):
                    raise StorageError("archive_changed_during_transaction")
                metadata["phase"] = "ready"
                _json(tx / "transaction.json", metadata)
                if output.exists():
                    output.rename(previous)
                try:
                    stage.rename(output)
                except Exception:
                    if previous.exists() and not output.exists():
                        previous.rename(output)
                    raise
                metadata["phase"] = "published"
                _json(tx / "transaction.json", metadata)
                cleanup = True
            except Exception:
                # Do not discard the only complete original if rollback itself fails.
                if previous.exists() and not output.exists():
                    previous.rename(output)
                cleanup = not previous.exists()
                raise
            finally:
                # BaseException (including injected hard stops) deliberately retains
                # the stage/journal for explicit recovery, rather than guessing.
                pass
    finally:
        if cleanup:
            _no_links(tx)
            shutil.rmtree(tx)


def recover_archive(output: Path, action: str, verify: Callable[[Path], None]) -> dict:
    output = output.absolute()
    if output.exists() and not output.is_dir():
        raise StorageError("sync_output_is_not_directory")
    tx = transaction_path(output)
    if not tx.exists():
        return {"status": "success", "transactionPresent": False, "action": action}
    _no_links(tx)
    metadata_path = tx / "transaction.json"
    try:
        metadata = json.loads(metadata_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageError("archive_recovery_metadata_invalid") from exc
    if not isinstance(metadata, dict):
        raise StorageError("archive_recovery_metadata_invalid")
    if metadata.get("format") != "wechat-directory-transaction.v1" or metadata.get(
        "target"
    ) != _target_key(output):
        raise StorageError("archive_recovery_target_mismatch")
    stage, previous = tx / "stage", tx / "previous"
    result = {
        "status": "success",
        "transactionPresent": True,
        "action": action,
        "phase": metadata.get("phase"),
        "outputPresent": output.is_dir(),
        "stagedPresent": stage.is_dir(),
        "previousPresent": previous.is_dir(),
    }
    if action == "inspect":
        return result
    if action not in {"rollback", "complete"}:
        raise StorageError("archive_recovery_action_invalid")
    with exclusive_file(tx / "lease"):
        if action == "complete":
            if stage.is_dir():
                if metadata.get("phase") != "ready":
                    raise StorageError("archive_staged_not_ready")
                verify(stage)
                if output.exists():
                    if previous.exists():
                        raise StorageError("archive_recovery_state_ambiguous")
                    verify(output)
                    output.rename(previous)
                stage.rename(output)
            elif (
                output.is_dir()
                and (previous.is_dir() or metadata.get("hadOriginal") is False)
                and metadata.get("phase") in {"ready", "published"}
            ):
                verify(output)
            else:
                raise StorageError("archive_recovery_state_ambiguous")
        elif previous.is_dir():
            verify(previous)
            if output.exists():
                # Published result remains usable; rollback doesn't silently
                # delete it. Finishing cleanup is the correct action here.
                raise StorageError("archive_already_published_use_complete")
            previous.rename(output)
        elif (
            output.is_dir()
            and not stage.exists()
            and metadata.get("phase") in {"ready", "published"}
        ):
            raise StorageError("archive_already_published_use_complete")
        elif metadata.get("phase") not in {"building", "ready"}:
            raise StorageError("archive_recovery_state_ambiguous")
        elif metadata.get("hadOriginal") and not output.is_dir():
            raise StorageError("archive_original_unavailable")
    _no_links(tx)
    shutil.rmtree(tx)
    return {
        **result,
        "recovered": True,
        "observedPhase": result.get("phase"),
        "phase": "completed" if action == "complete" else "rolled_back",
        "transactionPresent": tx.exists(),
        "outputPresent": output.is_dir(),
        "stagedPresent": stage.is_dir(),
        "previousPresent": previous.is_dir(),
    }


class ScratchDirectory:
    """Private task-local rebuildable scratch; no plaintext body in the marker."""

    def __init__(self, prefix: str = "wechat-direct-"):
        root = (
            Path(os.environ.get("WECHAT_DIRECT_TEMP_ROOT") or tempfile.gettempdir())
            / "wechat-direct-scratch"
        )
        root.mkdir(parents=True, exist_ok=True)
        self.name = tempfile.mkdtemp(prefix=prefix, dir=root)
        self._lease = exclusive_file(Path(self.name) / ".lease")
        self._lease.__enter__()
        _json(
            Path(self.name) / ".scratch.json",
            {
                "format": "wechat-disposable-scratch.v1",
                "rebuildable": True,
                "createdAtS": int(time.time()),
                "pid": os.getpid(),
            },
        )
        self._closed = False

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *args) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._lease.__exit__(None, None, None)
        self._closed = True
        shutil.rmtree(self.name)
        try:
            Path(self.name).parent.rmdir()
        except OSError:
            pass


def scratch_status(
    root: Path, *, session: str | None = None, clean: bool = False
) -> dict:
    """Inspect metadata only, or remove one explicitly named inactive scratch."""
    root = root.absolute() / "wechat-direct-scratch"
    _no_links(root)
    if clean and not session:
        raise StorageError("scratch_exact_session_required")
    if session and (Path(session).name != session or session in {".", ".."}):
        raise StorageError("scratch_session_invalid")
    candidates = (
        [root / session] if session else sorted(root.iterdir()) if root.is_dir() else []
    )
    items = []
    for path in candidates:
        if not path.is_dir():
            continue
        try:
            marker = json.loads((path / ".scratch.json").read_text("utf-8"))
        except (OSError, ValueError):
            items.append({"session": path.name, "status": "unknown_not_removed"})
            continue
        if (
            not isinstance(marker, dict)
            or marker.get("format") != "wechat-disposable-scratch.v1"
            or marker.get("rebuildable") is not True
        ):
            raise StorageError("scratch_marker_invalid")
        try:
            with exclusive_file(path / ".lease", create=False):
                pass
        except FileNotFoundError:
            items.append({"session": path.name, "status": "unknown_not_removed"})
            continue
        except StorageError as exc:
            items.append(
                {
                    "session": path.name,
                    "status": "active"
                    if str(exc) == "archive_operation_running"
                    else "unknown_not_removed",
                }
            )
            continue
        if clean:
            if not session:
                raise StorageError("scratch_exact_session_required")
            # Scratch sessions have unique names and no resume/reopen API.
            # A second explicit cleanup may finish this same inactive session.
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                items.append({"session": path.name, "status": "already_removed"})
                continue
        items.append(
            {
                "session": path.name,
                "status": "removed" if clean else "inactive",
                "createdAtS": marker.get("createdAtS"),
            }
        )
    return {"status": "success", "sessions": items}

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


@contextmanager
def _output_access() -> Iterator[None]:
    """Report a sharing violation from another process as one stable code."""
    try:
        yield
    except PermissionError as exc:
        raise StorageError("archive_output_in_use") from exc


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_int64),
            ("LastAccessTime", ctypes.c_int64),
            ("LastWriteTime", ctypes.c_int64),
            ("ChangeTime", ctypes.c_int64),
            ("FileAttributes", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _change_time(path: Path, _stat: os.stat_result) -> int:
        # Windows st_ctime is the creation time, so a rewrite that restores
        # its write time would be invisible. NTFS ChangeTime is not settable
        # through os.utime and is read without opening the file's data.
        handle = _kernel32.CreateFileW(
            str(path),
            0x80,  # FILE_READ_ATTRIBUTES
            0x7,  # share read, write and delete
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS (directories)
            None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _FileBasicInfo()
            if not _kernel32.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return info.ChangeTime
        finally:
            _kernel32.CloseHandle(handle)

else:

    def _change_time(_path: Path, stat: os.stat_result) -> int:
        return stat.st_ctime_ns


def _tree_signature(root: Path) -> list[tuple]:
    """Detect concurrent archive edits from metadata alone.

    Size, write time and the filesystem change time move on any rewrite,
    including one that restores the previous write time, so an unchanged
    archive is confirmed without reading and hashing every file.
    """
    if not root.exists():
        return []
    _no_links(root)
    entries = []
    for p in root.rglob("*"):
        stat = p.stat()
        entries.append(
            (
                p.relative_to(root).as_posix(),
                p.is_file(),
                stat.st_size,
                stat.st_mtime_ns,
                _change_time(p, stat),
            )
        )
    return sorted(entries)


def _copy_file(source: str, target: str) -> object:
    # copytree folds per-file OSErrors into one text-only shutil.Error; raise
    # the stable code before that loses the sharing-violation type.
    with _output_access():
        return shutil.copy2(source, target)


def transaction_path(output: Path) -> Path:
    return output.with_name(output.name + ".wechat-transaction")


def _target_key(output: Path) -> str:
    return hashlib.sha256(
        os.path.normcase(str(output.resolve())).encode("utf-8")
    ).hexdigest()


@contextmanager
def archive_transaction(
    output: Path,
    verify: Callable[[Path], None],
    *,
    probe: Callable[[Path], dict | None] | None = None,
) -> Iterator[Path | dict]:
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
    cleanup = failed = False
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
            try:
                with _output_access():
                    before = _tree_signature(output)
                if probe is not None and output.exists():
                    unchanged = probe(output)
                    if unchanged is not None:
                        with _output_access():
                            after = _tree_signature(output)
                        if after != before or (output / ".sync.lock").exists():
                            raise StorageError("archive_changed_during_transaction")
                        cleanup = True
                        yield unchanged
                        return
                if output.exists():
                    with _output_access():
                        shutil.copytree(output, stage, copy_function=_copy_file)
                else:
                    stage.mkdir()
                yield stage
                with _output_access():
                    verify(stage)
                    after = _tree_signature(output)
                if after != before or (output / ".sync.lock").exists():
                    raise StorageError("archive_changed_during_transaction")
                metadata["phase"] = "ready"
                _json(tx / "transaction.json", metadata)
                if output.exists():
                    try:
                        output.rename(previous)
                    except PermissionError as exc:
                        raise StorageError("archive_output_in_use") from exc
                try:
                    stage.rename(output)
                except Exception as exc:
                    if previous.exists() and not output.exists():
                        previous.rename(output)
                    if isinstance(exc, PermissionError):
                        raise StorageError("archive_output_in_use") from exc
                    raise
                metadata["phase"] = "published"
                _json(tx / "transaction.json", metadata)
                cleanup = True
            except Exception:
                failed = True
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
            try:
                shutil.rmtree(tx)
            except PermissionError as exc:
                # A held file in the replaced copy leaves the journal in place
                # for recover-export. Keep an earlier failure as the reported
                # cause instead of masking it with this cleanup error.
                if not failed:
                    raise StorageError("archive_output_in_use") from exc


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
    with exclusive_file(tx / "lease"), _output_access():
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
    with _output_access():
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

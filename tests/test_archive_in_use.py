from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_storage import (
    StorageError,
    archive_transaction,
    recover_archive,
    transaction_path,
)


def _archive(root: Path) -> Path:
    output = root / "export"
    (output / "media").mkdir(parents=True)
    (output / "record.txt").write_text("old", encoding="utf-8")
    (output / "media" / "a.bin").write_bytes(b"\x00" * 64)
    return output


class UnchangedArchiveSignatureTests(unittest.TestCase):
    def test_unchanged_archive_is_confirmed_without_reading_its_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            opened: list[Path] = []
            original_open = Path.open

            def tracking_open(path, *args, **kwargs):
                opened.append(Path(path))
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", tracking_open):
                with archive_transaction(
                    output,
                    lambda _stage: None,
                    probe=lambda _output: {"noChange": True},
                ) as receipt:
                    self.assertEqual(receipt, {"noChange": True})
            self.assertEqual([path for path in opened if output in path.parents], [])
            self.assertFalse(transaction_path(output).exists())

    def test_no_change_probe_detects_rewrite_that_restores_write_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            target = output / "media" / "a.bin"
            stat = target.stat()

            def probe(_output):
                target.write_bytes(b"\x01" * 64)
                os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                return {"noChange": True}

            with self.assertRaisesRegex(
                StorageError, "archive_changed_during_transaction"
            ):
                with archive_transaction(output, lambda _stage: None, probe=probe):
                    pass
            self.assertEqual(target.read_bytes(), b"\x01" * 64)
            self.assertFalse(transaction_path(output).exists())


class ArchiveInUseTests(unittest.TestCase):
    def assert_original_kept(self, output: Path) -> None:
        self.assertEqual((output / "record.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse(transaction_path(output).exists())

    def test_held_file_during_copy_reports_in_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            with patch(
                "wechat_storage.shutil.copy2",
                side_effect=PermissionError(13, "synthetic sharing violation"),
            ):
                with self.assertRaisesRegex(StorageError, "archive_output_in_use"):
                    with archive_transaction(output, lambda _stage: None):
                        self.fail("the body must not run after a failed copy")
            self.assert_original_kept(output)

    def test_held_file_during_signature_reports_in_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            with patch(
                "wechat_storage._change_time",
                side_effect=PermissionError(13, "synthetic sharing violation"),
            ):
                with self.assertRaisesRegex(StorageError, "archive_output_in_use"):
                    with archive_transaction(output, lambda _stage: None):
                        self.fail("the body must not run after a failed signature")
            self.assert_original_kept(output)

    def test_held_file_during_verification_reports_in_use(self) -> None:
        def verify(_stage: Path) -> None:
            raise PermissionError(13, "synthetic sharing violation")

        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            with self.assertRaisesRegex(StorageError, "archive_output_in_use"):
                with archive_transaction(output, verify) as stage:
                    (stage / "record.txt").write_text("new", encoding="utf-8")
            self.assert_original_kept(output)

    def test_held_file_during_cleanup_reports_in_use_and_stays_recoverable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            with patch(
                "wechat_storage.shutil.rmtree",
                side_effect=PermissionError(13, "synthetic sharing violation"),
            ):
                with self.assertRaisesRegex(StorageError, "archive_output_in_use"):
                    with archive_transaction(output, lambda _stage: None) as stage:
                        (stage / "record.txt").write_text("new", encoding="utf-8")
            # Publication finished before cleanup; only the journal remains.
            self.assertEqual((output / "record.txt").read_text(encoding="utf-8"), "new")
            self.assertTrue(transaction_path(output).exists())
            with patch(
                "wechat_storage.shutil.rmtree",
                side_effect=PermissionError(13, "synthetic sharing violation"),
            ):
                with self.assertRaisesRegex(StorageError, "archive_output_in_use"):
                    recover_archive(output, "complete", lambda _root: None)
            receipt = recover_archive(output, "complete", lambda _root: None)
            self.assertEqual(receipt["phase"], "completed")
            self.assertFalse(transaction_path(output).exists())

    def test_cleanup_error_does_not_mask_an_earlier_failure(self) -> None:
        def verify(_stage: Path) -> None:
            raise StorageError("archive_verification_failed")

        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            with patch(
                "wechat_storage.shutil.rmtree",
                side_effect=PermissionError(13, "synthetic sharing violation"),
            ):
                with self.assertRaisesRegex(
                    StorageError, "archive_verification_failed"
                ):
                    with archive_transaction(output, verify):
                        pass
            self.assertEqual((output / "record.txt").read_text(encoding="utf-8"), "old")

    @unittest.skipUnless(os.name == "nt", "Windows sharing violation")
    def test_real_exclusive_handle_during_copy_reports_in_use(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as temporary:
            output = _archive(Path(temporary))
            handle = kernel32.CreateFileW(
                str(output / "media" / "a.bin"),
                0x80000000,  # GENERIC_READ
                0,  # no sharing: another reader gets ERROR_SHARING_VIOLATION
                None,
                3,  # OPEN_EXISTING
                0,
                None,
            )
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            try:
                with self.assertRaisesRegex(StorageError, "archive_output_in_use"):
                    with archive_transaction(output, lambda _stage: None):
                        self.fail("the body must not run after a failed copy")
            finally:
                kernel32.CloseHandle(handle)
            self.assert_original_kept(output)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import import_service


class ImportZipSecurityTest(unittest.TestCase):
    def test_rejects_path_traversal_before_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            zip_path = temp_path / "import.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../escape.txt", "secret")

            with self.assertRaisesRegex(ValueError, "非法路径"):
                import_service.count_phone_dirs(zip_path, temp_path / "extract")

            self.assertFalse((temp_path / "escape.txt").exists())

    def test_rejects_extracted_total_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            zip_path = temp_path / "import.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("+12345678/account.session", b"1234")

            with patch.object(import_service, "MAX_EXTRACTED_BYTES", 3):
                with self.assertRaisesRegex(ValueError, "解压后总大小"):
                    import_service.count_phone_dirs(zip_path, temp_path / "extract")

    def test_rejects_too_many_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            zip_path = temp_path / "import.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("first.session", b"1")
                zf.writestr("second.session", b"2")

            with patch.object(import_service, "MAX_ZIP_MEMBERS", 1):
                with self.assertRaisesRegex(ValueError, "文件数量"):
                    import_service.count_phone_dirs(zip_path, temp_path / "extract")

    def test_oversized_upload_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            upload_dir = Path(temp)
            settings = SimpleNamespace(upload_dir=upload_dir)
            with (
                patch.object(import_service, "get_settings", return_value=settings),
                patch.object(import_service, "MAX_UPLOAD_BYTES", 3),
                patch.object(import_service, "UPLOAD_CHUNK_BYTES", 2),
            ):
                with self.assertRaisesRegex(ValueError, "不能超过"):
                    import_service.save_upload(io.BytesIO(b"1234"))

            self.assertEqual([], list(upload_dir.iterdir()))


class ImportCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_import_cleans_success_and_failed_batches(self) -> None:
        for error in (None, RuntimeError("import failed")):
            with self.subTest(error=error):
                with tempfile.TemporaryDirectory() as temp:
                    temp_path = Path(temp)
                    zip_path = temp_path / "import.zip"
                    zip_path.write_bytes(b"zip")
                    extract_root = temp_path / "extract"
                    (extract_root / "+12345678").mkdir(parents=True)
                    settings = SimpleNamespace(sessions_dir=temp_path / "sessions")
                    importer = AsyncMock(side_effect=error)
                    with (
                        patch.object(import_service, "get_settings", return_value=settings),
                        patch.object(import_service, "_import_one", importer),
                        patch.object(import_service.batch_store, "finish"),
                        patch.object(import_service.batch_store, "set_item"),
                    ):
                        await import_service.run_import(zip_path, "batch", extract_root)

                    self.assertFalse(zip_path.exists())
                    self.assertFalse(extract_root.exists())

    async def test_run_import_cleans_empty_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            zip_path = temp_path / "import.zip"
            zip_path.write_bytes(b"zip")
            extract_root = temp_path / "extract"
            extract_root.mkdir()
            settings = SimpleNamespace(sessions_dir=temp_path / "sessions")
            with (
                patch.object(import_service, "get_settings", return_value=settings),
                patch.object(import_service.batch_store, "finish"),
            ):
                await import_service.run_import(zip_path, "batch", extract_root)

            self.assertFalse(zip_path.exists())
            self.assertFalse(extract_root.exists())


if __name__ == "__main__":
    unittest.main()

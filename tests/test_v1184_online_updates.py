from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app.app_paths import AppPaths
from app.update_config import (
    UPDATE_CHECK_INTERVAL_SECONDS,
    UPDATE_DRIVE_FOLDER_ID,
    UPDATE_DRIVE_FOLDER_URL,
    UPDATE_MANIFEST_FILE_ID,
    UPDATE_MANIFEST_FILE_URL,
)
from app.update_service import (
    UpdateInfo,
    UpdateNetworkError,
    UpdateNotReady,
    UpdateService,
)


class _Closable:
    def close(self):
        pass


class _FakeResponse:
    def __init__(self, chunks, *, status=200):
        self._chunks = list(chunks)
        self.status = status
        self.headers = {}

    def getcode(self):
        return self.status

    def read(self, _size=-1):
        if not self._chunks:
            return b""
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


class V1185OnlineUpdateTests(unittest.TestCase):
    def _paths(self, root: Path) -> AppPaths:
        return AppPaths(
            executable_dir=root,
            resources=root,
            runtime=root,
            data=root / "data",
            documents=root / "data" / "documents",
            backups=root / "backups",
            logs=root / "logs",
            paddle_cache=root / "data" / "paddle_cache",
            assets=root / "assets",
            models=root / "models",
        )

    def test_production_drive_and_manifest_links_are_hardcoded_and_hourly(self):
        self.assertEqual(
            UPDATE_DRIVE_FOLDER_URL,
            "https://drive.google.com/drive/folders/1azf1k4aCEtzg6d6-NTffq52guIdARF7q?usp=sharing",
        )
        self.assertEqual(UPDATE_DRIVE_FOLDER_ID, "1azf1k4aCEtzg6d6-NTffq52guIdARF7q")
        self.assertEqual(UPDATE_MANIFEST_FILE_ID, "1DAYJCCB1eO0zSvqNeWsWJREXbyMoSse8")
        self.assertEqual(
            UPDATE_MANIFEST_FILE_URL,
            "https://drive.google.com/file/d/1DAYJCCB1eO0zSvqNeWsWJREXbyMoSse8/view?usp=sharing",
        )
        self.assertEqual(UPDATE_CHECK_INTERVAL_SECONDS, 3600)

    def test_remote_manifest_is_read_directly_by_stable_file_id(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            service = UpdateService(self._paths(root))
            package = b"remote-package"
            manifest = {
                "format_version": 1,
                "version": "1.18.6",
                "package": "DocumentRegistry_update.zip",
                "package_file_id": "P" * 25,
                "sha256": hashlib.sha256(package).hexdigest(),
                "size": len(package),
                "database_schema": 3,
                "release_notes": ["online", "background"],
            }
            with mock.patch.object(
                service,
                "_read_drive_file",
                return_value=json.dumps(manifest).encode("utf-8"),
            ) as read_mock:
                info = service.check_for_update(None, "1.18.5")

            read_mock.assert_called_once_with(UPDATE_MANIFEST_FILE_ID, mock.ANY)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.source, "google_drive")
            self.assertEqual(info.package_file_id, "P" * 25)
            self.assertIsNone(info.package_path)
            self.assertEqual(info.notes, "online\nbackground")

    def test_same_or_older_manifest_does_not_require_package_metadata(self):
        with tempfile.TemporaryDirectory() as name:
            service = UpdateService(self._paths(Path(name)))
            manifest = {"format_version": 1, "version": "1.18.4"}
            with mock.patch.object(
                service,
                "_read_drive_file",
                return_value=json.dumps(manifest).encode("utf-8"),
            ):
                self.assertIsNone(service.check_for_update(None, "1.18.5"))

    def test_new_remote_release_requires_package_file_id(self):
        with tempfile.TemporaryDirectory() as name:
            service = UpdateService(self._paths(Path(name)))
            payload = b"package"
            manifest = {
                "format_version": 1,
                "version": "1.18.6",
                "package": "DocumentRegistry_update.zip",
                "package_file_id": "",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            with mock.patch.object(
                service,
                "_read_drive_file",
                return_value=json.dumps(manifest).encode("utf-8"),
            ):
                with self.assertRaises(UpdateNotReady):
                    service.check_for_update(None, "1.18.5")

    def test_interrupted_remote_download_keeps_partial_and_next_try_resumes(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            service = UpdateService(self._paths(root))
            payload = b"abcdefghij"
            info = UpdateInfo(
                version="1.18.6",
                package_path=None,
                package_file_id="P" * 25,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                source="google_drive",
            )

            first_stream = _FakeResponse([b"abcd", OSError("offline")], status=200)
            with (
                mock.patch.object(
                    service,
                    "_open_drive_binary",
                    return_value=(object(), _Closable(), "https://example.invalid/file"),
                ),
                mock.patch.object(service, "_request", return_value=first_stream),
            ):
                with self.assertRaises(UpdateNetworkError):
                    service.prepare_update(info)

            partial = root / "data" / "updates" / "DocumentRegistry_1.18.6_update.zip.part"
            self.assertTrue(partial.is_file())
            self.assertEqual(partial.read_bytes(), b"abcd")

            second_stream = _FakeResponse([b"efghij", b""], status=206)
            with (
                mock.patch.object(
                    service,
                    "_open_drive_binary",
                    return_value=(object(), _Closable(), "https://example.invalid/file"),
                ),
                mock.patch.object(service, "_request", return_value=second_stream) as request_mock,
            ):
                completed = service.prepare_update(info)

            self.assertEqual(completed.read_bytes(), payload)
            self.assertFalse(partial.exists())
            headers = request_mock.call_args.kwargs.get("headers", {})
            self.assertEqual(headers.get("Range"), "bytes=4-")

    def test_confirmation_form_url_is_reconstructed(self):
        html = """
        <form id="download-form" action="https://drive.usercontent.google.com/download">
          <input type="hidden" name="id" value="FILE123" />
          <input type="hidden" name="confirm" value="token123" />
        </form>
        """
        url = UpdateService._confirmation_url(html)
        self.assertIn("drive.usercontent.google.com/download", url)
        self.assertIn("id=FILE123", url)
        self.assertIn("confirm=token123", url)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import tempfile

from app.portable_paths import RUNTIME_TOKEN, absolute_without_resolving
from app.resource_profile import normalize_performance_mode

logger = logging.getLogger(__name__)
VALID_PAGES = {"registry", "documents", "history", "settings"}


@dataclass(slots=True)
class AppSettings:
    documents_dir: str
    confirm_delete: bool = True
    last_page: str = "registry"
    performance_mode: str = "auto"
    update_source_dir: str = ""


class SettingsService:
    """Loads and atomically saves application settings.

    Paths inside the application runtime root are serialized as ``@runtime/...``
    instead of an absolute ``C:\\...`` path.  Therefore moving the whole app
    folder to another Desktop/disk/computer does not invalidate settings.json.
    """

    def __init__(
        self,
        settings_file: str | Path,
        default_documents_dir: str | Path,
        runtime_root: str | Path | None = None,
    ):
        self.settings_file = Path(settings_file)
        # Do not use Path.resolve() for user/runtime paths here. On Windows it may
        # expand an 8.3 component (for example C:\\Users\\ABCDEF~1) into the long
        # Unicode profile name. Both paths point to the same location, but changing
        # their spelling breaks deterministic portable settings and path equality.
        self.default_documents_dir = self._absolute_path(default_documents_dir)
        self.runtime_root = (
            self._absolute_path(runtime_root)
            if runtime_root is not None
            else self.default_documents_dir.parent.parent
        )

    @staticmethod
    def _absolute_path(value: str | Path) -> Path:
        """Return an absolute path without resolving aliases/symlinks/8.3 names."""
        return absolute_without_resolving(value)

    def defaults(self) -> AppSettings:
        return AppSettings(documents_dir=str(self.default_documents_dir))

    def _decode_path(self, raw_value: object) -> Path:
        text = str(raw_value or "").strip()
        if text.startswith(RUNTIME_TOKEN):
            # Avoid platform-specific string replacement tricks; Pure path parts
            # are reconstructed from '/' which is accepted by pathlib on Windows.
            parts = [part for part in text[len(RUNTIME_TOKEN):].split("/") if part not in {"", "."}]
            if ".." in parts:
                return self.default_documents_dir
            return self._absolute_path(self.runtime_root.joinpath(*parts))
        if not text:
            return self.default_documents_dir
        return self._absolute_path(text)

    def _encode_path(self, value: str | Path) -> str:
        path = self._absolute_path(value)
        try:
            relative = path.relative_to(self.runtime_root)
        except ValueError:
            return str(path)
        return RUNTIME_TOKEN + relative.as_posix()

    def _repair_legacy_documents_path(self, path: Path) -> Path:
        if path.exists():
            return path
        # A missing absolute folder usually means the app/database was moved to
        # another PC or disk.  Do not recreate a stale C:\old-machine\... path;
        # continue with the current portable documents folder instead.
        return self.default_documents_dir

    def load(self) -> AppSettings:
        defaults = self.defaults()
        if not self.settings_file.exists():
            self.save(defaults)
            return defaults

        try:
            with self.settings_file.open("r", encoding="utf-8") as file:
                raw = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Не удалось прочитать настройки, используются значения по умолчанию: %s", error)
            return defaults

        if not isinstance(raw, dict):
            logger.warning("Некорректный формат settings.json, используются значения по умолчанию")
            return defaults

        documents_path = self._repair_legacy_documents_path(self._decode_path(raw.get("documents_dir")))
        confirm_delete = bool(raw.get("confirm_delete", defaults.confirm_delete))
        last_page = str(raw.get("last_page") or defaults.last_page)
        if last_page not in VALID_PAGES:
            last_page = defaults.last_page
        performance_mode = normalize_performance_mode(raw.get("performance_mode"))
        update_source_dir = str(raw.get("update_source_dir") or "").strip()
        settings = AppSettings(
            documents_dir=str(documents_path),
            confirm_delete=confirm_delete,
            last_page=last_page,
            performance_mode=performance_mode,
            update_source_dir=update_source_dir,
        )

        # Rewrite legacy absolute default paths into their portable form once.
        if str(raw.get("documents_dir") or "") != self._encode_path(documents_path):
            try:
                self.save(settings)
            except OSError:
                logger.warning("Не удалось обновить переносимый путь documents_dir")
        return settings

    def save(self, settings: AppSettings) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(settings)
        payload["documents_dir"] = self._encode_path(payload.get("documents_dir") or self.default_documents_dir)
        payload["performance_mode"] = normalize_performance_mode(payload.get("performance_mode"))
        payload["update_source_dir"] = str(payload.get("update_source_dir") or "").strip()

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=self.settings_file.parent,
                prefix=f".{self.settings_file.name}.",
                suffix=".tmp",
            ) as temp_file:
                json.dump(payload, temp_file, ensure_ascii=False, indent=4)
                temp_file.flush()
                temp_path = Path(temp_file.name)
            temp_path.replace(self.settings_file)
        except OSError:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

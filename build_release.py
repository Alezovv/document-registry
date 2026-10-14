from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from app import __version__ as APP_VERSION
from app.db_migrations import CURRENT_SCHEMA_VERSION
from app.text_encoding import loads_json_with_fallback, repair_cyrillic_mojibake
from app.update_config import MAX_MANIFEST_BYTES, UPDATE_MANIFEST_FILE_ID
from app.update_service import UpdateService, parse_version

FORMAT_VERSION = 1
PROTECTED_DIRS = {"data", "backups", "logs", "models"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sync_app_init_version(project_root: Path, version: str) -> None:
    """Синхронизировать __version__ в app/__init__.py перед сборкой PyInstaller."""
    init_file = project_root / "app" / "__init__.py"
    expected = f'__version__ = "{version}"\n'
    if init_file.is_file():
        try:
            if init_file.read_text(encoding="utf-8") == expected:
                return
        except OSError:
            pass
    init_file.write_text(expected, encoding="utf-8")
    print(f"==> Обновлён app/__init__.py: __version__ = \"{version}\"")


def run_step(cmd: list[str], cwd: Path, title: str) -> None:
    print(f"\n==> {title}")
    print("   ", " ".join(str(c) for c in cmd))
    res = subprocess.run(cmd, cwd=cwd, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Ошибка на шаге «{title}» (код {res.returncode})")


def build_executables(project_root: Path, dist_dir: Path, build_dir: Path, version: str) -> Path:
    """Собрать DocumentRegistryUpdater.exe (onefile) и DocumentRegistry.exe (onedir) через PyInstaller."""
    sync_app_init_version(project_root, version)

    icon_ico = project_root / "assets" / "app_icon.ico"
    icon_args = ["--icon", str(icon_ico)] if icon_ico.is_file() else []

    # 1. Сборка standalone-обновлятора (one-file, без зависимости от PySide6)
    updater_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name",
        "DocumentRegistryUpdater",
        "--distpath",
        str(dist_dir / "_updater_dist"),
        "--workpath",
        str(build_dir / "updater"),
        "--specpath",
        str(build_dir),
        *icon_args,
        str(project_root / "app" / "updater_main.py"),
    ]
    run_step(updater_cmd, project_root, "Сборка DocumentRegistryUpdater.exe (onefile)")

    # 2. Сборка основного приложения (onedir)
    add_data_sep = ";" if sys.platform.startswith("win") else ":"
    assets_arg = []
    if (project_root / "assets").is_dir():
        assets_arg = ["--add-data", f"{project_root / 'assets'}{add_data_sep}assets"]

    main_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noconsole",
        "--name",
        "DocumentRegistry",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir / "main"),
        "--specpath",
        str(build_dir),
        "--collect-all",
        "paddleocr",
        "--collect-all",
        "paddlex",
        "--collect-all",
        "onnxruntime",
        *assets_arg,
        *icon_args,
        str(project_root / "app" / "main.py"),
    ]
    run_step(main_cmd, project_root, "Сборка DocumentRegistry.exe (onedir)")

    app_dist = dist_dir / "DocumentRegistry"
    updater_src = dist_dir / "_updater_dist" / "DocumentRegistryUpdater.exe"
    updater_dst = app_dist / "updater" / "DocumentRegistryUpdater.exe"
    updater_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(updater_src, updater_dst)
    return app_dist


def write_version_json(app_dist: Path, version: str, schema: int) -> None:
    """Записать version.json, не меняя mtime файла, если содержимое уже актуально."""
    version_file = app_dist / "version.json"
    version_payload = {
        "version": version,
        "database_schema": schema,
    }
    new_content = json.dumps(version_payload, ensure_ascii=False, indent=2) + "\n"
    if version_file.is_file():
        try:
            if version_file.read_text(encoding="utf-8") == new_content:
                return
        except OSError:
            pass
    version_file.write_text(new_content, encoding="utf-8")


def create_update_zip(
    app_dist: Path,
    output_dir: Path,
    *,
    version: str,
    schema: int,
    notes: str,
) -> tuple[Path, str, int]:
    """Сформировать релизный архив DocumentRegistry_<version>_update.zip со структурой payload/."""
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / f"DocumentRegistry_{version}_update.zip"
    zip_path.unlink(missing_ok=True)

    clean_notes = repair_cyrillic_mojibake(notes.strip())
    internal_manifest = {
        "format_version": FORMAT_VERSION,
        "version": version,
        "database_schema": schema,
        "notes": clean_notes,
    }

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1. Внутренний манифест строго в UTF-8 с фиксированным timestamp для воспроизводимого SHA-256
        manifest_info = zipfile.ZipInfo("update_manifest.json", date_time=(2026, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(
            manifest_info,
            json.dumps(internal_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )

        # 2. Файлы программы внутри payload/ (исключая защищённые пользовательские папки)
        for item in sorted(app_dist.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(app_dist)
            top_folder = rel.parts[0].lower() if rel.parts else ""
            if top_folder in PROTECTED_DIRS:
                continue
            arcname = Path("payload") / rel
            zf.write(item, str(arcname).replace("\\", "/"))

    digest = sha256_file(zip_path)
    size_bytes = zip_path.stat().st_size
    return zip_path, digest, size_bytes


def _collect_entries_from_manifest(old_data: dict, default_schema: int) -> list[dict]:
    """Извлечь как верхнеуровневый релиз, так и массив versions из старого манифеста."""
    collected: list[dict] = []
    if not isinstance(old_data, dict):
        return collected

    if "version" in old_data and "package" in old_data:
        top_entry = {
            "version": str(old_data.get("version") or "").strip(),
            "database_schema": int(old_data.get("database_schema", default_schema)),
            "package": str(old_data.get("package") or "").strip(),
            "package_file_id": str(old_data.get("package_file_id") or "").strip(),
            "sha256": str(old_data.get("sha256") or "").strip().lower(),
            "size": int(old_data.get("size", 0) or 0),
            "notes": repair_cyrillic_mojibake(str(old_data.get("notes") or "")),
        }
        if old_data.get("published_at"):
            top_entry["published_at"] = str(old_data["published_at"])
        collected.append(top_entry)

    for key in ("versions", "releases", "previous_versions"):
        items = old_data.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("version"):
                    entry = {
                        "version": str(item.get("version") or "").strip(),
                        "database_schema": int(item.get("database_schema", default_schema)),
                        "package": str(item.get("package") or "").strip(),
                        "package_file_id": str(item.get("package_file_id") or "").strip(),
                        "sha256": str(item.get("sha256") or "").strip().lower(),
                        "size": int(item.get("size", 0) or 0),
                        "notes": repair_cyrillic_mojibake(str(item.get("notes") or "")),
                    }
                    if item.get("published_at"):
                        entry["published_at"] = str(item["published_at"])
                    collected.append(entry)
    return collected


def update_channel_manifest(
    manifest_path: Path,
    *,
    version: str,
    package_name: str,
    package_file_id: str,
    sha256: str,
    size_bytes: int,
    schema: int,
    notes: str,
) -> None:
    """Обновить публичный manifest.json в UTF-8, автоматически подтягивая историю прошлых версий с локального диска и Google Drive."""
    clean_notes = repair_cyrillic_mojibake(notes.strip())
    published_at = datetime.now().astimezone().isoformat(timespec="seconds")
    raw_candidates: list[dict] = []

    # 1. Пробуем прочитать локальный manifest.json (если уже есть в папке release/)
    if manifest_path.is_file():
        try:
            local_data = loads_json_with_fallback(manifest_path)
            raw_candidates.extend(_collect_entries_from_manifest(local_data, schema))
        except Exception as exc:
            print(f"Предупреждение: не удалось прочитать локальный {manifest_path}: {exc}")

    # 2. Автоматически подтягиваем текущий manifest.json с Google Drive, чтобы не потерять прошлые версии
    try:
        service = UpdateService()
        remote_bytes = service._read_drive_file(UPDATE_MANIFEST_FILE_ID, MAX_MANIFEST_BYTES)
        remote_data = loads_json_with_fallback(remote_bytes)
        raw_candidates.extend(_collect_entries_from_manifest(remote_data, schema))
        print("==> История предыдущих версий успешно загружена из текущего manifest.json в Google Drive")
    except Exception as exc:
        print(f"Примечание: не удалось загрузить старый manifest.json из Google Drive ({exc}).")

    # Оставляем только предыдущие версии (не равные текущей) и сортируем по убыванию версии
    by_ver: dict[tuple[int, int, int], dict] = {}
    for item in raw_candidates:
        v = str(item.get("version") or "").strip()
        if not v or v == version:
            continue
        try:
            ver_key = parse_version(v)
        except ValueError:
            continue
        if ver_key not in by_ver:
            by_ver[ver_key] = item

    previous_versions = [by_ver[k] for k in sorted(by_ver.keys(), reverse=True)]

    manifest_data = {
        "format_version": FORMAT_VERSION,
        "version": version,
        "database_schema": schema,
        "package": package_name,
        "package_file_id": package_file_id,
        "sha256": sha256,
        "size": size_bytes,
        "notes": clean_notes,
        "published_at": published_at,
        "versions": previous_versions,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    # Строго UTF-8 без BOM, чтобы кириллица никогда не искажалась
    manifest_path.write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сборка релиза DocumentRegistry, пакета обновления .zip и manifest.json (строго в UTF-8)."
    )
    parser.add_argument("--version", default=APP_VERSION, help=f"Версия релиза (по умолчанию {APP_VERSION})")
    parser.add_argument(
        "--notes",
        default="Исправлена кодировка кириллицы, логика статуса «Отклонен» (не переходит в «Просрочен») и проверка совместимости БД.",
        help="Описание изменений (release notes) на русском языке",
    )
    parser.add_argument(
        "--drive-file-id",
        default="ЗАМЕНИТЕ_НА_ID_ZIP_ФАЙЛА_В_GOOGLE_DRIVE",
        help="Google Drive File ID загруженного .zip пакета",
    )
    parser.add_argument(
        "--skip-pyinstaller",
        action="store_true",
        help="Пропустить компиляцию PyInstaller и использовать готовую папку --app-dist (или уже созданный .zip при передаче --drive-file-id)",
    )
    parser.add_argument(
        "--repack",
        action="store_true",
        help="Принудительно пересоздать .zip даже если он уже существует в папке release/",
    )
    parser.add_argument(
        "--app-dist",
        default="dist/DocumentRegistry",
        help="Путь к собранной папке приложения (по умолчанию dist/DocumentRegistry)",
    )
    parser.add_argument(
        "--output-dir",
        default="release",
        help="Папка для сохранения готового .zip пакета и manifest.json (по умолчанию release/)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    dist_dir = project_root / "dist"
    build_dir = project_root / "build"
    output_dir = project_root / args.output_dir
    existing_zip = output_dir / f"DocumentRegistry_{args.version}_update.zip"

    # ВАЖНО: Если пользователь уже собрал .zip на первом шаге, загрузил его в Google Drive
    # и запустил скрипт второй раз с --skip-pyinstaller и --drive-file-id, НЕ пересоздаём .zip,
    # чтобы его SHA-256 гарантированно совпадал с файлом, загруженным в Google Drive!
    if (
        args.skip_pyinstaller
        and not args.repack
        and existing_zip.is_file()
        and args.drive_file_id != "ЗАМЕНИТЕ_НА_ID_ZIP_ФАЙЛА_В_GOOGLE_DRIVE"
    ):
        print(f"\n==> Используем уже созданный пакет {existing_zip} (без перепаковки, чтобы сохранить SHA-256)")
        zip_path = existing_zip
        digest = sha256_file(zip_path)
        size_bytes = zip_path.stat().st_size
    else:
        if args.skip_pyinstaller:
            app_dist = (project_root / args.app_dist).resolve()
            if not app_dist.is_dir():
                raise SystemExit(f"Папка сборки не найдена: {app_dist}")
        else:
            app_dist = build_executables(project_root, dist_dir, build_dir, args.version)

        write_version_json(app_dist, args.version, CURRENT_SCHEMA_VERSION)
        zip_path, digest, size_bytes = create_update_zip(
            app_dist,
            output_dir,
            version=args.version,
            schema=CURRENT_SCHEMA_VERSION,
            notes=args.notes,
        )

    manifest_path = output_dir / "manifest.json"
    update_channel_manifest(
        manifest_path,
        version=args.version,
        package_name=zip_path.name,
        package_file_id=args.drive_file_id,
        sha256=digest,
        size_bytes=size_bytes,
        schema=CURRENT_SCHEMA_VERSION,
        notes=args.notes,
    )

    print("\n============================================================")
    print(f"Релиз {args.version} успешно упакован!")
    print(f"  Пакет обновления : {zip_path}")
    print(f"  Размер (bytes)   : {size_bytes} ({size_bytes / (1024 * 1024):.1f} МБ)")
    print(f"  SHA-256          : {digest}")
    print(f"  Манифест канала  : {manifest_path} (UTF-8 без BOM)")
    print("============================================================")


if __name__ == "__main__":
    main()
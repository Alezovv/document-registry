from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import logging
from pathlib import Path

from app.database import Database
from app.document_service import (
    DocumentService,
    DocumentStorageError,
    StoredDocument,
    TrashStats,
)
from app.models import Request
from app.document_fingerprint import DocumentFingerprintService
from app.document_parser import PARSER_VERSION
from app.parser_review import compact_parser_diagnostics
from app.parser_correction_memory import ParserCorrectionMemory


logger = logging.getLogger(__name__)


class RequestService:
    """Application service coordinating SQLite state and PDF lifecycle.

    UI code should use this service instead of combining database and filesystem
    operations itself. File moves are compensated when a database mutation fails.
    """

    def __init__(
        self,
        database: Database | None = None,
        document_service: DocumentService | None = None,
    ):
        self.database = database or Database()
        self.documents = document_service or DocumentService(
            self.database, self.database.documents_dir
        )
        self.parser_corrections = ParserCorrectionMemory.for_data_dir(self.database.data_dir)
        self.fingerprints = DocumentFingerprintService(self.documents)

    @staticmethod
    def _release_transient_import_payload(request: Request) -> None:
        """Drop OCR/parser-only attributes once SQLite has persisted them."""
        for name in ("_ocr_text", "_parser_diagnostics"):
            try:
                delattr(request, name)
            except AttributeError:
                pass

    def get_requests(self) -> list[Request]:
        return self.database.get_requests()

    def query_requests_page(self, **kwargs):
        return self.database.query_requests_page(**kwargs)

    def get_registry_status_counts(self) -> dict[str, int]:
        return self.database.get_registry_status_counts()

    def get_form_suggestions(self, limit: int = 200) -> dict[str, list[str]]:
        suggestions = self.database.get_distinct_request_values(limit=limit)
        return self.parser_corrections.merge_suggestions(suggestions)

    def apply_parser_corrections(
        self, request: Request, context: dict | None = None
    ) -> Request:
        """Apply locally learned user corrections to freshly parsed fields."""
        return self.parser_corrections.apply(request, context=context)

    def learn_parser_corrections(
        self, before: Request, after: Request, context: dict | None = None
    ) -> int:
        """Learn explicit review edits together with safe parser context."""
        learned = self.parser_corrections.learn(before, after, context=context)
        try:
            confirmed_dept = (after.from_department or "").strip()
            if confirmed_dept:
                from app.blank_fingerprint_memory import get_blank_fingerprint_memory

                raw_header = getattr(before, "_ocr_text", "") or getattr(after, "_ocr_text", "") or ""
                if not raw_header and isinstance(context, dict):
                    raw_header = context.get("raw_text", "") or context.get("header_text", "")
                if raw_header:
                    get_blank_fingerprint_memory().learn_from_header(raw_header[:650], confirmed_dept)
        except Exception:
            pass
        return learned

    def get_deadline_summary(self) -> tuple[int, int]:
        return self.database.get_deadline_summary()

    def get_request(self, request_id: int) -> Request | None:
        return self.database.get_request(request_id)

    def get_history(self, limit: int = 2000, offset: int = 0, *, include_snapshot: bool = True):
        return self.database.get_history(limit=limit, offset=offset, include_snapshot=include_snapshot)

    def get_history_count(self) -> int:
        return self.database.get_history_count()

    def trim_history(self, keep_count: int = 2000) -> int:
        return self.database.trim_history(keep_count=keep_count)

    def get_history_record(self, history_id: int):
        return self.database.get_history_record(history_id)

    def get_trash_stats(self) -> TrashStats:
        return self.documents.get_trash_stats()

    def clear_history(self) -> TrashStats:
        staged = self.documents.stage_trash_cleanup()
        stats = staged.stats if staged is not None else TrashStats(0, 0)
        try:
            self.database.clear_history()
        except Exception:
            try:
                self.documents.rollback_staged_trash(staged)
            except DocumentStorageError:
                logger.exception("Не удалось вернуть корзину после ошибки очистки истории")
            raise

        try:
            self.documents.finalize_staged_trash(staged)
        except DocumentStorageError as error:
            # The database history is already cleared at this point. Keep the
            # undeleted files visible in the normal trash directory so the
            # user can retry cleanup instead of leaving a hidden orphan folder.
            try:
                self.documents.rollback_staged_trash(staged)
            except DocumentStorageError:
                logger.exception("Не удалось вернуть корзину после ошибки финальной очистки")
            raise DocumentStorageError(
                "История очищена, но файлы корзины удалить не удалось. "
                "Корзина сохранена; повторите очистку."
            ) from error
        return stats

    def set_documents_dir(self, documents_dir: str | Path) -> None:
        self.documents.set_documents_dir(documents_dir)

    @staticmethod
    def _normalize_completion_date(request: Request) -> None:
        if request.status == "Исполнен":
            if not (request.completed_date or "").strip():
                request.completed_date = date.today().strftime("%d.%m.%Y")
        else:
            request.completed_date = ""

    def create_request(self, request: Request) -> int:
        self._normalize_completion_date(request)
        ocr_text = str(getattr(request, "_ocr_text", "") or "").strip()
        parser_diagnostics = compact_parser_diagnostics(getattr(request, "_parser_diagnostics", {}) or {})
        stored: StoredDocument | None = None
        source_document = ""
        source_sha256 = ""
        source_size_bytes = 0
        if request.source_document:
            fingerprint = self.fingerprints.fingerprint(request.source_document)
            if fingerprint is not None:
                source_sha256 = fingerprint.sha256
                source_size_bytes = fingerprint.size_bytes
            stored = self.documents.store_document(request.source_document)
            source_document = str(stored.path)

        persisted = replace(
            request,
            source_document=source_document,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
        )
        try:
            request_id = self.database.add_request(persisted)
        except Exception:
            self.documents.discard_created_document(stored)
            raise

        request.id = request_id
        request.source_document = source_document
        request.source_sha256 = source_sha256
        request.source_size_bytes = source_size_bytes
        if parser_diagnostics:
            try:
                self.database.set_parser_metadata(request_id, parser_diagnostics, PARSER_VERSION)
            except Exception:
                logger.exception("Не удалось сохранить диагностику parser-а для запроса %s", request_id)
        if ocr_text:
            try:
                self.database.set_request_ocr_text(request_id, ocr_text)
            except Exception:
                # Search indexing is secondary: a successfully stored registry
                # record must not be rolled back because its OCR index failed.
                logger.exception("Не удалось добавить OCR-текст в поисковый индекс для запроса %s", request_id)
        self._release_transient_import_payload(request)
        return request_id

    def update_request(self, request: Request, action: str = "update") -> None:
        self._normalize_completion_date(request)
        ocr_text = getattr(request, "_ocr_text", None)
        parser_diagnostics = compact_parser_diagnostics(getattr(request, "_parser_diagnostics", {}) or {})
        if request.id is None:
            raise ValueError("Нельзя обновить запрос без ID.")

        current = self.database.get_request(request.id)
        if current is None:
            raise ValueError("Запрос не найден.")

        old_path = current.source_document or ""
        requested_path = request.source_document or ""
        stored: StoredDocument | None = None
        trash_path: Path | None = None

        same_document = False
        if old_path and requested_path:
            same_document = (
                self.documents.normalize_path(old_path)
                == self.documents.normalize_path(requested_path)
            )

        if same_document:
            final_path = old_path
            final_sha256 = current.source_sha256
            final_size_bytes = current.source_size_bytes
        elif requested_path:
            fingerprint = self.fingerprints.fingerprint(requested_path)
            final_sha256 = fingerprint.sha256 if fingerprint is not None else ""
            final_size_bytes = fingerprint.size_bytes if fingerprint is not None else 0
            stored = self.documents.store_document(requested_path)
            final_path = str(stored.path)
        else:
            final_path = ""
            final_sha256 = ""
            final_size_bytes = 0

        if old_path and final_path != old_path:
            trash_path = self.documents.move_to_trash(old_path, request.id)

        persisted = replace(
            request,
            source_document=final_path,
            source_sha256=final_sha256,
            source_size_bytes=final_size_bytes,
        )
        snapshot_extra = (
            {"_trash_path": str(trash_path)} if trash_path is not None else None
        )

        try:
            self.database.update_request(
                persisted,
                action=action,
                snapshot_extra=snapshot_extra,
            )
        except Exception:
            if trash_path is not None:
                try:
                    self.documents.restore_from_trash(trash_path, old_path)
                except DocumentStorageError:
                    logger.exception(
                        "Не удалось вернуть старый PDF после отката запроса %s",
                        request.id,
                    )
            self.documents.discard_created_document(stored)
            raise

        request.source_document = final_path
        request.source_sha256 = final_sha256
        request.source_size_bytes = final_size_bytes
        if parser_diagnostics:
            try:
                self.database.set_parser_metadata(request.id, parser_diagnostics, PARSER_VERSION)
            except Exception:
                logger.exception("Не удалось обновить диагностику parser-а для запроса %s", request.id)
        if ocr_text is not None:
            try:
                self.database.set_request_ocr_text(request.id, str(ocr_text or ""))
            except Exception:
                logger.exception("Не удалось обновить OCR-текст поискового индекса для запроса %s", request.id)
        self._release_transient_import_payload(request)

    def search_request_ids_full_text(self, value: str, *, limit: int = 5000) -> list[int]:
        return self.database.search_request_ids_full_text(value, limit=limit)

    def search_request_hits_full_text(self, value: str, *, limit: int = 500) -> list[dict]:
        return self.database.search_request_hits_full_text(value, limit=limit)

    def rebuild_search_index(self) -> int:
        return self.database.rebuild_search_index()

    def get_parser_metadata(self, request_id: int) -> dict:
        return self.database.get_parser_metadata(request_id)

    def find_duplicate_document(self, path: str | Path, *, exclude_request_id: int | None = None):
        return self.fingerprints.find_duplicate(path, exclude_request_id=exclude_request_id)

    def backfill_document_fingerprints(self, *, limit: int | None = None) -> int:
        return self.fingerprints.backfill_missing(limit=limit)

    def get_duplicate_document_groups(self):
        return self.fingerprints.duplicate_groups(backfill=True)

    def get_duplicate_map(self) -> dict[int, dict]:
        return self.database.get_duplicate_map()

    def get_request_duplicate_info(self, request_id: int) -> dict | None:
        return self.database.get_request_duplicate_info(request_id)

    def get_search_index_stats(self) -> dict[str, int]:
        return self.database.get_search_index_stats()

    def delete_request(self, request_id: int) -> bool:
        request = self.database.get_request(request_id)
        if request is None:
            return False

        trash_path: Path | None = None
        if request.source_document:
            trash_path = self.documents.move_to_trash(
                request.source_document, request_id
            )

        snapshot_extra = (
            {"_trash_path": str(trash_path)} if trash_path is not None else None
        )
        try:
            removed = self.database.delete_request(
                request_id, snapshot_extra=snapshot_extra
            )
        except Exception:
            if trash_path is not None:
                self.documents.restore_from_trash(
                    trash_path, request.source_document
                )
            raise

        if not removed and trash_path is not None:
            self.documents.restore_from_trash(trash_path, request.source_document)
        return removed

    def restore_request_from_history(self, history_id: int) -> bool:
        record = self.database.get_history_record(history_id)
        if record is None or not record["snapshot"]:
            raise ValueError("Не найден снапшот для этой записи истории.")

        snapshot = json.loads(record["snapshot"])
        request_id = int(snapshot["id"])
        expected_source = snapshot.get("source_document", "") or ""

        expected_name = self.documents.reference_name(expected_source) if expected_source else ""
        expected_existing = self.documents.resolve_document_reference(expected_source)
        expected_target = (
            expected_existing
            if expected_existing is not None
            else (self.documents.documents_dir / expected_name if expected_name else None)
        )
        source_override = str(expected_target) if expected_target is not None else ""
        expected_restored_from: Path | None = None
        current_trash: Path | None = None

        current = self.database.get_request(request_id)
        current_source = current.source_document if current else ""
        current_resolved = self.documents.resolve_document_reference(current_source)
        current_path = str(current_resolved) if current_resolved is not None else current_source

        # Preserve the currently attached version so restore itself is reversible.
        if current_path and (
            not source_override
            or self.documents.normalize_path(current_path)
            != self.documents.normalize_path(source_override)
        ):
            current_trash = self.documents.move_to_trash(current_path, request_id)

        if expected_target is not None and not expected_target.exists():
            exact_trash = snapshot.get("_trash_path")
            exact_trash_path = (
                Path(self.database.decode_runtime_reference(exact_trash))
                if exact_trash
                else None
            )
            trash_candidate = (
                exact_trash_path
                if exact_trash_path is not None and exact_trash_path.exists()
                else self.documents.find_latest_trashed(expected_source, request_id)
            )

            if trash_candidate is not None:
                expected_restored_from = trash_candidate
                restored_path = self.documents.restore_from_trash(
                    trash_candidate, expected_target
                )
                # History/delete restore returns a real existing file. Persist
                # its canonical spelling so callers comparing against the path
                # originally returned by store_document() see the same value.
                canonical_restored = self.documents.resolve_document_reference(
                    restored_path
                )
                source_override = str(canonical_restored or restored_path)
            else:
                source_override = ""

        snapshot_extra = (
            {"_trash_path": str(current_trash)}
            if current_trash is not None
            else None
        )

        try:
            return self.database.restore_request_from_history(
                history_id,
                source_document_override=source_override,
                snapshot_extra=snapshot_extra,
            )
        except Exception:
            # Put the requested historical file back where it came from.
            if expected_restored_from is not None and expected_target is not None and expected_target.exists():
                try:
                    expected_restored_from.parent.mkdir(parents=True, exist_ok=True)
                    expected_target.replace(expected_restored_from)
                except OSError:
                    logger.exception(
                        "Не удалось откатить восстановление исторического PDF %s",
                        request_id,
                    )

            # Return the pre-restore current file to its original/current storage location.
            if current_trash is not None and current_path:
                try:
                    restore_target = self.documents.documents_dir / Path(current_path).name
                    self.documents.restore_from_trash(current_trash, restore_target)
                except DocumentStorageError:
                    logger.exception(
                        "Не удалось вернуть текущий PDF после ошибки восстановления %s",
                        request_id,
                    )
            raise

    def attach_document(self, request_id: int, source_path: str) -> str:
        request = self.get_request(request_id)
        if request is None:
            raise ValueError("Запрос не найден.")
        request.source_document = source_path
        self.update_request(request, action="attach")
        return request.source_document

    def detach_document(self, request_id: int) -> bool:
        request = self.get_request(request_id)
        if request is None:
            return False
        request.source_document = ""
        self.update_request(request, action="detach")
        return True

    def search_requests(self, field: str, value: str) -> list[Request]:
        value = (value or "").strip()
        if not value:
            return self.database.get_requests()
        return self.database.search_requests(field, value)

    def is_request_pinned(self, request_id: int) -> bool:
        request = self.get_request(request_id)
        return bool(request and request.is_pinned)

    def set_request_pinned(self, request_id: int, pinned: bool) -> None:
        self.database.set_request_pinned(request_id, pinned)

    def toggle_request_pinned(self, request_id: int) -> bool:
        return self.database.toggle_request_pinned(
            request_id, record_history=True
        )

    def bulk_update_requests(
        self,
        request_ids,
        *,
        status: str | None = None,
        due_mode: str | None = None,
        pinned: bool | None = None,
    ) -> int:
        """Apply a lightweight change to many requests in one DB transaction."""
        ids = {int(request_id) for request_id in request_ids if request_id is not None}
        if not ids:
            return 0

        if status is not None and status not in {"Новый", "В работе", "Исполнен", "Отклонен"}:
            raise ValueError("Недопустимый статус для массовой операции.")
        if due_mode is not None and due_mode not in {"today", "+7", "+14", "+30", "clear"}:
            raise ValueError("Недопустимый режим срока исполнения.")

        updated: list[Request] = []
        for current in self.database.get_requests_by_ids(ids):
            request = replace(current)

            if status is not None:
                request.status = status
                self._normalize_completion_date(request)

            if due_mode is not None:
                if due_mode == "clear":
                    request.due_date = ""
                elif due_mode == "today":
                    request.due_date = date.today().strftime("%d.%m.%Y")
                else:
                    try:
                        base = datetime.strptime(request.received_date, "%d.%m.%Y").date()
                    except (TypeError, ValueError):
                        base = date.today()
                    days = int(due_mode[1:])
                    request.due_date = (base + timedelta(days=days)).strftime("%d.%m.%Y")

            if pinned is not None:
                request.is_pinned = bool(pinned)

            updated.append(request)

        action_parts = [
            status is not None,
            due_mode is not None,
            pinned is not None,
        ]
        if sum(action_parts) == 1:
            if status is not None:
                history_action = "bulk_status"
            elif due_mode is not None:
                history_action = "bulk_due"
            else:
                history_action = "bulk_pin"
        else:
            history_action = "bulk_update"

        return self.database.bulk_update_requests(updated, action=history_action)

    def save_document(self, source_path: str) -> str:
        return str(self.documents.store_document(source_path).path)

    @staticmethod
    def normalize_text(value: str | None) -> str:
        if value is None:
            return ""
        return " ".join(str(value).split())

    @staticmethod
    def safe_file_name(path: str | Path) -> str:
        return Path(path).name or "document.pdf"

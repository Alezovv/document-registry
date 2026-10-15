from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.document_service import DocumentService
from app.models import Request


@dataclass(frozen=True, slots=True)
class FingerprintResult:
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    sha256: str
    size_bytes: int
    requests: tuple[Request, ...]


class DocumentFingerprintService:
    """Indexed, streaming duplicate detection with lazy legacy backfill."""

    def __init__(self, document_service: DocumentService):
        self.documents = document_service
        self.database = document_service.database

    def fingerprint(self, path: str | Path) -> FingerprintResult | None:
        candidate = Path(path)
        digest = self.documents.calculate_file_hash(candidate)
        if not digest:
            return None
        try:
            size = int(candidate.stat().st_size)
        except OSError:
            size = 0
        return FingerprintResult(digest, size)

    def find_duplicate(
        self,
        path: str | Path,
        *,
        exclude_request_id: int | None = None,
        backfill_legacy: bool = True,
    ) -> Request | None:
        fp = self.fingerprint(path)
        if fp is None:
            return self.documents.find_duplicate_document(path, exclude_request_id=exclude_request_id)

        found = self.database.find_request_by_fingerprint(
            fp.sha256,
            fp.size_bytes,
            exclude_request_id=exclude_request_id,
        )
        if found is not None:
            return found

        if backfill_legacy:
            # Legacy rows are upgraded gradually. Hashing the entire archive from
            # one UI action can monopolize disk/CPU for minutes on an old PC.
            # Process only a tiny batch and re-check the indexed lookup.
            self.backfill_missing(limit=3)
            found = self.database.find_request_by_fingerprint(
                fp.sha256, fp.size_bytes, exclude_request_id=exclude_request_id
            )
            if found is not None:
                return found

            # Cheap same-name fallback for still-unmigrated rows. This does not
            # read PDF bodies and therefore keeps the pre-OCR duplicate check fast.
            target_name = self.documents.normalize_file_name(path)
            if target_name:
                for request in self.database.get_requests_missing_fingerprint(limit=50):
                    if exclude_request_id is not None and request.id == exclude_request_id:
                        continue
                    existing = self.documents.resolve_document_reference(request.source_document)
                    if existing is not None and self.documents.normalize_file_name(existing) == target_name:
                        return request
        return None

    def backfill_missing(self, *, limit: int | None = None) -> int:
        updated = 0
        rows = self.database.get_requests_missing_fingerprint(limit=limit)
        for request in rows:
            if request.id is None:
                continue
            path = self.documents.resolve_document_reference(request.source_document)
            if path is None:
                continue
            fp = self.fingerprint(path)
            if fp is None:
                continue
            self.database.set_document_fingerprint(request.id, fp.sha256, fp.size_bytes)
            updated += 1
        return updated

    def duplicate_groups(self, *, backfill: bool = True) -> list[DuplicateGroup]:
        if backfill:
            self.backfill_missing()
        groups: list[DuplicateGroup] = []
        for sha256, size_bytes, ids in self.database.get_duplicate_fingerprint_groups():
            requests = tuple(
                request for request_id in ids
                if (request := self.database.get_request(request_id)) is not None
            )
            if len(requests) > 1:
                groups.append(DuplicateGroup(sha256, size_bytes, requests))
        return groups

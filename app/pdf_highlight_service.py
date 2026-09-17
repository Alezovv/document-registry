from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any


class PdfHighlightPreview:
    """Build a temporary PDF copy with parser source regions highlighted."""

    def __init__(self) -> None:
        self._temp_dir: Path | None = None

    def close(self) -> None:
        if self._temp_dir and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None

    def build(
        self,
        source_pdf: str | Path,
        field_regions: dict[str, list[dict[str, Any]]],
    ) -> Path | None:
        regions = [item for items in field_regions.values() for item in (items or [])]
        if not regions:
            return None
        source = Path(source_pdf)
        if not source.is_file():
            return None
        try:
            import pymupdf
        except Exception:
            return None

        self.close()
        self._temp_dir = Path(tempfile.mkdtemp(prefix="docreg_pdf_preview_"))
        digest = hashlib.sha1(
            json.dumps(field_regions, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        target = self._temp_dir / f"{source.stem}_highlight_{digest}.pdf"

        document = pymupdf.open(str(source))
        try:
            for region in regions:
                try:
                    page_index = int(region.get("page_index", 0))
                    rect_norm = region.get("rect_norm") or []
                    if page_index < 0 or page_index >= len(document) or len(rect_norm) != 4:
                        continue
                    page = document[page_index]
                    page_rect = page.rect
                    x1, y1, x2, y2 = [float(value) for value in rect_norm]
                    rect = pymupdf.Rect(
                        page_rect.x0 + max(0.0, min(1.0, x1)) * page_rect.width,
                        page_rect.y0 + max(0.0, min(1.0, y1)) * page_rect.height,
                        page_rect.x0 + max(0.0, min(1.0, x2)) * page_rect.width,
                        page_rect.y0 + max(0.0, min(1.0, y2)) * page_rect.height,
                    )
                    if rect.width <= 0 or rect.height <= 0:
                        continue
                    page.draw_rect(
                        rect,
                        color=(1.0, 0.58, 0.0),
                        fill=(1.0, 0.92, 0.25),
                        width=1.1,
                        overlay=True,
                        stroke_opacity=0.85,
                        fill_opacity=0.22,
                    )
                except (TypeError, ValueError):
                    continue
            document.save(str(target), garbage=2, deflate=True)
        finally:
            document.close()
        return target if target.is_file() else None

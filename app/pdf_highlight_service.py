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
        *,
        active_field: str | None = None,
    ) -> Path | None:
        regions_exist = any(bool(items) for items in (field_regions or {}).values())
        if not regions_exist:
            return None
        source = Path(source_pdf)
        if not source.is_file():
            return None
        try:
            import pymupdf
        except Exception:
            return None

        if self._temp_dir is None or not self._temp_dir.exists():
            self._temp_dir = Path(tempfile.mkdtemp(prefix="docreg_pdf_preview_"))

        payload_key = {
            "regions": field_regions,
            "active": active_field or "",
        }
        digest = hashlib.sha1(
            json.dumps(payload_key, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        target = self._temp_dir / f"{source.stem}_highlight_{digest}.pdf"
        if target.is_file():
            return target

        document = pymupdf.open(str(source))
        try:
            target_fields = [active_field] if active_field else list((field_regions or {}).keys())
            for field_name in target_fields:
                items = (field_regions or {}).get(field_name) or []
                stroke_color = (0.14, 0.39, 0.92)
                fill_color = (0.58, 0.77, 0.99)
                line_width = 2.2
                fill_alpha = 0.32

                for region in items or []:
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
                        rect = pymupdf.Rect(
                            max(page_rect.x0, rect.x0 - 2.5),
                            max(page_rect.y0, rect.y0 - 2.5),
                            min(page_rect.x1, rect.x1 + 2.5),
                            min(page_rect.y1, rect.y1 + 2.5),
                        )
                        page.draw_rect(
                            rect,
                            color=stroke_color,
                            fill=fill_color,
                            width=line_width,
                            overlay=True,
                            stroke_opacity=0.95,
                            fill_opacity=fill_alpha,
                        )
                    except (TypeError, ValueError):
                        continue
            document.save(str(target), garbage=0, deflate=False)
        finally:
            document.close()
        return target if target.is_file() else None

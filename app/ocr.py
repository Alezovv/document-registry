from __future__ import annotations

import gc
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable

from app.app_paths import get_app_paths
from app.resource_profile import resource_profile_from_environment

logger = logging.getLogger(__name__)



class OCRError(Exception):
    """Ошибка при выполнении OCR."""
    pass


class PageExtractResult(tuple):
    """Backwards-compatible tuple (text, blocks) that also exposes .preparsed."""

    def __new__(
        cls,
        text: str,
        blocks: list[dict[str, Any]],
        preparsed: dict[str, Any] | None = None,
    ):
        return super().__new__(cls, (text, blocks))

    def __init__(
        self,
        text: str,
        blocks: list[dict[str, Any]],
        preparsed: dict[str, Any] | None = None,
    ):
        self.text = text
        self.blocks = blocks
        self.preparsed = preparsed or {}


class OCR:
    # Ищем текст только на 1-й странице, это ускоряет работу в разы
    MAX_PAGES = 1

    # Снижен DPI: матрица меньше, CPU обрабатывает картинку в 1.5 раза быстрее
    DEFAULT_DPI = 150
    RETRY_DPI = 200

    MAX_IMAGE_WIDTH = 2500
    MAX_IMAGE_HEIGHT = 3500

    MIN_NATIVE_TEXT_LENGTH = 30
    NATIVE_TEXT_QUALITY_THRESHOLD = 0.60
    LINE_Y_TOLERANCE = 0.45
    RETRY_QUALITY_THRESHOLD = 0.58
    RETRY_CONFIDENCE_THRESHOLD = 0.64
    ROTATION_RETRY_SCORE_THRESHOLD = 0.34
    SECOND_PAGE_TEXT_THRESHOLD = 90

    CYRILLIC_LOOKALIKE_MAP = str.maketrans(
        {
            "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н",
            "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
            "X": "Х", "Y": "У", "a": "а", "c": "с", "e": "е",
            "o": "о", "p": "р", "x": "х", "y": "у",
        }
    )
    _CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
    _LATIN_RE = re.compile(r"[A-Za-z]")
    _NON_LOOKALIKE_LATIN_RE = re.compile(r"[DdfGgIiJjLlNnQqRrSsUuVvWwZz]")
    _SIGNATURE_FIO_RE = re.compile(r"\b(?:[А-ЯЁ][а-яё-]{2,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.|[А-ЯЁ]\.\s*[А-ЯЁ]\.\s*[А-ЯЁ][а-яё-]{2,})\b")

    _shared_ocr: Any | None = None
    _shared_models_dir: Path | None = None
    _shared_engine_signature: tuple[int, bool] | None = None
    _engine_lock = threading.Lock()

    def __init__(self) -> None:
        paths = get_app_paths()
        self.models_dir = paths.models / "paddle" / "official_models"
        self.det_model_dir = self.models_dir / "PP-OCRv5_mobile_det_onnx"
        self.rec_model_dir = self.models_dir / "eslav_PP-OCRv5_mobile_rec_onnx"
        self.det_model_file = self.det_model_dir / "inference.onnx"
        self.rec_model_file = self.rec_model_dir / "inference.onnx"
        self.resource_profile = resource_profile_from_environment()
        self.default_dpi = self.resource_profile.ocr_dpi
        self.retry_dpi = self.resource_profile.retry_dpi
        self.max_image_width = self.resource_profile.max_image_width
        self.max_image_height = self.resource_profile.max_image_height
        available_cores = os.cpu_count() or 1
        self.cpu_threads = self._compute_optimal_cpu_threads(
            available_cores,
            self.resource_profile.performance_mode,
            worker_threads=getattr(self.resource_profile, "worker_threads", 1),
        )

    @staticmethod
    def _compute_optimal_cpu_threads(
        available_cores: int, mode: str, worker_threads: int = 1
    ) -> int:
        cores = max(1, int(available_cores or 1))
        if mode == "economy":
            return 1
        if mode == "performance":
            return min(4, max(2, cores - 1)) if cores > 1 else 1
        if cores <= 2:
            return 1
        base_threads = min(max(1, int(worker_threads or 1)), max(1, cores - 1))
        try:
            from app.memory_metrics import system_memory_status
            _total, avail = system_memory_status()
            if avail and avail < 450 * 1024 * 1024:
                return 1
            if avail and avail < 600 * 1024 * 1024:
                return min(2, base_threads)
        except Exception:
            pass
        return base_threads

    @staticmethod
    def _dynamic_memory_guard_allows_retry(economy_policy: bool) -> bool:
        """Check if system/process memory has enough headroom for high-DPI retry."""
        if not economy_policy:
            return True
        try:
            from app.memory_metrics import process_memory_info, system_memory_status, trim_process_working_set
            proc = process_memory_info()
            _total, avail = system_memory_status()
            if (proc.current_bytes and proc.current_bytes > 650 * 1024 * 1024) or (
                avail and avail < 500 * 1024 * 1024
            ):
                gc.collect()
                trim_process_working_set()
                _total, avail = system_memory_status()
                if avail and avail < 420 * 1024 * 1024:
                    return False
        except Exception:
            pass
        return True

    @classmethod
    def _has_signer_on_page(cls, text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        if any(
            m in low
            for m in (
                "главный врач", "начальник", "руководитель", "директор",
                "заведующ", "командир", "председатель", "судья", "мировой",
                "пристав", "прокурор", "следователь", "дознаватель", "инспектор",
                "оперуполномочен", "участков", "адвокат", "эксперт", "специалист",
                "врио", "врид", "и.о.", "подпись", "исп.", "исполнитель",
                "владелец", "сертификат",
            )
        ):
            return True
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        tail_start = max(0, len(lines) - max(3, int(len(lines) * 0.35)))
        tail_text = "\n".join(lines[tail_start:])
        return bool(cls._SIGNATURE_FIO_RE.search(tail_text))

    @property
    def _engine_signature(self) -> tuple[int, bool]:
        return (self.cpu_threads, self.resource_profile.memory_efficient_ocr)

    @classmethod
    def reset_shared_engine(cls) -> bool:
        """Drop the warm OCR engine after an explicit performance-mode change.

        Normal document processing still keeps one warm engine for the whole session.
        The reset is only used when the operator intentionally changes the runtime
        profile, because ONNX thread pools are fixed when a session is created.
        """
        with cls._engine_lock:
            was_loaded = cls._shared_ocr is not None
            engine = cls._shared_ocr
            cls._shared_ocr = None
            cls._shared_models_dir = None
            cls._shared_engine_signature = None
        if engine is not None:
            del engine
            gc.collect()
        return was_loaded

    def _get_engine(self):
        """Load PaddleOCR once and reuse it while its runtime profile is unchanged."""
        signature = self._engine_signature
        if (
            OCR._shared_ocr is not None
            and OCR._shared_models_dir == self.models_dir
            and OCR._shared_engine_signature == signature
        ):
            return OCR._shared_ocr

        # Import workers are sequential today, but the lock prevents an accidental
        # second model copy if two callers ever reach lazy initialization together.
        with OCR._engine_lock:
            if (
                OCR._shared_ocr is not None
                and OCR._shared_models_dir == self.models_dir
                and OCR._shared_engine_signature == signature
            ):
                return OCR._shared_ocr

            # A different performance mode means a different ONNX thread-pool/cache
            # configuration. Release the old session before creating the new one so
            # both model copies are never retained at the same time.
            stale_engine = OCR._shared_ocr
            OCR._shared_ocr = None
            OCR._shared_models_dir = None
            OCR._shared_engine_signature = None
            if stale_engine is not None:
                del stale_engine
                gc.collect()
            try:
                self._validate_models()
                from paddleocr import PaddleOCR

                common_kwargs = dict(
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_detection_model_dir=str(self.det_model_dir),
                    text_recognition_model_name="eslav_PP-OCRv5_mobile_rec",
                    text_recognition_model_dir=str(self.rec_model_dir),
                    text_recognition_batch_size=4,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    engine="onnxruntime",
                    device="cpu",
                    engine_config={
                        "intra_op_num_threads": self.cpu_threads,
                        "inter_op_num_threads": 1,
                        "execution_mode": "sequential",
                        "log_severity_level": 3,
                        "enable_cpu_mem_arena": not self.resource_profile.memory_efficient_ocr,
                        "enable_mem_pattern": not self.resource_profile.memory_efficient_ocr,
                    },
                )
                try:
                    engine = PaddleOCR(
                        **common_kwargs,
                        text_det_limit_side_len=960,
                        text_det_limit_type="max",
                    )
                except TypeError:
                    engine = PaddleOCR(**common_kwargs)

                try:
                    if hasattr(engine, "paddlex_pipeline") and hasattr(engine.paddlex_pipeline, "_pipeline"):
                        pipe = engine.paddlex_pipeline._pipeline
                        if hasattr(pipe, "text_rec_model") and hasattr(pipe.text_rec_model, "batch_sampler"):
                            pipe.text_rec_model.batch_sampler._batch_size = 4
                except Exception:
                    pass
            except OCRError:
                raise
            except Exception as error:
                raise OCRError(f"Не удалось инициализировать PaddleOCR: {error}") from error

            OCR._shared_ocr = engine
            OCR._shared_models_dir = self.models_dir
            OCR._shared_engine_signature = signature
            return engine

    def _validate_models(self) -> None:
        import os
        error_msg = []

        if not self.det_model_file.is_file():
            error_msg.append(
                f"❌ НЕТ ФАЙЛА ДЕТЕКЦИИ:\nИскал тут: {self.det_model_file}")
            if self.det_model_dir.exists():
                error_msg.append(
                    f"А в этой папке реально лежат: {os.listdir(self.det_model_dir)}")
            else:
                error_msg.append(
                    f"Папки {self.det_model_dir.name} вообще не существует!")

        if not self.rec_model_file.is_file():
            if error_msg:
                error_msg.append("\n" + "="*40 + "\n")
            error_msg.append(
                f"❌ НЕТ ФАЙЛА РАСПОЗНАВАНИЯ:\nИскал тут: {self.rec_model_file}")
            if self.rec_model_dir.exists():
                error_msg.append(
                    f"А в этой папке реально лежат: {os.listdir(self.rec_model_dir)}")
            else:
                error_msg.append(
                    f"Папки {self.rec_model_dir.name} вообще не существует!")

        if error_msg:
            raise OCRError("\n".join(error_msg))

    def recognize_pdf(
        self,
        pdf_path: str | Path,
        progress_callback: Callable[[int, str], None] | None = None,
        sufficiency_check: Callable[[str, list[dict[str, Any]]], bool | tuple[bool, str]] | None = None,
    ) -> dict[str, Any]:
        """Теперь возвращает словарь с 'text' и 'blocks' (гео-координатами)"""
        pdf_path = Path(pdf_path)

        if not pdf_path.exists() or not pdf_path.is_file():
            raise OCRError(f"Файл не найден:\n{pdf_path}")
        if pdf_path.suffix.lower() != ".pdf":
            raise OCRError("Поддерживаются только PDF-файлы.")

        try:
            import pymupdf
            document = pymupdf.open(str(pdf_path))
        except Exception as error:
            raise OCRError(f"Не удалось открыть PDF:\n{error}") from error

        from app.memory_metrics import set_process_below_normal_priority
        set_process_below_normal_priority(True)
        try:
            page_count = len(document)
            if page_count == 0:
                raise OCRError("PDF не содержит страниц.")

            result_texts: list[str] = []
            all_blocks: list[dict[str, Any]] = []

            self._progress(progress_callback, 5, "Подготовка документа...")

            # First page contains registration metadata in the normal workflow.
            # A second page is processed only when the first page yields very little
            # useful text, so quality improves without doubling the normal OCR cost.
            page_indices = [0]
            signature_only_pages: set[int] = set()
            page_index = 0
            final_preparsed: dict[str, Any] | None = None
            while page_index < len(page_indices):
                source_index = page_indices[page_index]
                current_page = source_index + 1
                self._progress(
                    progress_callback,
                    15 if source_index == 0 else 42,
                    f"Обработка страницы {current_page}...",
                )

                sig_only = source_index in signature_only_pages
                page_res = self._extract_page(
                    document,
                    source_index,
                    signature_only=sig_only,
                    sufficiency_check=sufficiency_check,
                )
                text, blocks = page_res[0], page_res[1]
                if page_index == 0:
                    final_preparsed = getattr(page_res, "preparsed", None)
                text = self._normalize_ocr_text(text)
                text = self._clean_text(text)

                if text:
                    result_texts.append(f"--- Страница {current_page} ---\n{text}")
                    all_blocks.extend(blocks)

                if page_count > 1 and source_index in (0, 1):
                    combined_so_far = "\n".join(result_texts)
                    has_signer = self._has_signer_on_page(combined_so_far)
                    if source_index == 0:
                        if len(text) < self.SECOND_PAGE_TEXT_THRESHOLD or (not has_signer and page_count <= 3):
                            target_next = 1 if (page_count == 2 or len(text) < self.SECOND_PAGE_TEXT_THRESHOLD) else page_count - 1
                            if target_next not in page_indices:
                                page_indices.append(target_next)
                                if len(text) >= 180 and not has_signer:
                                    signature_only_pages.add(target_next)
                    elif source_index == 1 and page_count == 3 and not has_signer and len(page_indices) < 3:
                        if (page_count - 1) not in page_indices:
                            page_indices.append(page_count - 1)
                            signature_only_pages.add(page_count - 1)
                page_index += 1

            final_text = "\n\n".join(result_texts).strip()
            if not final_text:
                raise OCRError("OCR не вернул текст.")

            self._progress(progress_callback, 55, "OCR завершён.")
            out_data: dict[str, Any] = {"text": final_text, "blocks": all_blocks}
            if len(page_indices) == 1 and final_preparsed:
                out_data["_preparsed"] = final_preparsed
            return out_data
        finally:
            document.close()
            set_process_below_normal_priority(False)

    def _extract_page(
        self,
        document,
        page_index: int,
        *,
        signature_only: bool = False,
        sufficiency_check: Callable[[str, list[dict[str, Any]]], bool | tuple[bool, str]] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        page = document[page_index]

        # 1. Пробуем извлечь нативный текст вместе с блоками (координатами)
        page_rect = getattr(page, "rect", None)
        pw_points = float(getattr(page_rect, "width", 0.0) or 0.0)
        ph_points = float(getattr(page_rect, "height", 0.0) or 0.0)
        if pw_points <= 0.0:
            pw_points = 595.0
        if ph_points <= 0.0:
            ph_points = 842.0

        base_scale = float(self.default_dpi) / 72.0
        page_width = pw_points * base_scale
        page_height = ph_points * base_scale

        native_blocks: list[dict[str, Any]] = []
        native_text_parts: list[str] = []
        dict_ok = False

        # Speed-up & Precision: extract line-level geometry via page.get_text("dict")
        try:
            text_dict = page.get_text("dict")
            for b in text_dict.get("blocks", []):
                if b.get("type") == 0:  # text block
                    for line in b.get("lines", []):
                        line_text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                        if line_text:
                            line_text = re.sub(r"[\xa0\u2000-\u200b\u202f\u205f\u3000]", " ", line_text)
                            line_text = re.sub(r"[ \t]+", " ", line_text).strip()
                            if line_text:
                                native_text_parts.append(line_text)
                                bbox = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
                                bx1 = float(bbox[0]) * base_scale
                                by1 = float(bbox[1]) * base_scale
                                bx2 = float(bbox[2]) * base_scale
                                by2 = float(bbox[3]) * base_scale
                                native_blocks.append({
                                    "text": line_text,
                                    "x1": bx1, "y1": by1, "x2": bx2, "y2": by2,
                                    "center_y": (by1 + by2) / 2.0,
                                    "height": max(1.0, by2 - by1),
                                    "confidence": 1.0,
                                    "page_index": page_index,
                                    "page_width": page_width,
                                    "page_height": page_height,
                                    "source": "native",
                                })
            dict_ok = True
        except Exception:
            native_blocks.clear()
            native_text_parts.clear()

        # Fallback to get_text("blocks") only if dict extraction failed with an exception
        if not dict_ok:
            try:
                native_blocks_raw = page.get_text("blocks")
                for b in native_blocks_raw:
                    if len(b) > 6 and b[6] == 0:  # text block
                        raw_block_text = str(b[4] or "").strip()
                        if raw_block_text:
                            raw_block_text = re.sub(r"[\xa0\u2000-\u200b\u202f\u205f\u3000]", " ", raw_block_text)
                            for block_line in raw_block_text.splitlines():
                                block_line = re.sub(r"[ \t]+", " ", block_line).strip()
                                if block_line:
                                    native_text_parts.append(block_line)
                                    bx1 = float(b[0]) * base_scale
                                    by1 = float(b[1]) * base_scale
                                    bx2 = float(b[2]) * base_scale
                                    by2 = float(b[3]) * base_scale
                                    native_blocks.append({
                                        "text": block_line,
                                        "x1": bx1, "y1": by1, "x2": bx2, "y2": by2,
                                        "center_y": (by1 + by2) / 2.0,
                                        "height": max(1.0, by2 - by1),
                                        "confidence": 1.0,
                                        "page_index": page_index,
                                        "page_width": page_width,
                                        "page_height": page_height,
                                        "source": "native",
                                    })
            except Exception:
                pass

        native_text = "\n".join(native_text_parts).strip()
        native_text = self._clean_text(self._normalize_ocr_text(native_text))

        native_usable = (
            len(native_text) >= self.MIN_NATIVE_TEXT_LENGTH
            and self._quality_score(native_text) >= self.NATIVE_TEXT_QUALITY_THRESHOLD
        )

        candidate_native_text = ""
        candidate_native_blocks: list[dict[str, Any]] = []

        if native_usable:
            is_native_sufficient = False
            native_reason = "threshold"
            native_regions: list[dict[str, Any]] = []

            if sufficiency_check is not None:
                try:
                    check_res = sufficiency_check(native_text, native_blocks)
                    if hasattr(check_res, "suggested_regions"):
                        native_regions = check_res.suggested_regions
                    if isinstance(check_res, tuple):
                        if len(check_res) >= 3:
                            is_native_sufficient, native_reason, native_regions = check_res[0], check_res[1], check_res[2]
                        elif len(check_res) >= 2:
                            is_native_sufficient, native_reason = check_res[0], check_res[1]
                        else:
                            is_native_sufficient = bool(check_res[0])
                    else:
                        is_native_sufficient = bool(check_res)
                        native_reason = "sufficient" if is_native_sufficient else "insufficient"
                except Exception as check_err:
                    is_native_sufficient = False
                    native_reason = f"error: {check_err}"
            else:
                is_native_sufficient = True
                native_reason = "quality_threshold"

            # Case 1: Pure Digital PDF - all core fields are present and sufficient!
            if is_native_sufficient:
                logger.info(
                    "Native text layer on page %d is sufficient (%s), skipping OCR entirely",
                    page_index + 1,
                    native_reason,
                )
                native_preparsed = getattr(check_res, "preparsed_fields", None) if sufficiency_check is not None else None
                return PageExtractResult(native_text, native_blocks, native_preparsed)

            # Case 2: Hybrid PDF - digital text layer is present, but missing specific fields
            # (e.g. raster stamp image in upper region or signature in footer).
            allow_heavy_retry = self._dynamic_memory_guard_allows_retry(
                self.resource_profile.economy_memory_policy
            )
            candidate_native_text = native_text
            candidate_native_blocks = native_blocks
            if len(native_text) >= 80 and native_regions and allow_heavy_retry:
                logger.info(
                    "Native text on page %d has content (%d chars) but missing (%s). Running hybrid zonal OCR on %d region(s)",
                    page_index + 1,
                    len(native_text),
                    native_reason,
                    len(native_regions),
                )
                hybrid_blocks = list(native_blocks)
                for region in native_regions:
                    norm_rect = region.get("norm_rect")
                    if not norm_rect or len(norm_rect) != 4:
                        continue
                    z_text, z_blocks = self.recognize_region(
                        page,
                        norm_rect,
                        dpi=self.retry_dpi,
                        page_index=page_index,
                        enhance=True,
                    )
                    if z_blocks:
                        hybrid_blocks = self._merge_zonal_blocks(hybrid_blocks, z_blocks, norm_rect)

                hybrid_text = self._reconstruct_text(hybrid_blocks)
                hybrid_text = self._clean_text(self._normalize_ocr_text(hybrid_text))
                candidate_native_text = hybrid_text
                candidate_native_blocks = hybrid_blocks

                # Re-check sufficiency of hybrid result
                hybrid_sufficient = False
                hybrid_reason = ""
                h_res = None
                if sufficiency_check is not None:
                    try:
                        h_res = sufficiency_check(hybrid_text, hybrid_blocks)
                        if isinstance(h_res, tuple):
                            hybrid_sufficient = bool(h_res[0])
                            hybrid_reason = str(h_res[1])
                        else:
                            hybrid_sufficient = bool(h_res)
                    except Exception:
                        pass

                if hybrid_sufficient:
                    logger.info(
                        "Hybrid zonal OCR resolved page %d (%s), full-page OCR bypassed",
                        page_index + 1,
                        hybrid_reason,
                    )
                    hybrid_preparsed = getattr(h_res, "preparsed_fields", None) if h_res is not None else None
                    return PageExtractResult(hybrid_text, hybrid_blocks, hybrid_preparsed)

                # If hybrid text has all critical core fields (date, department, number)
                # and substantial text length (>= 200 chars), accept without forcing full OCR.
                if len(hybrid_text) >= 200:
                    critical_missing = False
                    if sufficiency_check is not None:
                        try:
                            h_res = sufficiency_check(hybrid_text, hybrid_blocks)
                            if hasattr(h_res, "missing_reasons"):
                                critical_missing = any(
                                    r in h_res.missing_reasons
                                    for r in ("missing_or_unreliable_date", "missing_department", "missing_document_number")
                                )
                        except Exception:
                            pass
                    if not critical_missing:
                        logger.info(
                            "Hybrid text on page %d has all critical fields, accepting without full OCR",
                            page_index + 1,
                        )
                        hybrid_preparsed = getattr(h_res, "preparsed_fields", None) if h_res is not None else None
                        return PageExtractResult(hybrid_text, hybrid_blocks, hybrid_preparsed)

        # Speed-up 3: When inspecting a subsequent page solely for a missing signature,
        # render the bottom 55% first; fall back to full page only if empty/missing.
        if signature_only:
            sig_text, sig_blocks = self._ocr_page(
                page, dpi=self.default_dpi, page_index=page_index, y_start_ratio=0.45
            )
            sig_text = self._clean_text(self._normalize_ocr_text(sig_text))
            if len(sig_text) >= 16 and self._has_signer_on_page(sig_text):
                return PageExtractResult(sig_text, sig_blocks, None)

        # 2. Full-page OCR when native text layer is absent or poor quality.
        text, blocks = self._ocr_page(page, dpi=self.default_dpi, page_index=page_index)
        text = self._normalize_ocr_text(text)
        text = self._clean_text(text)

        quality = self._quality_score(text)
        confidence = self._average_confidence(blocks)

        is_sufficient = False
        sufficiency_reason = "threshold"
        suggested_regions: list[dict[str, Any]] = []
        if sufficiency_check is not None:
            try:
                check_res = sufficiency_check(text, blocks)
                if hasattr(check_res, "suggested_regions"):
                    suggested_regions = check_res.suggested_regions
                if isinstance(check_res, tuple):
                    if len(check_res) >= 3:
                        is_sufficient, sufficiency_reason, suggested_regions = check_res[0], check_res[1], check_res[2]
                    elif len(check_res) >= 2:
                        is_sufficient, sufficiency_reason = check_res[0], check_res[1]
                    else:
                        is_sufficient = bool(check_res[0])
                else:
                    is_sufficient = bool(check_res)
                    sufficiency_reason = "sufficient" if is_sufficient else "insufficient"
            except Exception as check_err:
                is_sufficient = False
                sufficiency_reason = f"error: {check_err}"

        best_text, best_blocks = text, blocks
        best_score = self._combined_ocr_score(quality, confidence)

        # Safety guard: if native/hybrid text was present, never discard it if full OCR scored worse
        if candidate_native_text and candidate_native_blocks:
            cand_score = self._combined_ocr_score(
                self._quality_score(candidate_native_text),
                self._average_confidence(candidate_native_blocks),
            )
            if cand_score > best_score:
                best_text, best_blocks = candidate_native_text, candidate_native_blocks
                best_score = cand_score

        allow_heavy_retry = self._dynamic_memory_guard_allows_retry(
            self.resource_profile.economy_memory_policy
        )

        # 3. Targeted Zonal Retry: when Pass 1 is not sufficient, re-OCR only the missing regions at 200 DPI
        re_check = None
        if not is_sufficient and suggested_regions and self.retry_dpi > self.default_dpi and allow_heavy_retry:
            merged_blocks = list(best_blocks)
            for region in suggested_regions:
                norm_rect = region.get("norm_rect")
                if not norm_rect or len(norm_rect) != 4:
                    continue
                z_text, z_blocks = self.recognize_region(
                    page,
                    norm_rect,
                    dpi=self.retry_dpi,
                    page_index=page_index,
                    enhance=True,
                )
                if z_blocks:
                    merged_blocks = self._merge_zonal_blocks(merged_blocks, z_blocks, norm_rect)

            merged_text = self._reconstruct_text(merged_blocks)
            merged_text = self._clean_text(self._normalize_ocr_text(merged_text))
            merged_quality = self._quality_score(merged_text)
            merged_conf = self._average_confidence(merged_blocks)
            merged_score = self._combined_ocr_score(merged_quality, merged_conf)

            # Re-evaluate sufficiency with merged blocks
            zonal_sufficient = False
            zonal_reason = ""
            if sufficiency_check is not None:
                try:
                    re_check = sufficiency_check(merged_text, merged_blocks)
                    if isinstance(re_check, tuple):
                        zonal_sufficient = bool(re_check[0])
                        zonal_reason = str(re_check[1])
                    else:
                        zonal_sufficient = bool(re_check)
                except Exception:
                    pass

            if zonal_sufficient or merged_score >= best_score:
                best_text, best_blocks = merged_text, merged_blocks
                best_score = max(best_score, merged_score)
                if zonal_sufficient:
                    is_sufficient = True
                    sufficiency_reason = f"zonal_resolved:{zonal_reason}"
                    logger.info(
                        "Zonal retry resolved sufficiency on page %d (%s), skipping full-page retry",
                        page_index + 1,
                        zonal_reason,
                    )

        final_preparsed = None
        if is_sufficient:
            if "zonal_resolved" in sufficiency_reason and re_check is not None:
                final_preparsed = getattr(re_check, "preparsed_fields", None)
            elif sufficiency_check is not None:
                final_preparsed = getattr(check_res, "preparsed_fields", None)
            logger.info(
                "OCR pass on page %d is sufficient (%s), skipping heavy retry and rotation",
                page_index + 1,
                sufficiency_reason,
            )
            needs_retry = False
        else:
            needs_retry = quality < self.RETRY_QUALITY_THRESHOLD or (
                confidence is not None and confidence < self.RETRY_CONFIDENCE_THRESHOLD
            ) or not is_sufficient

        # 4. Full-page fallback retry only if zonal retry did not resolve sufficiency
        if needs_retry and self.retry_dpi > self.default_dpi and allow_heavy_retry:
            final_preparsed = None
            logger.info("Executing full-page fallback retry at %d DPI for page %d", self.retry_dpi, page_index + 1)
            retry_text, retry_blocks = self._ocr_page(
                page, dpi=self.retry_dpi, enhance=True, retry_tuning=True, page_index=page_index
            )
            retry_text = self._normalize_ocr_text(retry_text)
            retry_text = self._clean_text(retry_text)

            retry_quality = self._quality_score(retry_text)
            retry_confidence = self._average_confidence(retry_blocks)
            retry_score = self._combined_ocr_score(retry_quality, retry_confidence)
            if retry_score > best_score:
                best_text, best_blocks, best_score = retry_text, retry_blocks, retry_score

        if not is_sufficient and best_score < self.ROTATION_RETRY_SCORE_THRESHOLD and allow_heavy_retry:
            final_preparsed = None
            for rotation_quarters in (1, 3):
                rotated_text, rotated_blocks = self._ocr_page(
                    page,
                    dpi=self.retry_dpi,
                    enhance=True,
                    rotation_quarters=rotation_quarters,
                    retry_tuning=True,
                    page_index=page_index,
                )
                rotated_text = self._clean_text(self._normalize_ocr_text(rotated_text))
                rotated_score = self._combined_ocr_score(
                    self._quality_score(rotated_text),
                    self._average_confidence(rotated_blocks),
                )
                if rotated_score > best_score:
                    best_text, best_blocks, best_score = rotated_text, rotated_blocks, rotated_score

        return PageExtractResult(best_text, best_blocks, final_preparsed)

    def recognize_region(
        self,
        page,
        norm_rect: tuple[float, float, float, float],
        *,
        dpi: int = 200,
        page_index: int = 0,
        enhance: bool = True,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Perform high-resolution OCR on a sub-region directly rendered from the PDF.

        `norm_rect` is (nx0, ny0, nx1, ny1) in normalized 0.0..1.0 page coordinates.
        The sub-region is clipped directly via PyMuPDF at `dpi`, passed through OCR,
        and all returned blocks have their coordinates translated into the standard
        full-page 150 DPI coordinate system.
        """
        import pymupdf

        page_rect = getattr(page, "rect", None)
        if page_rect is None or page_rect.width <= 0 or page_rect.height <= 0:
            return "", []

        pw, ph = float(page_rect.width), float(page_rect.height)
        nx0, ny0, nx1, ny1 = norm_rect
        nx0 = max(0.0, min(1.0, float(nx0)))
        ny0 = max(0.0, min(1.0, float(ny0)))
        nx1 = max(nx0 + 0.02, min(1.0, float(nx1)))
        ny1 = max(ny0 + 0.02, min(1.0, float(ny1)))

        # PDF point rectangle for PyMuPDF clip
        clip_rect = pymupdf.Rect(nx0 * pw, ny0 * ph, nx1 * pw, ny1 * ph)

        scale = float(dpi) / 72.0
        matrix = pymupdf.Matrix(scale, scale)
        colorspace = getattr(pymupdf, "csGRAY", None)
        pixmap = page.get_pixmap(matrix=matrix, colorspace=colorspace, alpha=False, clip=clip_rect)
        image = self._pixmap_to_numpy(pixmap, keep_grayscale=True)

        if enhance:
            image = self._enhance_image_for_retry(image, keep_grayscale=True)

        if image.ndim == 2:
            import cv2
            predict_input = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        elif image.ndim == 3 and image.shape[2] == 1:
            import cv2
            predict_input = cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
        else:
            predict_input = image

        engine = self._get_engine()
        results = engine.predict(predict_input)
        raw_text, raw_blocks = self._extract_text(results)
        del results
        del predict_input
        del image
        del pixmap

        base_scale = float(self.default_dpi) / 72.0
        coord_scale = float(self.default_dpi) / float(dpi)

        clip_offset_x = float(clip_rect.x0) * base_scale
        clip_offset_y = float(clip_rect.y0) * base_scale
        global_page_w = pw * base_scale
        global_page_h = ph * base_scale

        remapped_blocks: list[dict[str, Any]] = []
        for b in raw_blocks:
            lx1 = float(b.get("x1", 0.0))
            ly1 = float(b.get("y1", 0.0))
            lx2 = float(b.get("x2", lx1))
            ly2 = float(b.get("y2", ly1))

            gx1 = clip_offset_x + lx1 * coord_scale
            gy1 = clip_offset_y + ly1 * coord_scale
            gx2 = clip_offset_x + lx2 * coord_scale
            gy2 = clip_offset_y + ly2 * coord_scale

            remapped_blocks.append({
                "text": b.get("text", ""),
                "confidence": float(b.get("confidence", 0.85) or 0.85),
                "x1": gx1,
                "y1": gy1,
                "x2": gx2,
                "y2": gy2,
                "center_y": (gy1 + gy2) / 2.0,
                "height": max(1.0, gy2 - gy1),
                "page_index": page_index,
                "page_width": global_page_w,
                "page_height": global_page_h,
                "source": "ocr_zonal",
            })

        return raw_text, remapped_blocks

    @classmethod
    def _merge_zonal_blocks(
        cls,
        existing_blocks: list[dict[str, Any]],
        zonal_blocks: list[dict[str, Any]],
        norm_rect: tuple[float, float, float, float],
    ) -> list[dict[str, Any]]:
        """Merge high-res zonal OCR blocks into existing page blocks without losing context.

        Existing blocks outside the zone are untouched.
        Inside the zone, if zonal OCR found clear blocks, they replace or complement existing blocks.
        """
        if not zonal_blocks:
            return list(existing_blocks)
        if not existing_blocks:
            return list(zonal_blocks)

        page_w = max((float(b.get("page_width", 0) or 0) for b in existing_blocks), default=1.0)
        page_h = max((float(b.get("page_height", 0) or 0) for b in existing_blocks), default=1.0)
        nx0, ny0, nx1, ny1 = norm_rect
        zone_x1 = nx0 * page_w - 15.0
        zone_y1 = ny0 * page_h - 15.0
        zone_x2 = nx1 * page_w + 15.0
        zone_y2 = ny1 * page_h + 15.0

        preserved_outside: list[dict[str, Any]] = []
        candidates_inside: list[dict[str, Any]] = []

        for b in existing_blocks:
            cy = float(b.get("center_y", b.get("y1", 0)))
            cx = (float(b.get("x1", 0)) + float(b.get("x2", b.get("x1", 0)))) / 2.0
            if zone_x1 <= cx <= zone_x2 and zone_y1 <= cy <= zone_y2:
                candidates_inside.append(b)
            else:
                preserved_outside.append(b)

        # Inside zone, keep old block only if no zonal block overlaps vertically & horizontally
        kept_inside: list[dict[str, Any]] = []
        for old_b in candidates_inside:
            oy1 = float(old_b.get("y1", 0))
            oy2 = float(old_b.get("y2", oy1))
            ox1 = float(old_b.get("x1", 0))
            ox2 = float(old_b.get("x2", ox1))
            oh = max(1.0, oy2 - oy1)

            has_overlap = False
            for zb in zonal_blocks:
                zy1 = float(zb.get("y1", 0))
                zy2 = float(zb.get("y2", zy1))
                zx1 = float(zb.get("x1", 0))
                zx2 = float(zb.get("x2", zx1))
                v_overlap = max(0.0, min(oy2, zy2) - max(oy1, zy1))
                if v_overlap / oh >= 0.50:
                    h_overlap = max(0.0, min(ox2, zx2) - max(ox1, zx1))
                    if h_overlap > 0:
                        has_overlap = True
                        break
            if not has_overlap:
                kept_inside.append(old_b)

        merged = preserved_outside + kept_inside + list(zonal_blocks)
        merged.sort(key=lambda b: (
            int(b.get("page_index", 0) or 0),
            float(b.get("center_y", b.get("y1", 0) or 0)),
            float(b.get("x1", 0) or 0),
        ))
        return merged

    def _ocr_page(
        self,
        page,
        dpi: int,
        *,
        enhance: bool = False,
        rotation_quarters: int = 0,
        retry_tuning: bool = False,
        page_index: int = 0,
        clip_ratio: float = 1.0,
        y_start_ratio: float = 0.0,
    ) -> tuple[str, list[dict[str, Any]]]:
        image = None
        ocr_image = None
        predict_input = None
        pixmap = None
        results = None
        try:
            pixmap = self._render_page(
                page, dpi, clip_ratio=clip_ratio, y_start_ratio=y_start_ratio
            )
            # Speed-up 1: keep 1-channel grayscale array through crop, deskew, and enhance
            image = self._pixmap_to_numpy(pixmap, keep_grayscale=True)
            if enhance:
                image = self._enhance_image_for_retry(image, keep_grayscale=True)
            if rotation_quarters:
                import numpy as np
                image = np.ascontiguousarray(np.rot90(image, k=rotation_quarters))

            orig_height, orig_width = image.shape[:2]
            ocr_image, offset_x, offset_y = self._crop_margins(image)
            ocr_image, deskew_angle = self._deskew_image(ocr_image)
            deskew_h, deskew_w = ocr_image.shape[:2]

            # Convert 1-channel grayscale to 3-channel BGR once right before ONNX inference
            if ocr_image.ndim == 2:
                import cv2
                predict_input = cv2.cvtColor(ocr_image, cv2.COLOR_GRAY2BGR)
            elif ocr_image.ndim == 3 and ocr_image.shape[2] == 1:
                import cv2
                predict_input = cv2.cvtColor(ocr_image[:, :, 0], cv2.COLOR_GRAY2BGR)
            else:
                predict_input = ocr_image

            engine = self._get_engine()
            if retry_tuning:
                try:
                    results = engine.predict(
                        predict_input,
                        text_det_thresh=0.25,
                        text_det_box_thresh=0.48,
                        text_det_unclip_ratio=2.0,
                        text_rec_score_thresh=0.18,
                    )
                except TypeError:
                    results = engine.predict(predict_input)
            else:
                results = engine.predict(predict_input)
            text, blocks = self._extract_text(results)
            del results
            results = None
            if predict_input is not None:
                del predict_input
                predict_input = None
            if ocr_image is not None:
                del ocr_image
                ocr_image = None
            if image is not None:
                del image
                image = None
            if pixmap is not None:
                del pixmap
                pixmap = None
            if abs(deskew_angle) >= 1.2:
                import cv2
                import numpy as np

                cx, cy = deskew_w / 2.0, deskew_h / 2.0
                rot_inv = cv2.getRotationMatrix2D((cx, cy), -deskew_angle, 1.0)
                for block in blocks:
                    bx1 = float(block.get("x1", 0.0))
                    by1 = float(block.get("y1", 0.0))
                    bx2 = float(block.get("x2", 0.0))
                    by2 = float(block.get("y2", 0.0))
                    pts = np.array([[bx1, by1, 1.0], [bx2, by1, 1.0], [bx2, by2, 1.0], [bx1, by2, 1.0]])
                    transformed = pts @ rot_inv.T
                    block["x1"] = float(np.min(transformed[:, 0]))
                    block["y1"] = float(np.min(transformed[:, 1]))
                    block["x2"] = float(np.max(transformed[:, 0]))
                    block["y2"] = float(np.max(transformed[:, 1]))
            effective_ratio = max(0.05, clip_ratio - y_start_ratio)
            full_height = float(orig_height / effective_ratio) if effective_ratio < 1.0 else float(orig_height)
            y_shift = float(offset_y) + (full_height * y_start_ratio if y_start_ratio > 0.0 else 0.0)
            for block in blocks:
                if offset_x > 0 or y_shift > 0:
                    block["x1"] = float(block.get("x1", 0.0)) + offset_x
                    block["y1"] = float(block.get("y1", 0.0)) + y_shift
                    block["x2"] = float(block.get("x2", 0.0)) + offset_x
                    block["y2"] = float(block.get("y2", 0.0)) + y_shift
                    block["center_y"] = (block["y1"] + block["y2"]) / 2.0
                block["page_index"] = page_index
                block["page_width"] = float(orig_width)
                block["page_height"] = full_height
                block["source"] = "ocr"
            return text, blocks
        except Exception as error:
            raise OCRError(
                f"Ошибка распознавания страницы через PaddleOCR:\n{error}") from error
        finally:
            if results is not None:
                del results
            if predict_input is not None:
                del predict_input
            if ocr_image is not None:
                del ocr_image
            if image is not None:
                del image
            if pixmap is not None:
                del pixmap
            if self.resource_profile.memory_efficient_ocr:
                gc.collect()

    @staticmethod
    def _crop_margins(image, padding: int = 55):
        """Safely crop excessive whitespace margins on official documents."""
        if image is None or getattr(image, "size", 0) == 0 or len(getattr(image, "shape", ())) < 2:
            return image, 0, 0
        try:
            import cv2
            import numpy as np

            if len(image.shape) == 3 and image.shape[2] >= 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            elif len(image.shape) == 3 and image.shape[2] == 1:
                gray = image[:, :, 0]
            else:
                gray = image

            _, binary = cv2.threshold(gray, 230, 255, cv2.THRESH_BINARY_INV)
            row_sums = np.sum(binary > 0, axis=1)
            col_sums = np.sum(binary > 0, axis=0)

            text_rows = np.where(row_sums > 20)[0]
            text_cols = np.where(col_sums > 20)[0]

            if len(text_rows) == 0 or len(text_cols) == 0:
                return image, 0, 0

            img_h, img_w = image.shape[:2]
            x1 = max(0, int(text_cols[0]) - padding)
            y1 = max(0, int(text_rows[0]) - padding)
            x2 = min(img_w, int(text_cols[-1]) + padding)
            y2 = min(img_h, int(text_rows[-1]) + padding)

            orig_area = float(img_w * img_h)
            cropped_area = float((x2 - x1) * (y2 - y1))
            saving = 1.0 - (cropped_area / orig_area)

            if 0.08 <= saving <= 0.55:
                return image[y1:y2, x1:x2], x1, y1
        except Exception:
            pass
        return image, 0, 0

    @staticmethod
    def _deskew_image(image, min_angle: float = 1.2, max_angle: float = 12.0) -> tuple[Any, float]:
        """Detect and correct page skew on scanned documents.

        Speed-up 4: min_angle raised to 1.2 degrees (PP-OCRv5 detector handles <1.2 deg
        natively without full-page affine warp) and uses fast INTER_LINEAR on 1-channel gray.
        """
        if image is None or getattr(image, "size", 0) == 0:
            return image, 0.0
        try:
            import cv2
            import numpy as np

            if len(image.shape) == 3 and image.shape[2] >= 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            elif len(image.shape) == 3 and image.shape[2] == 1:
                gray = image[:, :, 0]
            else:
                gray = image

            h, w = gray.shape[:2]
            scale = 800.0 / max(h, w) if max(h, w) > 800 else 1.0
            if scale < 1.0:
                small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            else:
                small = gray

            _, thresh = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
            dilated = cv2.dilate(thresh, kernel, iterations=2)

            contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            angles = []
            for cnt in contours:
                if cv2.contourArea(cnt) < 150:
                    continue
                rect = cv2.minAreaRect(cnt)
                (rect_w, rect_h) = rect[1]
                angle = rect[2]
                if rect_w < rect_h:
                    rect_w, rect_h = rect_h, rect_w
                    angle += 90.0
                if rect_w < 50:
                    continue
                while angle > 45.0:
                    angle -= 90.0
                while angle < -45.0:
                    angle += 90.0
                if min_angle <= abs(angle) <= max_angle:
                    angles.append(angle)

            if not angles or len(angles) < 3:
                return image, 0.0

            median_angle = float(np.median(angles))
            if abs(median_angle) < min_angle or abs(median_angle) > max_angle:
                return image, 0.0

            center = (w / 2.0, h / 2.0)
            rot_mat = cv2.getRotationMatrix2D(center, median_angle, 1.0)
            deskewed = cv2.warpAffine(
                image,
                rot_mat,
                (w, h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(255, 255, 255) if len(image.shape) == 3 else 255,
            )
            return deskewed, median_angle
        except Exception:
            return image, 0.0

    def _render_page(self, page, dpi: int, clip_ratio: float = 1.0, y_start_ratio: float = 0.0):
        import pymupdf

        scale = dpi / 72.0
        rect = getattr(page, "rect", None)
        clip = None
        if (clip_ratio < 1.0 or y_start_ratio > 0.0) and rect is not None:
            clip = pymupdf.Rect(
                0,
                rect.height * max(0.0, y_start_ratio),
                rect.width,
                rect.height * min(1.0, clip_ratio),
            )

        target_rect = clip if clip is not None else rect
        if (
            target_rect is not None
            and getattr(target_rect, "width", 0.0) > 0.0
            and getattr(target_rect, "height", 0.0) > 0.0
        ):
            est_w = float(target_rect.width) * scale
            est_h = float(target_rect.height) * scale
            safe_max_w = max(1.0, float(self.max_image_width) - 1.0)
            safe_max_h = max(1.0, float(self.max_image_height) - 1.0)
            resize_factor = min(
                1.0,
                safe_max_w / max(1.0, est_w),
                safe_max_h / max(1.0, est_h),
            )
            scale *= resize_factor

        matrix = pymupdf.Matrix(scale, scale)
        colorspace = getattr(pymupdf, "csGRAY", None)
        pixmap = page.get_pixmap(matrix=matrix, colorspace=colorspace, alpha=False, clip=clip)

        width, height = pixmap.width, pixmap.height
        resize_factor = min(1.0, self.max_image_width / max(1, width), self.max_image_height / max(1, height))
        if resize_factor < 1.0:
            resized_scale = scale * resize_factor
            matrix = pymupdf.Matrix(resized_scale, resized_scale)
            pixmap = page.get_pixmap(matrix=matrix, colorspace=colorspace, alpha=False, clip=clip)

        return pixmap

    @staticmethod
    def _pixmap_to_numpy(pixmap, *, keep_grayscale: bool = False):
        import cv2
        import numpy as np

        samples = getattr(pixmap, "samples_mv", None)
        if samples is None:
            samples = pixmap.samples
        image = np.frombuffer(samples, dtype=np.uint8)
        if pixmap.n == 1:
            image = image.reshape(pixmap.height, pixmap.width)
            if not keep_grayscale:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            return image
        image = image.reshape(pixmap.height, pixmap.width, pixmap.n)
        if pixmap.n == 4:
            image = image[:, :, :3]
        return image

    @staticmethod
    def _enhance_image_for_retry(image, *, keep_grayscale: bool = False):
        """Lightweight contrast normalization for difficult scans."""
        import numpy as np

        array = np.asarray(image)
        if array.ndim == 3 and array.shape[2] >= 3:
            rgb = array[:, :, :3].astype(np.uint16, copy=False)
            gray = ((rgb[:, :, 0] * 77 + rgb[:, :, 1] * 150 + rgb[:, :, 2] * 29) >> 8).astype(np.uint8)
        elif array.ndim == 3 and array.shape[2] == 1:
            gray = array[:, :, 0].astype(np.uint8, copy=False)
        else:
            gray = array.astype(np.uint8, copy=False)

        sample = gray[::2, ::2] if gray.shape[0] >= 300 and gray.shape[1] >= 300 else gray
        low, high = np.percentile(sample, (2.0, 98.0))
        if high - low >= 18:
            import cv2

            lut_inputs = np.arange(256, dtype=np.float32)
            lut = np.clip((lut_inputs - float(low)) * (255.0 / float(high - low)), 0, 255).astype(np.uint8)
            gray = cv2.LUT(gray, lut)

        if float(gray.mean()) < 105.0:
            gray = 255 - gray

        if keep_grayscale:
            return np.ascontiguousarray(gray)
        return np.ascontiguousarray(np.repeat(gray[:, :, None], 3, axis=2))

    def _extract_text(self, results) -> tuple[str, list[dict[str, Any]]]:
        import numpy as np

        blocks: list[dict[str, float | str]] = []

        for result in results:
            data = self._get_result_data(result)
            if not data:
                continue

            rec_texts = data.get("rec_texts", [])
            rec_scores = data.get("rec_scores", [])
            rec_boxes = data.get("rec_boxes") or data.get("rec_polys")

            if rec_boxes is None:
                for index, raw_text in enumerate(rec_texts):
                    text = self._safe_text(raw_text)
                    if text:
                        confidence = self._safe_confidence(rec_scores, index)
                        blocks.append({
                            "text": text, "x1": 0.0, "y1": float(index),
                            "x2": 0.0, "y2": float(index), "center_y": float(index), "height": 1.0,
                            "confidence": confidence,
                        })
                continue

            try:
                boxes = np.asarray(rec_boxes)
            except Exception:
                continue

            for index, raw_text in enumerate(rec_texts):
                text = self._safe_text(raw_text)
                if not text or index >= len(boxes):
                    continue

                box = np.asarray(boxes[index]).reshape(-1)
                if len(box) < 4:
                    continue

                try:
                    x1, y1, x2, y2 = float(box[0]), float(
                        box[1]), float(box[2]), float(box[3])
                except (TypeError, ValueError):
                    continue

                if x2 < x1:
                    x1, x2 = x2, x1
                if y2 < y1:
                    y1, y2 = y2, y1

                blocks.append({
                    "text": text, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "center_y": (y1 + y2) / 2.0, "height": max(1.0, y2 - y1),
                    "confidence": self._safe_confidence(rec_scores, index),
                })

        if not blocks:
            return "", []

        full_text = self._reconstruct_text(blocks)
        return full_text, blocks

    @staticmethod
    def _safe_confidence(scores, index: int) -> float | None:
        try:
            value = float(scores[index])
        except (TypeError, ValueError, IndexError):
            return None
        return max(0.0, min(1.0, value))

    @staticmethod
    def _average_confidence(blocks: list[dict[str, Any]]) -> float | None:
        weighted_sum = 0.0
        total_weight = 0.0
        for block in blocks:
            confidence = block.get("confidence")
            if confidence is None:
                continue
            text = str(block.get("text", "")).strip()
            weight = float(max(1, min(24, len(text))))
            weighted_sum += float(confidence) * weight
            total_weight += weight
        if total_weight == 0:
            return None
        return weighted_sum / total_weight

    @staticmethod
    def _combined_ocr_score(quality: float, confidence: float | None) -> float:
        if confidence is None:
            return quality
        return quality * 0.72 + confidence * 0.28

    @staticmethod
    def _get_result_data(result) -> dict[str, Any] | None:
        try:
            data = result.json
        except Exception:
            data = None
        if callable(data):
            try:
                data = data()
            except Exception:
                data = None
        if not isinstance(data, dict):
            return None
        return data.get("res", data) if isinstance(data.get("res", data), dict) else None

    @classmethod
    def _reconstruct_text(cls, blocks: list[dict[str, Any]]) -> str:
        blocks = sorted(blocks, key=lambda block: (
            float(block["center_y"]), float(block["x1"])))
        lines: list[dict[str, Any]] = []

        for block in blocks:
            placed = False
            block_y, block_height = float(
                block["center_y"]), float(block["height"])

            for line in lines:
                tolerance = max(block_height, float(
                    line["height"])) * cls.LINE_Y_TOLERANCE
                if abs(block_y - float(line["center_y"])) <= tolerance:
                    line["blocks"].append(block)
                    centers = [float(item["center_y"])
                               for item in line["blocks"]]
                    line["center_y"] = sum(centers) / len(centers)
                    line["height"] = max(float(item["height"])
                                         for item in line["blocks"])
                    placed = True
                    break

            if not placed:
                lines.append(
                    {"center_y": block_y, "height": block_height, "blocks": [block]})

        lines.sort(key=lambda line: float(line["center_y"]))
        output: list[str] = []

        for line in lines:
            line_blocks = sorted(
                line["blocks"], key=lambda block: float(block["x1"]))
            parts: list[str] = []
            previous_x2: float | None = None
            average_height = max(1.0, float(line["height"]))

            for block in line_blocks:
                text = str(block["text"]).strip()
                if not text:
                    continue
                if previous_x2 is not None:
                    if float(block["x1"]) - previous_x2 > average_height * 2.2:
                        parts.append("   ")
                parts.append(text)
                previous_x2 = float(block["x2"])

            line_text = re.sub(r"[ \t]+", " ", "".join(parts)).strip()
            if line_text:
                output.append(line_text)

        joined = "\n".join(output)
        return cls._stitch_hyphenated_words(joined)

    _COMPOUND_HYPHEN_PREFIXES = {
        "врач", "судебно", "медико", "клинико", "лечебно", "санитарно", "военно",
        "акушерско", "приемно", "экспертно", "диагностико", "социально",
        "научно", "нормативно", "административно", "материально",
        "письмо", "интернет", "онлайн", "экспресс", "генерал", "полковник", "майор", "капитан",
        "премьер", "вице", "пресс", "шеф", "экс",
        "северо", "юго", "западно", "восточно", "санкт",
    }

    @classmethod
    def _stitch_hyphenated_words(cls, text: str) -> str:
        def _replace(match: re.Match) -> str:
            w1, w2 = match.group(1), match.group(2)
            if (
                w1.lower() in cls._COMPOUND_HYPHEN_PREFIXES
                or w2.lower() in {"хирург", "терапевт", "педиатр", "запрос", "ответ", "сообщение"}
            ):
                return f"{w1}-{w2}\n"
            return f"{w1}{w2}\n"
        return re.sub(r"([А-ЯЁа-яё]{2,})-\n([а-яё]{2,})", _replace, text)

    @classmethod
    def _quality_score(cls, text: str) -> float:
        if not text:
            return 0.0
        text = text.strip()
        if not text:
            return 0.0

        length = len(text)
        score = 1.0

        if length < 40:
            score -= 0.35
        elif length < 100:
            score -= 0.15

        useful = sum(char.isalnum() for char in text)
        useful_ratio = useful / max(1, length)
        if useful_ratio < 0.20:
            score -= 0.30
        elif useful_ratio < 0.35:
            score -= 0.12

        letters = sum(char.isalpha() for char in text)
        if letters >= 20:
            cyrillic = len(cls._CYRILLIC_RE.findall(text))
            latin = len(cls._LATIN_RE.findall(text))
            if cyrillic / max(1, cyrillic + latin) < 0.25:
                score -= 0.15

        garbage = sum(1 for char in text if unicodedata.category(
            char).startswith("C") or char == "\ufffd")
        if garbage / max(1, length) > 0.02:
            score -= 0.20

        tokens = re.findall(r"\S+", text)
        if tokens and sum(len(token) == 1 and not token.isdigit() for token in tokens) / len(tokens) > 0.35:
            score -= 0.15

        return max(0.0, min(1.0, score))

    @classmethod
    def _normalize_token_lookalikes(cls, token: str, line_has_cyrillic: bool) -> str:
        low = token.lower()
        if "@" in token or "http" in low or "www." in low:
            return token
        tok_cyr = len(cls._CYRILLIC_RE.findall(token))
        tok_lat = len(cls._LATIN_RE.findall(token))
        if tok_lat == 0:
            return token
        has_slash_or_code = "/" in token or ("-" in token and any(ch.isdigit() for ch in token))
        if has_slash_or_code and tok_cyr < 2:
            return token
        if tok_cyr == 0 and cls._NON_LOOKALIKE_LATIN_RE.search(token):
            return token
        if tok_cyr >= 1 or line_has_cyrillic or tok_lat >= 3:
            return token.translate(cls.CYRILLIC_LOOKALIKE_MAP)
        return token

    @classmethod
    def _normalize_ocr_text(cls, text: str) -> str:
        if not text:
            return ""
        normalized_lines: list[str] = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            cyrillic_count = len(cls._CYRILLIC_RE.findall(line))
            latin_count = len(cls._LATIN_RE.findall(line))

            if cyrillic_count >= 2 or (
                latin_count >= 3
                and cyrillic_count == 0
                and "@" not in line
                and "http" not in line.lower()
                and "www." not in line.lower()
            ):
                parts = re.split(r"(\s+)", line)
                line = "".join(
                    part if not part.strip() else cls._normalize_token_lookalikes(part, cyrillic_count >= 2)
                    for part in parts
                )

            line = re.sub(r"[ \t]+", " ", line).strip()
            if line:
                normalized_lines.append(line)

        return "\n".join(normalized_lines)

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        cleaned_lines: list[str] = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            cleaned_chars = [" " if char == "\t" else char for char in line if char != "\ufffd" and not (
                unicodedata.category(char).startswith("C") and char != "\t")]
            line = re.sub(r"[ \t]+", " ", "".join(cleaned_chars)).strip()
            if line:
                cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    @staticmethod
    def _safe_text(text: Any) -> str:
        return "" if text is None else str(text).strip()

    @staticmethod
    def _progress(callback: Callable[[int, str], None] | None, value: int, message: str) -> None:
        if callback:
            callback(value, message)

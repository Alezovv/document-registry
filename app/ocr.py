from __future__ import annotations

import gc
import re
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable

from app.app_paths import get_app_paths
from app.resource_profile import resource_profile_from_environment



class OCRError(Exception):
    """Ошибка при выполнении OCR."""
    pass


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
        self.cpu_threads = self.resource_profile.worker_threads

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

                engine = PaddleOCR(
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_detection_model_dir=str(self.det_model_dir),
                    text_recognition_model_name="eslav_PP-OCRv5_mobile_rec",
                    text_recognition_model_dir=str(self.rec_model_dir),
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
            page_index = 0
            while page_index < len(page_indices):
                source_index = page_indices[page_index]
                current_page = source_index + 1
                self._progress(
                    progress_callback,
                    15 if source_index == 0 else 42,
                    f"Обработка страницы {current_page}...",
                )

                text, blocks = self._extract_page(document, source_index)
                text = self._normalize_ocr_text(text)
                text = self._clean_text(text)

                if text:
                    result_texts.append(f"--- Страница {current_page} ---\n{text}")
                    all_blocks.extend(blocks)

                if (
                    source_index == 0
                    and page_count > 1
                    and len(text) < self.SECOND_PAGE_TEXT_THRESHOLD
                ):
                    page_indices.append(1)
                page_index += 1

            final_text = "\n\n".join(result_texts).strip()
            if not final_text:
                raise OCRError("OCR не вернул текст.")

            self._progress(progress_callback, 55, "OCR завершён.")
            return {"text": final_text, "blocks": all_blocks}
        finally:
            document.close()

    def _extract_page(self, document, page_index: int) -> tuple[str, list[dict[str, Any]]]:
        page = document[page_index]

        # 1. Пробуем извлечь нативный текст вместе с блоками (координатами)
        native_blocks_raw = page.get_text("blocks")
        page_rect = getattr(page, "rect", None)
        page_width = float(getattr(page_rect, "width", 0.0) or 0.0)
        page_height = float(getattr(page_rect, "height", 0.0) or 0.0)
        if page_width <= 0.0:
            page_width = max(
                (float(block[2]) for block in native_blocks_raw if len(block) > 2),
                default=1.0,
            )
        if page_height <= 0.0:
            page_height = max(
                (float(block[3]) for block in native_blocks_raw if len(block) > 3),
                default=1.0,
            )
        native_blocks = []
        native_text_parts: list[str] = []
        for b in native_blocks_raw:
            if len(b) > 6 and b[6] == 0:  # text block
                text = str(b[4] or "").strip()
                if text:
                    native_text_parts.append(text)
                    native_blocks.append({
                        "text": text,
                        "x1": b[0], "y1": b[1], "x2": b[2], "y2": b[3],
                        "center_y": (b[1] + b[3]) / 2.0,
                        "height": max(1.0, b[3] - b[1]),
                        "confidence": 1.0,
                        "page_index": page_index,
                        "page_width": page_width,
                        "page_height": page_height,
                        "source": "native",
                    })

        # One PyMuPDF text pass is enough: the block payload already contains text.
        native_text = "\n".join(native_text_parts).strip()
        if (
            len(native_text) >= self.MIN_NATIVE_TEXT_LENGTH
            and self._quality_score(native_text) >= self.NATIVE_TEXT_QUALITY_THRESHOLD
        ):
            return native_text, native_blocks

        # 2. If the embedded text layer is absent or obviously broken, use OCR.
        text, blocks = self._ocr_page(page, dpi=self.default_dpi, page_index=page_index)
        text = self._normalize_ocr_text(text)
        text = self._clean_text(text)

        quality = self._quality_score(text)
        confidence = self._average_confidence(blocks)
        needs_retry = quality < self.RETRY_QUALITY_THRESHOLD or (
            confidence is not None and confidence < self.RETRY_CONFIDENCE_THRESHOLD
        )
        best_text, best_blocks = text, blocks
        best_score = self._combined_ocr_score(quality, confidence)

        if needs_retry and self.retry_dpi > self.default_dpi:
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

        # Rotation fallback is intentionally rare. It rescues sideways scans without
        # loading orientation models or adding permanent RAM usage.
        if best_score < self.ROTATION_RETRY_SCORE_THRESHOLD:
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

        return best_text, best_blocks

    def _ocr_page(
        self,
        page,
        dpi: int,
        *,
        enhance: bool = False,
        rotation_quarters: int = 0,
        retry_tuning: bool = False,
        page_index: int = 0,
    ) -> tuple[str, list[dict[str, Any]]]:
        image = None
        pixmap = None
        results = None
        try:
            pixmap = self._render_page(page, dpi)
            image = self._pixmap_to_numpy(pixmap)
            if enhance:
                image = self._enhance_image_for_retry(image)
            if rotation_quarters:
                import numpy as np
                image = np.ascontiguousarray(np.rot90(image, k=rotation_quarters))
            engine = self._get_engine()
            if retry_tuning:
                try:
                    results = engine.predict(
                        image,
                        text_det_thresh=0.25,
                        text_det_box_thresh=0.48,
                        text_det_unclip_ratio=2.0,
                        text_rec_score_thresh=0.18,
                    )
                except TypeError:
                    # Compatibility with older PaddleOCR 3.x builds whose
                    # ONNX wrapper did not expose predict-time thresholds.
                    results = engine.predict(image)
            else:
                results = engine.predict(image)
            text, blocks = self._extract_text(results)
            height, width = image.shape[:2]
            for block in blocks:
                block["page_index"] = page_index
                block["page_width"] = float(width)
                block["page_height"] = float(height)
                block["source"] = "ocr"
            return text, blocks
        except Exception as error:
            raise OCRError(
                f"Ошибка распознавания страницы через PaddleOCR:\n{error}") from error
        finally:
            # Do not release OCR._shared_ocr here: keeping the model loaded makes
            # the next document fast. Only discard per-page inference buffers.
            if results is not None:
                del results
            if image is not None:
                del image
            if pixmap is not None:
                del pixmap
            # NumPy/PyMuPDF buffers are reference-counted. A full GC after every
            # page costs CPU and does not unload the warm OCR model; keep it only
            # for the explicit low-memory mode.
            if self.resource_profile.memory_efficient_ocr:
                gc.collect()

    def _render_page(self, page, dpi: int):
        import pymupdf

        scale = dpi / 72.0
        matrix = pymupdf.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)

        width, height = pixmap.width, pixmap.height
        resize_factor = min(1.0, self.max_image_width /
                            width, self.max_image_height / height)

        if resize_factor < 1.0:
            resized_scale = scale * resize_factor
            matrix = pymupdf.Matrix(resized_scale, resized_scale)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)

        return pixmap

    @staticmethod
    def _pixmap_to_numpy(pixmap):
        import numpy as np

        # PyMuPDF's samples_mv exposes the Pixmap buffer without allocating a
        # second full-page bytes object. Fall back to samples on older builds.
        samples = getattr(pixmap, "samples_mv", None)
        if samples is None:
            samples = pixmap.samples
        image = np.frombuffer(samples, dtype=np.uint8)
        image = image.reshape(pixmap.height, pixmap.width, pixmap.n)
        if pixmap.n == 4:
            image = image[:, :, :3]
        return image

    @staticmethod
    def _enhance_image_for_retry(image):
        """Lightweight contrast normalization for difficult scans.

        Uses only NumPy, so it adds no new dependency or resident OCR model.
        The normal first pass is untouched; this runs only after a weak result.
        """
        import numpy as np

        array = np.asarray(image)
        if array.ndim == 3 and array.shape[2] >= 3:
            # Integer luminance avoids a large temporary float RGB image.
            rgb = array[:, :, :3].astype(np.uint16, copy=False)
            gray = ((rgb[:, :, 0] * 77 + rgb[:, :, 1] * 150 + rgb[:, :, 2] * 29) >> 8).astype(np.uint8)
        else:
            gray = array.astype(np.uint8, copy=False)

        low, high = np.percentile(gray, (2.0, 98.0))
        if high - low >= 18:
            work = gray.astype(np.float32)
            work = (work - float(low)) * (255.0 / float(high - low))
            gray = np.clip(work, 0, 255).astype(np.uint8)

        # Dark-background scans are uncommon but otherwise confuse text detection.
        if float(gray.mean()) < 105.0:
            gray = 255 - gray

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

        return "\n".join(output)

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
            cyrillic = sum("CYRILLIC" in unicodedata.name(char, "")
                           for char in text)
            latin = sum("LATIN" in unicodedata.name(char, "") for char in text)
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
    def _normalize_ocr_text(cls, text: str) -> str:
        if not text:
            return ""
        normalized_lines: list[str] = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            cyrillic_count = sum(
                "CYRILLIC" in unicodedata.name(char, "") for char in line)
            latin_count = sum("LATIN" in unicodedata.name(char, "")
                              for char in line)

            if cyrillic_count >= 2:
                line = line.translate(cls.CYRILLIC_LOOKALIKE_MAP)
            elif latin_count >= 3 and cyrillic_count == 0 and "@" not in line and "http" not in line.lower() and "www." not in line.lower():
                line = line.translate(cls.CYRILLIC_LOOKALIKE_MAP)

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

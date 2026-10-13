from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
import math
import os
from pathlib import Path
import re
import threading
from time import perf_counter
import unicodedata
from typing import Any, Iterable

from app.ner import NerEngine, NerEntity, NerLabel
from app.parser_core import LayoutAnalyzer, PageZone


@dataclass(slots=True)
class EncodedText:
    input_ids: list[int]
    attention_mask: list[int]
    token_type_ids: list[int]
    offsets: list[tuple[int, int]]
    tokens: list[str]


class BertWordPieceTokenizer:
    """Small dependency-free BERT tokenizer with character offsets.

    The selected Russian NER model uses the classic ``BertTokenizer`` with
    ``do_lower_case=false``.  Keeping the tokenizer in the application avoids
    pulling the full Transformers stack into the portable build just for NER.
    """

    def __init__(self, vocab_file: str | Path):
        self.vocab_file = Path(vocab_file)
        tokens = self.vocab_file.read_text(encoding="utf-8").splitlines()
        if not tokens:
            raise RuntimeError(f"Пустой словарь NER: {self.vocab_file}")
        self.vocab = {token: index for index, token in enumerate(tokens)}
        for required in ("[PAD]", "[UNK]", "[CLS]", "[SEP]"):
            if required not in self.vocab:
                raise RuntimeError(f"В словаре NER отсутствует {required}: {self.vocab_file}")

    @staticmethod
    def _is_whitespace(char: str) -> bool:
        return char.isspace() or char in {"\t", "\n", "\r"}

    @staticmethod
    def _is_control(char: str) -> bool:
        if char in {"\t", "\n", "\r"}:
            return False
        return unicodedata.category(char) in {"Cc", "Cf"}

    @staticmethod
    def _is_punctuation(char: str) -> bool:
        code = ord(char)
        if (33 <= code <= 47) or (58 <= code <= 64) or (91 <= code <= 96) or (123 <= code <= 126):
            return True
        return unicodedata.category(char).startswith("P")

    @classmethod
    def _basic_tokens(cls, text: str) -> list[tuple[str, int, int]]:
        output: list[tuple[str, int, int]] = []
        start: int | None = None
        for index, char in enumerate(text):
            if cls._is_control(char) or cls._is_whitespace(char):
                if start is not None:
                    output.append((text[start:index], start, index))
                    start = None
                continue
            if cls._is_punctuation(char):
                if start is not None:
                    output.append((text[start:index], start, index))
                    start = None
                output.append((char, index, index + 1))
                continue
            if start is None:
                start = index
        if start is not None:
            output.append((text[start:], start, len(text)))
        return output

    def _wordpiece(self, token: str, absolute_start: int) -> list[tuple[str, int, int]]:
        if token in self.vocab:
            return [(token, absolute_start, absolute_start + len(token))]
        if len(token) > 100:
            return [("[UNK]", absolute_start, absolute_start + len(token))]

        pieces: list[tuple[str, int, int]] = []
        start = 0
        while start < len(token):
            end = len(token)
            match: str | None = None
            match_end = start
            while start < end:
                fragment = token[start:end]
                candidate = fragment if start == 0 else "##" + fragment
                if candidate in self.vocab:
                    match = candidate
                    match_end = end
                    break
                end -= 1
            if match is None:
                return [("[UNK]", absolute_start, absolute_start + len(token))]
            pieces.append((match, absolute_start + start, absolute_start + match_end))
            start = match_end
        return pieces

    def encode(self, text: str, max_length: int = 256) -> EncodedText:
        max_length = max(8, int(max_length))
        pieces: list[tuple[str, int, int]] = []
        for token, start, _end in self._basic_tokens(text):
            pieces.extend(self._wordpiece(token, start))
            if len(pieces) >= max_length - 2:
                pieces = pieces[: max_length - 2]
                break

        tokens = ["[CLS]"] + [item[0] for item in pieces] + ["[SEP]"]
        offsets = [(-1, -1)] + [(item[1], item[2]) for item in pieces] + [(-1, -1)]
        input_ids = [self.vocab.get(token, self.vocab["[UNK]"]) for token in tokens]
        return EncodedText(
            input_ids=input_ids,
            attention_mask=[1] * len(input_ids),
            token_type_ids=[0] * len(input_ids),
            offsets=offsets,
            tokens=tokens,
        )


class OnnxRussianNerEngine(NerEngine):
    """CPU-only Russian NER backend for the tiny quantized ONNX model.

    Heavy modules and the ONNX session are loaded lazily on the first document.
    One engine instance is intended to be reused for the whole process.
    """

    name = "onnx-rubert-tiny-news-int8-v1"
    MODEL_FILENAMES = ("model_int8.onnx", "model_quantized.onnx", "model.onnx")

    def __init__(
        self,
        model_dir: str | Path,
        *,
        max_length: int = 256,
        intra_op_threads: int | None = None,
        min_confidence: float = 0.70,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.max_length = max(64, min(512, int(max_length)))
        self.intra_op_threads = intra_op_threads
        self.min_confidence = max(0.0, min(0.99, float(min_confidence)))
        self._load_lock = threading.RLock()
        self._run_lock = threading.RLock()
        self._loaded = False
        self._session: Any = None
        self._np: Any = None
        self._tokenizer: BertWordPieceTokenizer | None = None
        self._id2label: dict[int, str] = {}
        self._model_path: Path | None = None
        self._load_ms = 0.0
        self.last_metrics: dict[str, Any] = {
            "engine": self.name,
            "active_engine": self.name,
            "loaded": False,
        }

    @classmethod
    def required_files_present(cls, model_dir: str | Path) -> bool:
        path = Path(model_dir)
        if not (path / "config.json").is_file() or not (path / "vocab.txt").is_file():
            return False
        return any((path / name).is_file() for name in cls.MODEL_FILENAMES)

    def _choose_model_path(self) -> Path:
        for filename in self.MODEL_FILENAMES:
            candidate = self.model_dir / filename
            if candidate.is_file():
                return candidate
        raise RuntimeError(
            "Не найден ONNX-файл NER. Ожидался model_int8.onnx "
            f"в {self.model_dir}."
        )

    @staticmethod
    def _rss_mb() -> float | None:
        try:
            psutil = importlib.import_module("psutil")
            return round(float(psutil.Process(os.getpid()).memory_info().rss) / (1024 * 1024), 1)
        except Exception:
            return None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            start = perf_counter()
            rss_before = self._rss_mb()
            try:
                np = importlib.import_module("numpy")
                ort = importlib.import_module("onnxruntime")
            except ImportError as error:
                raise RuntimeError(
                    "Для ML NER нужен onnxruntime. Запустите приложение из рабочего окружения "
                    "или пересоберите portable-версию с ONNX Runtime."
                ) from error

            config_path = self.model_dir / "config.json"
            vocab_path = self.model_dir / "vocab.txt"
            if not config_path.is_file() or not vocab_path.is_file():
                raise RuntimeError(f"Неполный комплект NER-модели: {self.model_dir}")

            try:
                config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise RuntimeError(f"Не удалось прочитать NER config.json: {error}") from error
            raw_labels = config.get("id2label") or {}
            self._id2label = {int(key): str(value) for key, value in raw_labels.items()}
            if not self._id2label:
                raise RuntimeError("NER config.json не содержит id2label.")

            options = ort.SessionOptions()
            threads = self.intra_op_threads
            if threads is None:
                try:
                    threads = int(os.environ.get("DOCUMENT_REGISTRY_OCR_THREADS", "1"))
                except ValueError:
                    threads = 1
            options.intra_op_num_threads = max(1, min(2, int(threads or 1)))
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            model_path = self._choose_model_path()
            session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            tokenizer = BertWordPieceTokenizer(vocab_path)

            self._np = np
            self._session = session
            self._tokenizer = tokenizer
            self._model_path = model_path
            self._load_ms = round((perf_counter() - start) * 1000, 1)
            self._loaded = True
            rss_after = self._rss_mb()
            self.last_metrics = {
                "engine": self.name,
                "active_engine": self.name,
                "loaded": True,
                "model": model_path.name,
                "load_ms": self._load_ms,
                "rss_before_mb": rss_before,
                "rss_after_load_mb": rss_after,
                "rss_load_delta_mb": (
                    round(rss_after - rss_before, 1)
                    if rss_before is not None and rss_after is not None else None
                ),
            }

    @staticmethod
    def _normalize_label(raw: str) -> tuple[str, str] | None:
        text = str(raw or "").upper()
        if text == "O" or "-" not in text:
            return None
        prefix, label = text.split("-", 1)
        mapped = {
            "PER": NerLabel.PERSON.value,
            "ORG": NerLabel.ORGANIZATION.value,
            "LOC": NerLabel.LOCATION.value,
            "GEOPOLIT": NerLabel.LOCATION.value,
        }.get(label)
        if mapped is None:
            return None
        return prefix, mapped

    @staticmethod
    def _softmax_confidence(row: Any, label_index: int, np: Any) -> float:
        shifted = row - np.max(row)
        exponent = np.exp(shifted)
        denom = float(np.sum(exponent))
        if denom <= 0:
            return 0.0
        return float(exponent[label_index] / denom)

    def _run_line(self, line: str, block: dict[str, Any], zone: str) -> tuple[list[NerEntity], int, float]:
        assert self._session is not None and self._np is not None and self._tokenizer is not None
        np = self._np
        encoded = self._tokenizer.encode(line, self.max_length)
        if len(encoded.input_ids) <= 2:
            return [], len(encoded.input_ids), 0.0

        inputs = {
            "input_ids": np.asarray([encoded.input_ids], dtype=np.int64),
            "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
            "token_type_ids": np.asarray([encoded.token_type_ids], dtype=np.int64),
        }
        expected = {item.name for item in self._session.get_inputs()}
        feed = {name: value for name, value in inputs.items() if name in expected}
        if "input_ids" not in feed:
            raise RuntimeError("ONNX NER model does not expose input_ids.")

        started = perf_counter()
        outputs = self._session.run(None, feed)
        inference_ms = (perf_counter() - started) * 1000
        if not outputs:
            raise RuntimeError("ONNX NER model returned no outputs.")
        logits = outputs[0]
        if getattr(logits, "ndim", 0) != 3 or logits.shape[0] != 1:
            raise RuntimeError(f"Unexpected NER output shape: {getattr(logits, 'shape', None)}")

        labels = np.argmax(logits[0], axis=-1)
        entities: list[NerEntity] = []
        current: dict[str, Any] | None = None

        def flush() -> None:
            nonlocal current
            if current is None:
                return
            start = int(current["start"])
            end = int(current["end"])
            value = line[start:end].strip(" \t\r\n,;:")
            if value:
                confidences = current["confidences"]
                entities.append(NerEntity(
                    text=value,
                    label=current["label"],
                    confidence=round(sum(confidences) / max(1, len(confidences)), 4),
                    zone=zone,
                    page_index=self._page(block),
                    source_blocks=[dict(block)],
                    engine=self.name,
                    metadata={
                        "span": [start, end],
                        "token_count": len(confidences),
                        "onnx_model": self._model_path.name if self._model_path else "",
                    },
                ))
            current = None

        for index, label_id_raw in enumerate(labels.tolist()):
            if index >= len(encoded.offsets):
                break
            start, end = encoded.offsets[index]
            if start < 0 or end <= start:
                flush()
                continue
            label_id = int(label_id_raw)
            normalized = self._normalize_label(self._id2label.get(label_id, "O"))
            if normalized is None:
                flush()
                continue
            prefix, label = normalized
            confidence = self._softmax_confidence(logits[0][index], label_id, np)
            if confidence < self.min_confidence:
                flush()
                continue

            if current is None or prefix == "B" or current["label"] != label:
                flush()
                current = {
                    "label": label,
                    "start": start,
                    "end": end,
                    "confidences": [confidence],
                }
            else:
                current["end"] = end
                current["confidences"].append(confidence)
        flush()
        return entities, len(encoded.input_ids), inference_ms

    @staticmethod
    def _page(block: dict[str, Any]) -> int | None:
        raw = block.get("page_index")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dedupe(entities: Iterable[NerEntity]) -> list[NerEntity]:
        best: dict[tuple[str, str, int | None, str], NerEntity] = {}
        for entity in entities:
            key = (
                entity.label,
                re.sub(r"\s+", " ", entity.text.lower().replace("ё", "е")).strip(),
                entity.page_index,
                entity.zone,
            )
            previous = best.get(key)
            if previous is None or entity.confidence > previous.confidence:
                best[key] = entity
        return sorted(best.values(), key=lambda item: item.confidence, reverse=True)

    def extract(self, text: str, blocks: Iterable[dict[str, Any]] = ()) -> list[NerEntity]:
        rss_before = self._rss_mb()
        self._ensure_loaded()
        annotated = LayoutAnalyzer.annotate_blocks(blocks)
        if not annotated:
            annotated = [
                {"text": line, "parser_zone": PageZone.UNKNOWN.value, "block_index": index}
                for index, line in enumerate(str(text or "").splitlines())
                if line.strip()
            ]

        entities: list[NerEntity] = []
        total_tokens = 0
        inference_ms = 0.0
        with self._run_lock:
            for block in annotated:
                line = str(block.get("text", "") or "").strip()
                if not line:
                    continue
                zone = str(block.get("parser_zone", PageZone.UNKNOWN.value) or PageZone.UNKNOWN.value)
                found, token_count, elapsed = self._run_line(line, block, zone)
                entities.extend(found)
                total_tokens += token_count
                inference_ms += elapsed

        output = self._dedupe(entities)
        rss_after = self._rss_mb()
        self.last_metrics = {
            "engine": self.name,
            "active_engine": self.name,
            "loaded": True,
            "model": self._model_path.name if self._model_path else "",
            "load_ms": self._load_ms,
            "inference_ms": round(inference_ms, 1),
            "blocks": len(annotated),
            "tokens": total_tokens,
            "entities": len(output),
            "min_confidence": self.min_confidence,
            "rss_before_mb": rss_before,
            "rss_after_mb": rss_after,
            "rss_delta_mb": (
                round(rss_after - rss_before, 1)
                if rss_before is not None and rss_after is not None else None
            ),
        }
        return output

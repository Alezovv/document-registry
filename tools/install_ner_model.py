from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request

MODEL_REPO = "onnx-community/ner-rubert-tiny-news-ONNX"
MODEL_DIR_NAME = "rubert_tiny_news"
FILES = {
    "model_int8.onnx": "onnx/model_int8.onnx",
    "config.json": "config.json",
    "vocab.txt": "vocab.txt",
    "tokenizer_config.json": "tokenizer_config.json",
}


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "DocumentRegistry-NER-Installer/1.20.1"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as out:
        total = int(response.headers.get("Content-Length", 0) or 0)
        received = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            received += len(chunk)
            if total:
                percent = min(100, round(received * 100 / total))
                print(f"\r  {destination.name}: {percent:3d}%", end="", flush=True)
        print()


def install(target: Path) -> None:
    target = target.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="documentregistry_ner_"))
    try:
        staged = temp_root / MODEL_DIR_NAME
        staged.mkdir(parents=True, exist_ok=True)
        print(f"Downloading compact Russian NER model: {MODEL_REPO}")
        for local_name, remote_name in FILES.items():
            url = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{remote_name}?download=true"
            _download(url, staged / local_name)

        config = json.loads((staged / "config.json").read_text(encoding="utf-8-sig"))
        labels = set(str(value) for value in (config.get("id2label") or {}).values())
        required_labels = {"B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC"}
        if not required_labels.issubset(labels):
            raise RuntimeError("Downloaded model has an unexpected NER label map.")
        model = staged / "model_int8.onnx"
        if model.stat().st_size < 20 * 1024 * 1024:
            raise RuntimeError("Downloaded ONNX model is unexpectedly small/incomplete.")
        if (staged / "vocab.txt").stat().st_size < 500_000:
            raise RuntimeError("Downloaded NER vocabulary is unexpectedly small/incomplete.")

        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staged), str(target))
        print(f"NER model installed: {target}")
        print("Restart DocumentRegistry. Parser cache will be invalidated automatically.")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install compact offline NER model for DocumentRegistry.")
    parser.add_argument(
        "--target",
        type=Path,
        default=_root() / "models" / "ner" / MODEL_DIR_NAME,
        help="Target model directory (default: models/ner/rubert_tiny_news)",
    )
    args = parser.parse_args()
    try:
        install(args.target)
    except Exception as error:
        print(f"NER model installation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

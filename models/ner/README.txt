DocumentRegistry NER models

The v1.20.1 ML backend looks for:
  models/ner/rubert_tiny_news/model_int8.onnx
  models/ner/rubert_tiny_news/config.json
  models/ner/rubert_tiny_news/vocab.txt
  models/ner/rubert_tiny_news/tokenizer_config.json

Install the model on the development PC with:
  python tools/install_ner_model.py

The model stays outside DocumentRegistry.exe. When present, build.ps1 copies it
into the portable models folder. If it is absent or fails at runtime, the parser
automatically falls back to the dependency-free heuristic NER.

Model: onnx-community/ner-rubert-tiny-news-ONNX
Source: https://huggingface.co/onnx-community/ner-rubert-tiny-news-ONNX
License: MIT (see the upstream model repository).

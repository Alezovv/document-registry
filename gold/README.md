# Gold Dataset

`gold_dataset.json` — локальный набор вручную проверенных документов для защиты parser от регрессий.
Сами PDF и OCR-текст сюда не копируются: запись содержит только SHA-256, имя файла и эталонные поля.

1. Создайте/обновите черновик: `python tools/create_gold_dataset.py --out gold/gold_dataset.json`.
2. Проверьте значения вручную и только после проверки поставьте для документа `"reviewed": true`.
3. Создайте baseline текущего стабильного parser:
   `python tools/gold_regression_gate.py --gold gold/gold_dataset.json --write-baseline gold/baseline.json`.
4. После этого `packaging/build.ps1` автоматически запускает gate, если существуют оба файла.

Gate не запускает OCR заново: он использует локальный `data/parser_cache/<sha256>.json`. Отсутствующий OCR-cache у reviewed-документа считается ошибкой проверки, чтобы релиз не проходил неполный набор.

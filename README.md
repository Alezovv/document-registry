# DocumentRegistry

DocumentRegistry — локальная Windows-программа для регистрации, оцифровки, поиска и обработки входящих документов и запросов.

Проект создаётся для рабочего процесса, где информация исторически хранится в бумажном архиве: поступает запрос по человеку, событию или документу, сотрудник ищет нужные сведения, готовит ответ и фиксирует результат. DocumentRegistry переносит этот процесс в локальный цифровой реестр и сокращает время поиска.

## Основная идея

Программа объединяет в одном месте:

- регистрацию входящих запросов;
- прикрепление PDF и сканов;
- OCR-распознавание документов;
- автоматическое извлечение реквизитов;
- поиск по реестру и распознанному содержимому;
- хранение истории изменений;
- работу с PDF-источником и подсветкой найденных фрагментов;
- резервное копирование;
- диагностику;
- безопасное обновление программы на удалённом рабочем ПК.

Документы и база остаются локальными. Для основного OCR не требуется отправлять содержимое документов во внешние облачные сервисы.

## Типичный рабочий процесс

```text
Пришёл запрос
    ↓
Запрос зарегистрирован в DocumentRegistry
    ↓
Прикреплён PDF / скан
    ↓
OCR распознал текст и координаты блоков
    ↓
Parser извлёк реквизиты и предложил значения полей
    ↓
Сотрудник проверил / исправил данные
    ↓
Запрос найден и обработан через цифровой реестр
    ↓
Результат и история работы остаются в базе
```

Главная цель проекта — не просто хранить PDF, а постепенно превратить бумажный архив в удобную поисковую систему, в которой можно быстро найти сведения по человеку, организации, дате, событию или содержанию документа.

## Текущие возможности

### Реестр

- создание, редактирование и удаление записей;
- статусы запросов;
- даты получения, исполнения и завершения;
- отправитель, должность, ФИО, адресат, суть и примечания;
- закрепление записей;
- фильтрация и поиск;
- история изменений.

### Документы и PDF

- прикрепление PDF к записи;
- portable-пути вида `@runtime/...`, поэтому папку программы можно переносить на другой компьютер или диск;
- просмотр PDF внутри программы;
- подсветка OCR-фрагмента, из которого было получено поле;
- исходный PDF при подсветке не изменяется.

### OCR

Основной OCR-движок — PaddleOCR.

Программа хранит:

- распознанный текст;
- координаты текстовых блоков;
- confidence OCR;
- кэш результатов для повторной обработки без нового OCR.

OCR выполняется последовательно в фоновой очереди, чтобы не запускать несколько тяжёлых моделей одновременно и не создавать лишнюю нагрузку на оперативную память.

### Parser

Текущий parser использует:

- текст OCR;
- координаты блоков;
- логические строки;
- правила и якорные слова;
- scoring кандидатов;
- локальную память пользовательских исправлений.

Пользовательские исправления могут сохраняться в `data/parser_corrections.json` и использоваться для более точного разбора похожих документов.

### Данные

Основная база данных — SQLite.

Рабочие данные находятся отдельно от файлов приложения:

```text
DocumentRegistry\
├─ DocumentRegistry.exe
├─ _internal\
├─ models\
├─ data\
│  ├─ requests.db
│  ├─ documents\
│  ├─ parser_cache\
│  └─ parser_corrections.json
├─ backups\
├─ logs\
└─ updater\
```

`data\` и `backups\` являются пользовательскими данными и не должны удаляться при обычном обновлении программы.

## Безопасные обновления

Начиная с ветки v1.18 программа получает механизм удалённого обновления. Для текущего сценария с двумя компьютерами источником обновлений является отдельная публичная папка Google Drive. Ссылка на неё встроена в приложение; Google Drive for Desktop на рабочем ПК не требуется.

Схема:

```text
ПК разработчика
    ↓
build.ps1
    ↓
проверенная portable-сборка
    ↓
manifest.json + update ZIP публикуются в Google Drive
    ↓
DocumentRegistry на удалённом ПК сам скачивает manifest.json по постоянному Google Drive file ID
    ↓
пользователь получает уведомление о новой версии
    ↓
скачивание идёт в фоне и не блокирует работу
    ↓
после полной загрузки и проверки пользователь запускает установку
```


### Проверка и скачивание обновлений

Программа проверяет наличие новой версии автоматически при запуске и затем один раз в час, пока приложение открыто. Автоматическая проверка работает в фоне и имеет короткий сетевой timeout: отсутствие или плохое качество интернета не блокирует запуск и обычную работу DocumentRegistry.

Большой ZIP не скачивается во время каждой проверки. Сначала загружается только маленький `manifest.json`. Пакет обновления начинает скачиваться только после нажатия пользователем кнопки «Скачать обновление».

Во время скачивания реестр, поиск, PDF и OCR остаются доступны. Если интернет пропал или пользователь нажал «Приостановить», частично загруженный `.part` сохраняется. При следующей попытке приложение продолжает загрузку с сохранённого места, если Google Drive поддерживает byte-range для этого файла.

Только после 100% скачивания, проверки размера и SHA-256 появляется предложение «Установить сейчас». До этого момента рабочая программа и БД вообще не изменяются. Пользователь может выбрать «Установить позже» и продолжить работу.

Папка обновлений: `https://drive.google.com/drive/folders/1azf1k4aCEtzg6d6-NTffq52guIdARF7q?usp=sharing`. Постоянный файл `manifest.json`: `https://drive.google.com/file/d/1DAYJCCB1eO0zSvqNeWsWJREXbyMoSse8/view?usp=sharing`. Оба ресурса должны быть доступны в режиме «Все, у кого есть ссылка». Программа не просматривает HTML папки: она скачивает manifest напрямую по его file ID. При публикации новых версий нужно обновлять содержимое существующего manifest-файла, а не удалять его и создавать новый, иначе изменится file ID.

### Принцип атомарности

Обновление должно завершиться одним из двух результатов:

1. Новая версия полностью установлена, старая БД успешно преобразована в новую схему, все прежние данные сохранены, а резервная копия старых данных оставлена в старом формате.
2. Если любой критический этап завершился ошибкой, рабочее состояние остаётся таким, каким было до обновления.

Перед миграцией создаётся backup старой БД и, когда требуется, связанных PDF. Миграция выполняется над отдельной копией БД. Рабочая база переключается на новую только после проверки целостности и успешного self-check новой версии.

Старая версия приложения после успешного обновления постоянно не хранится. Временная копия файлов приложения существует только на время транзакции обновления и удаляется после успешного завершения.

Версия схемы SQLite хранится через `PRAGMA user_version`. Миграции выполняются последовательно, поэтому будущая версия программы сможет преобразовать старую БД через несколько промежуточных схем.

## Резервное копирование

Программа поддерживает резервные копии рабочих данных. Отдельно создаются pre-update backup перед изменениями структуры БД.

Критический принцип проекта: пользовательские данные важнее версии программы. Обновление не должно необратимо менять единственную копию рабочей базы.

## Portable-режим

DocumentRegistry проектируется как portable Windows-приложение.

Это означает, что рабочую папку можно перенести целиком на другой Windows-ПК без установки серверной БД. Управляемые пути к документам сохраняются относительно runtime-папки, а не как абсолютные пути конкретного пользователя Windows.

Переносить следует всю папку `DocumentRegistry`, а не один `DocumentRegistry.exe`.

## Сборка

Основные файлы сборки находятся только в `packaging\`:

```text
packaging\
├─ DocumentRegistry.spec
└─ build.ps1
```

Сборка:

```powershell
packaging\build.ps1
```

Перед PyInstaller выполняется полная проверка проекта. Затем собираются основное приложение и updater, выполняются frozen self-check, формируется один итоговый portable ZIP. После успешной сборки временные `build\` и `dist\` удаляются.

Итоговый файл создаётся в `release\`.

## Публикация обновления

После ручной проверки уже собранной версии из неё формируются `manifest.json` и `DocumentRegistry_update.zip`. Для интернет-канала в manifest также должен быть `package_file_id` — Google Drive file ID опубликованного ZIP. Эти файлы публикуются в указанную папку Google Drive; `manifest.json` должен становиться доступным только после полной загрузки ZIP. Текущий `build.ps1 -PublishExisting` умеет подготовить эти файлы в локальную папку, откуда их можно загрузить на Google Drive.

Важно: публиковаться должна именно та сборка, которая уже была проверена, а не заново собранный непроверенный вариант.

## Диагностика и тесты

Перед релизом используется:

```powershell
python verify_project.py --full
```

Проверяются регрессии интерфейса, parser, portable-пути, резервное копирование, миграции БД, updater и конфигурация Windows/PyInstaller.

Windows-специфичные сценарии обязательно проверяются на реальной Windows-среде, потому что блокировки файлов и поведение SQLite отличаются от Linux.

## План развития

Ближайшее направление развития — улучшение качества получения полей из OCR без отказа от уже работающих правил.

Планируемая гибридная схема:

```text
PDF
 ↓
PaddleOCR
 ↓
OCR blocks + bbox + confidence
 ↓
нормализация
 ↓
layout-анализ + тип документа
 ↓
┌─────────┬─────────┬─────────┬────────────┐
│ Regex   │ Layout  │ NER     │ Dictionary │
│         │         │         │ + Fuzzy    │
└─────────┴─────────┴─────────┴────────────┘
                 ↓
          FieldCandidate[]
                 ↓
              scoring
                 ↓
            RoleResolver
                 ↓
 FieldResult + confidence + source
```

NER будет дополнительным источником кандидатов для ФИО, организаций и других сущностей, но не заменит regex, layout и существующий parser. Роль найденной сущности — отправитель, адресат, подписант, лицо запроса и т. п. — будет определяться отдельным resolver-ом с учётом положения на странице и контекста.

Также запланированы:

- benchmark нескольких NER-моделей;
- ONNX/CPU-friendly NER для обычного рабочего ПК;
- сравнение PaddleOCR и RapidOCR;
- Surya OCR 2 как возможный fallback для сложных страниц;
- отдельное распознавание русских рукописных областей;
- confidence и альтернативы для каждого извлечённого поля;
- повторный parser/NER-анализ из OCR-cache без повторного OCR;
- расширенное обучение на пользовательских исправлениях.

## Принципы проекта

1. Не ломать рабочую portable-версию ради новой функции.
2. Все существенные изменения покрывать регрессионными тестами.
3. Не заменять стабильный OCR/Parser без измеримого выигрыша на реальных документах.
4. Сохранять исходный OCR и исходный PDF.
5. Не перезаписывать пользовательские исправления без явной причины.
6. Сомнительное поле лучше показать для проверки, чем уверенно подставить неправильное значение.
7. Обновления БД должны быть атомарными и иметь проверяемый backup.
8. Пользовательские данные не должны зависеть от конкретного пути Windows или имени пользователя.

## Статус

Текущая ветка развития: **v1.21.x — production hybrid parser поверх стабильного PaddleOCR, Parser Core и confirmation-only NER**.


## Проверка OCR после обновления

Начиная с v1.18.7 в разделе «Настройки → Обновления» есть кнопка «Проверить OCR». Проверка запускается в фоне и реально инициализирует PaddleOCR с внешними моделями. Это позволяет быстро убедиться, что frozen-сборка содержит необходимые PaddleOCR/PaddleX runtime-ресурсы и что OCR готов к работе, не импортируя тестовый документ.


## Диагностика frozen OCR

Windows-сборка выполняет отдельный реальный self-check OCR до создания релизного ZIP. Для PaddleOCR/PaddleX в PyInstaller явно включаются данные PaddleX, нативные библиотеки Paddle и metadata установленных OCR-зависимостей. Если frozen OCR не инициализируется, `build.ps1` сохраняет JSON-отчёт в `build\frozen_ocr_self_check.json` и печатает причину ошибки в консоль.


## v1.19.0 — Parser Core

В v1.19.0 добавлен отдельный `app/parser_core.py` — нейтральный слой между OCR и заполнением формы. Он вводит `FieldCandidate`/`FieldResult`, полевую нормализацию, геометрические зоны страницы, лёгкую классификацию типа документа и единый `CandidateResolver`. Текущий детерминированный parser сохраняет прежнее поведение, но теперь одновременно формирует структурированные кандидаты, альтернативы и provenance в diagnostics/cache. Это позволяет на следующем этапе подключать NER как ещё один источник кандидатов, не давая модели напрямую перезаписывать поля формы.

Окно фонового скачивания обновлений также получило системную кнопку сворачивания: сворачивание не приостанавливает загрузку.

### Упрощённая публикация Google Drive с v1.19.0

`build.ps1` теперь сразу создаёт `manifest.json` с явным полем `package_file_id` (до загрузки ZIP оно пустое) и пишет JSON в UTF-8 без BOM. После загрузки `DocumentRegistry_update.zip` на Google Drive и получения его file ID больше не нужно редактировать JSON вручную. Достаточно выполнить:

```powershell
.\packaging\build.ps1 `
    -Version 1.19.0 `
    -ManifestOnly `
    -PublishTo "C:\DocumentRegistry_Publish" `
    -PackageFileId "GOOGLE_DRIVE_FILE_ID" `
    -ReleaseNotes "Описание изменений"
```

Команда не пересобирает и не меняет `DocumentRegistry_update.zip`: она пересчитывает его `sha256`/`size` и формирует окончательный `manifest.json`. Затем этот manifest загружается в Google Drive как новая версия существующего стабильного файла.


## v1.19.1 — Role Resolver и контекстная память исправлений

В v1.19.1 Parser Core получил отдельный `RoleResolver`. Он не придумывает новые сущности и пока не использует NER: вместо этого он связывает уже выбранные parser-ом значения с семантическими ролями `sender`, `recipient`, `signer` и `executor`, учитывая геометрическую зону страницы и тип документа. Diagnostics/cache теперь также содержат `role_assignments` и `entity_groups`, что даст NER единый контракт на следующем этапе.

`parser_corrections.json` обновлён до формата v2. Старые правила v1 загружаются совместимо и не теряются. Для новых пользовательских исправлений вместе с парой `source -> target` сохраняется только безопасный контекст: тип документа, зона страницы и источник кандидата. OCR-текст документа в память исправлений не записывается. Точные исправления остаются применимыми после одного подтверждения; fuzzy-повторное использование по-прежнему требует минимум двух подтверждений, а контекст служит только дополнительным сигналом при выборе между равноценными вариантами.


## v1.20.0 — NER pipeline

В v1.20.0 появился стабильный контракт `NerEngine` и первый полностью локальный baseline `HeuristicRussianNerEngine`. Он извлекает сущности `PERSON`, `ORGANIZATION` и `LOCATION` из OCR-блоков с учётом геометрической зоны страницы и confidence OCR. NER не пишет напрямую в форму: `NerCandidateAdapter` превращает сущности в обычные `FieldCandidate`, а `NerRoleAdapter` — в семантические роли `recipient`, `signer`, `executor` и `mentioned_person`.

Для совместимости существующие label/layout/filename-правила остаются приоритетными. NER может заполнить только пустые `from_department`, `from_person` или `to_person` и только при высоком layout-aware confidence. Все найденные сущности, используемый NER-engine и счётчики по типам сохраняются в parser diagnostics/cache, поэтому качество можно измерять без повторного OCR.

Текущий baseline специально не добавляет тяжёлую ML-зависимость в portable-сборку. Следующий шаг — benchmark нескольких русскоязычных моделей на реальных документах и подключение лучшей через тот же `NerEngine` (предпочтительно ONNX/CPU), не меняя остальную архитектуру parser-а.

## v1.20.1 — optional ONNX NER backend

v1.20.1 adds an optional CPU-only ML backend for Russian NER without making it a hard dependency. The selected model is `onnx-community/ner-rubert-tiny-news-ONNX` (RuBERT-tiny2, Collection3, MIT license) and the application uses the compact INT8 ONNX variant. The model remains outside `DocumentRegistry.exe` under `models/ner/rubert_tiny_news`, just like OCR models.

Install the model on the development machine once:

```powershell
python tools\install_ner_model.py
```

After installation the folder contains `model_int8.onnx`, `config.json`, `vocab.txt` and `tokenizer_config.json`. `build.ps1` already copies the whole external `models` directory into a portable build. The updater still treats `models` as protected user/runtime content, so model distribution remains a separate concern from application-code updates.

The runtime mode is conservative:

- if the model is present, one process-wide `OnnxRussianNerEngine` is created and the ONNX session is loaded lazily on the first parsed document;
- the same session is reused for later documents, so model RAM is not multiplied by the queue length;
- ONNX inference is limited to CPU and at most two intra-op threads to keep the UI responsive;
- if model files are missing, `onnxruntime` is unavailable, or inference fails, the parser falls back to `HeuristicRussianNerEngine` instead of failing document import;
- parser cache compatibility includes the active NER backend, so installing/removing the ML model automatically re-parses cached OCR without rerunning OCR;
- diagnostics now include NER load time, inference time, entity count and process RSS deltas when available.

For a performance/coverage comparison on already cached OCR (no repeated OCR), run:

```powershell
python tools\benchmark_ner.py --limit 30
```

This benchmark compares execution time and entity overlap between heuristic and ONNX NER. It is intentionally not presented as an accuracy/F1 score; a separate gold-labelled document set is required for quality measurement.

## v1.20.2 — калибровка confidence и hybrid resolver

v1.20.2 не добавляет ещё одну тяжёлую модель. Вместо этого она калибрует уже подключённый ML NER относительно дешёвого deterministic baseline и делает решение parser-а объяснимым.

Когда активен ONNX NER, `HeuristicRussianNerEngine` параллельно запускается как очень лёгкий shadow-baseline на том же уже готовом OCR-тексте. Он не выполняет OCR повторно. Diagnostics сохраняют `ner_comparison`: количество совпавших/разошедшихся сущностей, agreement ratio и разбивку по `PERSON`/`ORGANIZATION`/`LOCATION`. Это не считается accuracy: обе системы могут ошибаться одновременно, поэтому для реальной оценки всё равно нужен gold-набор.

`CandidateResolver` теперь учитывает небольшие полевые priors по геометрической зоне страницы. Например, ФИО в `signature` получает небольшой плюс как кандидат `from_person`, а ФИО из `addressee` получает штраф для этого поля; для `to_person` действует обратная логика. Совпадение независимых источников/NER-engine даёт небольшой bonus, но не позволяет одной геометрии перебить явный label или filename.

Каждый `FieldResult` теперь содержит дополнительные диагностические поля:

- `decision`: `autofill`, `review`, `suggestion`, `low_confidence` или `empty`;
- `margin`: разница между выбранным значением и ближайшей альтернативой;
- `evidence`: краткие причины выбора (`source`, `zone`, agreement);
- `warnings`: например `close_alternative` или несовпадение ожидаемой зоны.

Текущая форма остаётся совместимой: эти классы решения пока используются для diagnostics/review и не заставляют ML перезаписывать уже уверенно выбранные label/layout/filename значения. NER-only автозаполнение пустых полей остаётся с отдельными строгими порогами.

Для проверки поведения на реальном OCR-cache без повторного OCR доступны два инструмента:

```powershell
python tools\benchmark_ner.py --limit 30 --json-out data\ner_benchmark.json
python tools\benchmark_parser.py --limit 50 --json-out data\parser_calibration.json
```

`benchmark_parser.py` намеренно не пишет OCR-текст и значения извлечённых полей в JSON-отчёт: только тип документа, confidence, decision, список полей для проверки, NER agreement и время parser-а. Это позволяет собирать статистику на реальном архиве без создания лишней копии содержимого документов.

## v1.20.3 — NER guardrails for archive documents

Real OCR-cache calibration showed that the news-domain ONNX model can produce far more entities than the deterministic parser on administrative documents. v1.20.3 therefore separates **raw ML diagnostics** from **trusted ML evidence**:

- raw ONNX entities are still kept in diagnostics and benchmark reports;
- only baseline-supported entities or very high-confidence entities in role-bearing page zones may influence sender/addressee/organization fields;
- exact entity agreement is reported separately from tolerant compatible agreement (for example, `Иванов Иван Иванович` vs `Иванов И.И.`);
- the ONNX decoder now ignores token labels below a 0.70 confidence floor;
- `benchmark_ner.py` reports raw ML count, trusted ML count, exact overlap and compatible overlap;
- `benchmark_parser.py` reports the compatible calibration signal and trusted ML count per document.

This is intentionally conservative: until a gold-labelled archive set exists, ML NER must improve evidence without turning every body mention into a competing registry-field value.


## v1.20.4 — confirmation-only ML NER

Calibration on 31 real OCR-cache documents showed a strong domain shift: the news-domain ONNX model produced many more raw entities than the archive-oriented deterministic baseline. v1.20.4 therefore makes production ML NER confirmation-only. Raw ONNX entities remain in diagnostics and benchmarks, but a PERSON/ORG span may influence registry fields only when the deterministic baseline independently supports a compatible entity. The earlier standalone high-confidence policy remains available only as an explicit experimental code path for benchmarks/tests.

The test suite is also environment-independent now: installing the external ONNX model no longer makes heuristic-specific unit tests fail, and parser-cache tests validate whichever backend is actually active.

## v1.20.5 — role-aware NER confirmation and sender calibration

Calibration on 31 cached archive documents showed that confirmation-only NER was a clear improvement, but sender organisations were still almost always marked for review and LOCATION/ORG diagnostics remained noisy. v1.20.5 tightens production ML evidence further: PERSON confirmation must agree in the same role-bearing page zone, ORGANIZATION confirmation must be supported in sender/header zones, and LOCATION stays diagnostic-only until a registry field actually consumes it. Nested organisation names (for example a short NER span inside a longer formal department name) are treated as supporting evidence rather than competing alternatives. The deterministic sender-department confidence receives a small semantic calibration boost when a stable archive organisation marker is present; unknown/non-dictionary department strings remain conservative.

## v1.20.6 — advisory-NER resolver policy

Calibration on 36 cached archive documents showed that role-aware confirmation reduced ML noise, but `from_department` was still marked for review on almost every document. The remaining reviews were largely a resolver-policy issue: confirmation-only NER alternatives were still allowed to shrink the decision margin of an already selected deterministic label/layout/filename value. v1.20.6 keeps those NER alternatives visible in diagnostics, but they no longer veto a deterministic sender/person/addressee value by themselves. Genuine non-NER alternatives still force review. Compatible nested sender-organisation candidates can also reinforce the selected formal department name.


## v1.21.0 — production hybrid parser

v1.21.0 moves the calibrated candidate system into the actual field-selection path. Regex/layout, filename metadata, confirmation-only NER and user correction history are now represented as provenance-rich `FieldCandidate` values and resolved through one conservative hybrid policy. Explicit labels remain protected, NER stays advisory for populated deterministic fields, and an exact user-confirmed correction can override a recurring OCR/parser mistake. Parser cache compatibility now includes a fingerprint of `parser_corrections.json`, so learning a new correction automatically forces a cheap re-parse from existing OCR cache instead of rerunning OCR. Diagnostics expose `hybrid_changed_fields`, per-field actions and candidate counts; `tools/benchmark_parser.py` reports how often hybrid fusion actually changes a field.

## v1.21.1 — gold/proxy A/B benchmark

v1.21.1 keeps the production parser policy from v1.21.0 unchanged and adds a calibration step that can finally measure whether hybrid changes help or hurt on the local archive. `tools/benchmark_parser_gold.py` matches managed PDFs to existing OCR cache by SHA-256, parses every matched document twice (production hybrid and deterministic baseline), and compares both outputs with values already saved in `requests.db`. Blank saved fields are ignored because they are not reliable negative labels. The report never writes OCR text or field values; it stores only match flags, field names, request IDs and aggregate rates. Saved database values should be treated as a gold *proxy* only for records an operator has actually reviewed.

Run with DocumentRegistry closed:

```powershell
python tools\benchmark_parser_gold.py --limit 50 --json-out data\parser_gold.json
```

The key result is the per-field `changed=... +improved/-harmed` column. It tells us whether v1.21 hybrid changes (especially `to_person`) actually improve agreement with reviewed registry values instead of merely reducing or increasing review counters. The parser also exposes an internal `hybrid_enabled=False` mode for this A/B benchmark; normal application behaviour remains hybrid-enabled. Because the production parsing algorithm did not change, `PARSER_VERSION` intentionally remains `1.21.0` and existing OCR/parser cache is not invalidated.

## v1.22.0 — hybrid stabilization, date ranking, gold-cache coverage and visible updater

v1.22.0 bundles the first larger post-calibration cleanup based on the local proxy-gold A/B results. On the reviewed sample, hybrid changes improved `to_person` but degraded `request_subject`, so production policy is now field-specific instead of uniformly permissive.

- `request_subject` is locked to the deterministic subject extractor once a baseline value exists. Hybrid candidates remain visible in diagnostics but cannot rewrite the reviewed baseline; a rejected/empty subject is not resurrected by a strong filename/NER alternative.
- `to_person` keeps the conservative hybrid path that already produced improvements without observed regressions in the proxy-gold sample.
- `received_date` now ranks all plausible header dates by incoming/registration context instead of taking the first date. It also repairs date-shaped OCR confusions such as `О -> 0`, `I/l/| -> 1`, `З -> 3`, `б -> 6`, accepts spaces around separators, and validates every result as a real calendar date.
- `tools/prepare_gold_cache.py` can prepare missing OCR cache for existing managed PDFs without changing the database or source documents. This lets the proxy-gold benchmark expand from a few dozen cached documents toward 50–100 reviewed records without manually opening every PDF.
- `benchmark_parser_gold.py` now prints a cache-coverage hint when reviewed PDFs are skipped because OCR cache is missing.
- The standalone updater now writes a durable `data/update_status.json`, shows a small native Windows progress window while the main application is closed, reports backup/migration/activation/final-check stages, and the relaunched app shows a one-time success/failure toast. The download window remains non-modal and minimizable.

To expand benchmark coverage, close DocumentRegistry and run:

```powershell
python tools\prepare_gold_cache.py --limit 100
python tools\benchmark_parser_gold.py --limit 100 --json-out data\parser_gold.json
```

`prepare_gold_cache.py` performs OCR only for missing entries. Existing OCR cache is reused; parser results may be refreshed from cached OCR so no PDF rendering is repeated unnecessarily.



## v1.23.0 — diagnostics, update history, full-text search and import queue

- Добавлен отдельный центр диагностики с версиями приложения/parser, состоянием SQLite, OCR-моделей, путей, backup и поискового индекса.
- Standalone updater ведёт долговечную историю реальных этапов установки; итог старого updater автоматически переносится из `update_status.json` при первом запуске.
- Схема БД поднята до 6: SQLite FTS5 индексирует реквизиты запроса и сохранённый OCR-текст PDF. Поиск «Все поля» теперь использует полнотекстовый индекс.
- Новые импорты сохраняют OCR-текст в локальный поисковый индекс; существующий parser-cache тихо переиндексируется в фоне без повторного OCR.
- Массовый импорт показывает очередь файлов, состояние каждого документа, общий прогресс и безопасную паузу между документами.
- `PARSER_VERSION` остаётся 1.22.1: алгоритм извлечения полей не менялся и parser-cache не инвалидируется.

## v1.22.1 — conservative received-date regression fix

Proxy-gold validation of v1.22.0 showed a regression in `received_date`: cross-line date ranking could borrow an incoming marker from a neighbouring OCR line and promote the wrong date. v1.22.1 restores the proven v1.21 deterministic extraction order while retaining v1.22 OCR repairs for spaced separators and common digit confusions. Only same-line incoming/registration context is allowed to outrank the normal header fallback.

## v1.23.1 — guaranteed visible updater hand-off

- Main DocumentRegistry no longer quits immediately after starting the standalone updater. For future updates it waits for a visibility handshake from `data/update_status.json` and only exits after the updater confirms that an installation-status display is actually visible.
- The native Win32 installer window now reports success only after `IsWindowVisible()` succeeds, is centred on the current screen, and is brought to the front on a best-effort basis.
- If the native Win32 window cannot be created, the updater opens a visible console fallback and prints every real installation stage and percentage there.
- If neither display can be shown, the hand-off is aborted before the main application exits; installation does not reach file replacement.
- `update_status.json` and diagnostics now record `window_mode` (`native`, `console`, or `none`) together with `window_started` and any window error.
- The Diagnostics Center has a safe **Проверить окно установки** action. It starts `DocumentRegistryUpdater.exe --ui-test`, animates the real updater display, and does not install or modify application files.
- Database schema stays at 6 and `PARSER_VERSION` stays at 1.22.1; this hotfix does not invalidate OCR/parser cache.


## v1.23.3 — updater TEMP bootstrap + Win32 compatibility

- installer UI no longer relies on `ctypes.wintypes.HCURSOR` or other optional handle aliases;
- every update launches the `DocumentRegistryUpdater.exe` embedded in the verified update ZIP from `%TEMP%`;
- stale installed updater binaries can no longer control the next update;
- updater itself is replaced as part of the same atomic program transaction instead of using `.next.exe`;
- `update_status.json` records updater version and executable path for diagnostics;
- legacy `.next.exe` promotion remains only as a one-time compatibility cleanup for older installs.

## v1.23.2 — diagnostics hotfix

- Исправлен запуск Центра диагностики: `DiagnosticsService` теперь корректно читает `data/update_status.json`; ошибка `name 'json' is not defined` устранена.
- Добавлен регрессионный тест с реальным `update_status.json`, чтобы путь чтения статуса обновления выполнялся в тестах, а не проверялся только по структуре исходников.
- Нижняя панель действий Центра диагностики перестроена в сетку 3×2, поэтому длинные подписи кнопок больше не обрезаются на стандартной ширине окна.
- Схема БД остаётся 6, `PARSER_VERSION` остаётся 1.22.1; OCR/parser cache не инвалидируется.

## v1.27.0 — reliability, review and low-memory workflow

- SHA-256 fingerprinting detects duplicate PDFs before OCR and supports intentional duplicate import.
- Full-text search uses SQLite FTS5 with OCR snippets plus sender/recipient/date/status filters and direct PDF opening.
- Compact parser metadata persists confidence, alternatives and provenance without duplicating the full OCR payload in RAM/DB.
- Review UI highlights uncertain fields, offers parser alternatives and jumps to the source region in the PDF; confirmed edits feed correction memory.
- Import queue supports background operation, pause/resume, cancellation of pending files, retry of failures and elapsed-time/status reporting.
- OCR engine is released after a completed batch; worker OCR references are dropped immediately and SQLite temp/cache memory is capped.
- Backups are free-space checked and deep-verified (ZIP + SQLite integrity); pre-update backups are also verified and diagnostics show the last verified backup.
- Database schema 7 adds indexed document fingerprints and compact parser-review metadata.
- Local Gold Dataset tooling can export a review draft, create a stable baseline and block a release when parser accuracy regresses on reviewed documents.


## v1.27.1 — Windows verification hotfix

- SQLite test connections are explicitly closed before temporary directories are removed, preventing WinError 32 on Windows.
- Import UI no longer imports `app.ocr` directly; shared OCR engine release is delegated to `app.import_workers.release_shared_ocr_engine()`.
- Runtime behavior is unchanged: the OCR engine is still released after a completed batch to keep idle RAM usage low.

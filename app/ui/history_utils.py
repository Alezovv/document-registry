import json


HISTORY_ACTION_LABELS = {
    "create": "Создание",
    "update": "Изменение",
    "delete": "Удаление",
    "attach": "Добавление файла",
    "detach": "Удаление файла",
    "pin": "Закрепление",
    "unpin": "Открепление",
    "restore": "Восстановление",
    "merge_import": "Импорт при слиянии",
    "conflict_resolved": "Разрешение конфликта",
    "bulk_update": "Массовое изменение",
    "bulk_status": "Массовая смена статуса",
    "bulk_due": "Массовое изменение срока",
    "bulk_pin": "Массовое закрепление",
}


def format_history_action(action):
    return HISTORY_ACTION_LABELS.get(action, action)


def parse_history_diff(description):
    """Return [(field, old, new), ...] for a stored JSON diff description."""
    if not isinstance(description, str):
        return []

    json_text = description.strip()
    if json_text.startswith("__DIFF__"):
        json_text = json_text[len("__DIFF__"):].strip()

    if not json_text:
        return []

    try:
        parsed = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(parsed, list):
        return []

    changes = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", "")).strip()
        if not field:
            continue
        old_value = str(item.get("old", "") or "пусто")
        new_value = str(item.get("new", "") or "пусто")
        changes.append((field, old_value, new_value))
    return changes


def format_history_description(description):
    changes = parse_history_diff(description)
    if not changes:
        return description or ""
    return "; ".join(
        f"{field}: «{old_value}» → «{new_value}»"
        for field, old_value, new_value in changes
    )

import unittest

from app.ui.history_utils import (
    format_history_action,
    format_history_description,
    parse_history_diff,
)


class HistoryUtilsTests(unittest.TestCase):
    def test_parse_prefixed_diff(self):
        raw = '__DIFF__[{"field":"Статус","old":"Новый","new":"В работе"}]'
        self.assertEqual(
            parse_history_diff(raw),
            [("Статус", "Новый", "В работе")],
        )

    def test_parse_invalid_description_returns_empty_list(self):
        self.assertEqual(parse_history_diff("обычное описание"), [])
        self.assertEqual(parse_history_diff(None), [])

    def test_format_description_uses_diff(self):
        raw = '[{"field":"Отдел","old":"А","new":"Б"}]'
        self.assertEqual(
            format_history_description(raw),
            "Отдел: «А» → «Б»",
        )

    def test_format_description_keeps_plain_text(self):
        self.assertEqual(
            format_history_description("Документ откреплен"),
            "Документ откреплен",
        )

    def test_action_label(self):
        self.assertEqual(format_history_action("restore"), "Восстановление")
        self.assertEqual(format_history_action("bulk_update"), "Массовое изменение")
        self.assertEqual(format_history_action("bulk_status"), "Массовая смена статуса")
        self.assertEqual(format_history_action("bulk_due"), "Массовое изменение срока")
        self.assertEqual(format_history_action("bulk_pin"), "Массовое закрепление")
        self.assertEqual(format_history_action("custom"), "custom")


if __name__ == "__main__":
    unittest.main()

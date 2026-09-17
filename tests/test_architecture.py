import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def module_import_roots(self, relative_path):
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots


    def top_level_import_roots(self, relative_path):
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        roots = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_service_layer_has_no_pyside_dependency(self):
        for relative in (
            "app/document_service.py",
            "app/request_service.py",
            "app/settings_service.py",
            "app/backup_service.py",
            "app/export_service.py",
            "app/resource_profile.py",
            "app/performance_diagnostics.py",
            "app/performance_mode_service.py",
            "app/parser_postprocess.py",
            "app/legacy_import_service.py",
            "app/database.py",
        ):
            with self.subTest(relative=relative):
                self.assertNotIn("PySide6", self.module_import_roots(relative))

    def test_main_window_remains_a_thin_application_shell(self):
        path = ROOT / "app/window.py"
        source = path.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 400)
        for forbidden in (
            "QPdfView",
            "QEventLoop",
            "ImportDocumentDialog",
            "RequestDialog",
            "calculate_file_hash",
            "find_duplicate_document",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


    def test_ocr_heavy_dependencies_are_loaded_lazily(self):
        roots = self.top_level_import_roots("app/ocr.py")
        for dependency in ("numpy", "pymupdf", "paddleocr"):
            with self.subTest(dependency=dependency):
                self.assertNotIn(dependency, roots)

        worker_roots = self.top_level_import_roots("app/import_workers.py")
        self.assertNotIn("app", worker_roots)

        ocr_source = (ROOT / "app/ocr.py").read_text(encoding="utf-8")
        self.assertIn('"intra_op_num_threads": self.cpu_threads', ocr_source)
        self.assertIn('"inter_op_num_threads": 1', ocr_source)

    def test_import_dialog_uses_workers_instead_of_ocr_parser_directly(self):
        source = (ROOT / "app/import_dialog.py").read_text(encoding="utf-8")
        self.assertIn("from app.import_workers import OCRWorker, ParserWorker", source)
        self.assertNotIn("from app.ocr import", source)
        self.assertNotIn("from app.document_parser import", source)

    def test_request_form_uses_wheel_safe_date_and_combo_controls(self):
        source = (ROOT / "app/ui/dialogs/request_dialog.py").read_text(encoding="utf-8")
        self.assertIn("self.status = NoWheelComboBox()", source)
        self.assertIn("SmartDateEdit", source)
        controls = (ROOT / "app/ui/date_controls.py").read_text(encoding="utf-8")
        self.assertIn("class NoWheelComboBox", controls)
        self.assertIn("class NoWheelDateEdit", controls)
        self.assertGreaterEqual(controls.count("event.ignore()"), 2)

    def test_dashboard_feature_is_removed_from_active_ui(self):
        window = (ROOT / "app/window.py").read_text(encoding="utf-8")
        sidebar = (ROOT / "app/ui/navigation_sidebar.py").read_text(encoding="utf-8")
        registry = (ROOT / "app/ui/registry_page.py").read_text(encoding="utf-8")
        settings = (ROOT / "app/settings_service.py").read_text(encoding="utf-8")
        self.assertNotIn("DashboardPage", window)
        self.assertNotIn("DashboardService", window)
        self.assertNotIn('("dashboard",', sidebar)
        self.assertNotIn('"dashboard"', settings.split("VALID_PAGES", 1)[1].split("\n", 1)[0])
        self.assertNotIn('create_stat_card("Срок сегодня"', registry)
        self.assertIn('self.deadline_filter', registry)
        self.assertIn('"Сегодня"', registry)


    def test_saved_filter_feature_is_removed(self):
        # ZIP updates may leave obsolete files from older versions on disk.
        # What matters architecturally is that active application code no longer
        # imports or references the removed saved-filter feature.
        window = (ROOT / "app/window.py").read_text(encoding="utf-8")
        registry = (ROOT / "app/ui/registry_page.py").read_text(encoding="utf-8")
        backup = (ROOT / "app/backup_service.py").read_text(encoding="utf-8")
        for source in (window, registry, backup):
            self.assertNotIn("filter_preset", source)
            self.assertNotIn("saved_filters", source)


    def test_performance_diagnostics_is_read_only_and_lightweight(self):
        source = (ROOT / "app/performance_diagnostics.py").read_text(encoding="utf-8")
        self.assertNotIn("PySide6", source)
        self.assertNotIn("psutil", self.module_import_roots("app/performance_diagnostics.py"))
        settings = (ROOT / "app/ui/settings_page.py").read_text(encoding="utf-8")
        self.assertIn("Производительность", settings)
        self.assertIn("Режим нагрузки", settings)
        self.assertIn("OCR-модель", settings)
        self.assertIn("Память приложения", settings)

    def test_application_icon_assets_are_present(self):
        self.assertTrue((ROOT / "assets/app_icon.png").is_file())
        self.assertTrue((ROOT / "assets/app_icon.ico").is_file())
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("app.setWindowIcon", main)

    def test_bulk_toolbar_uses_explicit_vertical_alignment(self):
        registry = (ROOT / "app/ui/registry_page.py").read_text(encoding="utf-8")
        self.assertIn("row.setAlignment(Qt.AlignmentFlag.AlignVCenter)", registry)
        self.assertIn("self.bulk_count_label.setFixedHeight(34)", registry)
        self.assertIn("self.bulk_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)", registry)


if __name__ == "__main__":
    unittest.main()

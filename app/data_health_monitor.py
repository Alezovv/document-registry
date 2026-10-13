from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil

from PySide6.QtCore import QObject, QTimer, Signal

from app.background_task_coordinator import COORDINATOR
from app.resource_profile import resource_profile_from_environment


class DataHealthMonitor(QObject):
    """Very lightweight periodic storage/database health checks.

    The monitor never scans documents and never opens OCR models. It performs a
    SQLite quick_check and disk-space/backup-age metadata checks every six hours.
    """

    warning = Signal(str, str, str)

    def __init__(self, database, backup_service, parent=None, *, interval_hours: int = 6):
        super().__init__(parent)
        self.database = database
        self.backup_service = backup_service
        self._last_warning = ""
        self.timer = QTimer(self)
        self.timer.setInterval(max(1, int(interval_hours)) * 60 * 60 * 1000)
        self.timer.timeout.connect(self.check_now)
        self.timer.start()
        QTimer.singleShot(5000, self.check_now)

    def check_now(self) -> None:
        if resource_profile_from_environment().economy_memory_policy and COORDINATOR.is_busy():
            return
        warnings: list[str] = []
        try:
            result = str(self.database.quick_check() or "unknown")
            if result.lower() != "ok":
                warnings.append(f"SQLite quick_check: {result}")
        except Exception as error:
            warnings.append(f"Не удалось проверить БД: {error}")

        try:
            root = Path(self.database.db_path).parent
            free = int(shutil.disk_usage(root).free)
            if free < 512 * 1024 * 1024:
                warnings.append(f"Мало свободного места: {free / (1024 ** 2):.0f} МБ")
        except OSError:
            pass

        try:
            health = self.backup_service.verification_info()
            stamp = str(health.get("verified_at", "")) if health.get("ok") else ""
            if stamp:
                verified = datetime.fromisoformat(stamp)
                age_days = (datetime.now(verified.tzinfo) - verified).days if verified.tzinfo else (datetime.now() - verified).days
                if age_days > 8:
                    warnings.append(f"Последний проверенный backup старше {age_days} дн.")
        except (ValueError, TypeError):
            pass

        signature = " | ".join(warnings)
        if signature and signature != self._last_warning:
            self.warning.emit("Состояние данных", signature, "warning")
        self._last_warning = signature

from __future__ import annotations

import argparse
import compileall
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIRED = (
    "packaging/DocumentRegistry.spec",
    "packaging/build.ps1",
    "app/__init__.py",
    "app/main.py",
    "app/database.py",
    "app/db_migrations.py",
    "app/document_parser.py",
    "app/ner.py",
    "app/ocr.py",
    "app/diagnostics_service.py",
    "app/update_service.py",
    "app/updater_main.py",
    "app/ui/update_controller.py",
    "app/ui/diagnostics_controller.py",
    "app/ui/settings_page.py",
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    failures = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            failures.append(f"missing: {relative}")

    if not compileall.compile_dir(ROOT / "app", quiet=1, force=True):
        failures.append("compileall failed")

    if args.full:
        proc = subprocess.Popen(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines = []
        for line in proc.stdout:
            sys.stdout.write(line)
            lines.append(line)
        proc.wait()
        full_out = "".join(lines)
        is_ok = ("\nOK" in full_out or full_out.startswith("OK")) and "FAILED (" not in full_out
        if proc.returncode != 0 and not is_ok:
            failures.append("unit tests failed")

    if failures:
        print("VERIFY FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

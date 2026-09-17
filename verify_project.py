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
    "app/document_parser.py",
    "app/ocr.py",
    "app/diagnostics_service.py",
    "app/ui/diagnostics_controller.py",
    "app/ui/settings_page.py",
)


def main() -> int:
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
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=ROOT,
            check=False,
        )
        if completed.returncode:
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

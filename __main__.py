from app.app_paths import get_app_paths
from app.update_cleanup import cleanup_legacy_update_files

paths = get_app_paths()
cleanup_legacy_update_files(paths.runtime)
if paths.executable_dir != paths.runtime:
    cleanup_legacy_update_files(paths.executable_dir)

from app.main import main


if __name__ == "__main__":
    main()

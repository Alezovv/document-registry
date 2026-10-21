from __future__ import annotations

"""Static production update-channel configuration.

The user does not configure an update source.  DocumentRegistry checks one
public Google Drive manifest owned by the developer.  The folder URL is kept
only for diagnostics / the Settings link; automatic discovery uses the stable
file ID of ``manifest.json`` directly and never scrapes the folder HTML.
"""

UPDATE_DRIVE_FOLDER_URL = ""
UPDATE_DRIVE_FOLDER_ID = ""

# Permanent public Google Drive file ID of manifest.json. When publishing a
# release, update the contents of this same Drive file instead of deleting it
# and uploading a new manifest with a different ID.
UPDATE_MANIFEST_FILE_ID = ""
UPDATE_MANIFEST_FILE_URL = ""

# Checks are intentionally cheap: only manifest.json is downloaded.  The large
# update ZIP is downloaded only after the user presses the update button.
UPDATE_CHECK_INTERVAL_SECONDS = 60 * 60
UPDATE_STARTUP_DELAY_MS = 8_000
UPDATE_HTTP_TIMEOUT_SECONDS = 8
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 20

# Keep network operations bounded and memory usage low.
UPDATE_CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024

# Load local developer / private production settings if present.
try:
    from app.update_config_local import (  # type: ignore
        UPDATE_DRIVE_FOLDER_ID as _LOCAL_DRIVE_ID,
        UPDATE_DRIVE_FOLDER_URL as _LOCAL_DRIVE_URL,
        UPDATE_MANIFEST_FILE_ID as _LOCAL_MANIFEST_ID,
        UPDATE_MANIFEST_FILE_URL as _LOCAL_MANIFEST_URL,
    )

    if _LOCAL_DRIVE_ID:
        UPDATE_DRIVE_FOLDER_ID = _LOCAL_DRIVE_ID
    if _LOCAL_DRIVE_URL:
        UPDATE_DRIVE_FOLDER_URL = _LOCAL_DRIVE_URL
    if _LOCAL_MANIFEST_ID:
        UPDATE_MANIFEST_FILE_ID = _LOCAL_MANIFEST_ID
    if _LOCAL_MANIFEST_URL:
        UPDATE_MANIFEST_FILE_URL = _LOCAL_MANIFEST_URL
except ImportError:
    pass

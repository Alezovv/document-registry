from __future__ import annotations

"""Static production update-channel configuration.

The user does not configure an update source.  DocumentRegistry checks one
public Google Drive manifest owned by the developer.  The folder URL is kept
only for diagnostics / the Settings link; automatic discovery uses the stable
file ID of ``manifest.json`` directly and never scrapes the folder HTML.
"""

UPDATE_DRIVE_FOLDER_URL = (
    "https://drive.google.com/drive/folders/"
    "1azf1k4aCEtzg6d6-NTffq52guIdARF7q?usp=sharing"
)
UPDATE_DRIVE_FOLDER_ID = "1azf1k4aCEtzg6d6-NTffq52guIdARF7q"

# Permanent public Google Drive file ID of manifest.json.  When publishing a
# release, update the contents of this same Drive file instead of deleting it
# and uploading a new manifest with a different ID.
UPDATE_MANIFEST_FILE_ID = "1DAYJCCB1eO0zSvqNeWsWJREXbyMoSse8"
UPDATE_MANIFEST_FILE_URL = (
    "https://drive.google.com/file/d/"
    f"{UPDATE_MANIFEST_FILE_ID}/view?usp=sharing"
)

# Checks are intentionally cheap: only manifest.json is downloaded.  The large
# update ZIP is downloaded only after the user presses the update button.
UPDATE_CHECK_INTERVAL_SECONDS = 60 * 60
UPDATE_STARTUP_DELAY_MS = 8_000
UPDATE_HTTP_TIMEOUT_SECONDS = 8
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 20

# Keep network operations bounded and memory usage low.
UPDATE_CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024

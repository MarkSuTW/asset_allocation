"""
SQLite → Google Drive backup service.

Credentials are NEVER stored in code. They are read from the path specified
in the GDRIVE_CREDENTIALS_PATH environment variable (a service-account JSON
key file that lives outside the project directory).

Scope used: drive.file  — the service account can only see files it created,
not the entire Drive. This is the minimal permission needed.
"""
import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _build_drive_service(credentials_path: str):
    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=_SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def create_local_backup(db_path: str, backup_dir: str) -> str:
    """
    Hot-backup the live SQLite database using the official backup API,
    then gzip-compress it. Returns the path to the .gz file.
    """
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    db_name = Path(db_path).stem
    raw_path = Path(backup_dir) / f"{db_name}_backup_{ts}.db"
    gz_path = Path(backup_dir) / f"{db_name}_backup_{ts}.db.gz"

    # sqlite3.Connection.backup() is safe on a live database
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(raw_path))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    with open(raw_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    raw_path.unlink()
    return str(gz_path)


def upload_to_drive(local_path: str, folder_id: str, credentials_path: str) -> str:
    """
    Upload a file to the specified Google Drive folder.
    Returns the Drive file ID of the uploaded file.
    """
    service = _build_drive_service(credentials_path)
    file_name = Path(local_path).name
    metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="application/gzip", resumable=True)
    result = (
        service.files()
        .create(body=metadata, media_body=media, fields="id,name")
        .execute()
    )
    return result["id"]


def cleanup_old_drive_backups(
    folder_id: str, credentials_path: str, keep_n: int = 7
) -> int:
    """
    List all .gz files in the Drive folder sorted by createdTime (oldest first),
    delete any beyond the keep_n newest. Returns count of deleted files.
    """
    service = _build_drive_service(credentials_path)
    query = f"'{folder_id}' in parents and name contains '.db.gz' and trashed=false"
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id,name,createdTime)",
            orderBy="createdTime asc",
        )
        .execute()
    )
    files = response.get("files", [])
    to_delete = files[: max(0, len(files) - keep_n)]
    for f in to_delete:
        service.files().delete(fileId=f["id"]).execute()
    return len(to_delete)


def run_backup() -> dict:
    """
    Full backup pipeline driven entirely by environment variables:

      GDRIVE_CREDENTIALS_PATH  path to service-account JSON key (required)
      GDRIVE_FOLDER_ID         Drive folder ID to upload into (required)
      DB_PATH                  SQLite file to back up (default: wealth.db)
      BACKUP_LOCAL_DIR         local staging dir   (default: backups/)
      GDRIVE_KEEP_VERSIONS     number of Drive copies to keep (default: 7)
      GDRIVE_DELETE_LOCAL      delete local .gz after upload? (default: true)
    """
    cred_path = os.environ.get("GDRIVE_CREDENTIALS_PATH", "")
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "")
    db_path = os.environ.get("DB_PATH", "wealth.db")
    backup_dir = os.environ.get("BACKUP_LOCAL_DIR", "backups")
    keep_n = int(os.environ.get("GDRIVE_KEEP_VERSIONS", "7"))
    delete_local = os.environ.get("GDRIVE_DELETE_LOCAL", "true").lower() != "false"

    if not cred_path or not folder_id:
        raise EnvironmentError(
            "GDRIVE_CREDENTIALS_PATH and GDRIVE_FOLDER_ID must be set in the environment."
        )
    if not Path(cred_path).exists():
        raise FileNotFoundError(f"Credentials file not found: {cred_path}")

    gz_path = create_local_backup(db_path, backup_dir)
    drive_id = upload_to_drive(gz_path, folder_id, cred_path)
    deleted = cleanup_old_drive_backups(folder_id, cred_path, keep_n)

    if delete_local:
        Path(gz_path).unlink(missing_ok=True)

    return {
        "backup_file": Path(gz_path).name,
        "drive_file_id": drive_id,
        "old_backups_deleted": deleted,
    }

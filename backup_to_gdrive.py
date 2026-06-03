"""
Manual backup trigger. Run from the project root:
    python backup_to_gdrive.py

Reads config from .env (via python-dotenv if installed, else pure os.environ).
"""
import os
import sys
from pathlib import Path

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Make sure app/ is importable when run from project root
sys.path.insert(0, str(Path(__file__).parent))

from app.services.backup import run_backup

if __name__ == "__main__":
    try:
        result = run_backup()
        print("Backup successful:")
        print(f"  File    : {result['backup_file']}")
        print(f"  Drive ID: {result['drive_file_id']}")
        if result["old_backups_deleted"]:
            print(f"  Cleaned : {result['old_backups_deleted']} old backup(s) removed from Drive")
    except Exception as e:
        print(f"Backup failed: {e}", file=sys.stderr)
        sys.exit(1)

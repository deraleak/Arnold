"""
Arnold DB Backup
Copies arnold.db safely to a Google Drive synced folder using SQLite's
built-in backup API (safe even while Arnold is running / mid-write).
Run manually via backup.bat, or let Task Scheduler call it nightly.
"""

import sqlite3
import os
import sys
import datetime
import glob

# ── CONFIG ───────────────────────────────────────────────────────────────────
DB_PATH    = r"c:\Python Projects\Arnold\arnold.db"
KEEP_LAST  = 30   # number of daily backups to keep before pruning older ones

# Google Drive folder — script tries common locations automatically.
# If yours is somewhere else, set BACKUP_FOLDER manually and remove the
# auto-detect block below, e.g.:
#   BACKUP_FOLDER = r"G:\My Drive\Arnold Backups"
BACKUP_FOLDER = None
# ─────────────────────────────────────────────────────────────────────────────


def find_google_drive():
    user = os.path.expanduser("~")
    candidates = [
        os.path.join(user, "Google Drive", "My Drive"),
        os.path.join(user, "My Drive"),
        r"G:\My Drive",
        r"H:\My Drive",
        r"D:\My Drive",
        r"E:\My Drive",
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def run_backup():
    global BACKUP_FOLDER

    # Auto-detect Google Drive if not manually set
    if not BACKUP_FOLDER:
        drive = find_google_drive()
        if not drive:
            print(
                "[Backup] ERROR: Could not find your Google Drive folder.\n"
                "  Either install Google Drive Desktop from drive.google.com/drive/download\n"
                "  or set BACKUP_FOLDER manually at the top of backup.py"
            )
            sys.exit(1)
        BACKUP_FOLDER = os.path.join(drive, "Arnold Backups")

    if not os.path.exists(DB_PATH):
        print(f"[Backup] ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    os.makedirs(BACKUP_FOLDER, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    dest  = os.path.join(BACKUP_FOLDER, f"arnold_{stamp}.db")

    # SQLite .backup() is the correct way to copy a live database.
    # It creates a consistent snapshot even if Arnold is mid-write.
    src  = sqlite3.connect(DB_PATH)
    out  = sqlite3.connect(dest)
    src.backup(out)
    out.close()
    src.close()

    size_kb = os.path.getsize(dest) // 1024
    print(f"[Backup] Saved  → {dest}  ({size_kb} KB)")

    # Prune oldest backups beyond KEEP_LAST
    all_backups = sorted(glob.glob(os.path.join(BACKUP_FOLDER, "arnold_*.db")))
    to_delete   = all_backups[:-KEEP_LAST] if len(all_backups) > KEEP_LAST else []
    for f in to_delete:
        os.remove(f)
        print(f"[Backup] Pruned {os.path.basename(f)}")

    remaining = len(all_backups) - len(to_delete)
    print(f"[Backup] Done.  {remaining} backup(s) stored in Google Drive.")


if __name__ == "__main__":
    run_backup()

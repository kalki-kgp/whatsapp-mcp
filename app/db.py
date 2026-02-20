import sqlite3
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import (
    CHAT_STORAGE_DB,
    CONTACTS_DB,
    LID_DB,
    TEMP_DB_DIR,
    APPLE_EPOCH_OFFSET,
)

# Cache: track when we last copied the DBs
_last_copy_time: float = 0
COPY_INTERVAL = 30  # re-copy every 30 seconds at most


def _ensure_db_copies(force: bool = False) -> None:
    """Copy WhatsApp SQLite files to temp dir to avoid locking issues."""
    global _last_copy_time
    now = time.time()
    if not force and (now - _last_copy_time) < COPY_INTERVAL:
        return

    TEMP_DB_DIR.mkdir(parents=True, exist_ok=True)

    for db_path in [CHAT_STORAGE_DB, CONTACTS_DB, LID_DB]:
        if not db_path.exists():
            continue
        dest = TEMP_DB_DIR / db_path.name
        shutil.copy2(db_path, dest)
        # Also copy WAL and SHM files for consistency
        for suffix in ["-wal", "-shm"]:
            wal = db_path.parent / (db_path.name + suffix)
            if wal.exists():
                shutil.copy2(wal, TEMP_DB_DIR / (db_path.name + suffix))

    _last_copy_time = now


def get_chat_db() -> sqlite3.Connection:
    """Get a read-only connection to the ChatStorage database copy."""
    _ensure_db_copies()
    db_path = TEMP_DB_DIR / "ChatStorage.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_contacts_db() -> sqlite3.Connection:
    """Get a read-only connection to the ContactsV2 database copy."""
    _ensure_db_copies()
    db_path = TEMP_DB_DIR / "ContactsV2.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_lid_db() -> sqlite3.Connection:
    """Get a read-only connection to the LID database copy."""
    _ensure_db_copies()
    db_path = TEMP_DB_DIR / "LID.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def refresh_db():
    """Force a fresh copy of the databases."""
    _ensure_db_copies(force=True)


def apple_ts_to_datetime(ts: Optional[float]) -> Optional[datetime]:
    """Convert Apple Core Data timestamp to Python datetime."""
    if ts is None:
        return None
    unix_ts = ts + APPLE_EPOCH_OFFSET
    # Sanity check: if the year is > 3000, something is off
    try:
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        if dt.year > 3000:
            return None
        return dt
    except (OSError, ValueError):
        return None


def datetime_to_apple_ts(dt: datetime) -> float:
    """Convert Python datetime to Apple Core Data timestamp."""
    return dt.timestamp() - APPLE_EPOCH_OFFSET


def format_dt(dt: Optional[datetime]) -> str:
    """Format datetime for display."""
    if dt is None:
        return "unknown"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

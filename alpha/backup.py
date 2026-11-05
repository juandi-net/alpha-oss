"""Copy of the database and saved images in the backup directory (iCloud Drive by default):
one database file per day, the last 14 days kept.

The database is copied with SQLite's online backup, so a run writing at the same time cannot
leave a torn copy. Images are copied once each; they never change.
"""

import shutil
import sqlite3
from datetime import date

from .config import Settings
from .enrich import media_directory

KEEP_DAILY_COPIES = 14


def backup(connection: sqlite3.Connection, settings: Settings) -> None:
    directory = settings.backup_directory
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    # One file per day, replaced by each run that day, so the newest copy is never more than a run behind.
    target = directory / f"alpha-{date.today().isoformat()}.db"
    partial = target.with_suffix(".db.partial")
    partial.unlink(missing_ok=True)
    copy = sqlite3.connect(partial)
    connection.backup(copy)
    copy.close()
    partial.replace(target)
    for old in sorted(directory.glob("alpha-*.db"))[:-KEEP_DAILY_COPIES]:
        old.unlink()
    media = media_directory(settings)
    copied = 0
    if media.is_dir():
        (directory / "media").mkdir(exist_ok=True)
        for path in media.iterdir():
            destination = directory / "media" / path.name
            if not destination.exists():
                shutil.copy2(path, destination)
                copied += 1
    print(f"Backup: {target.name} in {directory}" + (f", {copied} new images" if copied else ""))

"""Consistent local SQLite snapshots, safe while the application is running."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import closing
import sqlite3
import uuid


def snapshot(source, directory, daily=False):
    source, directory = Path(source), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    existing = sorted(directory.glob('shipment-*.sqlite3'), key=lambda p:p.stat().st_mtime, reverse=True)
    if daily and existing and now.timestamp() - existing[0].stat().st_mtime < timedelta(days=1).total_seconds():
        return existing[0]
    target = directory / f'shipment-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.sqlite3'
    pending = target.with_suffix('.partial')
    try:
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(pending)) as dest:
                src.backup(dest)
                if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed.')
        pending.replace(target)
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    return target


def monitor(stop, source, directory, status):
    while not stop.is_set():
        try:
            path = snapshot(source, directory, daily=True)
            status.update(last_backup=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), error=None)
        except Exception:
            status.update(error='Automatic backup failed. Check disk space and folder permissions on the host.')
        if stop.wait(300):
            break


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    print(snapshot(root/'data'/'shipments.sqlite3', root/'backups'))

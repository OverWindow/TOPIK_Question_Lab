"""Read cleanup receipts without creating or migrating authoring databases."""
from contextlib import closing
import json
import sqlite3


PREFIX = "registered_set_cleanup:"


def cleanup_receipts(root):
    receipts = []
    for directory in ("data/types", "data/listening/types"):
        for path in sorted((root / directory).glob("*.db")):
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
                if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
                    continue
                for (value,) in c.execute("SELECT value FROM settings WHERE key LIKE ?", (PREFIX + "%",)):
                    receipts.append({**json.loads(value), "source_db": path.relative_to(root).as_posix()})
    return receipts


def batch_was_cleaned(root, batch_id):
    return any(batch_id in r["batch_ids"] for r in cleanup_receipts(root))

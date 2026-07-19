#!/usr/bin/env python3
"""Daily FoodRescue database backup.

Dumps every collection to a single gzipped Extended-JSON file and prunes
anything older than FR_BACKUP_KEEP_DAYS. Extended JSON (via bson.json_util)
preserves ObjectIds, dates and every other BSON type, so restore is lossless.

Run on the server from cron (see docs/DEPLOYMENT.md):
    30 2 * * * root /opt/food-rescue/backend/.venv/bin/python \
        /opt/food-rescue/scripts/backup_db.py >> /var/log/foodrescue-backup.log 2>&1

MONGO_URI is read from the repo-root .env. Override the output directory with
FR_BACKUP_DIR and the retention window with FR_BACKUP_KEEP_DAYS.
"""
import gzip
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

BACKUP_DIR = Path(os.environ.get("FR_BACKUP_DIR", "/opt/food-rescue-backups"))
KEEP_DAYS = int(os.environ.get("FR_BACKUP_KEEP_DAYS", "14"))


def main():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db = MongoClient(
        os.environ["MONGO_URI"], serverSelectionTimeoutMS=20000
    ).get_default_database()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"foodrescue-{stamp}.jsonl.gz"
    docs = 0
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        for coll in db.list_collection_names():
            for doc in db[coll].find():
                fh.write(json_util.dumps({"c": coll, "d": doc}) + "\n")
                docs += 1
    print(f"[backup] {out.name}: {docs} docs, {out.stat().st_size} bytes")

    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for old in BACKUP_DIR.glob("foodrescue-*.jsonl.gz"):
        if datetime.fromtimestamp(old.stat().st_mtime, timezone.utc) < cutoff:
            old.unlink()
            print(f"[backup] pruned {old.name}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Restore a FoodRescue backup produced by backup_db.py.

Usage:
    python restore_db.py <file.jsonl.gz> [--wipe]

By default documents are upserted by _id (merged into whatever is currently in
the database). Pass --wipe to drop every collection first, giving an exact
point-in-time restore of the backup.

MONGO_URI is read from the repo-root .env — double-check it points at the
database you intend to restore into before running with --wipe.
"""
import gzip
import os
import sys
from pathlib import Path

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: restore_db.py <file.jsonl.gz> [--wipe]")
    path = sys.argv[1]
    wipe = "--wipe" in sys.argv[2:]

    db = MongoClient(
        os.environ["MONGO_URI"], serverSelectionTimeoutMS=20000
    ).get_default_database()

    if wipe:
        for coll in db.list_collection_names():
            db[coll].drop()
        print("[restore] wiped existing collections")

    docs = 0
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            rec = json_util.loads(line)
            doc = rec["d"]
            db[rec["c"]].replace_one({"_id": doc["_id"]}, doc, upsert=True)
            docs += 1
    print(f"[restore] {docs} docs restored from {path}")


if __name__ == "__main__":
    main()

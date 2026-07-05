#!/usr/bin/env python3
"""Create, verify, or safely restore SQLite backups."""

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from booking_app import config


def verify(path):
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        conn.close()
    if result != "ok" or tables < 1:
        raise RuntimeError(f"备份校验失败：{result}")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "tables": tables, "verifiedAt": datetime.now().isoformat(timespec="seconds")}


def create(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(config.DB_PATH)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close(); source.close()
    result = verify(destination)
    destination.with_suffix(destination.suffix + ".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def restore(source, force):
    verify(source)
    if config.DB_PATH.exists() and not force:
        raise RuntimeError("目标数据库已存在；如确认恢复，请增加 --force")
    temporary = config.DB_PATH.with_suffix(".restore.tmp")
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(temporary)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close(); source_conn.close()
    verify(temporary)
    temporary.replace(config.DB_PATH)
    return verify(config.DB_PATH)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify(args.verify)
    elif args.restore:
        result = restore(args.restore, args.force)
    else:
        output = args.output or config.ROOT / "backups" / f"booking-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
        result = create(output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

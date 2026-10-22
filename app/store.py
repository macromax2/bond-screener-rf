#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Хранилище дневных снапшотов скринера (SQLite).

Идея: биржа отдаёт торговые данные за закрытый день. Утром (до открытия)
live-оборот у всех бумаг нулевой, поэтому скринер строит дневной снапшот
по истории за последний закрытый торговый день и хранит его в базе.
Сервер всегда отдаёт последний снапшот из базы -- так скринер работает
24/7 с одинаково полными данными.

Схема:
    snapshots(trade_date PK, fetched_at, bonds_gz, curve_gz, meta_json)
    kv(key PK, value)  -- служебные записи (последний день и т.п.)
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import moex

DB_PATH = moex.DATA_DIR / "screener.db"
KEEP_DAYS = 30  # сколько дневных снапшотов хранить

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        moex.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                trade_date TEXT PRIMARY KEY,
                fetched_at REAL NOT NULL,
                bonds_gz BLOB NOT NULL,
                curve_gz BLOB,
                meta_json TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        _conn.commit()
    return _conn


def _pack(obj) -> bytes:
    return gzip.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _unpack(blob):
    if not blob:
        return None
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def save_snapshot(trade_date: str, bonds: list[dict], curve: list[dict],
                  meta: dict) -> None:
    """Сохранить (или заменить) дневной снапшот."""
    with _lock:
        db = _db()
        db.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(trade_date, fetched_at, bonds_gz, curve_gz, meta_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (trade_date, time.time(), _pack(bonds), _pack(curve),
             json.dumps(meta, ensure_ascii=False)),
        )
        db.execute("INSERT OR REPLACE INTO kv VALUES ('last_trade_date', ?)",
                   (trade_date,))
        db.commit()


def latest_snapshot() -> dict | None:
    """Последний сохранённый снапшот: {trade_date, bonds, curve, meta}."""
    with _lock:
        row = _db().execute(
            "SELECT trade_date, bonds_gz, curve_gz, meta_json "
            "FROM snapshots ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
    except Exception:  # noqa: BLE001
        meta = {}
    return {
        "trade_date": row["trade_date"],
        "bonds": _unpack(row["bonds_gz"]) or [],
        "curve": _unpack(row["curve_gz"]) or [],
        "meta": meta,
    }


def list_dates() -> list[str]:
    """Даты сохранённых снапшотов (новые вперёд)."""
    with _lock:
        rows = _db().execute(
            "SELECT trade_date FROM snapshots ORDER BY trade_date DESC"
        ).fetchall()
    return [r["trade_date"] for r in rows]


def get_kv(key: str) -> str | None:
    with _lock:
        row = _db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def prune(keep_days: int = KEEP_DAYS) -> int:
    """Удалить снапшоты старше keep_days. Возвращает число удалённых."""
    import datetime as _dt
    cutoff = (_dt.date.today() - _dt.timedelta(days=keep_days)).isoformat()
    with _lock:
        cur = _db().execute(
            "DELETE FROM snapshots WHERE trade_date < ?", (cutoff,))
        _db().commit()
    return cur.rowcount

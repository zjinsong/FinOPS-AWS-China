from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class SQLiteStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collector_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collector TEXT NOT NULL,
                    account_alias TEXT,
                    region TEXT,
                    status TEXT NOT NULL,
                    detail TEXT,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendation_tasks (
                    recommendation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    owner TEXT,
                    estimated_monthly_savings REAL,
                    actual_monthly_savings REAL,
                    note TEXT,
                    updated_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def get_cache(self, key: str) -> Any | None:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM cache WHERE cache_key = ? AND expires_at > ?",
                (key, now),
            ).fetchone()
        return json.loads(row["value_json"]) if row else None

    def set_cache(self, key: str, value: Any, ttl_seconds: int) -> None:
        now = int(time.time())
        payload = json.dumps(value, ensure_ascii=False, default=str)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cache(cache_key, value_json, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    expires_at=excluded.expires_at,
                    created_at=excluded.created_at
                """,
                (key, payload, now + ttl_seconds, now),
            )

    def get_setting(self, key: str, default: Any) -> Any:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE setting_key = ?", (key,)
            ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(setting_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    def add_run(
        self,
        collector: str,
        account_alias: str | None,
        region: str | None,
        status: str,
        detail: str,
        started_at: int,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO collector_runs(
                    collector, account_alias, region, status, detail, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collector,
                    account_alias,
                    region,
                    status,
                    detail[:1000],
                    started_at,
                    int(time.time()),
                ),
            )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM collector_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def audit(self, actor: str, action: str, detail: str = "") -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(actor, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (actor, action, detail[:1000], int(time.time())),
            )

    def list_tasks(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recommendation_tasks ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_task(
        self,
        recommendation_id: str,
        status: str,
        owner: str,
        estimated_monthly_savings: float | None,
        actual_monthly_savings: float | None,
        note: str,
        updated_by: str,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recommendation_tasks(
                    recommendation_id, status, owner, estimated_monthly_savings,
                    actual_monthly_savings, note, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recommendation_id) DO UPDATE SET
                    status=excluded.status,
                    owner=excluded.owner,
                    estimated_monthly_savings=excluded.estimated_monthly_savings,
                    actual_monthly_savings=excluded.actual_monthly_savings,
                    note=excluded.note,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (
                    recommendation_id,
                    status,
                    owner[:128],
                    estimated_monthly_savings,
                    actual_monthly_savings,
                    note[:2000],
                    updated_by[:64],
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM recommendation_tasks WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
        return dict(row)

"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_domain_siteless_key
    ON domain_records(category, external_key) WHERE site_id IS NULL;
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaint_cases (
    case_id TEXT PRIMARY KEY,
    region_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'confirmed')),
    first_event_at TEXT NOT NULL,
    last_event_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS complaint_events (
    event_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    channel TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    region_code TEXT NOT NULL,
    description_norm TEXT NOT NULL,
    text_fingerprint TEXT NOT NULL,
    contact_token TEXT,
    contact_mask TEXT,
    location_json TEXT,
    terms_json TEXT NOT NULL DEFAULT '[]',
    merge_reasons TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_version INTEGER PRIMARY KEY AUTOINCREMENT,
    family TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded')),
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    kind TEXT NOT NULL CHECK(kind IN ('analysis', 'decision')),
    facts_json TEXT NOT NULL,
    facts_hash TEXT NOT NULL,
    rule_version INTEGER,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_versions (
    case_version_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    version INTEGER NOT NULL,
    rule_version INTEGER NOT NULL,
    fact_snapshot_id TEXT NOT NULL REFERENCES fact_snapshots(snapshot_id),
    origin TEXT NOT NULL CHECK(origin IN ('generated', 'manual_exclude', 'manual_add')),
    candidates_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    needs_field_inspection INTEGER NOT NULL CHECK(needs_field_inspection IN (0, 1)),
    is_current INTEGER NOT NULL CHECK(is_current IN (0, 1)),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(case_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_case_current_version
    ON case_versions(case_id) WHERE is_current = 1;
CREATE TABLE IF NOT EXISTS case_decisions (
    decision_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    case_version_id TEXT NOT NULL REFERENCES case_versions(case_version_id),
    fact_snapshot_id TEXT NOT NULL REFERENCES fact_snapshots(snapshot_id),
    action TEXT NOT NULL CHECK(action IN
        ('excluded_candidate', 'added_candidate', 'confirmed_case', 'revoked_confirmation')),
    target_site_id TEXT,
    reason TEXT NOT NULL,
    from_state_hash TEXT NOT NULL,
    to_state_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_conclusions (
    conclusion_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    case_version_id TEXT NOT NULL REFERENCES case_versions(case_version_id),
    rule_version INTEGER NOT NULL,
    fact_snapshot_id TEXT NOT NULL REFERENCES fact_snapshots(snapshot_id),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    revoked_by TEXT REFERENCES actors(actor_id),
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_case_active_conclusion
    ON case_conclusions(case_id) WHERE status = 'active';
"""


class _LockedConnection:
    """把单个 SQLite 连接的语句执行串行化。

    ThreadingHTTPServer 会在多个工作线程间共享同一个连接；SQLite 连接
    对象本身不保证跨线程并发使用，这里用一把可重入锁把 execute/提交
    串行化，使短事务在进程内不会彼此穿插。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.lock = threading.RLock()

    def execute(self, sql: str, parameters: tuple | list = ()):
        with self.lock:
            return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters):
        with self.lock:
            return self._connection.executemany(sql, parameters)

    def executescript(self, script: str):
        with self.lock:
            return self._connection.executescript(script)

    def commit(self) -> None:
        with self.lock:
            self._connection.commit()

    def rollback(self) -> None:
        with self.lock:
            self._connection.rollback()

    def close(self) -> None:
        with self.lock:
            self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        raw = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA busy_timeout = 5000")
        raw.executescript(SCHEMA)
        self.connection = _LockedConnection(raw)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        # 进程内先串行化，跨进程再由 SQLite 的 IMMEDIATE 写锁与 busy_timeout
        # 串行，保证同一投诉案件的并发研判不会产生两个当前版本。
        with self.connection.lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()

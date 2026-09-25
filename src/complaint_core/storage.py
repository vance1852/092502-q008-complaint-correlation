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
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
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
CREATE TABLE IF NOT EXISTS complaint_events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    location_text TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    description TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    shingles_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    contact_id TEXT,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_zone_time ON complaint_events(zone_id, occurred_at);
CREATE TABLE IF NOT EXISTS event_contacts (
    contact_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES complaint_events(event_id),
    contact_name TEXT NOT NULL,
    contact_phone TEXT NOT NULL,
    masked_name TEXT NOT NULL,
    masked_phone TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaint_cases (
    case_id TEXT PRIMARY KEY,
    case_code TEXT NOT NULL UNIQUE,
    zone_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','confirmed','revoked')),
    first_occurred_at TEXT NOT NULL,
    last_occurred_at TEXT NOT NULL,
    current_version_id TEXT,
    confirmed_version_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_zone_status ON complaint_cases(zone_id, status);
CREATE TABLE IF NOT EXISTS case_events (
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    event_id TEXT NOT NULL UNIQUE REFERENCES complaint_events(event_id),
    merge_basis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(case_id, event_id)
);
CREATE TABLE IF NOT EXISTS correlation_rules (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('active','retired')),
    params_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(rule_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_active_rule
    ON correlation_rules(rule_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS correlation_versions (
    version_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    version_no INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    rule_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('current','superseded','confirmed','revoked')),
    revision INTEGER NOT NULL DEFAULT 0,
    generation_snapshot_json TEXT NOT NULL,
    generation_snapshot_hash TEXT NOT NULL,
    engine_result_hash TEXT NOT NULL,
    requires_site_inspection INTEGER NOT NULL CHECK(requires_site_inspection IN (0, 1)),
    conclusion_json TEXT,
    conclusion_snapshot_hash TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, version_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_current_version
    ON correlation_versions(case_id) WHERE status='current';
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES correlation_versions(version_id),
    site_id TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('auto','manual')),
    score REAL,
    confidence_low REAL,
    confidence_high REAL,
    status TEXT NOT NULL CHECK(status IN ('suggested','excluded','added','confirmed')),
    factors_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, site_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_version ON candidates(version_id);
CREATE TABLE IF NOT EXISTS manual_decisions (
    decision_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES complaint_cases(case_id),
    version_id TEXT NOT NULL REFERENCES correlation_versions(version_id),
    candidate_id TEXT REFERENCES candidates(candidate_id),
    action TEXT NOT NULL CHECK(action IN (
        'exclude_candidate','add_candidate','confirm_candidate','confirm_case','revoke_confirmation'
    )),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    fact_snapshot_json TEXT NOT NULL,
    fact_snapshot_hash TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_case ON manual_decisions(case_id, decided_at);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # 同一连接在多线程 HTTP 服务中共享：用可重入锁串行化事务，
        # 跨进程/跨连接的并发仍由 BEGIN IMMEDIATE 与 busy_timeout 兜底。
        self._tx_lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；同进程内事务互斥执行。"""

        with self._tx_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """串行化只读访问，避免与同连接上的写事务交叉执行。"""

        with self._tx_lock:
            yield self.connection

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()

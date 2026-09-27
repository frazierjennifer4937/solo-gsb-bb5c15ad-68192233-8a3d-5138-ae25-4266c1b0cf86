"""SQLite 持久化层：保存每次审查的文档、结论、发现与限时豁免。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# 容器内默认落在挂载卷 /data；本地开发默认项目下 data/
DB_PATH = os.environ.get("REVIEW_DB", "data/reviews.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    title        TEXT,
    old_filename TEXT,
    new_filename TEXT,
    old_version  TEXT,
    new_version  TEXT,
    api_id       TEXT,
    summary      TEXT    NOT NULL,
    findings     TEXT    NOT NULL,
    old_spec     TEXT    NOT NULL,
    new_spec     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS exemptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    api_id       TEXT    NOT NULL,
    rule_id      TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    endpoint     TEXT    NOT NULL,
    location     TEXT    NOT NULL,
    old_value    TEXT    NOT NULL,
    new_value    TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_exemptions_api ON exemptions(api_id);
"""


def init_db() -> None:
    """建表并执行轻量迁移（随应用启动完成数据库升级）。"""
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)
        # 历史库升级：reviews 增加 api_id 列（已存在则跳过）
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
        if "api_id" not in cols:
            conn.execute("ALTER TABLE reviews ADD COLUMN api_id TEXT")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_review(
    *,
    title: str | None,
    old_filename: str | None,
    new_filename: str | None,
    old_version: str | None,
    new_version: str | None,
    api_id: str | None,
    summary: dict[str, Any],
    findings: list[dict[str, Any]],
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO reviews
                (title, old_filename, new_filename, old_version, new_version,
                 api_id, summary, findings, old_spec, new_spec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                old_filename,
                new_filename,
                old_version,
                new_version,
                api_id,
                json.dumps(summary, ensure_ascii=False),
                json.dumps(findings, ensure_ascii=False),
                json.dumps(old_doc, ensure_ascii=False),
                json.dumps(new_doc, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def _row_to_brief(row: sqlite3.Row) -> dict[str, Any]:
    cols = set(row.keys())
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": row["title"],
        "old_filename": row["old_filename"],
        "new_filename": row["new_filename"],
        "old_version": row["old_version"],
        "new_version": row["new_version"],
        "api_id": row["api_id"] if "api_id" in cols else None,
        "summary": json.loads(row["summary"]),
    }


def list_reviews() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return [_row_to_brief(r) for r in rows]


def get_review(review_id: int, *, with_specs: bool = False) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
    if row is None:
        return None
    data = _row_to_brief(row)
    data["findings"] = json.loads(row["findings"])
    if with_specs:
        data["old_spec"] = json.loads(row["old_spec"])
        data["new_spec"] = json.loads(row["new_spec"])
    return data


def get_specs(review_id: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT old_spec, new_spec FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["old_spec"]), json.loads(row["new_spec"])


def delete_review(review_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------- #
# 限时豁免（按 API 标识共享，删除审查不影响豁免记录）
# ---------------------------------------------------------------------- #
def _row_to_exemption(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "api_id": row["api_id"],
        "rule_id": row["rule_id"],
        "direction": row["direction"],
        "endpoint": row["endpoint"],
        "location": row["location"],
        "old_value": row["old_value"],
        "new_value": row["new_value"],
        "reason": row["reason"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
    }


def create_exemption(
    *,
    api_id: str,
    rule_id: str,
    direction: str,
    endpoint: str,
    location: str,
    old_value: str,
    new_value: str,
    reason: str,
    expires_at: str,
    created_at: str,
) -> dict[str, Any]:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO exemptions
                (created_at, api_id, rule_id, direction, endpoint, location,
                 old_value, new_value, reason, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                api_id,
                rule_id,
                direction,
                endpoint,
                location,
                old_value,
                new_value,
                reason,
                expires_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM exemptions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_exemption(row)


def list_exemptions(api_id: str) -> list[dict[str, Any]]:
    """某 API 标识下的全部豁免（含已过期 / 已撤销），按创建先后升序。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM exemptions WHERE api_id = ? ORDER BY id ASC", (api_id,)
        ).fetchall()
        return [_row_to_exemption(r) for r in rows]


def revoke_exemption(exemption_id: int, revoked_at: str) -> dict[str, Any] | None:
    """撤销豁免（幂等：已撤销的保持原撤销时间）；不存在返回 None。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM exemptions WHERE id = ?", (exemption_id,)
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is None:
            conn.execute(
                "UPDATE exemptions SET revoked_at = ? WHERE id = ?",
                (revoked_at, exemption_id),
            )
            row = conn.execute(
                "SELECT * FROM exemptions WHERE id = ?", (exemption_id,)
            ).fetchone()
        return _row_to_exemption(row)

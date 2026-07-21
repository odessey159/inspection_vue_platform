"""SQLite storage for inspection rules used by Rule RAG retrieval."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..settings import RULE_DB_PATH


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    resolved = (db_path or RULE_DB_PATH).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    return conn


def init_rule_db(db_path: Path | None = None) -> None:
    conn = get_conn(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS inspection_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT UNIQUE NOT NULL,

            domain TEXT,
            category TEXT,
            object_name TEXT,
            check_item TEXT,
            checker_scope TEXT,

            hazard_desc TEXT NOT NULL,
            legal_basis TEXT,
            severity TEXT,

            visual_detectable INTEGER NOT NULL,
            source TEXT,

            raw_payload_json TEXT,

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rule_evidence_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT NOT NULL,
            object_name TEXT NOT NULL,
            object_name_norm TEXT,
            FOREIGN KEY(rule_id) REFERENCES inspection_rules(rule_id)
        );
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rules_rule_id
        ON inspection_rules(rule_id);
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rules_visual_detectable
        ON inspection_rules(visual_detectable);
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rules_object_name
        ON inspection_rules(object_name);
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_evidence_object
        ON rule_evidence_objects(object_name_norm);
        """
    )

    conn.commit()
    conn.close()


def upsert_rules_to_db(
    rules: list[dict[str, Any]],
    db_path: Path | None = None,
) -> None:
    """Write export_rules_payload() structures into SQLite."""
    init_rule_db(db_path)

    conn = get_conn(db_path)
    cur = conn.cursor()

    for rule in rules:
        rule_id = rule.get("ruleId") or rule.get("rule_id")
        if not rule_id:
            continue

        evidence_objects = rule.get("evidenceObjects") or rule.get("evidence_objects") or []

        cur.execute(
            """
            INSERT INTO inspection_rules (
                rule_id,
                domain,
                category,
                object_name,
                check_item,
                checker_scope,
                hazard_desc,
                legal_basis,
                severity,
                visual_detectable,
                source,
                raw_payload_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(rule_id) DO UPDATE SET
                domain = excluded.domain,
                category = excluded.category,
                object_name = excluded.object_name,
                check_item = excluded.check_item,
                checker_scope = excluded.checker_scope,
                hazard_desc = excluded.hazard_desc,
                legal_basis = excluded.legal_basis,
                severity = excluded.severity,
                visual_detectable = excluded.visual_detectable,
                source = excluded.source,
                raw_payload_json = excluded.raw_payload_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                rule_id,
                rule.get("domain"),
                rule.get("category"),
                rule.get("objectName") or rule.get("object_name"),
                rule.get("checkItem") or rule.get("check_item"),
                rule.get("checkerScope") or rule.get("checker_scope"),
                rule.get("hazardDesc") or rule.get("hazard_desc") or "",
                rule.get("legalBasis") or rule.get("legal_basis"),
                rule.get("severity"),
                1 if rule.get("visualDetectable") or rule.get("visual_detectable") else 0,
                rule.get("source"),
                json.dumps(rule, ensure_ascii=False),
            ),
        )

        cur.execute(
            "DELETE FROM rule_evidence_objects WHERE rule_id = ?",
            (rule_id,),
        )

        for obj in evidence_objects:
            if not obj:
                continue
            cur.execute(
                """
                INSERT INTO rule_evidence_objects (
                    rule_id,
                    object_name,
                    object_name_norm
                )
                VALUES (?, ?, ?)
                """,
                (
                    rule_id,
                    obj,
                    str(obj).strip().lower(),
                ),
            )

    conn.commit()
    conn.close()

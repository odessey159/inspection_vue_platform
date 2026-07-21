"""Rule retrieval for provider_yolo when RULE_RAG_ENABLED is on."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import HazardRule
from ..settings import RULE_DB_PATH, RULE_RAG_FALLBACK_TOP_K, RULE_RETRIEVAL_TOP_K
from .object_alias import normalize_yolo_classes
from .rule_db import get_conn, init_rule_db


def hazard_rules_from_payloads(
    payloads: list[dict[str, Any]],
    *,
    project_id: int = 0,
) -> list[HazardRule]:
    rules: list[HazardRule] = []
    for payload in payloads:
        rule_id = str(payload.get("ruleId") or payload.get("rule_id") or "").strip()
        if not rule_id:
            continue
        evidence_objects = payload.get("evidenceObjects") or payload.get("evidence_objects") or []
        rules.append(
            HazardRule(
                project_id=project_id,
                rule_id=rule_id,
                domain=str(payload.get("domain") or "industrial-inspection"),
                category=str(payload.get("category") or ""),
                object_name=str(payload.get("objectName") or payload.get("object_name") or ""),
                check_item=str(payload.get("checkItem") or payload.get("check_item") or ""),
                checker_scope=str(payload.get("checkerScope") or payload.get("checker_scope") or ""),
                hazard_desc=str(payload.get("hazardDesc") or payload.get("hazard_desc") or ""),
                legal_basis=str(payload.get("legalBasis") or payload.get("legal_basis") or ""),
                evidence_objects_json=json.dumps(evidence_objects, ensure_ascii=False),
                severity=str(payload.get("severity") or "medium"),
                visual_detectable=bool(payload.get("visualDetectable") or payload.get("visual_detectable")),
                source=str(payload.get("source") or "rules.db"),
            )
        )
    return rules


def _row_to_rule(row: Any) -> dict[str, Any]:
    raw = row["raw_payload_json"]
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        "ruleId": row["rule_id"],
        "domain": row["domain"],
        "category": row["category"],
        "objectName": row["object_name"],
        "checkItem": row["check_item"],
        "checkerScope": row["checker_scope"],
        "hazardDesc": row["hazard_desc"],
        "legalBasis": row["legal_basis"],
        "severity": row["severity"],
        "visualDetectable": bool(row["visual_detectable"]),
        "source": row["source"],
    }


def retrieve_rules_for_clip(
    yolo_detections: list[dict[str, Any]],
    checker_scope: str | None = None,
    domain: str | None = "industrial-inspection",
    top_k: int | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve visualDetectable rules from the rule DB based on YOLO detections."""
    resolved_top_k = top_k if top_k is not None else RULE_RETRIEVAL_TOP_K
    init_rule_db(db_path)

    raw_classes: list[str] = []
    for det in yolo_detections:
        cls = det.get("class") or det.get("class_name") or det.get("label")
        if cls:
            raw_classes.append(str(cls))

    yolo_classes = normalize_yolo_classes(raw_classes)

    if not yolo_classes:
        return retrieve_default_visual_rules(
            checker_scope=checker_scope,
            domain=domain,
            top_k=resolved_top_k,
            db_path=db_path,
        )

    conn = get_conn(db_path)
    cur = conn.cursor()

    candidates: dict[str, dict[str, Any]] = {}

    for cls in yolo_classes:
        like_pattern = f"%{cls}%"

        cur.execute(
            """
            SELECT DISTINCT r.*
            FROM inspection_rules r
            LEFT JOIN rule_evidence_objects e
                ON r.rule_id = e.rule_id
            WHERE r.visual_detectable = 1
              AND (
                    r.object_name = ?
                 OR e.object_name_norm = ?
                 OR r.hazard_desc LIKE ?
                 OR r.check_item LIKE ?
                 OR r.category LIKE ?
              )
            """,
            (
                cls,
                cls.strip().lower(),
                like_pattern,
                like_pattern,
                like_pattern,
            ),
        )

        for row in cur.fetchall():
            rule_id = row["rule_id"]

            if rule_id not in candidates:
                candidates[rule_id] = {
                    "row": row,
                    "score": 0.0,
                    "matched_classes": set(),
                    "matched_reasons": [],
                }

            item = candidates[rule_id]
            item["matched_classes"].add(cls)

            object_name = row["object_name"] or ""
            hazard_desc = row["hazard_desc"] or ""
            check_item = row["check_item"] or ""
            category = row["category"] or ""

            if object_name == cls:
                item["score"] += 5
                item["matched_reasons"].append(f"object_name={cls}")

            if cls in hazard_desc:
                item["score"] += 2
                item["matched_reasons"].append(f"hazard_desc contains {cls}")

            if cls in check_item:
                item["score"] += 2
                item["matched_reasons"].append(f"check_item contains {cls}")

            if cls in category:
                item["score"] += 1
                item["matched_reasons"].append(f"category contains {cls}")

            cur.execute(
                """
                SELECT 1
                FROM rule_evidence_objects
                WHERE rule_id = ?
                  AND object_name_norm = ?
                LIMIT 1
                """,
                (
                    rule_id,
                    cls.strip().lower(),
                ),
            )

            if cur.fetchone():
                item["score"] += 4
                item["matched_reasons"].append(f"evidence_object={cls}")

            if checker_scope:
                rule_scope = row["checker_scope"] or ""
                if rule_scope in ("mixed", checker_scope):
                    item["score"] += 1
                    item["matched_reasons"].append(f"scope match {checker_scope}")

            severity = row["severity"] or ""
            if severity == "critical":
                item["score"] += 0.8
            elif severity == "high":
                item["score"] += 0.5

    conn.close()

    ranked = sorted(
        candidates.values(),
        key=lambda item: item["score"],
        reverse=True,
    )

    results: list[dict[str, Any]] = []

    for item in ranked[:resolved_top_k]:
        rule = _row_to_rule(item["row"])
        rule["_retrieval"] = {
            "score": item["score"],
            "matched_classes": sorted(item["matched_classes"]),
            "matched_reasons": item["matched_reasons"],
        }
        results.append(rule)

    if not results:
        return retrieve_default_visual_rules(
            checker_scope=checker_scope,
            domain=domain,
            top_k=resolved_top_k,
            db_path=db_path,
        )

    return results


def retrieve_default_visual_rules(
    checker_scope: str | None = None,
    domain: str | None = "industrial-inspection",
    top_k: int | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Fallback rules when YOLO finds no relevant objects."""
    resolved_top_k = top_k if top_k is not None else RULE_RAG_FALLBACK_TOP_K
    init_rule_db(db_path)

    conn = get_conn(db_path)
    cur = conn.cursor()

    sql = """
    SELECT *
    FROM inspection_rules
    WHERE visual_detectable = 1
    """
    params: list[Any] = []

    if domain:
        sql += " AND (domain = ? OR domain IS NULL OR domain = '')"
        params.append(domain)

    if checker_scope:
        sql += " AND (checker_scope = ? OR checker_scope = 'mixed' OR checker_scope IS NULL OR checker_scope = '')"
        params.append(checker_scope)

    sql += """
    ORDER BY
        CASE severity
            WHEN 'critical' THEN 4
            WHEN 'high' THEN 3
            WHEN 'medium' THEN 2
            WHEN 'low' THEN 1
            ELSE 0
        END DESC,
        rule_id ASC
    LIMIT ?
    """
    params.append(resolved_top_k)

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    return [_row_to_rule(row) for row in rows]


def save_retrieval_artifact(
    output_path: Path,
    clip_id: str,
    yolo_detections: list[dict[str, Any]],
    retrieved_rules: list[dict[str, Any]],
    provider_prompt_section: str,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "clip_id": clip_id,
        "yolo_classes": sorted(
            {
                str(det.get("class") or det.get("class_name") or det.get("label"))
                for det in yolo_detections
                if det.get("class") or det.get("class_name") or det.get("label")
            }
        ),
        "retrieved_rule_ids": [
            rule.get("ruleId") or rule.get("rule_id")
            for rule in retrieved_rules
        ],
        "retrieved_rules": [
            {
                "rule_id": rule.get("ruleId") or rule.get("rule_id"),
                "hazard_desc": rule.get("hazardDesc") or rule.get("hazard_desc"),
                "severity": rule.get("severity"),
                "retrieval": rule.get("_retrieval"),
            }
            for rule in retrieved_rules
        ],
        "yolo_detections": yolo_detections,
        "provider_prompt_section": provider_prompt_section,
    }

    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

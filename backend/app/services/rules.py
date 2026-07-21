from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ..models import HazardRule


XML_NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

POSITIVE_VISUAL_KEYWORDS = [
    "dust",
    "gear",
    "belt",
    "blade",
    "cover",
    "box",
    "cabinet",
    "fire",
    "extinguisher",
    "channel",
    "exit",
    "ladder",
    "warning sign",
]

POSITIVE_VISUAL_CJK = [
    "\u79ef\u5c18",
    "\u9632\u62a4\u7f69",
    "\u914d\u7535\u7bb1",
    "\u8b66\u793a",
    "\u706d\u706b\u5668",
    "\u6d88\u9632",
    "\u901a\u9053",
    "\u6807\u5fd7",
    "\u6c14\u74f6",
    "\u680f\u6746",
    "\u7535\u7ebf",
    "\u7535\u74f6\u8f66",
]

NEGATIVE_VISUAL_CJK = [
    "\u5236\u5ea6",
    "\u53f0\u8d26",
    "\u57f9\u8bad",
    "\u8ba1\u5212",
    "\u9884\u6848",
    "\u8bb0\u5f55",
    "\u62a5\u544a",
    "\u8d23\u4efb",
]


def parse_rules(standards_dir: Path, project_id: int) -> list[HazardRule]:
    excel_rules = _parse_excel_rules(standards_dir, project_id)
    existing = {rule.hazard_desc for rule in excel_rules}
    docx_rules = _parse_docx_rules(standards_dir, project_id, existing)
    return excel_rules + docx_rules


def export_rules_payload(rules: list[HazardRule]) -> list[dict[str, object]]:
    return [
        {
            "ruleId": rule.rule_id,
            "domain": rule.domain,
            "category": rule.category,
            "objectName": rule.object_name,
            "checkItem": rule.check_item,
            "checkerScope": rule.checker_scope,
            "hazardDesc": rule.hazard_desc,
            "legalBasis": rule.legal_basis,
            "evidenceObjects": json.loads(rule.evidence_objects_json),
            "severity": rule.severity,
            "visualDetectable": rule.visual_detectable,
            "source": rule.source,
        }
        for rule in rules
    ]


def sync_rules_to_db(rules: list[HazardRule]) -> None:
    from .rule_db import upsert_rules_to_db

    upsert_rules_to_db(export_rules_payload(rules))


def _parse_excel_rules(standards_dir: Path, project_id: int) -> list[HazardRule]:
    xlsx_files = sorted(standards_dir.glob("*.xlsx"))
    if not xlsx_files:
        return []

    path = xlsx_files[0]
    rules: list[HazardRule] = []
    counter = 1
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        for sheet_name, target in _read_workbook_sheets(archive):
            if not target.startswith("worksheets/"):
                continue
            sheet = ET.fromstring(archive.read(f"xl/{target}"))
            rows = sheet.findall(".//a:sheetData/a:row", XML_NS)
            current = {
                "A": "",
                "B": "",
                "C": "",
                "E": "",
                "G": "",
            }
            for row in rows[1:]:
                cells = _row_to_cells(row, shared_strings)
                for column in current:
                    if cells.get(column):
                        current[column] = cells[column]
                hazard_desc = (cells.get("D") or "").strip()
                if not hazard_desc:
                    continue
                checker_scope = (cells.get("E") or current["E"]).strip()
                evidence_text = (cells.get("H") or "").strip()
                evidence_objects = [
                    item.strip()
                    for item in re.split(r"[\u3001\uff0c,]", evidence_text)
                    if item.strip()
                ]
                rules.append(
                    HazardRule(
                        project_id=project_id,
                        rule_id=f"rule-{counter:03d}",
                        domain="industrial-inspection",
                        category=current["A"] or sheet_name,
                        object_name=current["B"],
                        check_item=current["C"],
                        checker_scope=checker_scope,
                        hazard_desc=hazard_desc,
                        legal_basis=(cells.get("G") or current["G"]).strip(),
                        evidence_objects_json=json.dumps(evidence_objects, ensure_ascii=False),
                        severity=_infer_severity(hazard_desc),
                        visual_detectable=_infer_visual_detectable(hazard_desc, checker_scope, evidence_text),
                        source=f"xlsx:{sheet_name}",
                    )
                )
                counter += 1
    return rules


def _parse_docx_rules(standards_dir: Path, project_id: int, existing_desc: set[str]) -> list[HazardRule]:
    docx_files = sorted(standards_dir.glob("*.docx"))
    if not docx_files:
        return []

    path = docx_files[0]
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))

    lines: list[str] = []
    for paragraph in root.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        text = "".join(node.text or "" for node in paragraph.iterfind(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        text = text.strip()
        if text:
            lines.append(text)

    rules: list[HazardRule] = []
    counter = 1000
    for line in lines:
        if line in existing_desc:
            continue
        if not re.match(r"^[\uFF08(].+", line):
            continue
        rules.append(
            HazardRule(
                project_id=project_id,
                rule_id=f"rule-{counter:03d}",
                domain="industrial-inspection",
                category="major-hazard-standard",
                object_name="hazard-scene",
                check_item="major-hazard",
                checker_scope="mixed",
                hazard_desc=line,
                legal_basis=path.stem,
                evidence_objects_json="[]",
                severity="critical",
                visual_detectable=_infer_visual_detectable(line, "", ""),
                source="docx",
            )
        )
        counter += 1
    return rules


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    result: list[str] = []
    for item in root.findall("a:si", XML_NS):
        text = "".join(node.text or "" for node in item.iterfind(".//a:t", XML_NS))
        result.append(text)
    return result


def _read_workbook_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.find("a:sheets", XML_NS) or []:
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rel_map.get(rel_id)
        if target:
            sheets.append((sheet.attrib.get("name", "Sheet"), target))
    return sheets


def _row_to_cells(row: ET.Element, shared_strings: list[str]) -> dict[str, str]:
    cells: dict[str, str] = {}
    for cell in row.findall("a:c", XML_NS):
        reference = cell.attrib.get("r", "")
        column = "".join(char for char in reference if char.isalpha())
        value_node = cell.find("a:v", XML_NS)
        if value_node is None:
            cells[column] = ""
            continue
        value = value_node.text or ""
        if cell.attrib.get("t") == "s" and value:
            value = shared_strings[int(value)]
        cells[column] = value
    return cells


def _infer_severity(text: str) -> str:
    if "\u2605" in text or text.startswith("*") or "\u91cd\u5927" in text:
        return "critical"
    if "\u672a\u8bbe\u7f6e" in text or "\u4e0d\u7b26\u5408" in text or "\u7f3a\u635f" in text:
        return "high"
    return "medium"


def _infer_visual_detectable(hazard_desc: str, checker_scope: str, evidence_text: str) -> bool:
    merged = " ".join([hazard_desc, checker_scope, evidence_text]).lower()
    if "\u5de1\u68c0\u8f66" in checker_scope:
        return True
    if any(keyword in merged for keyword in POSITIVE_VISUAL_KEYWORDS):
        return True
    if any(keyword in hazard_desc for keyword in POSITIVE_VISUAL_CJK):
        return True
    if any(keyword in hazard_desc for keyword in NEGATIVE_VISUAL_CJK):
        return False
    return False

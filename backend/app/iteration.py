"""审核迭代规则的发布快照与审核提示词注入。"""
from __future__ import annotations

from typing import Any

from backend.app.database import SessionLocal
from backend.app import models


def rule_snapshot(rule: models.ReviewRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "title": rule.title,
        "category": rule.category,
        "product_unit": rule.product_unit or "",
        "applicable_conditions": rule.applicable_conditions or "",
        "rule_text": rule.rule_text,
        "standard_ref": rule.standard_ref or "",
        "source_correction_id": rule.source_correction_id,
    }


def _rule_applies_to_family(rule: dict[str, Any], standard_family: str | None) -> bool:
    if not standard_family:
        return True
    product_unit = str(rule.get("product_unit", "")).lower()
    if any(marker in product_unit for marker in ("pvc", "聚氯乙烯")):
        return standard_family == "pvc"
    if any(marker in product_unit for marker in ("橡套", "橡皮")):
        return standard_family == "rubber"
    text = " ".join(str(rule.get(key, "")) for key in (
        "title", "category", "product_unit", "applicable_conditions", "rule_text", "standard_ref"
    )).lower()
    pvc = any(marker in text for marker in ("pvc", "5023", "8734", "60227", "rvv", "聚氯乙烯"))
    rubber = any(marker in text for marker in ("5013", "8735", "60245", "yzw", "ycw", "ie4", "se4", "橡套", "橡皮"))
    if pvc and not rubber:
        return standard_family == "pvc"
    if rubber and not pvc:
        return standard_family == "rubber"
    return True


def get_active_rules_context(standard_family: str | None = None) -> tuple[str, dict[str, Any]]:
    """读取当前活动版本，并生成只含已发布规则的提示词文本。"""
    db = SessionLocal()
    try:
        version = (
            db.query(models.ReviewVersion)
            .filter(models.ReviewVersion.status == "active")
            .order_by(models.ReviewVersion.version_no.desc())
            .first()
        )
        if not version:
            return "", {"version_id": None, "version_no": None, "version_name": "基础版本", "rule_count": 0}
        all_rules = version.rules_json or []
        rules = [rule for rule in all_rules if _rule_applies_to_family(rule, standard_family)]
        lines = [
            "以下规则来自管理员确认并发布的人工纠错，优先级高于通用经验。",
            "必须先核对适用产品单元和适用条件，不满足条件时不得套用。",
        ]
        for index, rule in enumerate(rules, start=1):
            lines.append(f"\n规则{index}：{rule.get('title', '')}")
            if rule.get("product_unit"):
                lines.append(f"适用产品单元：{rule['product_unit']}")
            if rule.get("applicable_conditions"):
                lines.append(f"适用条件：{rule['applicable_conditions']}")
            lines.append(f"正确审核规则：{rule.get('rule_text', '')}")
            if rule.get("standard_ref"):
                lines.append(f"标准依据：{rule['standard_ref']}")
        return "\n".join(lines), {
            "version_id": version.id,
            "version_no": version.version_no,
            "version_name": version.name,
            "rule_count": len(rules),
        }
    finally:
        db.close()


def issue_count(result: dict | None) -> int:
    return sum(
        1
        for sample in (result or {}).get("samples", [])
        for item in sample.get("items", [])
        if item.get("severity") in ("must_fix", "suggestion")
    )

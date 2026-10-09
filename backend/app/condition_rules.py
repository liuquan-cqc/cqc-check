"""可执行矩阵条件：从样品证据提取事实，并以 true/false/unknown 三态求值。"""
from __future__ import annotations

import math
import json
import re
from typing import Any

from sqlalchemy import bindparam, inspect, text

from backend.app.database import engine
from backend.app.structural_facts import spec_facts, reconcile_facts


TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(float(value)) else None
    matched = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(?:mm(?:²|2)?|毫米)?\s*", str(value or ""), re.I)
    return float(matched.group(1)) if matched else None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "yes", "1", "是"}:
        return True
    if normalized in {"false", "no", "0", "否"}:
        return False
    return None


def _spec_facts(spec: str) -> tuple[int | None, float | None]:
    return spec_facts(spec)


def extract_condition_facts(sample: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    sources: dict[str, str] = {}

    core_count = next((_float(sample.get(key)) for key in ("core_count", "cores") if _float(sample.get(key)) is not None), None)
    section = next((_float(sample.get(key)) for key in ("nominal_section_mm2", "section_mm2", "section") if _float(sample.get(key)) is not None), None)
    spec_core, spec_section = _spec_facts(str(sample.get("spec") or ""))
    if core_count is None and spec_core is not None:
        core_count, sources["core_count"] = float(spec_core), "sample.spec"
    if section is None and spec_section is not None:
        section, sources["nominal_section_mm2"] = spec_section, "sample.spec"
    if core_count is not None:
        facts["core_count"] = int(core_count)
        sources.setdefault("core_count", "sample.explicit")
    if section is not None:
        facts["nominal_section_mm2"] = section
        sources.setdefault("nominal_section_mm2", "sample.explicit")

    for key in ("outer_diameter_mm", "diameter_mm"):
        value = _float(sample.get(key))
        if value is not None:
            facts["outer_diameter_mm"] = value
            sources["outer_diameter_mm"] = f"sample.{key}"
            break
    for key in ("insulated_core_diameter_mm", "core_outer_diameter_mm"):
        value = _float(sample.get(key))
        if value is not None:
            facts["insulated_core_diameter_mm"] = value
            sources["insulated_core_diameter_mm"] = f"sample.{key}"
            break
    value = _float(sample.get("short_axis_mm"))
    if value is not None:
        facts["short_axis_mm"] = value
        facts["shape"] = "flat"
        sources["short_axis_mm"] = "sample.short_axis_mm"
        sources["shape"] = "sample.short_axis_mm"

    shape_text = " ".join(str(sample.get(key) or "") for key in ("shape", "model", "spec"))
    compact_model = re.sub(r"[\s()（）_-]", "", str(sample.get("model") or "").upper())
    if "扁形" in shape_text or "扁平" in shape_text or re.search(r"(?:YZWB|YZB)$", compact_model):
        facts["shape"], sources["shape"] = "flat", "sample.text"
    elif str(sample.get("shape") or "").strip().lower() in {"round", "圆形"}:
        facts["shape"], sources["shape"] = "round", "sample.shape"
    elif re.search(r"(?:YQW?|YZW?|YCW?|RVVP1?|RVVPS|BL?VV)$", compact_model):
        facts["shape"], sources["shape"] = "round", "sample.model"

    for check in sample.get("checks") or []:
        item = str(check.get("item") or "")
        reported = str(check.get("reported") or "")
        numbers = [float(value) for value in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", reported)]
        insulated_core_match = re.search(
            r"绝缘线芯(?:平均)?外径\D{0,12}(\d+(?:\.\d+)?)\s*mm",
            reported,
        )
        if "绝缘线芯" in item and "外径" in item and len(numbers) == 1:
            facts["insulated_core_diameter_mm"] = numbers[0]
            sources["insulated_core_diameter_mm"] = "checks.reported"
        elif insulated_core_match:
            facts["insulated_core_diameter_mm"] = float(insulated_core_match.group(1))
            sources["insulated_core_diameter_mm"] = "checks.reported.explicit_phrase"
        elif ("外形尺寸" in item or "短轴" in item) and len(numbers) == 2:
            facts["short_axis_mm"] = min(numbers)
            facts["shape"] = "flat"
            sources["short_axis_mm"] = sources["shape"] = "checks.reported"
        elif "外径" in item and len(numbers) == 1 and "outer_diameter_mm" not in facts:
            facts["outer_diameter_mm"] = numbers[0]
            sources["outer_diameter_mm"] = "checks.reported"
            if "shape" not in facts:
                facts["shape"] = "round"
                sources["shape"] = "checks.item"

    concentric = _bool(sample.get("concentric_multilayer"))
    combined_text = " ".join(
        str(sample.get(key) or "") for key in ("model", "spec", "construction", "description")
    )
    if concentric is None and re.search(r"(?:两层以上|多层).*同心|同心.*(?:两层以上|多层)", combined_text):
        concentric = True
    if concentric is not None:
        facts["concentric_multilayer"] = concentric
        sources["concentric_multilayer"] = "sample.explicit_or_text"

    facts["_sources"] = sources
    return reconcile_facts(sample, facts)


def evaluate_condition(node: dict[str, Any], facts: dict[str, Any]) -> str:
    op = str(node.get("op") or "").lower()
    if op in {"all", "any"}:
        states = [evaluate_condition(rule, facts) for rule in node.get("rules") or []]
        if not states:
            return UNKNOWN
        if op == "all":
            return FALSE if FALSE in states else UNKNOWN if UNKNOWN in states else TRUE
        return TRUE if TRUE in states else UNKNOWN if UNKNOWN in states else FALSE
    if op == "not":
        state = evaluate_condition(node.get("rule") or {}, facts)
        return FALSE if state == TRUE else TRUE if state == FALSE else UNKNOWN

    field = str(node.get("field") or "")
    if not field or field not in facts or field in (facts.get('_fact_conflicts') or {}):
        return UNKNOWN
    actual, expected = facts[field], node.get("value")
    try:
        if op in {"<", "<=", ">", ">="}:
            if isinstance(actual, bool) or isinstance(expected, bool):
                return UNKNOWN
            left, right = float(actual), float(expected)
            if not math.isfinite(left) or not math.isfinite(right):
                return UNKNOWN
            matched = {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
        elif op == "==":
            matched = actual == expected
        elif op == "in":
            matched = actual in (expected or [])
        elif op == "not_in":
            matched = actual not in (expected or [])
        elif op == "between":
            lower, upper = node.get("min"), node.get("max")
            matched = float(lower) <= float(actual) <= float(upper)
        else:
            return UNKNOWN
    except (TypeError, ValueError):
        return UNKNOWN
    return TRUE if matched else FALSE


def load_active_matrix_conditions(matrix_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not matrix_ids or not inspect(engine).has_table("matrix_condition_rules"):
        return {}
    statement = text("""
        SELECT id, matrix_id, condition_json, evidence_ref, version_no
        FROM matrix_condition_rules
        WHERE verification_status = 'verified' AND enabled_for_review = true
          AND matrix_id IN :matrix_ids
        ORDER BY matrix_id, version_no DESC, id DESC
    """).bindparams(bindparam("matrix_ids", expanding=True))
    with engine.connect() as connection:
        rows = connection.execute(statement, {"matrix_ids": matrix_ids}).mappings()
        active: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            if isinstance(item.get("condition_json"), str):
                try:
                    item["condition_json"] = json.loads(item["condition_json"])
                except json.JSONDecodeError:
                    item["condition_json"] = {}
            active.setdefault(int(row["matrix_id"]), item)
        return active

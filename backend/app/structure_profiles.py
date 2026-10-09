"""已验证结构档案的运行时读取与三态条件计算。

安全边界：只读取 verification_status='verified' 且
enabled_for_review=true 的档案。档案禁用时返回空结果，不改变现有审核输出。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text

from backend.app.condition_rules import evaluate_condition, extract_condition_facts
from backend.app.structural_facts import spec_facts, reconcile_facts
from backend.app.database import engine


CATALOG_PATH = Path(__file__).with_name("structure_profile_conditions.json")


def normalize_model(value: object) -> str:
    model = str(value or "").upper().strip()
    model = re.sub(r"^(?:ZA|ZB|ZC|ZD)-", "", model)
    model = model.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", model)


def _reported(check: dict[str, Any]) -> str:
    return str(check.get("reported") or check.get("reported_value") or "").strip()


def _verdict(check: dict[str, Any]) -> str:
    value = str(check.get("verdict") or check.get("status") or "").lower()
    if value in {"pass", "p", "ok", "qualified"}:
        return "pass"
    if value in {"fail", "f", "error", "unqualified"}:
        return "fail"
    if value in {"n", "na", "n/a", "not_applicable"}:
        return "not_applicable"
    return "unknown"


def _not_applicable(value: str) -> bool:
    normalized = str(value or "").strip().lower().replace("／", "/")
    normalized = re.sub(r"[\s,，;；]", "", normalized)
    return normalized in {"", "-", "—", "/", "n", "na", "n/a", "不适用", "未进行", "未做"} or bool(
        re.fullmatch(r"[/\-—]*(?:n|na|n/a)?", normalized)
    )


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "有", "是"}:
        return True
    if normalized in {"0", "false", "no", "none", "无", "否"}:
        return False
    return None


def _set_fact(
    facts: dict[str, Any], sources: dict[str, str], field: str, value: Any, source: str,
) -> None:
    if value is None:
        return
    if field in facts and facts[field] != value:
        facts.pop(field, None)
        sources[field] = f"conflict:{sources.get(field, 'unknown')}|{source}"
        return
    if not str(sources.get(field, "")).startswith("conflict:"):
        facts[field] = value
        sources[field] = source


def _spec_facts(spec: str) -> tuple[int | None, float | None]:
    return spec_facts(spec)


def _parallel_value(item: str, value: str, labels: tuple[str, ...]) -> str | None:
    item_parts = [part.strip() for part in re.split(r"[;,；，]", item) if part.strip()]
    value_parts = [part.strip() for part in re.split(r"[;；]", value) if part.strip()]
    if len(item_parts) != len(value_parts):
        return None
    for index, label in enumerate(item_parts):
        if any(expected in label for expected in labels):
            return value_parts[index]
    return None


def extract_structure_facts(sample: dict[str, Any], ocr_text: str = "") -> dict[str, Any]:
    """从型号、规格和已定位核对项提取结构事实。

    合并字段只按并行标签取值，不允许把 30×0.12 的 30 当作外径。
    同一事实出现冲突时删除该事实，使后续条件保持 unknown。
    """
    facts: dict[str, Any] = {}
    sources: dict[str, str] = {}
    model = normalize_model(sample.get("model"))
    core_count, section = _spec_facts(str(sample.get("spec") or ""))
    _set_fact(facts, sources, "core_count", core_count, "sample.spec")
    _set_fact(facts, sources, "nominal_section_mm2", section, "sample.spec")

    defaults = {
        normalize_model("60227 IEC 10(BVV)"): {"has_sheath": True, "shape": "round"},
        normalize_model("60227 IEC 71c(TVV)"): {"has_sheath": True, "shape": "round"},
        normalize_model("60227 IEC 71f(TVVB)"): {"has_sheath": True, "shape": "flat"},
        normalize_model("60227 IEC 74(RVVYP)"): {
            "has_sheath": True, "has_shield": True, "has_inner_sheath": True,
        },
        normalize_model("60227 IEC 75(RVVY)"): {
            "has_sheath": True, "has_shield": False, "has_inner_sheath": False,
        },
        normalize_model("AV"): {"has_sheath": False},
        normalize_model("AV-90"): {"has_sheath": False},
        normalize_model("AVR"): {"has_sheath": False},
        normalize_model("AVR-90"): {"has_sheath": False},
        normalize_model("AVRB"): {"has_sheath": False},
        normalize_model("AVRS"): {"has_sheath": False},
        normalize_model("AVVR"): {"has_sheath": True},
    }
    for field, value in defaults.get(model, {}).items():
        _set_fact(facts, sources, field, value, "verified_model_definition")

    combined_text = "；".join(
        str(sample.get(key) or "")
        for key in ("construction", "description", "product_name", "sample_name")
    )
    intended_use = str(sample.get("intended_use") or "").strip().lower()
    use_map = {
        "elevator": "elevator", "电梯": "elevator", "电梯电缆": "elevator",
        "flexible_connection": "flexible_connection", "挠性连接": "flexible_connection",
        "挠性连接电缆": "flexible_connection",
    }
    if intended_use in use_map:
        _set_fact(facts, sources, "intended_use", use_map[intended_use], "sample.explicit")
    else:
        elevator_use = bool(re.search(r"用于电梯|电梯用电缆|用途[：:]?\s*电梯", combined_text))
        connection_use = bool(re.search(r"用于挠性连接|挠性连接用电缆|用途[：:]?\s*挠性连接", combined_text))
        if "电梯电缆" in combined_text and "挠性连接" in combined_text:
            elevator_use = connection_use = True
        if elevator_use != connection_use:
            _set_fact(
                facts, sources, "intended_use",
                "elevator" if elevator_use else "flexible_connection",
                "sample.unambiguous_text",
            )

    tensile_member = _bool(sample.get("has_tensile_member"))
    tensile_positive = bool(re.search(
        r"(?<!不)(?:设有|具有|含有?|带有?)\s*(?:承拉|承力)元件",
        combined_text,
    ))
    tensile_negative = bool(re.search(r"(?:无|不含|未设)\s*(?:承拉|承力)元件", combined_text))
    if re.search(r"(?:有[/／或]无|设有或不设).{0,8}(?:承拉|承力)元件", combined_text):
        tensile_positive = tensile_negative = True
    if tensile_member is None and tensile_positive != tensile_negative:
        tensile_member = tensile_positive
    _set_fact(
        facts, sources, "has_tensile_member", tensile_member,
        "sample.explicit_or_unambiguous_text",
    )

    checks = list(sample.get("checks") or [])
    for check in checks:
        item = str(check.get("item") or "")
        category = str(check.get("category") or "")
        value = _reported(check)
        state = _verdict(check)
        is_structure = "结构" in category or "结构" in item or any(
            marker in item for marker in ("外径", "外形尺寸", "屏蔽", "承拉元件", "承力元件")
        )
        if is_structure:
            dimension_value = _parallel_value(item, value, ("平均外径", "外径", "外形尺寸")) or value
            simple_item = bool(re.fullmatch(r"\s*(?:平均)?外径(?:[-—]*平均外径)?(?:测量)?\s*|外形尺寸\s*", item))
            pair = re.search(
                r"(?:外径|外形尺寸)[^\d]{0,8}(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(?:mm)?",
                dimension_value,
            )
            if not pair and (dimension_value != value or simple_item):
                pair = re.search(
                    r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(?:mm)?",
                    dimension_value,
                )
            if pair:
                _set_fact(facts, sources, "short_axis_mm", min(float(pair.group(1)), float(pair.group(2))), "check.flat_dimensions")
                _set_fact(facts, sources, "shape", "flat", "check.flat_dimensions")
            else:
                single = re.search(
                    r"(?:平均外径|外径|外形尺寸)[^\d]{0,10}(\d+(?:\.\d+)?)\s*(?:mm)?",
                    dimension_value,
                )
                if not single and dimension_value != value:
                    single = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm)?", dimension_value)
                if not single and simple_item:
                    single = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm)?", value)
                if single:
                    _set_fact(facts, sources, "outer_diameter_mm", float(single.group(1)), "check.outer_diameter")
                    # 单一外径是圆形证据；若与型号或双轴尺寸的扁形证据
                    # 冲突，_set_fact 会删除 shape，让适用性保持 unknown。
                    _set_fact(facts, sources, "shape", "round", "check.single_diameter")

            if "屏蔽" in item:
                if state == "pass" and not _not_applicable(value):
                    _set_fact(facts, sources, "has_shield", True, "check.shield")
                elif state == "not_applicable" or _not_applicable(value):
                    _set_fact(facts, sources, "has_shield", False, "check.shield_na")
            if any(marker in item for marker in ("承拉元件", "承力元件")):
                if state == "pass" and not _not_applicable(value):
                    _set_fact(facts, sources, "has_tensile_member", True, "check.tensile_member")
                elif state == "not_applicable" or _not_applicable(value):
                    _set_fact(facts, sources, "has_tensile_member", False, "check.tensile_member_na")

    # OCR兜底只由调用方在单一样品报告中传入。必须是同一行明确的承拉/承力
    # 元件试验N，不能从产品名称、覆盖型号或多样品公用文字推断结构。
    if ocr_text and re.search(
        r"(?:承拉|承力)元件(?:的)?抗拉强度[^\r\n]*?(?:[/／]\s*)?N(?:\s|$)",
        ocr_text,
        re.I,
    ):
        _set_fact(
            facts, sources, "has_tensile_member", False,
            "ocr.single_sample_explicit_tensile_member_na_row",
        )

    facts["_sources"] = sources
    return reconcile_facts(sample, facts)


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload.get("profiles"), dict):
        raise ValueError("结构条件目录格式无效")
    return payload


def _active_bindings(connection) -> list[dict[str, Any]]:
    inspector = inspect(connection)
    if not inspector.has_table("structure_rule_profiles") or not inspector.has_table("structure_rule_profile_models"):
        return []
    return [dict(row) for row in connection.execute(text("""
        SELECT p.id AS profile_id,p.profile_code,p.profile_kind,p.version_no,
               m.model_code,m.standard_no
        FROM structure_rule_profiles p
        JOIN structure_rule_profile_models m ON m.profile_id=p.id
        WHERE p.verification_status='verified'
          AND p.enabled_for_review=true
        ORDER BY p.id,m.id
    """)).mappings()]


def _merge_facts(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    sources = dict(base.get("_sources") or {})
    conflicts = {**(base.get('_fact_conflicts') or {}), **(extra.get('_fact_conflicts') or {})}
    blocked = set(conflicts) | {
        key for data in (base, extra) for key, value in (data.get('_sources') or {}).items()
        if str(value).startswith('conflict:')
    }
    for field in blocked:
        merged.pop(field, None)
        sources[field] = 'conflict:structural_input'
    for field, value in extra.items():
        if field.startswith('_') or field in blocked or value is None:
            continue
        if field in merged and merged[field] != value:
            conflicts[field] = [merged.pop(field), value]
            sources[field] = "conflict:existing_condition_fact|structure_profile"
            continue
        merged[field] = value
        sources[field] = str((extra.get("_sources") or {}).get(field) or "structure_profile")
    merged["_sources"] = sources
    if conflicts:
        merged["_profile_conflicts"] = conflicts
        merged['_fact_conflicts'] = conflicts
    return merged


def active_profile_facts(
    connection, sample: dict[str, Any], model_code: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    target = normalize_model(model_code or sample.get("model"))
    matches = [
        row for row in _active_bindings(connection)
        if normalize_model(row.get("model_code")) == target
    ]
    if not matches:
        return {}, []
    profile_codes = sorted({str(row["profile_code"]) for row in matches})
    return extract_structure_facts(sample), profile_codes


def merge_active_profile_facts(
    connection, sample: dict[str, Any], base_facts: dict[str, Any], model_code: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    profile_facts, profile_codes = active_profile_facts(connection, sample, model_code)
    if not profile_codes:
        return base_facts, []
    return _merge_facts(base_facts, profile_facts), profile_codes


def apply_active_structure_profile_conditions(
    result: dict[str, Any], source_text: str = "", standard_family: str | None = None,
    *, connection=None,
) -> dict[str, Any]:
    """计算已启用档案的结构条件，仅写入确定性验证元数据。"""
    close_connection = connection is None
    connection = connection or engine.connect()
    try:
        bindings = _active_bindings(connection)
        if not bindings:
            return result
        catalog = _catalog()
        records: list[dict[str, Any]] = []
        bindings_by_model: dict[str, set[str]] = {}
        for row in bindings:
            bindings_by_model.setdefault(normalize_model(row.get("model_code")), set()).add(str(row["profile_code"]))

        samples = list(result.get("samples") or [])
        single_sample_ocr = source_text if len(samples) == 1 else ""
        for sample in samples:
            model = normalize_model(sample.get("model"))
            profile_codes = sorted(bindings_by_model.get(model) or [])
            if not profile_codes:
                continue
            check_text = json.dumps(sample.get("checks") or [], ensure_ascii=False)
            if "变更报告" in check_text and ("无型式试验数据" in check_text or "无需重复型式试验" in check_text):
                continue
            facts = _merge_facts(
                extract_condition_facts(sample),
                extract_structure_facts(sample, single_sample_ocr),
            )
            for profile_code in profile_codes:
                for rule in (catalog.get("profiles") or {}).get(profile_code, []):
                    if normalize_model(rule.get("model_code")) != model:
                        continue
                    state = evaluate_condition(rule.get("condition_json") or {}, facts)
                    records.append({
                        "sample": " ".join(str(sample.get(key) or "") for key in ("model", "voltage", "spec")).strip(),
                        "model": str(sample.get("model") or ""),
                        "profile_code": profile_code,
                        "profile_catalog_version": catalog.get("version"),
                        "item_code": rule.get("item_code"),
                        "item": rule.get("item_name"),
                        "condition_state": state,
                        "condition": rule.get("applicable_conditions"),
                        "standard": f"{rule.get('standard_no') or ''} {rule.get('source_table') or ''} 项次{rule.get('table_item_no') or ''}".strip(),
                        "condition_facts": {
                            key: value for key, value in facts.items()
                            if not key.startswith("_")
                        },
                        "fact_conflicts": facts.get("_profile_conflicts") or {},
                    })
        if records:
            result.setdefault("_deterministic_validation", {})["structure_profile_conditions"] = {
                "mode": "observation_only",
                "records": records,
                "state_counts": {
                    state: sum(1 for row in records if row["condition_state"] == state)
                    for state in ("true", "false", "unknown")
                },
            }
        return result
    except Exception as exc:
        result.setdefault("_deterministic_validation", {})["structure_profile_conditions"] = {
            "mode": "observation_only",
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }
        return result
    finally:
        if close_connection:
            connection.close()

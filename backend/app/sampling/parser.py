"""将企业申请范围文本确定性解析为结构化型号范围。

本模块只做型号目录匹配和规格字段提取，不参与下样决策，也不调用AI。
无法可靠识别的内容必须原样返回给管理员确认。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .rules import SamplingRuleRepository, get_sampling_repository


SPLIT_RE = re.compile(r"[;；\r\n]+")
# 企业常省略额定电压后的 V，例如“300/300 0.5-0.75”。
# 识别时允许省略，规整输出仍使用型号目录中的“300/300V”。
VOLTAGE_RE = re.compile(r"\b\d+\s*/\s*\d+\s*V?\b", re.IGNORECASE)
CORE_GROUP_RE = re.compile(r"\(([^()]*(?:芯|core)[^()]*)\)", re.IGNORECASE)
NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
SHAPE_WORDS = {
    "圆形": "round",
    "扁形": "flat",
    "绞合": "twisted",
    "平行": "parallel",
    "成组": "grouped",
}


def unsupported_scope_conditions(text: str) -> list[str]:
    issues = []
    if re.search(r"(?:^|[;；\n])\s*(?:Z[A-D]|ZR|WDZ[A-D]?|NH)\s*[-－]", _normalize(text), re.IGNORECASE):
        issues.append("申请含阻燃或耐火前缀，现有规则不能自动完成附加送样，请转人工，不能按普通型号生成")
    if re.search(r"透明|共挤|多层|(?:红|黄|绿|蓝|白|黑|棕|橙|灰|紫)色|内白|外红", text):
        issues.append("申请含限定颜色、透明或多层条件，需人工确认专项送样，不能套用默认黑白方案")
    return issues


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    return (value.replace("–", "-").replace("—", "-").replace("－", "-")
            .replace("～", "-").replace("~", "-").replace("＋", "+")
            .replace("＊", "*").replace("／", "/").strip())


def _key(text: str) -> str:
    return re.sub(r"\s+", "", _normalize(text)).upper()


def _bounds(model: dict[str, Any], field: str) -> tuple[float | int | None, float | int | None]:
    values: list[float | int] = []
    for group in model.get("specification_groups", []):
        spec = group.get(field) or {}
        values.extend(spec.get("values") or [])
        if isinstance(spec.get("min"), (int, float)):
            values.append(spec["min"])
        if isinstance(spec.get("max"), (int, float)):
            values.append(spec["max"])
    return (min(values), max(values)) if values else (None, None)


def _allowed_values(model: dict[str, Any], field: str) -> set[float | int]:
    values: set[float | int] = set()
    for group in model.get("specification_groups", []):
        spec = group.get(field) or {}
        values.update(spec.get("values") or [])
    return values


def _uses_only_exact_values(model: dict[str, Any], field: str) -> bool:
    """仅当型号所有规格段都给出明确档位时，才校验端点是否落档。

    AVVR一类型号同时含范围段和特殊结构段。若把特殊结构段中的单个
    ``values`` 与普通范围段混在一起，会把合法的0.08误报为“未确认档位”。
    """
    dimensions = [
        group.get(field) or {}
        for group in model.get("specification_groups", [])
        if not group.get("special_expression")
    ]
    return bool(dimensions) and all(
        bool(dimension.get("values"))
        and "min" not in dimension
        and "max" not in dimension
        and not dimension.get("unspecified")
        for dimension in dimensions
    )


def _model_shapes(model: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        shape
        for group in model.get("specification_groups", [])
        for shape in group.get("shapes", [])
    ))


def _models(repository: SamplingRuleRepository) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for unit in repository.catalog():
        for source in unit.get("models", []):
            model = dict(source)
            model["unit_code"] = str(unit["unit_code"]).zfill(2)
            rows.append(model)
    return rows


def _match_model(entry: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
    entry_key = _key(entry)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for model in models:
        names = [(model.get("display_name") or "", 2)]
        names.extend((alias, 1) for alias in model.get("aliases", []) if alias)
        for name, exact_weight in names:
            name_key = _key(name)
            if name_key and name_key in entry_key:
                candidates.append((len(name_key), exact_weight, model))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _core_values(specification: str) -> tuple[list[int], str]:
    values: list[int] = []
    for matched in CORE_GROUP_RE.finditer(specification):
        group = matched.group(1)
        product_match = re.search(r"(\d+)\s*[x×*]\s*(\d+)\s*芯", group, re.IGNORECASE)
        if product_match:
            values.append(int(product_match.group(1)) * int(product_match.group(2)))
            continue
        for core_match in re.finditer(r"(\d+)(?:\s*-\s*(\d+))?\s*芯", group, re.IGNORECASE):
            values.append(int(core_match.group(1)))
            if core_match.group(2):
                values.append(int(core_match.group(2)))
    return values, CORE_GROUP_RE.sub(" ", specification)


def _entry_specification(entry: str, model: dict[str, Any]) -> str:
    voltage_match = VOLTAGE_RE.search(entry)
    if voltage_match:
        return entry[voltage_match.end():].strip(" ,，")
    value = entry
    for name in [model.get("display_name") or "", *(model.get("aliases") or [])]:
        if name and name.lower() in value.lower():
            value = re.sub(re.escape(name), "", value, count=1, flags=re.IGNORECASE)
            break
    return value.strip(" ,，")


def _clean_scope_fragment(text: str) -> str:
    """只规整书写形式，不合并规格段或推断新的申请范围。"""
    value = _normalize(text).strip(" ,，;；")
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"(?<=[\d)])\s*[xX×*]\s*(?=\d)", "×", value)
    value = re.sub(r"\s*\+\s*", "+", value)
    value = re.sub(r"\s*\(\s*", "(", value)
    value = re.sub(r"\s*\)\s*", ")", value)
    # 企业常省略多段规格之间的逗号，例如
    # ``0.08-0.4(2芯) 0.12-0.4(3-8芯)``。规整时补回分段符，
    # 防止复制结果把两个合法规格段粘连在一起。
    value = re.sub(r"\)(?=\d)", "), ", value)
    value = re.sub(r"\s*[,，]\s*", ", ", value)
    value = re.sub(r"\s*芯", "芯", value)
    return re.sub(r"\s+", " ", value).strip()


def _clean_specification_fragment(text: str) -> str:
    """规整型号规格并移除重复单位。

    截面单位在页面标题和规则字段中已明确，复制范围统一保留
    数值本身，避免“0.5-0.75mm2”与系统标准表达混用。
    """
    value = re.sub(r"\s*(?:mm\s*(?:2|²)|平方毫米)", "", text, flags=re.IGNORECASE)
    return _clean_scope_fragment(value)


def _validate_range(
    model: dict[str, Any], field: str, low: float | int | None, high: float | int | None,
    label: str, issues: list[str],
) -> None:
    allowed_low, allowed_high = _bounds(model, field)
    if low is None or high is None or allowed_low is None or allowed_high is None:
        return
    if low < allowed_low or high > allowed_high:
        issues.append(f"{label}{low:g}-{high:g}超出型号目录范围{allowed_low:g}-{allowed_high:g}")
        return
    exact_values = _allowed_values(model, field)
    if _uses_only_exact_values(model, field) and (low not in exact_values or high not in exact_values):
        issues.append(f"{label}端点不在已确认规格档中")


def _parse_entry(raw_entry: str, models: list[dict[str, Any]]) -> dict[str, Any]:
    entry = _normalize(raw_entry)
    model = _match_model(entry, models)
    if not model:
        return {
            "raw_text": raw_entry.strip(),
            "normalized_text": _clean_scope_fragment(raw_entry),
            "status": "unrecognized",
            "issues": ["未识别到规则库中的型号"],
        }

    issues: list[str] = []
    blocking_issues = unsupported_scope_conditions(entry)
    issues.extend(blocking_issues)
    voltage_match = VOLTAGE_RE.search(entry)
    input_voltage = re.sub(r"\s+", "", voltage_match.group(0).upper()) if voltage_match else ""
    if input_voltage and not input_voltage.endswith("V"):
        input_voltage += "V"
    model_voltage = re.sub(r"\s+", "", str(model.get("voltage") or "").upper())
    if not voltage_match:
        issues.append("未写明额定电压，已按型号目录电压填入")
    elif input_voltage != model_voltage:
        issues.append(f"输入电压{input_voltage}与型号目录{model_voltage}不一致")

    specification = _entry_specification(entry, model)
    normalized_specification = _clean_specification_fragment(specification)
    core_values, section_source = _core_values(specification)
    include_special = bool(re.search(r"[×*].*\+|\+.*[×*]", specification))
    regular_segments = [
        segment for segment in re.split(r"[,，]", section_source)
        if not re.search(r"[×*]", segment)
    ]
    section_source = " ".join(regular_segments)
    section_source = re.sub(r"mm\s*(?:2|²)|平方毫米", "", section_source, flags=re.IGNORECASE)
    section_values = [float(value) for value in NUMBER_RE.findall(section_source)]

    default_section_min, default_section_max = _bounds(model, "sections")
    default_core_min, default_core_max = _bounds(model, "cores")
    section_min = min(section_values) if section_values else default_section_min
    section_max = max(section_values) if section_values else default_section_max
    core_min = min(core_values) if core_values else default_core_min
    core_max = max(core_values) if core_values else default_core_max

    if not section_values and default_section_min is not None:
        issues.append("未明确识别截面，已填入型号目录范围")
    if not core_values and default_core_min != default_core_max:
        issues.append("未明确识别芯数，已填入型号目录范围")
    if include_special:
        issues.append("识别到不等截面或组合结构，请确认结构范围")

    _validate_range(model, "sections", section_min, section_max, "截面", issues)
    _validate_range(model, "cores", core_min, core_max, "芯数", issues)

    requested_shapes = [shape for word, shape in SHAPE_WORDS.items() if word in entry]
    allowed_shapes = _model_shapes(model)
    shapes = requested_shapes or allowed_shapes
    # 某段注明圆形，不代表同型号其他未注明的规格段也只能是圆形。
    # 延迟导入以复用本模块的书写规整方法。
    from .scope import scope_segments
    try:
        segments = scope_segments(normalized_specification)
        if segments:
            shapes = list(dict.fromkeys(shape for segment in segments for shape in (segment.shapes or allowed_shapes)))
            sections = [v for s in segments for v in (s.section_min, s.section_max) if v is not None]
            cores = [v for s in segments for v in (s.core_min, s.core_max) if v is not None]
            if sections:
                section_min, section_max = min(sections), max(sections)
            if cores:
                core_min, core_max = min(cores), max(cores)
            if any(s.special_expression for s in segments):
                include_special = True
    except ValueError as exc:
        issues.append(str(exc))
        blocking_issues.append(str(exc))
    invalid_shapes = [shape for shape in shapes if shape not in allowed_shapes]
    if invalid_shapes:
        issues.append("输入的圆扁形状与型号目录不一致")

    return {
        "raw_text": raw_entry.strip(),
        "normalized_text": " ".join(filter(None, [
            str(model.get("display_name") or "").strip(),
            str(model.get("voltage") or "").strip(),
            normalized_specification,
        ])),
        "status": "recognized" if not issues else "needs_review",
        "model_ref": model["id"],
        "model": model["display_name"],
        "voltage": model.get("voltage") or "",
        "unit_code": model["unit_code"],
        "section_min": section_min,
        "section_max": section_max,
        "core_min": core_min,
        "core_max": core_max,
        "shapes": shapes,
        "include_special": include_special,
        "scope_expression": normalized_specification,
        "issues": issues,
        "blocking_issues": blocking_issues,
        "unsupported_conditions": blocking_issues,
    }


def _merge_models(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def lower(left: Any, right: Any) -> Any:
        values = [value for value in (left, right) if value is not None]
        return min(values) if values else None

    def upper(left: Any, right: Any) -> Any:
        values = [value for value in (left, right) if value is not None]
        return max(values) if values else None

    merged: dict[str, dict[str, Any]] = {}
    for entry in entries:
        ref = entry.get("model_ref")
        if not ref:
            continue
        if ref not in merged:
            merged[ref] = {key: entry.get(key) for key in (
                "model_ref", "section_min", "section_max", "core_min", "core_max", "shapes", "include_special", "scope_expression", "unsupported_conditions"
            )}
            continue
        target = merged[ref]
        target["unsupported_conditions"] = list(dict.fromkeys([*(target.get("unsupported_conditions") or []), *(entry.get("unsupported_conditions") or [])]))
        target["section_min"] = lower(target["section_min"], entry["section_min"])
        target["section_max"] = upper(target["section_max"], entry["section_max"])
        target["core_min"] = lower(target["core_min"], entry["core_min"])
        target["core_max"] = upper(target["core_max"], entry["core_max"])
        target["shapes"] = list(dict.fromkeys([*(target.get("shapes") or []), *(entry.get("shapes") or [])]))
        target["include_special"] = bool(target.get("include_special") or entry.get("include_special"))
        expressions = [value for value in (target.get("scope_expression"), entry.get("scope_expression")) if value]
        target["scope_expression"] = ", ".join(dict.fromkeys(expressions))
    return list(merged.values())


def parse_scope_text(text: str, repository: SamplingRuleRepository | None = None) -> dict[str, Any]:
    """解析整段申请型号文字，始终保留原条目和识别问题。"""
    source = _normalize(text)
    raw_entries = [item.strip() for item in SPLIT_RE.split(source) if item.strip()]
    if not raw_entries:
        return {
            "entries": [], "models": [], "groups": [], "unit_code": None, "unit_codes": [],
            "normalized_scope_text": "",
            "recognized_count": 0, "unrecognized_count": 0, "review_count": 0,
            "can_apply": False, "issues": ["请粘贴至少一条申请型号规格"],
        }

    repo = repository or get_sampling_repository()
    entries = [_parse_entry(item, _models(repo)) for item in raw_entries]
    normalized_scope_text = "\n".join(
        f"{entry.get('normalized_text') or _clean_scope_fragment(entry.get('raw_text') or '')};"
        for entry in entries
    )
    recognized = [entry for entry in entries if entry.get("model_ref")]
    unit_codes = list(dict.fromkeys(entry["unit_code"] for entry in recognized))
    groups = [
        {
            "unit_code": unit_code,
            "models": _merge_models([entry for entry in recognized if entry["unit_code"] == unit_code]),
        }
        for unit_code in unit_codes
    ]
    issues: list[str] = []
    if len(unit_codes) > 1:
        issues.append(f"已识别为{len(unit_codes)}个产品单元，并按单元自动分组")
    unrecognized_count = sum(entry["status"] == "unrecognized" for entry in entries)
    review_count = sum(entry["status"] == "needs_review" for entry in entries)
    if unrecognized_count:
        issues.append(f"有{unrecognized_count}条未识别，系统未静默忽略")
    if review_count:
        issues.append(f"有{review_count}条需要人工确认识别结果")
    return {
        "entries": entries,
        "normalized_scope_text": normalized_scope_text,
        "models": _merge_models(recognized),
        "groups": groups,
        "unit_code": unit_codes[0] if len(unit_codes) == 1 else None,
        "unit_codes": unit_codes,
        "recognized_count": len(recognized),
        "unrecognized_count": unrecognized_count,
        "review_count": review_count,
        "can_apply": bool(recognized) and not unrecognized_count and not any(e.get("blocking_issues") for e in entries),
        "issues": issues + [issue for e in entries for issue in e.get("blocking_issues", [])],
    }

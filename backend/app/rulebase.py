"""结构化规则库定向检索。

只按报告中已经识别到的型号和产品标准取规则，避免把整库数据塞进提示词。
规则库未安装或查询失败时安全降级为空文本，不影响既有审核流程。
"""
from __future__ import annotations

import html
import re
from typing import Any

from sqlalchemy import bindparam, inspect, text

from backend.app.condition_rules import (
    FALSE, TRUE, UNKNOWN, evaluate_condition, extract_condition_facts,
    load_active_matrix_conditions,
)
from backend.app.database import engine
from backend.app.ocr_readable import present_material_check
from backend.app.ocr_loss_bridge import loss_signatures, is_loss_label


MAX_MODELS = 4
MAX_CONTEXT_CHARS = 14000

RVV_FINISHED_VOLTAGE_CONDITION = {
    "op": "any",
    "rules": [
        {
            "op": "all",
            "rules": [
                {"field": "prescribed_insulation_thickness_mm", "op": "<=", "value": 0.6},
                {"field": "rated_voltage", "op": "==", "value": "300/300"},
            ],
        },
        {"field": "prescribed_insulation_thickness_mm", "op": ">", "value": 0.6},
    ],
}

# 用于核对 AI 返回的“项目名称-标准表-项次”是否自洽。
# 只取能区分试验的语义词，不用“绝缘/护套/试验”等通用词猜测。
_ITEM_CONCEPTS = (
    ("非污染", "non_pollution"), ("绝缘线芯电压", "insul_voltage"),
    ("曲挠负载电流", "flex_current"), ("曲挠线芯间电压", "flex_voltage"),
    ("成品低温冲击", "lowtemp_impact"), ("低温冲击", "lowtemp_impact"),
    ("低温弯曲", "lowtemp_bend"), ("低温拉伸", "lowtemp_tensile"),
    ("空气烘箱", "air_oven"), ("空气弹", "air_bomb"),
    ("高温压力", "heat_press"), ("热稳定", "thermal_stability"),
    ("热冲击", "thermal_shock"), ("热延伸", "thermal_extension"),
    ("耐臭氧", "ozone"), ("浸矿物油", "oil"), ("浸油", "oil"),
    ("成束燃烧", "flame_bundle"), ("单根垂直燃烧", "flame_single"),
    ("单根阻燃性能", "flame_single"),
    ("热收缩", "heat_shrink"), ("失重", "loss_weight"),
    ("老化前", "tensile_before"), ("老化后", "tensile_after"),
    ("曲挠", "flexing"), ("导体电阻", "conductor_res"),
    ("绝缘电阻", "insul_res"), ("成品电压", "finished_voltage"),
    ("绝缘厚度", "thickness"), ("外径", "outer_diameter"),
)

_EVIDENCE_ALIASES = {
    "结构检查": (
        "结构检查", "结构尺寸检查", "电缆芯数", "标称截面",
        "受检验绝缘线芯颜色", "绝缘线芯颜色", "导体单线根数",
    ),
    "成品电压": ("成品电压", "成品电缆电压", "电压试验"),
    "标志": ("标志内容检查", "标志连续性检查", "标志耐擦性检查", "标志清晰度检查", "标志"),
    "热稳定": ("热稳定性", "热稳定"),
    "绝缘线芯电压": ("绝缘线芯电压", "绝缘线芯按规定的绝缘厚度", "绝缘线芯"),
    "绝缘低温弯曲": ("绝缘低温弯曲", "低温弯曲", "低温卷绕"),
    "护套低温弯曲": ("护套低温弯曲", "低温弯曲", "低温卷绕"),
    "绝缘厚度": ("绝缘平均厚度", "绝缘最薄处厚度", "绝缘厚度"),
    "浸矿物油": ("浸矿物油", "耐矿物油后的性能", "耐矿物油"),
    "成束阻燃": ("成束阻燃", "成束燃烧试验", "成束燃烧"),
}


def _normalized(value: Any) -> str:
    normalized = str(value or "").upper().translate(str.maketrans({"（": "(", "）": ")"}))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # IEC完整型号的空格不是型号含义：避免60227IEC 53只命中括号
    # 中的RVV别名，错误切到8734.3的通用RVV矩阵。
    normalized = re.sub(r'\b(60227|60245)\s*IEC\s*(\d{2})(?!\d)', r'\1 IEC \2', normalized)
    # OCR常在型号括号前多一个空格：53 (YZ)与53(YZ)应视为同一型号。
    return re.sub(r"\s+([(（])", r"\1", normalized)


def _model_without_flame_prefix(value: Any) -> str:
    """仅用于唯一候选对账：兼容AI偶发漏写ZB/ZC等阻燃类别前缀。"""
    return re.sub(r"^Z[A-D]-", "", _normalized(value))


def _unique_prefix_alias_registry_key(
    key: tuple[str, str, str],
    registry: list[dict[str, Any]],
    sample: dict[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    """型号前缀差异只有唯一原页候选时才允许绑定，避免按顺序猜样品。"""
    model, voltage, spec = key
    if not all(key):
        return None
    base_model = _model_without_flame_prefix(model)
    candidates = [
        entry["key"] for entry in registry
        if entry["key"][1:] == (voltage, spec)
        and _model_without_flame_prefix(entry["key"][0]) == base_model
        and entry["key"][0] != model
    ]
    if len(candidates) == 1:
        return candidates[0]

    # 同型号/电压/规格可能同时有 ZB、ZC、ZD 样品。AI偶发漏写
    # 型号前缀时，只有它自身返回了明确的“成束阻燃/2003烧 X类”证据，
    # 才用该类别在多候选中消歧。无类别或同时出现多类时仍返回 None。
    if sample and len(candidates) > 1 and not re.match(r"^Z[A-D]-", model):
        evidence_parts: list[str] = []
        for row in (sample.get("checks") or []) + (sample.get("items") or []):
            evidence_parts.extend(str(row.get(field) or "") for field in (
                "category", "item", "reported", "required", "basis", "note",
            ))
        evidence_text = " ".join(evidence_parts).upper()
        flame_classes = set(re.findall(
            r"成束(?:阻燃(?:性能)?|燃烧(?:试验)?)\D{0,24}?([A-D])\s*类",
            evidence_text,
            re.I,
        ))
        if len(flame_classes) == 1:
            expected_prefix = f"Z{next(iter(flame_classes)).upper()}-"
            class_candidates = [candidate for candidate in candidates if candidate[0].startswith(expected_prefix)]
            if len(class_candidates) == 1:
                return class_candidates[0]
    return None


def _contains_token(haystack: str, token: str) -> bool:
    token = _normalized(token)
    if not token:
        return False
    pattern = re.escape(token).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![A-Z0-9]){pattern}(?![A-Z0-9])", haystack, re.I))


def _standard_key(standard_no: str) -> str:
    match = re.search(r"(?:5013|5023|8734|8735)\.\d+", standard_no or "")
    return match.group(0) if match else ""


def _concept(value: Any, item_code: str = "") -> str:
    text_value = str(value or "")
    concept = next((code for marker, code in _ITEM_CONCEPTS if marker in text_value), "")
    if not concept:
        return ""
    code = str(item_code or "").upper()
    surface = "sheath" if "护套" in text_value or code.startswith("SHEATH_") else "insulation" if "绝缘" in text_value or code in {
        "LOSS_WEIGHT", "TENSILE_BEFORE", "TENSILE_AFTER", "HEAT_PRESS",
        "LOWTEMP_BEND_INSUL", "LOWTEMP_TENSILE_INSUL",
    } else ""
    return f"{surface}:{concept}" if surface else concept


def _citation(value: Any) -> tuple[str, str, str] | None:
    text_value = str(value or "")
    standard = _standard_key(text_value)
    table_match = re.search(r"表\s*(\d+)", text_value)
    item_match = re.search(r"(?:项次|项目)\s*(\d+(?:\.\d+)*)", text_value)
    if not (standard and table_match and item_match):
        return None
    return standard, table_match.group(1), item_match.group(1)


def _catalog_citation(row: dict[str, Any]) -> str:
    return f"{row['standard_no']} {row['table_no']} 项次{row['table_item_no']}"


def _same_issue(check: dict[str, Any], item: dict[str, Any]) -> bool:
    left = _concept(f"{check.get('category', '')}{check.get('item', '')}")
    right = _concept(item.get("item", ""))
    if left and right:
        return left == right
    a = re.sub(r"\W+", "", str(check.get("item", "")))
    b = re.sub(r"\W+", "", str(item.get("item", "")))
    return bool(a and b and (a in b or b in a))


def _single_model(
    model_rows: list[dict[str, Any]],
    sample: dict[str, Any],
    standard_family: str | None,
) -> dict[str, Any] | None:
    """唯一定位样品型号；通用别名与完整型号同时命中时优先完整型号。"""
    label = " ".join(str(sample.get(key) or "") for key in ("model", "voltage", "spec"))
    models = _select_models(model_rows, label, standard_family)
    if len(models) > 1:
        longest = max(len(_normalized(row.get("model_code"))) for row in models)
        models = [row for row in models if len(_normalized(row.get("model_code"))) == longest]
    return models[0] if len(models) == 1 else None


def _coverage_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(field) or "")
        for field in (
            "category", "item", "reported", "required", "basis", "note",
            "should_be", "standard", "review_action",
        )
    )


def _required_applicability_conflicts(matrix_row: dict[str, Any], row: dict[str, Any]) -> list[dict[str, str]]:
    """Reject explicit model exclusions of a proven required project, not report N cells.

    The caller determines applicability from verified matrix data. A scoped
    model assertion is only conflict evidence, never proof of a report defect.
    """
    if not (matrix_row.get('applicability_status') == 'required' or matrix_row.get('_applicability_required')):
        return []
    name = re.sub(r'\s+', '', str(matrix_row.get('item_name') or ''))
    if not name:
        return []
    previous = row.get('matrix_exclusion_resolution') or {}
    if previous.get('item_name') == name:
        return list(previous.get('claims') or [])
    alias = name[:-2] if name.endswith('试验') else name
    label = re.sub(r'\s+', '', str(row.get('item') or ''))
    exact_subject = label in {name, alias}
    conflicts = []
    for field in ('required', 'note', 'should_be'):
        for clause in re.split(r'[；;。\n，,]', str(row.get(field) or '')):
            compact = re.sub(r'\s+', '', clause)
            match = re.search(r'不要求|不适用|无需(?:进行|开展|做)', compact)
            if not match:
                continue
            before = compact[:match.start()]
            if re.search(r'(?:并非|不是|并不是|不能认为|不得认为)$', before):
                continue
            # No cross-clause propagation: a neighbouring project/component's
            # N assertion must not invalidate the current project.  In some
            # RVV tables an auxiliary ``2.0 mm2 conductor`` row is N while the
            # ordinary conductor/insulation resistance row immediately above
            # is measured and P.  That qualified sub-row is not an exclusion
            # of the whole matrix project.
            auxiliary_subrow = bool(re.search(
                r"\d+(?:\.\d+)?\s*mm\s*(?:2|²|\^\s*2)?\s*线芯\s*$",
                before[:before.rfind(alias)] if alias and alias in before else "",
                re.I,
            ))
            scoped = bool(alias and alias in before and not auxiliary_subrow)
            bare = exact_subject and re.fullmatch(r'(?:标准)?(?:不要求|不适用)', compact)
            if scoped or bare:
                conflicts.append({'field': field, 'claim': clause.strip()})
    return conflicts


def _normalize_exclusion_only_check(matrix_row: dict[str, Any], check: dict[str, Any]) -> bool:
    """Replace a disproven *sole* applicability objection, without passing tests.

    If a note contains any other objection, do not rewrite its verdict or text.
    Original model fields remain in the structured resolution audit.
    """
    claims = _required_applicability_conflicts(matrix_row, check)
    if not claims or check.get('matrix_exclusion_resolution'):
        return False
    old_note = str(check.get('note') or '')
    clauses = [s.strip() for s in re.split(r'[；;。\n，,]', old_note) if s.strip()]
    invalid_notes = {c['claim'] for c in claims if c['field'] == 'note'}
    if not clauses or not invalid_notes:
        return False
    continuation = r'(?:报告)?(?:判)?P(?:应|应当)(?:改)?判N'
    if any(c not in invalid_notes and not re.fullmatch(continuation, re.sub(r'\s+', '', c), re.I) for c in clauses):
        return False
    name = re.sub(r'\s+', '', str(matrix_row['item_name']))
    original = {k: check.get(k) for k in ('required','note','verdict')}
    required = str(check.get('required') or '')
    for claim in claims:
        if claim['field'] == 'required':
            required = required.replace(claim['claim'], f'{name}为必审，条件与结果需独立核对')
    check['required'] = required
    check['note'] = f'已验证矩阵确认{name}为必审；不据原模型不适用说明要求改判N。合并项目的条件和结果仍须独立核对。'
    check['verdict'] = 'manual_review'
    check['matrix_exclusion_resolution'] = {
        'item_name':name, 'matrix_id':matrix_row.get('matrix_id'),
        'basis':_matrix_reference(matrix_row), 'claims':claims,
        'original_model_fields':original,
        'status':'exclusion_objection_retracted_tests_not_passed',
    }
    return True


def _matrix_item_covered(
    matrix_row: dict[str, Any],
    checks: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> bool:
    """判断AI证据或问题项是否明确覆盖了矩阵项目。"""
    expected_name = re.sub(r"\W+", "", str(matrix_row.get("item_name") or ""))
    expected_concept = _concept(matrix_row.get("item_name"), matrix_row.get("item_code", ""))
    item_code = str(matrix_row.get("item_code") or "").upper()
    matrix_status = str(matrix_row.get("applicability_status") or "")
    for origin, row in [("check", value) for value in checks] + [("item", value) for value in items]:
        # 上一次必审矩阵生成的“覆盖缺失”只是人工拦截占位符，
        # 不能在再次计算时反过来被当作“项目已审”的证据。
        if row.get("coverage_reason") and row.get("action_type") == "manual_review":
            continue
        if _required_applicability_conflicts(matrix_row, row):
            continue
        if origin == "check" and (matrix_status == "required" or matrix_row.get('_applicability_required')) and str(row.get("verdict") or "").lower() in {
            "not_applicable", "n", "na", "n/a",
        }:
            continue
        if origin == 'check' and matrix_row.get('_independent_applicability') and (
            str(row.get('verdict') or '').lower() in {'not_applicable','n','na','n/a'}
            or str(row.get('reported') or '').strip().lower() in {'n','na','n/a','不适用'}
        ):
            continue
        evidence = _coverage_text(row)
        normalized = re.sub(r"\W+", "", evidence)
        # 模型经常把多个同页项目合并为一条check，或使用报告模板中的
        # 等价名称。只在原页已定位且整条check判定通过时，按项目代码
        # 认领这些有明确字段组合的证据；不使用模糊相似度猜测。
        if (
            origin == "check"
            and str(row.get("verdict") or "").lower() == "pass"
            and row.get("evidence_status") in {"located", "rule_derived"}
        ):
            canonical_markers: dict[str, tuple[str, ...]] = {
                "FLAME_SINGLE": ("单根",),
                "THICKNESS_MEAS": ("绝缘平均厚度", "绝缘最薄处"),
                "SHEATH_THICKNESS_MEAS": ("护套", "平均", "最薄"),
                "OD_MEAS": ("外径",),
                "VOLTAGE_FINISHED": ("成品", "电压", "未击穿"),
                "NON_POLLUTION": ("非污染",),
                "TENSILE_BEFORE": ("绝缘", "老化前", "抗张"),
                "TENSILE_AFTER": ("绝缘", "老化后", "抗张"),
                "LOSS_WEIGHT": ("绝缘", "失重"),
                "HEAT_PRESS": ("绝缘", "高温压力"),
                "THERMAL_SHOCK": ("绝缘", "热冲击"),
                "LOWTEMP_BEND_INSUL": ("绝缘", "低温弯曲"),
                "SHEATH_TENSILE_BEFORE": ("护套", "老化前", "抗张"),
                "SHEATH_TENSILE_AFTER": ("护套", "老化后", "抗张"),
                "SHEATH_LOSS_WEIGHT": ("护套", "失重"),
                "HEAT_PRESS_SHEATH": ("护套", "高温压力"),
                "THERMAL_SHOCK_SHEATH": ("护套", "热冲击"),
                "LOWTEMP_BEND_SHEATH": ("护套", "低温弯曲"),
                "LOWTEMP_TENSILE_SHEATH": ("护套", "低温拉伸"),
            }
            markers = canonical_markers.get(item_code)
            if markers and all(marker in evidence for marker in markers):
                if item_code != "FLAME_SINGLE" or re.search(r"单根(?:垂直燃烧|阻燃性能)", evidence):
                    return True
            if item_code == "THICKNESS_MEAS" and all(
                marker in evidence for marker in ("绝缘平均厚度", "绝缘最薄")
            ):
                return True
            if item_code == "THICKNESS_MEAS" and all(
                marker in evidence for marker in ("绝缘", "平均", "最薄")
            ):
                return True
            if item_code == "OD_MEAS" and re.search(r"外(?:径|形尺寸)", evidence):
                return True
        # 部分报告把全部结构尺寸合并成一个“结构检查”核对项。只有该项已绑定
        # 原页、判P，并同时给出目标实测值和限值时，才可覆盖具体尺寸矩阵项。
        if (
            origin == "check"
            and str(row.get("verdict") or "").lower() == "pass"
            and row.get("evidence_status") == "located"
            and row.get("source_pages")
            and item_code in {"THICKNESS_MEAS", "SHEATH_THICKNESS_MEAS", "OD_MEAS"}
        ):
            reported = str(row.get("reported") or "")
            required = str(row.get("required") or "")
            marker_patterns = {
                "THICKNESS_MEAS": (r"绝缘平均厚度", r"绝缘最薄(?:处厚度)?"),
                "SHEATH_THICKNESS_MEAS": (r"护套平均厚度", r"护套最薄(?:处厚度)?"),
                "OD_MEAS": (r"外(?:径|形尺寸)",),
            }[item_code]
            if all(re.search(pattern, reported) and re.search(pattern, required) for pattern in marker_patterns):
                return True
            if item_code == "THICKNESS_MEAS" and all(
                all(marker in value for marker in ("绝缘", "平均", "最薄"))
                for value in (reported, required)
            ):
                return True
            if item_code == "SHEATH_THICKNESS_MEAS" and all(
                all(marker in value for marker in ("护套", "平均", "最薄"))
                for value in (reported, required)
            ):
                return True
        if expected_name and expected_name in normalized:
            return True
        observed = _concept(
            f"{row.get('category', '')}{row.get('item', '')}",
        )
        if expected_concept and observed == expected_concept:
            return True
    return False


def _matrix_reference(row: dict[str, Any]) -> str:
    parts = [str(row.get("standard_no") or "").strip()]
    if row.get("table_no"):
        table_no = str(row["table_no"]).strip()
        parts.append(table_no if table_no.startswith("表") else f"表{table_no}")
    if row.get("table_item_no") and str(row["table_item_no"]) != "—":
        parts.append(f"项次{row['table_item_no']}")
    return " ".join(part for part in parts if part) or "已验证型号—试验项目矩阵"


def _source_inspection_numbers(source_group: dict[str, Any]) -> tuple[str, ...]:
    """Read explicitly labelled specimen identifiers, never a model's guess."""
    from backend.app.ocr_identity import normalize_wrapped_inspections
    normalized, _ = normalize_wrapped_inspections(str(source_group.get('text') or ''))
    plain = _plain_table_text(normalized)
    # PDF字体会把同一编号的连接号写为短横/长横/不换行连字符。
    # 若不规范，会在连接号前截断，导致不同试样尾号被误合并。
    plain = plain.translate(str.maketrans({char: '-' for char in '‐‑‒–—−－'}))
    plain = re.sub(r'(?<=[A-Za-z0-9])[ \t]+(?=[./_-][A-Za-z0-9])', '', plain)
    plain = re.sub(r'(?<=[./_-])[ \t]+(?=[A-Za-z0-9])', '', plain)
    numbers = re.findall(
        r'检验编号[\s|:：]*([A-Za-z0-9][A-Za-z0-9./_-]{3,}'
        r'(?:[ \t]*\|[ \t]*(?:[-./]\d+|\d[\d./_-]*))*)', plain)
    # A leading OCR dot and wrapped digits are recoverable only when the
    # complete candidate exactly matches an explicitly labelled report ID.
    # Never infer a suffix from nearby samples or accept a partial prefix.
    report_numbers = set(re.findall(r'报告编号[\s|:：]*([A-Za-z0-9][A-Za-z0-9./_-]{3,})', plain))
    damaged = re.findall(
        r'检验编号[\s|:：]*[.．]([A-Za-z0-9][A-Za-z0-9./_-]{3,}'
        r'(?:[ \t]*\|[ \t]*[0-9][0-9./_-]*)*)', plain)
    for value in damaged:
        recovered = re.sub(r'[\s|]', '', value)
        if recovered in report_numbers:
            numbers.append(recovered)
    return tuple(sorted({re.sub(r'[\s|]', '', number) for number in numbers}))


def source_sample_start_indices(pages: list[dict[str, Any]]) -> list[int]:
    """Locate sample starts without dropping a cover that omits 样品名称.

    Some official reports use ``样品名称`` only on later specimen covers,
    while the first specimen begins directly with a ``试样型号和规格`` table.
    A cover opens a pending specimen; its following repeated table header binds
    to that cover instead of creating another specimen.  Without such a cover,
    only a change in the complete model/voltage/spec identity opens a specimen.
    """
    starts: list[int] = []
    active_key: tuple[str, str, str] | None = None
    pending_cover = False
    for index, page in enumerate(pages):
        text = str(page.get('text') or '')
        if re.search(r'样品\s*名称\s*[:：]', text):
            if not starts or starts[-1] != index:
                starts.append(index)
            pending_cover = True
            active_key = None
        identity = _source_group_identity_from_header({'text': text, 'pages': [page]})
        key = _sample_identity_key(identity or {})
        if not all(key):
            continue
        if pending_cover:
            active_key = key
            pending_cover = False
        elif key != active_key:
            if not starts or starts[-1] != index:
                starts.append(index)
            active_key = key
    return starts


def _sample_source_pages(source_text: str) -> list[dict[str, Any]]:
    """按真实样品切分PDF文字层，保留页码；MinerU补充段不重复计页。"""
    parts = re.split(
        r"^--- MinerU document parse[^\n]*---\s*$", source_text or "", maxsplit=1, flags=re.M
    )
    primary = parts[0]
    # 全扫描报告只有MinerU段。新版MinerU归一化文本会从content_list恢复
    # `Page N`标记；此时使用该段，不能继续把分隔符之前的空文本当主来源。
    if not re.search(r"^--- Page \d+[^\n]*---\s*$", primary, re.M):
        if len(parts) == 2 and re.search(r"^--- Page \d+[^\n]*---\s*$", parts[1], re.M):
            primary = parts[1]

    def grouped(segment: str) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        for page_part in re.split(r"(?=^--- Page \d+[^\n]*---\s*$)", segment, flags=re.M):
            match = re.search(r"^--- Page (\d+)", page_part, re.M)
            if match:
                pages.append({"page": int(match.group(1)), "text": page_part})
        starts = source_sample_start_indices(pages)
        groups: list[dict[str, Any]] = []
        for index, start in enumerate(starts):
            selected = pages[start:(starts[index + 1] if index + 1 < len(starts) else len(pages))]
            groups.append({"pages": selected, "text": "".join(page["text"] for page in selected)})
        return groups

    primary_groups = grouped(primary)
    if len(parts) != 2 or primary == parts[1]:
        return primary_groups
    supplement_groups = grouped(parts[1])
    if not supplement_groups:
        return primary_groups

    primary_by_key: dict[tuple[str, str, str], list[int]] = {}
    for index, group in enumerate(primary_groups):
        identity = _source_group_identity(group)
        if identity is not None:
            primary_by_key.setdefault(_sample_identity_key(identity), []).append(index)
    for supplement in supplement_groups:
        identity = _source_group_identity(supplement)
        key = _sample_identity_key(identity or {})
        if not all(key):
            continue
        candidates = primary_by_key.get(key, [])
        supplement_numbers = _source_inspection_numbers(supplement)
        matches = []
        for index in candidates:
            primary_numbers = _source_inspection_numbers(primary_groups[index])
            # Conflicting or multiple specimen numbers cannot be reconciled
            # by matching product parameters or even a coincident page number.
            if len(supplement_numbers) > 1 or len(primary_numbers) > 1:
                continue
            if supplement_numbers and primary_numbers:
                if supplement_numbers == primary_numbers:
                    matches.append(index)
            elif len(candidates) == 1:
                matches.append(index)
        target = matches[0] if len(matches) == 1 else None
        if target is None:
            primary_groups.append(supplement)
            primary_by_key.setdefault(key, []).append(len(primary_groups) - 1)
        elif "<table" in supplement["text"].lower() and "<table" not in primary_groups[target]["text"].lower():
            # 纵向文字层的数值分隔更清晰，MinerU表的栏目/页码更稳定；
            # 两类证据同时保留，不因单一OCR的优缺点丢项。
            primary_pages=[dict(p) for p in primary_groups[target]['pages']]
            supplement_pages=[dict(p) for p in supplement['pages']]
            if len(supplement_numbers)==1 and supplement_numbers==_source_inspection_numbers(primary_groups[target]):
                from hashlib import sha256
                for extra in supplement_pages:
                    peers=[p for p in primary_pages if p['page']==extra['page']]
                    extras=[p for p in supplement_pages if p['page']==extra['page']]
                    if len(peers)!=1 or len(extras)!=1:continue
                    native=peers[0]
                    if not re.match(r'^--- Page \d+ \(text layer(?: fallback)?\) ---',native['text']):continue
                    if not re.match(r'^--- Page \d+ \(MinerU structured\) ---',extra['text']):continue
                    proof={'inspection_number':supplement_numbers[0],'page':extra['page'],
                           'native_sha256':sha256(native['text'].encode()).hexdigest(),
                           'supplement_sha256':sha256(extra['text'].encode()).hexdigest()}
                    native['_cross_layer_binding']=dict(proof,layer='native')
                    extra['_cross_layer_binding']=dict(proof,layer='supplement')
            merged_pages = [*primary_pages, *supplement_pages]
            primary_groups[target] = {
                "pages": merged_pages,
                "text": primary_groups[target]["text"] + supplement["text"],
            }
    return primary_groups


def _spec_identity_from_text(value: str) -> str:
    from backend.app.structural_facts import canonical_spec
    # Consume the entire structural expression; malformed suffixes must not
    # turn a paired cable into a shorter, apparently valid ordinary cable.
    matched = re.search(r"(?<![\d.])\d[\d.\s×xX*+＋()（）]*", value)
    if not matched:
        return ""
    return canonical_spec(matched.group().strip())


def _sample_identity_key(sample: dict[str, Any]) -> tuple[str, str, str]:
    """归一化原页和AI样品身份。

    三个字段必须同时存在才能用于证据绑定；任一字段缺失时不做
    “最近样品”或数组序号猜测。
    """
    model_value = str(sample.get("model") or "").upper().strip()
    parenthetical = re.findall(r"[\(（]([^\)）]{1,24})[\)）]", model_value)
    if parenthetical:
        alias = re.sub(r"[^A-Z0-9\-]", "", parenthetical[-1].upper())
        if re.search(r"[A-Z]", alias):
            flame_prefix = re.match(r"^(Z[A-Z]?)\s*-", model_value)
            model_value = f"{flame_prefix.group(1)}-{alias}" if flame_prefix else alias
    model = re.sub(r"[\s()（）]", "", model_value)
    voltage_match = re.search(r"(\d{2,4})\s*/\s*(\d{2,4})", str(sample.get("voltage") or ""))
    voltage = f"{voltage_match.group(1)}/{voltage_match.group(2)}" if voltage_match else ""
    from backend.app.structural_facts import canonical_spec
    # 形状（扁/圆）不是身份维度：扁形标记使 parse_spec_groups 失败并导致
    # 同一试样与源注册表键不匹配（5005897 fabricate 空壳样品）。
    # 结构核验按实测轴数判定形状，不受此处影响。
    spec_raw = re.sub(r"[(（]\s*扁\s*[)）]|扁$", "", str(sample.get("spec") or ""))
    spec = canonical_spec(spec_raw)
    if not spec and re.search(
        r"60227IEC0?[12]|60245IEC66|^(?:Z[A-Z]?[-]?)?(?:BV|BLV|RV|BVR|YC|YCW)$",
        model,
    ):
        bare_area = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*mm\s*(?:2|²)", spec_raw, re.I
        )
        if bare_area:
            spec = canonical_spec(f"1×{bare_area.group(1)}")
    return model, voltage, spec


def _source_group_identity(source_group: dict[str, Any]) -> dict[str, str] | None:
    """Cross-check specimen identity; a cover's P conclusion is never a verdict.

    An explicit single-specimen conclusion may retain the full product label
    when a stamped table header is truncated. Require a complete model/voltage/
    specification, and reject disagreement with a readable table header.
    """
    from backend.app.structural_facts import canonical_spec
    header = _source_group_identity_from_header(source_group)
    model_pattern = r'(?:Z[A-Z]?-)?(?:602(?:27|45)\s*IEC\s*\d+\s*\([A-Z][A-Z0-9-]*\)|[A-Z][A-Z0-9-]*)'
    plain = _plain_table_text(str(source_group.get('text') or ''))
    covers = {}
    for match in re.finditer(r'试验结论\s*[:：]\s*([^|]{1,200}?)\s*样品', plain):
        label = match[1].strip()
        voltage = re.search(r'(\d{2,4})\s*/\s*(\d{2,4})\s*V?', label, re.I)
        if not voltage:
            continue
        model = label[:voltage.start()].strip()
        if not re.fullmatch(model_pattern, model, re.I):
            continue
        spec = canonical_spec(label[voltage.end():].strip())
        if not spec:
            continue
        candidate = {'model':re.sub(r'\s+', ' ', model),
                     'voltage':f'{voltage[1]}/{voltage[2]}', 'spec':spec}
        covers[_sample_identity_key(candidate)] = candidate
    if len(covers) > 1:
        return None
    if covers:
        key, candidate = next(iter(covers.items()))
        # A caption, barcode or table delimiter swallowed into the model is
        # not a readable conflicting product identifier.
        header_key = _sample_identity_key(header) if header else None
        readable_header = header_key and re.fullmatch(model_pattern, header_key[0], re.I)
        if readable_header and header_key != key:
            return None
        if readable_header:
            header_model = str(header.get('model') or '')
            header_iec = re.search(r'602(?:27|45)\s*IEC\s*\d+', header_model, re.I)
            cover_iec = re.search(r'602(?:27|45)\s*IEC\s*\d+', candidate['model'], re.I)
            if header_iec and cover_iec and re.sub(r'\s+', '', header_iec[0]).upper() != re.sub(r'\s+', '', cover_iec[0]).upper():
                return None
            if header_iec and not cover_iec and re.fullmatch(model_pattern, header_model, re.I):
                return header  # Do not replace a specific IEC product by its short alias.
        return candidate
    return header


def _source_group_identity_from_header(source_group: dict[str, Any]) -> dict[str, str] | None:
    """从当前原页分组首个“试样型号和规格”表头提取身份。"""
    source = str(source_group.get("text") or "")
    plain = _plain_table_text(source)
    sample_name_match = re.search(r"样品\s*名称\s*[:：]\s*([^\n]{3,200})", plain, re.I)
    if sample_name_match and not re.search(r"试样型号", source, re.I):
        sample_label = sample_name_match.group(1).strip(" |：:")
        voltage_match = re.search(r"(\d{2,4})\s*/\s*(\d{2,4})\s*V?", sample_label, re.I)
        spec_match = re.search(
            r"(\d+)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(?:mm\s*(?:2|²))?",
            sample_label,
            re.I,
        )
        if voltage_match and spec_match and voltage_match.start() > 0:
            identity = {
                "model": re.sub(r"\s+", " ", sample_label[:voltage_match.start()].strip(" |：:-")),
                "voltage": f"{voltage_match.group(1)}/{voltage_match.group(2)}",
                "spec": _spec_identity_from_text(sample_label[voltage_match.end():]),
            }
            if all(_sample_identity_key(identity)):
                return identity
    identity_match = re.search(
        r"试样型号\s*(?:\|\s*)?和(?:\s*\|\s*)?规格\s*(?:\|\s*)?(.{1,160}?)\s*(?:\|\s*)?检验编号",
        plain,
        re.I,
    )
    if identity_match:
        label = identity_match.group(1).strip(" |：:")
    else:
        block_match = re.search(
            r"试样型号\s*\n\s*和规格\s*\n([\s\S]{1,240}?)\n\s*检验编号",
            source,
            re.I,
        )
        if block_match:
            label = re.sub(r"\s*\n\s*", " ", block_match.group(1)).strip(" |：:")
        else:
        # RapidOCR/PDF文字层常把表头纵向读成：
        # “试样型号和\nRVV 300/500 2×10\n检验编号\n...规格”。
        # 这时“规格”不在“试样型号和”同一单元格，但中间一行
        # 仍同时包含型号、电压和规格，可作为严格三元身份。
            vertical_match = re.search(
                r"试样型号\s*(?:\n\s*)?和(?:\s*(?:\n\s*)?规格)?\s*\n\s*([^\n]{3,160}?)"
                r"\s*\n(?:\s*(?:2|²)\s*\n)?\s*检验编号",
                source,
                re.I,
            )
            if not vertical_match:
                # 另一常见文字层会把型号与“电压+规格”分两行。
                vertical_match = re.search(
                    r"试样型号\s*\n\s*和规格\s*\n\s*([^\n]{1,80})\s*\n\s*"
                    r"((?:\d{2,4})\s*/\s*(?:\d{2,4})\s*V?\s*\d+\s*[×xX*][^\n]+)"
                    r"\s*\n\s*检验编号",
                    source,
                    re.I,
                )
            if not vertical_match:
                # RapidOCR也会把“和规格”排到检验编号之后，而型号、
                # 电压和规格完整保留在“试样型号”的下一行。
                header_value_match = re.search(
                    r"试样型号\s*\n\s*([^\n]{1,160}?\d{2,4}\s*/\s*\d{2,4}\s*V?\s*"
                    r"\d+\s*[×xX*]\s*\d+(?:\.\d+)?[^\n]*)\s*\n\s*检验编号",
                    source,
                    re.I,
                )
                if header_value_match:
                    label = header_value_match.group(1).strip(" |：:")
                    vertical_match = None
                else:
                    label = ""
            else:
                label = ""
            if not vertical_match and not label:
                reordered_match = re.search(
                    r"试样型号(?:[^\n]*\n){0,3}?\s*([^\n]*\d{2,4}\s*/\s*\d{2,4}\s*V?\s*"
                    r"\d+\s*[×xX*]\s*\d+(?:\.\d+)?[^\n]*)",
                    source,
                    re.I,
                )
                if not reordered_match:
                    return None
                label = reordered_match.group(1).strip(" |：:")
            elif vertical_match:
                label = " ".join(group.strip(" |：:") for group in vertical_match.groups() if group)
    label = re.sub(r"(?<=\d)\.\s+(?=\d)", ".", label)
    label = _plain_table_text(label).strip(" |：:")
    # MinerU表头用数学标记表达同一规格；仅转换明确乘号和平方毫米，
    # 不删除未知LaTeX命令或截断结构表达式来凑一个可匹配身份。
    label = label.replace(r'\times', '×')
    label = re.sub(r'\\(?:text|mathrm)\{mm\}\s*\^\s*(?:\{2\}|2)', 'mm²', label)
    label = re.sub(r'mm\s*\^\s*\{*2\}*', 'mm²', label)
    label = label.replace('$', '')
    label = re.sub(r"^和规格\s*(?:\|\s*)?", "", label)
    compact_identity = re.search(
        r"(\d{2,4})\s*/\s*(300|500|750)\s*V?\s*(\d+)\s*[×xX*]\s*(\d+(?:\.\d+)?)"
        r"\s*(?:mm\s*(?:2|²))?",
        label,
        re.I,
    )
    if compact_identity:
        voltage_match = compact_identity
        spec_match = compact_identity
        voltage_left, voltage_right, cores, area = compact_identity.groups()
        model = label[:compact_identity.start()].strip(" |：:-")
        identity = {
            "model": re.sub(r"\s+", " ", model),
            "voltage": f"{voltage_left}/{voltage_right}",
            "spec": _spec_identity_from_text(label[compact_identity.start(3):]),
        }
        return identity if all(_sample_identity_key(identity)) else None

    voltage_match = re.search(r"(\d{2,4})\s*/\s*(\d{2,4})\s*V?", label, re.I)
    spec_match = re.search(r"(\d+)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*(?:mm\s*(?:2|²))?", label, re.I)
    if not voltage_match or voltage_match.start() <= 0:
        return None
    model = label[:voltage_match.start()].strip(" |：:-")
    if not spec_match and re.search(
        r"60227\s*IEC\s*0?[12]|60245\s*IEC\s*66|\b(?:BV|BLV|BVR|YC|YCW)\b",
        model,
        re.I,
    ):
        bare_area = re.search(
            r"(\d+(?:\.\d+)?)\s*mm\s*(?:\|\s*)?(?:2|²)(?![\d.])",
            label[voltage_match.end():],
            re.I,
        )
        if bare_area:
            spec_match = re.match(r"(1)×(\d+(?:\.\d+)?)", f"1×{bare_area.group(1)}")
    if not spec_match:
        return None
    identity = {
        "model": re.sub(r"\s+", " ", model),
        "voltage": f"{voltage_match.group(1)}/{voltage_match.group(2)}",
        "spec": (_spec_identity_from_text(label[voltage_match.end():])
                 or (f"{int(spec_match.group(1))}×{float(spec_match.group(2)):g}mm²"
                     if not re.search(r"[×xX*+＋]", label[voltage_match.end():]) else "")),
    }
    return identity if all(_sample_identity_key(identity)) else None


def source_sample_registry(source_text: str) -> list[dict[str, Any]]:
    """以原页为唯一来源建立样品主清单，并保留每个样品的真实页组。"""
    registry: list[dict[str, Any]] = []
    import hashlib
    seen: dict[tuple[str, ...], dict[str, Any]] = {}
    for source_index, group in enumerate(_sample_source_pages(source_text), start=1):
        identity = _source_group_identity(group)
        if not identity:
            continue
        key = _sample_identity_key(identity)
        numbers = _source_inspection_numbers(group)
        pages = sorted({int(page['page']) for page in group.get('pages') or []})
        identity_seed = ('inspection:' + numbers[0] if len(numbers) == 1
                         else 'pages:' + ','.join(map(str, pages)))
        specimen_id = hashlib.sha256(identity_seed.encode('utf-8')).hexdigest()
        physical_key = key + (specimen_id,)
        if physical_key in seen:
            previous = seen[physical_key]['group']
            existing = {(p['page'], p['text']) for p in previous.get('pages') or []}
            previous['pages'].extend(p for p in group.get('pages') or [] if (p['page'], p['text']) not in existing)
            previous['text'] = ''.join(p['text'] for p in previous['pages'])
            continue
        entry = {**identity, "key": key, "group": group, "source_index": source_index,
                 '_source_specimen_id': specimen_id,
                 'specimen_identity_basis': 'inspection_number' if len(numbers) == 1 else 'page_group'}
        seen[physical_key] = entry
        registry.append(entry)
    return registry


def align_batch_source_registry(local_registry, full_registry):
    """Use the full-source ID only for one exact product/page-set match.

    A local batch may omit the MinerU supplement that supplies the number.
    Explicitly conflicting numbers must never be overridden by page matching.
    """
    for entry in local_registry:
        pages = {int(p['page']) for p in entry['group'].get('pages') or []}
        numbers = _source_inspection_numbers(entry['group'])
        candidates = [other for other in full_registry if other['key'] == entry['key']
                      and pages and {int(p['page']) for p in other['group'].get('pages') or []} == pages]
        if len(candidates) != 1:
            continue
        other = candidates[0]
        other_numbers = _source_inspection_numbers(other['group'])
        if len(numbers) > 1 or len(other_numbers) > 1 or (numbers and other_numbers and numbers != other_numbers):
            continue
        entry['_source_specimen_id'] = other['_source_specimen_id']
    return local_registry


def bind_batch_sample_identity(result: dict[str, Any], registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore a missing area exponent/prefix from unique same-batch evidence.

    Explicit conflicting prefixes, voltage/spec changes and absent fields are
    not repairs. They must survive for the normal mismatch guard to inspect.
    """
    records = []
    for sample in result.get("samples") or []:
        sample.pop('_source_specimen_id', None)  # Never trust model-provided IDs.
        key = _sample_identity_key(sample)
        # Older model caches sometimes lose the superscript in the SPEC field.
        # Do not make bare mm a globally valid area unit: repair only when the
        # full numeric expression, exact model and voltage match one source.
        raw_spec = str(sample.get('spec') or '').strip()
        if key[0] and key[1] and not key[2] and re.search(r'mm\s*$', raw_spec, re.I):
            proposed = dict(sample, spec=re.sub(r'mm\s*$', 'mm²', raw_spec, flags=re.I))
            proposed_key = _sample_identity_key(proposed)
            matches = [entry for entry in registry if entry['key'] == proposed_key]
            if all(proposed_key) and len(matches) == 1:
                original = {field: sample.get(field) for field in ('model','voltage','spec')}
                sample['spec'] = matches[0]['spec']
                records.append({'before':original,
                                'after':{field:sample.get(field) for field in original},
                                'reason':'unique_same_batch_source_area_exponent'})
                key = _sample_identity_key(sample)
        if not all(key) or re.match(r"^Z[A-Z]?-", key[0]):
            continue
        candidates = [entry for entry in registry
                      if entry["key"][1:] == key[1:]
                      and re.sub(r"^Z[A-Z]?-", "", entry["key"][0]) == key[0]]
        if len(candidates) != 1:
            continue
        source = candidates[0]
        if source["key"] == key:
            continue
        original = {field: sample.get(field) for field in ("model", "voltage", "spec")}
        for field in ("model", "voltage", "spec"):
            sample[field] = source[field]
        records.append({"before": original, "after": {field: source[field] for field in original},
                        "reason": "unique_same_batch_source_flame_prefix"})
    for sample in result.get('samples') or []:
        matches = [entry for entry in registry if entry['key'] == _sample_identity_key(sample)]
        if len(matches) == 1 and matches[0].get('_source_specimen_id'):
            sample['_source_specimen_id'] = matches[0]['_source_specimen_id']
    return records


def source_group_for_sample(
    sample: dict[str, Any],
    source_text: str = "",
    registry: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """严格按型号+电压+规格找原页；无唯一匹配时返回None。"""
    key = _sample_identity_key(sample)
    if not all(key):
        return None
    candidates = [entry for entry in (registry or source_sample_registry(source_text)) if entry["key"] == key]
    if sample.get('_source_specimen_id'):
        candidates = [entry for entry in candidates
                      if entry.get('_source_specimen_id') == sample['_source_specimen_id']]
    return candidates[0]["group"] if len(candidates) == 1 else None


def _merge_identity_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """合并同一物理样品的AI返回，不丢失分段证据。"""
    merged = {**samples[0], "checks": [], "items": []}
    seen_checks: set[tuple[str, ...]] = set()
    seen_items: set[tuple[str, ...]] = set()
    for sample in samples:
        for check in sample.get("checks") or []:
            key = tuple(str(check.get(field) or "") for field in (
                "category", "item", "reported", "required", "verdict",
            ))
            if key not in seen_checks:
                merged["checks"].append(check)
                seen_checks.add(key)
        for item in sample.get("items") or []:
            key = tuple(str(item.get(field) or "") for field in (
                "item", "reported", "should_be", "standard", "severity",
            ))
            if key not in seen_items:
                merged["items"].append(item)
                seen_items.add(key)
    return merged


def reconcile_result_samples_with_source(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """在执行确定性规则前，将AI样品与原页主清单对账。"""
    registry = source_sample_registry(source_text)
    if not registry:
        result.setdefault("_deterministic_validation", {})["source_sample_registry"] = {
            "source_samples": 0, "status": "identity_not_available",
        }
        return result

    ai_by_key: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    unmatched_ai: list[dict[str, Any]] = []
    prefix_alias_matches: list[dict[str, str]] = []
    registry_keys = {entry["key"] for entry in registry}
    def add_bound(sample, product_key):
        candidates = [entry for entry in registry if entry['key'] == product_key]
        if sample.get('_source_specimen_id'):
            candidates = [entry for entry in candidates
                          if entry.get('_source_specimen_id') == sample['_source_specimen_id']]
        if len(candidates) != 1:
            return False
        physical_key = product_key + (str(candidates[0].get('_source_specimen_id') or ''),)
        ai_by_key.setdefault(physical_key, []).append(sample)
        return True
    for sample in result.get("samples") or []:
        key = _sample_identity_key(sample)
        if all(key) and key in registry_keys:
            if not add_bound(sample, key):
                unmatched_ai.append(sample)
        else:
            zero_spec = re.fullmatch(r"(\d+)×0(?:\.0+)?mm²", key[2] or "")
            zero_candidates = [
                entry["key"] for entry in registry
                if zero_spec
                and entry["key"][0] == key[0]
                and entry["key"][1] == key[1]
                and entry["key"][2].startswith(f"{int(zero_spec.group(1))}×")
            ]
            if len(zero_candidates) == 1:
                if add_bound(sample, zero_candidates[0]):
                    prefix_alias_matches.append({"ai_model": key[0], "source_model": zero_candidates[0][0]})
                else:
                    unmatched_ai.append(sample)
                continue
            alias_key = _unique_prefix_alias_registry_key(key, registry, sample)
            if alias_key and add_bound(sample, alias_key):
                prefix_alias_matches.append({"ai_model": key[0], "source_model": alias_key[0]})
            else:
                unmatched_ai.append(sample)

    reconciled: list[dict[str, Any]] = []
    created: list[str] = []
    duplicate_merged = 0
    for entry in registry:
        physical_key = entry['key'] + (str(entry.get('_source_specimen_id') or ''),)
        sources = ai_by_key.get(physical_key) or []
        label = " ".join(entry[field] for field in ("model", "voltage", "spec"))
        if sources:
            sample = _merge_identity_samples(sources)
            sample["items"] = [
                item for item in (sample.get("items") or [])
                if str(item.get("item") or "") not in {
                    "AI样品身份无法与原页匹配", "AI样品识别缺失",
                }
            ]
            duplicate_merged += max(0, len(sources) - 1)
            sample.update({field: entry[field] for field in ("model", "voltage", "spec")})
            sample["_source_registry_status"] = "matched"
        else:
            sample = {
                "model": entry["model"], "voltage": entry["voltage"], "spec": entry["spec"],
                "checks": [],
                "items": [{
                    "item": "AI样品识别缺失",
                    "reported": "原页存在该样品，但AI结构化结果未返回",
                    "should_be": "必须按原PDF和必审矩阵逐项补齐；证据不足的项目转人工复核",
                    "severity": "suggestion", "action_required": True,
                    "action_type": "manual_review", "review_action": "人工确认该样品的完整审核结果",
                    "evidence_status": "located",
                    "source_pages": [int(page["page"]) for page in entry["group"].get("pages") or []],
                    "source_excerpt": label,
                }],
                "_source_registry_status": "created_from_source",
            }
            created.append(label)
        sample["_source_registry_index"] = entry["source_index"]
        if entry.get('_source_specimen_id'):
            sample['_source_specimen_id'] = entry['_source_specimen_id']
        reconciled.append(sample)

    unmatched_labels: list[str] = []
    for sample in unmatched_ai:
        label = " ".join(str(sample.get(field) or "") for field in ("model", "voltage", "spec")).strip()
        unmatched_labels.append(label)
        sample.setdefault("items", []).append({
            "item": "AI样品身份无法与原页匹配",
            "reported": label or "AI未返回完整样品身份",
            "should_be": "按型号、电压、规格三元组核对原PDF",
            "severity": "suggestion", "action_required": True,
            "action_type": "manual_review", "review_action": "人工确认该AI样品对应的原页；系统未按顺序猜测绑定",
            "evidence_status": "not_located", "source_pages": [], "source_excerpt": "",
        })
        sample["_source_registry_status"] = "unmatched_ai"
        reconciled.append(sample)

    result["samples"] = reconciled
    result.setdefault("_deterministic_validation", {})["source_sample_registry"] = {
        "source_samples": len(registry), "matched": len(registry) - len(created),
        "created_from_source": created, "duplicate_ai_samples_merged": duplicate_merged,
        "unmatched_ai_samples": unmatched_labels, "prefix_alias_matches": prefix_alias_matches,
        "binding": "model+voltage+spec; unique optional flame prefix alias",
    }
    return result


def _measurement_after_heading(lines: list[str], heading_index: int) -> dict[str, Any] | None:
    """读取纵向OCR表格中的要求、实测值和P/N；证据不完整时返回None。"""
    window = [line.strip() for line in lines[heading_index + 1:heading_index + 18]]
    requirements: list[tuple[str, float, str]] = []
    verdict_index = next((
        index for index, line in enumerate(window)
        if re.fullmatch(r"[PFN]", line, re.I)
    ), None)
    if verdict_index is None:
        return None

    def numeric_cells(value: str) -> list[float]:
        compact = re.sub(r"\s+", "", value)
        # RapidOCR可把相邻单元格合并成0.460.56；只在整行可严格
        # 分成多个“一位整数+两位小数”时拆分，避免任意猜数。
        if re.fullmatch(r"(?:\d\.\d{2}){2,}", compact):
            return [float(token) for token in re.findall(r"\d\.\d{2}", compact)]
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:\s+[0-9]+(?:\.[0-9]+)?)+", value.strip()):
            return [float(token) for token in re.findall(r"[0-9]+(?:\.[0-9]+)?", value)]
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", compact):
            return [float(compact)]
        return []

    direction_index = next((
        index for index, line in enumerate(window[:verdict_index])
        if re.search(r"最大|最小", line)
    ), None)
    if direction_index is None:
        return None
    direction_line = window[direction_index]
    direction = "最大" if "最大" in direction_line else "最小"
    inline = re.search(r"(?:最大|最小)\s*([0-9]+(?:\.[0-9]+)?)", direction_line)
    numeric_entries: list[tuple[int, list[float]]] = []
    if inline:
        numeric_entries.append((direction_index, [float(inline.group(1))]))
    numeric_entries.extend(
        (index, values)
        for index, line in enumerate(window[direction_index + 1:verdict_index], direction_index + 1)
        if (values := numeric_cells(line))
    )
    if len(numeric_entries) < 2:
        return None
    limit_index, limit_values = numeric_entries[0]
    if len(limit_values) != 1:
        return None
    requirements.append((direction, limit_values[0], f"{direction}{limit_values[0]:g}"))
    reported_values = [
        value for index, values in numeric_entries[1:] if index > limit_index for value in values
    ]
    if not reported_values:
        return None

    # 外径表的最小限值有时因rowspan排在P之后，与前面同一实测值共用。
    after = window[verdict_index + 1:verdict_index + 6]
    for index, line in enumerate(after):
        if not re.fullmatch(r"最大|最小", line):
            continue
        extra_direction = line
        extra_limit = next((
            values[0] for later in after[index + 1:index + 4]
            if len(values := numeric_cells(later)) == 1
        ), None)
        if extra_limit is not None:
            requirements.append((extra_direction, extra_limit, f"{extra_direction}{extra_limit:g}"))
        break

    passed = all(
        all(
            value <= limit + 1e-9 if direction == "最大" else value + 1e-9 >= limit
            for value in reported_values
        )
        for direction, limit, _ in requirements
    )
    verdict = window[verdict_index].upper()
    excerpt = " | ".join(
        line for line in [lines[heading_index].strip(), *window[:verdict_index + 6]] if line
    )[:420]
    return {
        "reported": min(reported_values),
        "reported_text": "/".join(f"{value:g}" for value in reported_values),
        "requirements": requirements,
        "report_verdict": verdict,
        "passed": passed and verdict == "P",
        "excerpt": excerpt,
    }


def _measurement_before_heading(lines: list[str], heading_index: int) -> dict[str, Any] | None:
    """兼容纵向PDF文字层把“要求/实测/P”排在项目名之前。"""
    window = [line.strip() for line in lines[max(0, heading_index - 8):heading_index]]
    verdict_index = next((index for index in range(len(window) - 1, -1, -1)
                          if re.fullmatch(r"[PFN]", window[index], re.I)), None)
    if verdict_index is None:
        return None
    prefix = window[:verdict_index]
    reported_index = next((index for index in range(len(prefix) - 1, -1, -1)
                           if re.fullmatch(r"[0-9]+(?:\.\s*[0-9]+)?", prefix[index])), None)
    if reported_index is None:
        return None
    requirement_index = next((index for index in range(reported_index - 1, -1, -1)
                              if re.fullmatch(r"[0-9]+(?:\.\s*[0-9]+)?", prefix[index])), None)
    direction_index = next((index for index in range((requirement_index or 0) - 1, -1, -1)
                            if re.fullmatch(r"最大|最小", prefix[index])), None)
    if requirement_index is None or direction_index is None:
        return None
    direction = prefix[direction_index]
    limit = float(re.sub(r"\s+", "", prefix[requirement_index]))
    reported = float(re.sub(r"\s+", "", prefix[reported_index]))
    verdict = window[verdict_index].upper()
    passed = (reported <= limit + 1e-9 if direction == "最大" else reported + 1e-9 >= limit)
    return {
        "reported": reported,
        "requirements": [(direction, limit, f"{direction}{limit:g}")],
        "report_verdict": verdict,
        "passed": passed and verdict == "P",
        "excerpt": " | ".join([*window[direction_index:], lines[heading_index].strip()])[:360],
    }


def _dimension_source_evidence(
    matrix_row: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """高置信度提取绝缘厚度测量和外径测量，不对其他项目猜值。"""
    if not source_group:
        return None
    item_code = str(matrix_row.get("item_code") or "").upper()
    item_name = str(matrix_row.get("item_name") or "")
    concept = _concept(item_name, item_code)
    if item_code == "THICKNESS_MEAS" or concept.endswith(":thickness") or concept == "thickness":
        heading_patterns = (r"绝缘平均厚度", r"绝缘最薄处厚度")
        minimum_measurements = 2
    elif item_code == "SHEATH_THICKNESS_MEAS":
        heading_patterns = (r"护套平均厚度", r"护套最薄处厚度")
        minimum_measurements = 2
    elif item_code == "OD_MEAS" or concept == "outer_diameter":
        heading_patterns = (
            r"(?:外径(?:尺寸)?|外形尺寸)\s*[-—－]{1,3}\s*平均外径(?:\s*[（(]扁[）)])?",
            # 纵向PDF文字层常只保留“外径”行，要求/实测/P在其前后独立行。
            r"(?m)^\s*外径\s*$",
        )
        minimum_measurements = 1
    else:
        return None

    if item_code == "OD_MEAS" or concept == "outer_diameter":
        for page in source_group.get("pages") or []:
            plain = _plain_table_text(str(page.get("text") or ""))
            # RapidOCR会把外径与椭圆度两行纵向交错为：
            # “外径/椭圆度/mm/mm/%/最大9.3/最小7.4/最大15/8.6/4/P/P”。
            # 只在完整列序和两个P同时存在时分栏，避免把4%当成4mm。
            interleaved_outer_ellipse = re.search(
                r"外径\s*[-—－]{1,3}\s*平均外径\s*\|?\s*椭圆度"
                r".{0,100}?最大\s*\|?\s*([0-9]+(?:\.\d+)?)\s*\|?\s*"
                r"最小\s*\|?\s*([0-9]+(?:\.\d+)?)\s*\|?\s*"
                r"最大\s*\|?\s*[0-9]+(?:\.\d+)?\s*\|?\s*"
                r"([0-9]+(?:\.\d+)?)\s*\|?\s*[0-9]+(?:\.\d+)?\s*\|?\s*P\s*\|?\s*P",
                plain,
                re.I | re.S,
            )
            if interleaved_outer_ellipse:
                maximum, minimum, reported = map(float, interleaved_outer_ellipse.groups())
                passed = minimum - 1e-9 <= reported <= maximum + 1e-9
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{reported:g}mm",
                    "required": f"外径-平均外径：最大{maximum:g}、最小{minimum:g}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序按外径/椭圆度交错列序分栏提取，椭圆度百分数不作为mm值",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": interleaved_outer_ellipse.group(0)[:600],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            vertical_outer_row = re.search(
                r"外径\s*[-—－]{1,3}\s*平均外径.{0,60}?"
                r"最大\s*\|?\s*([0-9]+(?:\.\d+)?)\s*\|?\s*"
                r"最小\s*\|?\s*(?:([0-9]+(?:\.\d+)?)|[-—－]+)\s*\|?\s*"
                r"([0-9]+(?:\.\d+)?)\s*\|?\s*P\s*(?:\|\s*)*(?=椭圆度|标\s*志|$)",
                plain,
                re.I | re.S,
            )
            if vertical_outer_row:
                maximum_text, minimum_text, reported_text = vertical_outer_row.groups()
                maximum, reported = float(maximum_text), float(reported_text)
                minimum = float(minimum_text) if minimum_text else None
                passed = reported <= maximum + 1e-9 and (minimum is None or reported + 1e-9 >= minimum)
                requirement = f"最大{maximum:g}" + (f"、最小{minimum:g}" if minimum is not None else "")
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{reported:g}mm",
                    "required": f"外径-平均外径：{requirement}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序从RapidOCR纵向外径行提取限值、实测值和P评定",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": vertical_outer_row.group(0)[:500],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            interleaved_single_core_row = re.search(
                r"外径\s*[-—－]\s*平均外径\s*\|\s*mm\s*\|\s*mm\s*\|\s*"
                r"最大\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"最小\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"([0-9]+(?:\.\d+)?)\s*\|\s*P",
                plain,
                re.I | re.S,
            )
            if interleaved_single_core_row:
                maximum, minimum, reported = map(float, interleaved_single_core_row.groups())
                passed = minimum - 1e-9 <= reported <= maximum + 1e-9
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{reported:g}mm",
                    "required": f"外径-平均外径：最大{maximum:g}、最小{minimum:g}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序按交错列外径模板提取上下限、实测值并复算",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": interleaved_single_core_row.group(0)[:500],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            horizontal_range_row = re.search(
                r"外径\s*[-—－]\s*平均外径(?:\s*\|\s*mm)?\s*\|\s*"
                r"最大\s*([0-9]+(?:\.\d+)?)\s+最小\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"([0-9]+(?:\.\d+)?)\s*\|\s*P",
                plain,
                re.I | re.S,
            )
            if horizontal_range_row:
                maximum, minimum, reported = map(float, horizontal_range_row.groups())
                passed = minimum - 1e-9 <= reported <= maximum + 1e-9
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{reported:g}mm",
                    "required": f"外径-平均外径：最大{maximum:g}、最小{minimum:g}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序从横向外径上下限表确定性提取并复算",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": horizontal_range_row.group(0)[:500],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            range_row = re.search(
                r"外径\s*[-—－]\s*平均外径\s*\|\s*"
                r"(?:外径尺寸|外形尺寸)\s*[-—－]\s*平均外径(?:\s*[（(]圆[）)])?\s*\|\s*"
                r"mm\s*\|\s*(?:mm\s*\|\s*)?最大\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"最小\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"([0-9]+(?:\.\d+)?)\s*\|\s*P",
                plain,
                re.I | re.S,
            )
            if range_row:
                maximum, minimum, reported = map(float, range_row.groups())
                passed = minimum - 1e-9 <= reported <= maximum + 1e-9
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{reported:g}mm",
                    "required": f"外径-平均外径：最大{maximum:g}、最小{minimum:g}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序从圆形电缆外径上下限模板确定性提取并复算",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": range_row.group(0)[:500],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            max_only_row = re.search(
                r"外径\s*[-—－]\s*平均外径\s*\|\s*"
                r"(?:外径尺寸|外形尺寸)\s*[-—－]\s*平均外径(?:\s*[（(](?:圆|扁)[）)])?\s*\|\s*"
                r"mm\s*\|\s*(?:mm\s*\|\s*)?最大\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"最小\s*[-—－/]\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*P",
                plain,
                re.I | re.S,
            )
            if max_only_row:
                maximum, reported = map(float, max_only_row.groups())
                passed = reported <= maximum + 1e-9
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{reported:g}mm",
                    "required": f"外径-平均外径：最大{maximum:g}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序按仅设上限的圆形/扁形外径模板提取并复算",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": max_only_row.group(0)[:500],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            # 单芯电线模板把“标准上限、两个实测值、P”横向排成：
            # 最大 | 最小 | 3.2 | 2.6 | 2.9 | P。
            # 其中“最小”是检验结果列的子标题，不是3.2的限值方向。
            single_core_max_row = re.search(
                r"外径\s*[-—－]\s*平均外径\s*\|\s*mm\s*\|\s*mm\s*\|\s*"
                r"最大\s*\|\s*最小\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"([0-9]+(?:\.\d+)?)\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*P",
                plain,
                re.I | re.S,
            )
            if single_core_max_row:
                maximum, first, second = map(float, single_core_max_row.groups())
                passed = first <= maximum + 1e-9 and second <= maximum + 1e-9
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{first:g}/{second:g}mm",
                    "required": f"外径-平均外径：最大{maximum:g}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序按单芯电线外径模板提取标准上限和两组实测值，并核对原页P评定",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": single_core_max_row.group(0)[:500],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            round_row = re.search(
                r"外径\s*[-—－]\s*平均外径\s*\|\s*外形尺寸\s*[-—－]\s*平均外径"
                r"(?:\s*[（(]圆[）)])?\s*\|\s*椭圆度"
                r".{0,220}?最大\s*([0-9]+(?:\.\d+)?)\s*\|\s*最小\s*([0-9]+(?:\.\d+)?)"
                r"\s*\|\s*最大\s*[0-9]+(?:\.\d+)?\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*\|\s*"
                r"[0-9]+(?:\.\d+)?\s*\|\s*P",
                plain,
                re.I | re.S,
            )
            if round_row:
                maximum, minimum, reported = map(float, round_row.groups())
                passed = minimum - 1e-9 <= reported <= maximum + 1e-9
                return {
                    "category": "结构尺寸", "item": item_name,
                    "reported": f"外径-平均外径：{reported:g}mm",
                    "required": f"外径-平均外径：最大{maximum:g}、最小{minimum:g}mm",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序按圆形电缆外径列提取；相邻椭圆度百分数不作为mm值",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": round_row.group(0)[:700],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }

    measurements: list[dict[str, Any]] = []
    seen_measurements: set[tuple[Any, ...]] = set()
    source_pages: list[int] = []
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        plain = _plain_table_text(page_text)
        page_measurements = [
            measured
            for pattern in heading_patterns
            for measured in _dimension_tabular_measurements(plain, pattern)
        ]
        if not page_measurements:
            lines = page_text.splitlines()
            for index, line in enumerate(lines):
                if not any(re.search(pattern, line) for pattern in heading_patterns):
                    continue
                measured = _measurement_after_heading(lines, index)
                if not measured and re.fullmatch(r"\s*外径\s*", line):
                    measured = _measurement_before_heading(lines, index)
                if measured:
                    measured["heading"] = line.strip()
                    measured.setdefault("reported_text", f"{measured['reported']:g}")
                    measured["requirements_text"] = "、".join(
                        f"{direction}{limit:g}" for direction, limit, _ in measured["requirements"]
                    )
                    page_measurements.append(measured)
        for measured in page_measurements:
            if measured:
                key = (
                    int(page["page"]), measured["reported_text"],
                    measured["requirements_text"],
                )
                if key in seen_measurements:
                    continue
                seen_measurements.add(key)
                measurements.append(measured)
                if int(page["page"]) not in source_pages:
                    source_pages.append(int(page["page"]))
    if len(measurements) < minimum_measurements:
        return None

    if item_code == "OD_MEAS" or concept == "outer_diameter":
        # 同一单芯模板在通用提取后会暂时形成“最小3.2、实测2.6/2.9”。
        # 通过原始行的完整列序和P评定，把它还原为“最大3.2”。
        for measured in measurements:
            template = re.search(
                r"最大\s*\|\s*最小\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*"
                r"([0-9]+(?:\.\d+)?)\s*\|\s*([0-9]+(?:\.\d+)?)\s*\|\s*P",
                str(measured.get("excerpt") or ""),
                re.I,
            )
            if not template or measured.get("report_verdict") != "P":
                continue
            maximum, first, second = map(float, template.groups())
            measured["reported_text"] = f"{first:g}/{second:g}"
            measured["requirements"] = [("最大", maximum, f"最大{maximum:g}")]
            measured["requirements_text"] = f"最大{maximum:g}"
            measured["passed"] = first <= maximum + 1e-9 and second <= maximum + 1e-9

        # 圆形电缆模板常在“外径-平均外径”之后紧接“椭圆度”行。
        # OCR可能把椭圆度3%误生成为第二条“外形尺寸3mm”。只要原页
        # 存在明确的“外径-平均外径”测量，就以该行作为外径证据；
        # 扁形电缆仅有“外形尺寸-平均外径(扁)”时仍保留原逻辑。
        explicit_outer = [
            item for item in measurements
            if re.search(r"(?:^|[^形])外径\s*[-—－]{1,3}\s*平均外径", str(item.get("heading") or ""))
        ]
        if explicit_outer:
            clean_single = next((
                item for item in explicit_outer
                if "/" not in str(item.get("reported_text") or "")
                and not re.search(r"[×xX*]", str(item.get("reported_text") or ""))
                and item.get("report_verdict") == "P"
                and re.search(r"\d+(?:\.\d+)?", str(item.get("reported_text") or item.get("reported") or ""))
            ), None)
            if clean_single:
                value = float(re.search(
                    r"\d+(?:\.\d+)?",
                    str(clean_single.get("reported_text") or clean_single.get("reported") or ""),
                ).group(0))
                requirements: list[tuple[str, float, str]] = []
                for candidate in explicit_outer:
                    for direction, limit, label in candidate.get("requirements") or []:
                        # “最大15%”属于紧邻的椭圆度列，不是17.4mm外径上限。
                        if (direction == "最大" and limit + 1e-9 < value) or (
                            direction == "最小" and limit > value + 1e-9
                        ):
                            continue
                        if (direction, limit, label) not in requirements:
                            requirements.append((direction, limit, label))
                clean_single["requirements"] = requirements
                clean_single["requirements_text"] = "、".join(
                    f"{direction}{limit:g}" for direction, limit, _ in requirements
                )
                clean_single["passed"] = bool(requirements) and all(
                    value <= limit + 1e-9 if direction == "最大" else value + 1e-9 >= limit
                    for direction, limit, _ in requirements
                )
                measurements = [clean_single]
            else:
                measurements = explicit_outer

    reported = "；".join(f"{item['heading']}：{item['reported_text']}mm" for item in measurements)
    required = "；".join(
        f"{item['heading']}：{item['requirements_text']}mm"
        for item in measurements
    )
    report_verdicts = {item["report_verdict"] for item in measurements if item["report_verdict"]}
    passed = all(item["passed"] for item in measurements) and "N" not in report_verdicts
    return {
        "category": "结构尺寸",
        "item": item_name,
        "reported": reported,
        "required": required,
        "verdict": "pass" if passed else "fail",
        "basis": _matrix_reference(matrix_row),
        "note": "程序从报告原文确定性补齐；外网模型未返回该必审项目",
        "source_pages": source_pages,
        "source_excerpt": "；".join(item["excerpt"] for item in measurements)[:700],
        "evidence_status": "located",
        "coverage_origin": "deterministic_source_recovery",
    }


def _structure_source_evidence(
    matrix_row: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """确认结构检查大项已在原表完整呈现。

    具体厚度和外径仍由独立数值规则复算；这里只解决矩阵中
    STRUCT_CHECK与具体尺寸项并存造成的重复覆盖缺口。
    """
    if not source_group or str(matrix_row.get("item_code") or "").upper() != "STRUCT_CHECK":
        return None
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        plain = _plain_table_text(page_text)
        compact = re.sub(r"\s+", "", plain)
        markers = (
            "导体材料", "绝缘平均厚度", "绝缘最薄处厚度",
            "护套平均厚度", "外径", "标志连续性检查",
        )
        matched = [marker for marker in markers if marker in compact]
        verdicts = re.findall(r"(?:^|[^A-Z])([PFN])(?:$|[^A-Z])", plain, re.I)
        if len(matched) < 4 or len(verdicts) < 5:
            continue
        verdict = "fail" if any(value.upper() == "F" for value in verdicts) else "pass"
        return {
            "category": "结构尺寸",
            "item": str(matrix_row.get("item_name") or "结构检查"),
            "reported": f"原表已列出{len(matched)}类结构检查项并给出P/F/N评定",
            "required": "结构检查大项必须有原表和评定；具体尺寸由独立规则复算",
            "verdict": verdict,
            "basis": _matrix_reference(matrix_row),
            "note": "按原表自评确认结构大项已覆盖；不替代绝缘厚度、护套厚度和外径的独立数值复算",
            "source_pages": [int(page["page"])],
            "source_excerpt": plain[:900],
            "evidence_status": "located",
            "coverage_origin": "source_report_self_assessment",
        }
    return None


def _yellow_green_ratio_observation(
    source_group: dict[str, Any] | None,
) -> dict[str, Any]:
    """按同一样品原表核对黄/绿组合色比例。

    只在“受检验绝缘线芯颜色”行明确包含黄绿色时启用；
    比例行的要求、结果和P/F/N必须保持在同一HTML表格行内，
    避免借用相邻项目的30/70或P/N。
    """
    if not source_group:
        return {"status": "unresolved", "reason": "sample_source_not_uniquely_bound"}

    colour_pages: set[int] = set()
    ratio_rows: list[dict[str, Any]] = []
    pair_pattern = r"(?:黄(?:色)?\s*(?:[/／\-]\s*)?绿(?:色)?|绿(?:色)?\s*(?:[/／\-]\s*)?黄(?:色)?)"
    colour_pattern = re.compile(pair_pattern)
    ratio_pattern = re.compile(pair_pattern + r"(?:组合色)?线芯比例")

    for page in source_group.get("pages") or []:
        page_no = int(page.get("page") or 0)
        page_text = str(page.get("text") or "")
        pending_ratio_heading = False
        for row_match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", page_text, re.I | re.S):
            row_html = row_match.group(1)
            cells: list[str] = []
            for cell_match in re.finditer(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, re.I | re.S):
                cell = html.unescape(cell_match.group(1))
                cell = re.sub(r"<[^>]+>", " ", cell)
                cells.append(re.sub(r"\s+", "", cell))
            if not cells:
                continue

            heading = cells[0]
            if "受检验绝缘线芯颜色" in heading or "受检绝缘线芯颜色" in heading:
                # 颜色必须出现在该行的检验结果单元格中。
                # 行标题自身不含黄绿字样，不会把比例行或标准说明当成结果。
                if any(colour_pattern.search(cell) for cell in cells[1:]):
                    colour_pages.add(page_no)
                continue

            is_ratio_heading = bool(ratio_pattern.search(heading))
            is_ratio_continuation = bool(
                pending_ratio_heading
                and re.search(r"其中.*颜色.*比例|其中.*颜色.*所占比例", heading)
            )
            if not is_ratio_heading and not is_ratio_continuation:
                pending_ratio_heading = False
                continue
            verdict = cells[-1].upper() if cells[-1].upper() in {"P", "F", "N"} else ""
            requirement_index = next((
                index for index, cell in enumerate(cells)
                if "30" in cell and "70" in cell
            ), None)
            if requirement_index is None:
                if is_ratio_heading:
                    # 常见模板把“黄/绿组合色线芯比例”和
                    # “其中一种颜色的比例”分成相邻两行。
                    pending_ratio_heading = True
                    continue
                ratio_rows.append({
                    "page": page_no,
                    "status": "unresolved",
                    "reason": "ratio_requirement_not_bound",
                    "cells": cells,
                })
                continue
            pending_ratio_heading = False
            result_cells = cells[requirement_index + 1:-1] if verdict else cells[requirement_index + 1:]
            result_text = "".join(result_cells)
            values = [float(value) for value in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", result_text)]
            ratio_rows.append({
                "page": page_no,
                "status": "located",
                "requirement": cells[requirement_index],
                "reported": result_text or "/",
                "values": values,
                "verdict": verdict,
                "source_excerpt": " | ".join(cells),
            })

    if not colour_pages:
        return {"status": "not_applicable", "reason": "no_yellow_green_insulated_core"}
    if not ratio_rows:
        return {
            "status": "unresolved",
            "reason": "yellow_green_core_without_located_ratio_row",
            "source_pages": sorted(colour_pages),
        }

    located = [row for row in ratio_rows if row.get("status") == "located"]
    identities = {
        (tuple(row.get("values") or []), row.get("verdict"), row.get("reported"))
        for row in located
    }
    if len(identities) != 1:
        return {
            "status": "unresolved",
            "reason": "ratio_rows_conflict_or_incomplete",
            "source_pages": sorted(colour_pages | {int(row.get("page") or 0) for row in ratio_rows}),
        }

    row = located[0]
    values = row.get("values") or []
    row["source_pages"] = sorted(colour_pages | {int(item.get("page") or 0) for item in located})
    if not values:
        row["status"] = "missing" if row.get("verdict") == "N" or row.get("reported") in {"", "/"} else "unresolved"
        row["reason"] = "yellow_green_ratio_not_reported"
        return row

    if len(values) == 1:
        numeric_pass = 30.0 <= float(values[0]) <= 70.0
        row["ratio_percent"] = float(values[0])
    elif len(values) == 2:
        numeric_pass = (
            all(30.0 <= float(value) <= 70.0 for value in values)
            and abs(sum(float(value) for value in values) - 100.0) <= 1.0
        )
        row["ratio_pair_percent"] = [float(value) for value in values]
    else:
        row["status"] = "unresolved"
        row["reason"] = "yellow_green_ratio_value_count_unresolved"
        return row
    row["status"] = "pass" if numeric_pass and row.get("verdict") == "P" else "fail"
    row["reason"] = (
        "yellow_green_ratio_within_range"
        if row["status"] == "pass"
        else "yellow_green_ratio_out_of_range_or_wrong_verdict"
    )
    return row


def apply_yellow_green_ratio_guard(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """黄绿组合色出现时，确定性核对任意15 mm段的覆盖比例。"""
    if not source_text:
        return result
    registry = source_sample_registry(source_text)
    audit = {"checked": 0, "passed": 0, "failed": 0, "unresolved": 0, "records": []}
    for sample in result.get("samples") or []:
        source_group = source_group_for_sample(sample, registry=registry)
        observation = _yellow_green_ratio_observation(source_group)
        status = str(observation.get("status") or "unresolved")
        if status == "not_applicable":
            continue

        audit["checked"] += 1
        sample_label = " ".join(str(sample.get(key) or "") for key in ("model", "voltage", "spec")).strip()
        record = {
            "sample": sample_label,
            "status": status,
            "reason": observation.get("reason"),
            "source_pages": list(observation.get("source_pages") or []),
        }
        audit["records"].append(record)

        check = {
            "category": "标志与线芯识别",
            "item": "黄/绿组合色线芯比例",
            "reported": (
                f"{float(observation['ratio_percent']):g}%"
                if observation.get("ratio_percent") is not None
                else str(observation.get("reported") or "未填写")
            ),
            "required": "任意15 mm段内，其中一种颜色覆盖绝缘线芯表面的比例应为30%～70%",
            "verdict": "pass" if status == "pass" else "fail" if status in {"missing", "fail"} else "manual_review",
            "basis": "GB/T 5023.1-2008 4.1.3；GB/T 5013.1-2008 4.1.3",
            "note": "程序按同样品颜色行与比例行确定性核对",
            "source_pages": list(observation.get("source_pages") or []),
            "source_excerpt": str(observation.get("source_excerpt") or "")[:700],
            "evidence_status": "located" if status in {"pass", "missing", "fail"} else "not_located",
            "coverage_origin": "deterministic_yellow_green_ratio_guard",
        }
        existing_checks = [
            item for item in sample.get("checks") or []
            if "组合色" in str(item.get("item") or "") and "比例" in str(item.get("item") or "")
        ]
        if not existing_checks:
            sample.setdefault("checks", []).append(check)

        if status == "pass":
            audit["passed"] += 1
            continue

        if status in {"missing", "fail"}:
            audit["failed"] += 1
            item_name = "黄/绿组合色线芯比例未填写" if status == "missing" else "黄/绿组合色线芯比例不符合"
            reported = check["reported"]
            action_type = "correction"
            review_action = "补充实测颜色覆盖比例并改为P/F评定，不得继续判N"
            severity = "must_fix"
        else:
            audit["unresolved"] += 1
            item_name = "黄/绿组合色线芯比例证据待核对"
            reported = "已识别黄绿色线芯，但未能唯一绑定比例行"
            action_type = "manual_review"
            review_action = "回看同一样品原PDF的黄/绿组合色比例行"
            severity = "suggestion"

        existing_items = [
            item for item in sample.get("items") or []
            if "组合色" in str(item.get("item") or "") and "比例" in str(item.get("item") or "")
        ]
        if not existing_items:
            sample.setdefault("items", []).append({
                "item": item_name,
                "reported": reported,
                "should_be": "黄绿组合色线芯必须给出30%～70%的实测比例并正确评定",
                "standard": "GB/T 5023.1-2008 4.1.3；GB/T 5013.1-2008 4.1.3",
                "severity": severity,
                "action_required": True,
                "action_type": action_type,
                "review_action": review_action,
                "coverage_reason": str(observation.get("reason") or ""),
                "source_pages": list(observation.get("source_pages") or []),
                "source_excerpt": str(observation.get("source_excerpt") or "")[:700],
                "evidence_status": "located" if status in {"missing", "fail"} else "not_located",
            })

    result.setdefault("_deterministic_validation", {})["yellow_green_ratio"] = audit
    return result


def _dimension_tabular_measurements(plain_text: str, heading_pattern: str) -> list[dict[str, Any]]:
    """读取横向HTML表和多线芯/扁形尺寸，不拆散成对数值。"""
    measurements: list[dict[str, Any]] = []
    number = r"[0-9]+(?:\.[0-9]+)?"
    requirement_pattern = re.compile(
        rf"(最大|最小)\s*(?:\|\s*)?({number})(?:\s*[×xX]\s*({number}))?"
    )
    for heading in re.finditer(heading_pattern, plain_text, re.I):
        tail = plain_text[heading.start():heading.start() + 420]
        verdict_match = re.search(r"\|\s*([PFN])\s*(?:\||$)", tail, re.I)
        if not verdict_match:
            continue
        row_text = tail[:verdict_match.end()]
        requirements = list(requirement_pattern.finditer(row_text))
        if not requirements:
            continue
        reported_part = row_text[requirements[-1].end():verdict_match.start()]
        reported_pairs = [
            (float(left), float(right))
            for left, right in re.findall(rf"({number})\s*[×xX]\s*({number})", reported_part)
        ]
        reported_scalars = [
            float(value)
            for value in re.findall(rf"(?<![A-Za-z0-9.])({number})(?![A-Za-z0-9.]|\s*[×xX])", reported_part)
        ]
        parsed_requirements: list[tuple[str, tuple[float, ...]]] = []
        for requirement in requirements:
            direction, left, right = requirement.groups()
            parsed_requirements.append((
                direction,
                (float(left), float(right)) if right is not None else (float(left),),
            ))
        if any(len(values) == 2 for _, values in parsed_requirements):
            reported_values = reported_pairs
        else:
            reported_values = [(value,) for value in reported_scalars]
        if not reported_values:
            continue

        def satisfies(direction: str, limits: tuple[float, ...], values: tuple[float, ...]) -> bool:
            if len(limits) != len(values):
                return False
            comparisons = zip(values, limits)
            return all(
                value <= limit + 1e-9 if direction == "最大" else value + 1e-9 >= limit
                for value, limit in comparisons
            )

        numeric_pass = all(
            all(satisfies(direction, limits, values) for direction, limits in parsed_requirements)
            for values in reported_values
        )
        verdict = verdict_match.group(1).upper()
        render_values = lambda values: "×".join(f"{value:g}" for value in values)
        measurements.append({
            "heading": re.sub(r"\s+", "", heading.group(0)),
            "reported_text": "/".join(render_values(values) for values in reported_values),
            "requirements_text": "、".join(
                f"{direction}{render_values(limits)}" for direction, limits in parsed_requirements
            ),
            "report_verdict": verdict,
            "passed": numeric_pass and verdict == "P",
            "excerpt": row_text[:420],
        })
    return measurements


def _plain_table_text(value: str) -> str:
    """把PDF文字层或MinerU HTML表格转成保留单元格边界的单行文本。"""
    text_value = html.unescape(str(value or ""))
    text_value = re.sub(r"</t[dh]>", " | ", text_value, flags=re.I)
    text_value = re.sub(r"<br\s*/?>", " | ", text_value, flags=re.I)
    text_value = re.sub(r"<[^>]+>", " ", text_value)
    text_value = text_value.replace("\n", " | ")
    text_value = re.sub(r"\s+", " ", text_value).strip()
    # PDF文字层可把 1.05 拆成“1. 05”；仅在小数点两侧都是数字时合并。
    return re.sub(r"(?<=\d)\.\s+(?=\d)", ".", text_value)


def _tabular_measurements(plain_text: str, heading_pattern: str) -> list[dict[str, Any]]:
    """提取同名表格行；要求、结果和P/F必须同时存在。"""
    measurements: list[dict[str, Any]] = []
    for heading in re.finditer(heading_pattern, plain_text, re.I):
        tail = plain_text[heading.start():heading.start() + 520]
        # 同一页可能在“耐矿物油后的性能”再次出现同名空行。
        # 如果不在下一个表格行/分组标题处截断，会把后面热延伸的
        # “最大175 / 13 / P”错绑到护套老化后拉力。
        following = tail[heading.end() - heading.start():]
        next_row = re.search(
            r"\|\s*(?:"
            r"老化前抗张强度|老化前断裂伸长率|"
            r"老化后抗张强度|老化前后抗张强度变化率|"
            r"老化后断裂伸长率|老化前后断裂伸长率变化率|"
            r"高温压力|耐矿物油后的性能|热延伸试验|"
            r"低温(?:弯曲|卷绕)试验|低温拉伸试验|低温冲击试验|耐臭氧试验|"
            r"注\s*[:：]"
            r")",
            following,
            re.I,
        )
        if next_row:
            tail = tail[:heading.end() - heading.start() + next_row.start()]
        verdict_match = re.search(r"\|\s*([PFN])\s*(?:\||$)", tail, re.I)
        if not verdict_match:
            continue
        row_text = tail[:verdict_match.end()]
        verdict = verdict_match.group(1).upper()
        requirement = re.search(
            r"(最小|最大)\s*(?:\|\s*)?([±＋－+-])?\s*(?:\|\s*)?([0-9]+(?:\.[0-9]+)?)",
            row_text,
        )
        explicit_no_requirement = (
            verdict == "N"
            and bool(re.search(r"(?:\||mm[²2]?)\s*[—－-]\s*\|", row_text, re.I))
        )
        if not requirement and not explicit_no_requirement:
            continue
        reported_start = requirement.end() if requirement else re.search(
            r"(?:\||mm[²2]?)\s*[—－-]\s*\|", row_text, re.I
        ).end()
        reported_part = row_text[reported_start:verdict_match.start()]
        reported_part = reported_part.replace("＋", "+").replace("－", "-")
        reported_values = [
            float(value) for value in re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?", reported_part)
        ]
        if not reported_values or verdict not in {"P", "F", "N"}:
            continue
        if explicit_no_requirement:
            direction, signed, limit = "不判定", "", None
            numeric_pass = True
        else:
            direction, signed, limit_text = requirement.groups()
            limit = float(limit_text)
        if direction == "最小" and limit is not None:
            numeric_pass = all(value + 1e-9 >= limit for value in reported_values)
        elif signed and limit is not None:
            numeric_pass = all(abs(value) <= limit + 1e-9 for value in reported_values)
        elif limit is not None:
            numeric_pass = all(value <= limit + 1e-9 for value in reported_values)
        measurements.append({
            "heading": re.sub(r"\s+", "", heading.group(0)),
            "direction": direction,
            "signed": bool(signed),
            "limit": limit,
            "reported_values": reported_values,
            "report_verdict": verdict,
            "passed": numeric_pass and verdict in {"P", "N"},
            "excerpt": row_text[:420],
        })
    return measurements


def _is_sheath_mechanical_page(page_text: str) -> bool:
    """兼容横向表格和纵向OCR的“护套机械性能”栏目。"""
    compact = re.sub(r"\W+", "", _plain_table_text(page_text))
    if "护套机械性能" in compact:
        return True
    # 纵向文字层会把类别栏和试验条件交叉，例如：
    # “护\n老化条件...\n套机械\n时间168h\n性\n能”。
    return bool(
        re.search(r"护[\s\S]{0,160}套机械[\s\S]{0,160}性[\s\S]{0,80}能", page_text)
        or re.search(
            r"护[\s\S]{0,320}套[\s\S]{0,320}机[\s\S]{0,320}械[\s\S]{0,320}性[\s\S]{0,320}能",
            page_text,
        )
    )


def _sheath_mechanical_section(page_text: str) -> str:
    """Scope values to the sheath section, not any page mentioning sheath."""
    separator = r"(?:\s|\||<[^>]*>)*"
    sheath = re.search(separator.join('护套机械性能'), page_text)
    insulation = re.search(separator.join('绝缘机械性能'), page_text)
    if sheath:
        section = page_text[sheath.start():]
        following = re.search(separator.join('绝缘机械性能'), section)
        section = section[:following.start()] if following else section
        # Preserve the format marker for downstream HTML-vs-vertical parsing.
        return ('<table>' if '<table' in page_text.lower() else '') + section
    if insulation:
        return ''
    # Interleaved vertical categories cannot be safely offset by character
    # position; retain only pages already classified as sheath-only candidates.
    return page_text if _is_sheath_mechanical_page(page_text) else ''


def _inline_mechanical_measurement(page_text: str, heading_pattern: str) -> dict[str, Any] | None:
    """Read a complete tab-separated row, not numbers from adjacent rows."""
    if '<table' in page_text.lower():
        return None
    # Oven-aging and non-pollution repeat headings; don't substitute the latter.
    segment = re.split(r'非污染试验', page_text, maxsplit=1)[0] if '老化' in heading_pattern else page_text
    candidates = []
    for line in segment.splitlines():
        heading = re.search(heading_pattern, line)
        if not heading or '\t' not in line:
            continue
        requirement = re.search(r'(最大|最小)\s*(±)?\s*([+\-]?\d+(?:\.\d+)?)\s*\t(.+?)\t\s*([PFN])\s*$', line)
        if not requirement:
            continue
        direction, signed, limit, values, verdict = requirement.groups()
        tokens = re.split(r'\s+', values.strip())
        if not tokens or not all(re.fullmatch(r'[+\-−]?\d+(?:\.\d+)?', token) for token in tokens):
            continue
        values = [float(token.replace('−','-')) for token in tokens]
        limit = float(limit)
        if direction == '最大' and not signed and limit < 0 and '变化率' in line[heading.start():].split('\t',1)[0]:
            # A maximum negative change denotes the allowed decrease, not an
            # upper bound on the signed value (SE4 table2 footnote b).
            direction = '最小'
        passed = all(abs(v) <= limit if signed else v <= limit if direction == '最大' else v >= limit for v in values)
        candidates.append({'heading':line[heading.start():].split('\t',1)[0], 'direction':direction,
                           'signed':bool(signed), 'limit':limit, 'reported_values':values,
                           'report_verdict':verdict, 'passed':passed and verdict=='P', 'excerpt':line[:520]})
    return candidates[0] if len(candidates) == 1 else None


def _vertical_rapidocr_measurement(
    page_text: str,
    heading_pattern: str,
) -> dict[str, Any] | None:
    """读取RapidOCR逐行排列的单个表格项。

    只有项目名、最大/最小限值、实测值和P/F同时存在才返回；
    调用方先限定部件；老化仅在非污染试验之前取数，同名歧义不猜。
    """
    inline = _inline_mechanical_measurement(page_text, heading_pattern)
    if inline is not None:
        return inline
    segment = str(page_text or "")
    if '老化' in heading_pattern:
        segment = re.split(r'非污染试验', segment, maxsplit=1)[0]
    lines = [line.strip() for line in segment.splitlines() if line.strip()]
    heading_indexes = [
        index for index, line in enumerate(lines)
        if re.search(heading_pattern, line, re.I)
    ]
    if len(heading_indexes) != 1:
        return None
    heading_index = heading_indexes[0]
    window = lines[heading_index:heading_index + 18]
    # A missing value/P must not consume the next measurement row's data.
    for index, line in enumerate(window[1:], 1):
        if re.search(r'老化(?:前后|前|后)(?:抗张强度|断裂[伸仲]长率)|高温压力|低温(?:弯曲|拉伸|冲击)|失重试验|热延伸|非污染试验', line):
            window = window[:index]
            break
    verdict_index = next((
        index for index, line in enumerate(window[1:], 1)
        if re.fullmatch(r"[PFN]", line, re.I)
    ), None)
    if verdict_index is None:
        return None
    verdict = window[verdict_index].upper()
    direction_index = next((
        index for index, line in enumerate(window[1:verdict_index], 1)
        if re.search(r"最大|最小", line)
    ), None)
    if direction_index is None:
        return None
    direction_line = window[direction_index]
    direction = "最大" if "最大" in direction_line else "最小"
    signed = "±" in direction_line

    numeric_token = re.compile(r"^[±+＋\-−－]?[0-9]+(?:\.[0-9]+)?$")
    candidates: list[tuple[int, str]] = []
    inline_tail = re.split(r"最大|最小", direction_line, maxsplit=1)[-1]
    inline_tail = re.sub(r"\s+", "", inline_tail)
    if numeric_token.fullmatch(inline_tail):
        candidates.append((direction_index, inline_tail))
    candidates.extend(
        (index, re.sub(r"\s+", "", line))
        for index, line in enumerate(window[direction_index + 1:verdict_index], direction_index + 1)
        if numeric_token.fullmatch(re.sub(r"\s+", "", line))
    )
    if len(candidates) < 2:
        return None
    limit_index, limit_token = candidates[0]
    signed = signed or "±" in limit_token
    reported_tokens = [token for index, token in candidates[1:] if index > limit_index]
    if not reported_tokens:
        return None

    def number(token: str) -> float:
        normalized = token.replace("±", "").replace("＋", "+").replace("−", "-").replace("－", "-")
        return float(normalized)

    limit = abs(number(limit_token)) if signed else number(limit_token)
    if direction == '最大' and not signed and limit < 0 and '变化率' in lines[heading_index]:
        direction = '最小'
    reported_values = [number(token) for token in reported_tokens]
    numeric_pass = all(
        abs(value) <= limit + 1e-9 if signed
        else value <= limit + 1e-9 if direction == "最大"
        else value + 1e-9 >= limit
        for value in reported_values
    )
    return {
        "heading": lines[heading_index],
        "direction": direction,
        "signed": signed,
        "limit": limit,
        "reported_values": reported_values,
        "report_verdict": verdict,
        "passed": numeric_pass and verdict == "P",
        "excerpt": " | ".join(window[:verdict_index + 1])[:520],
    }


def _sheath_material_from_description(source_group: dict[str, Any]) -> set[str]:
    pattern = r'(?<![A-Z0-9])SE\s*([34])(?![A-Z0-9])'
    descriptions = []
    for page in source_group.get('pages') or []:
        plain = _plain_table_text(str(page.get('text') or ''))
        for match in re.finditer(r'样品描述\s*[:：]([\s\S]*?)(?:备\s*注\s*[:：]|$)', plain):
            # The group is already uniquely sample-bound. Public annex lists
            # are not a sample description; contradictory descriptions remain.
            if '护套' in match[1]:
                descriptions.append(match[1])
    if descriptions:
        return set(re.findall(pattern, ' '.join(descriptions), re.I))
    return set(re.findall(pattern, str(source_group.get('text') or ''), re.I))


def _display_change_overlap(before: str, after: str, reported: float) -> bool:
    """Possibility only under decimal rounding, never proof of correctness."""
    if not all(re.fullmatch(r'\d+(?:\.\d+)?', token) for token in (before, after)):
        return False
    def bounds(token):
        decimals = len(token.split('.')[1]) if '.' in token else 0
        half = 0.5 * 10 ** -decimals
        return float(token)-half, float(token)+half
    b0,b1 = bounds(before); a0,a1 = bounds(after)
    if b0 <= 0 or a0 < 0:
        return False
    low,high = (a0/b1-1)*100, (a1/b0-1)*100
    return low <= reported+0.5 and high >= reported-0.5


def _rubber_sheath_aging_n_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any]) -> dict[str, Any] | None:
    """Recover complete SE3/SE4 oven aging, not a blanket exemption for N."""
    if str(matrix_row.get('item_code') or '').upper() != 'SHEATH_TENSILE_AFTER':
        return None
    if not re.search(r'5013|8735', str(matrix_row.get('standard_no') or '')):
        return None
    materials = _sheath_material_from_description(source_group)
    if len(materials) != 1:
        return None
    material = next(iter(materials))
    for page in source_group.get('pages') or []:
        section = _sheath_mechanical_section(str(page.get('text') or ''))
        if not section or '<table' in section.lower():
            continue
        section = re.split(r'耐矿物油|浸矿物油|非污染试验|热延伸试验', section, maxsplit=1)[0]
        # Four separate rows and one scalar per row; never borrow a result
        # from another aging method, component, or an unbound material.
        absent = re.search(r'老化后抗张强度\s*[-—－一]{0,3}\s*中间值\s*\nN/mm[²2]\s*(?:最小)?\s*[-—－/]+\s*\n(\d+(?:\.\d+)?)\s*\nN\b', section)
        if not absent:
            continue
        patterns = (r'老化前抗张强度', r'老化前断裂[伸仲]长率',
                    r'老化前后抗张强度变化率', r'老化后断裂[伸仲]长率\s*[-—－一]{0,3}\s*中间值',
                    r'老化前后断裂[伸仲]长率变化率')
        rows = [_vertical_rapidocr_measurement(section, pattern) for pattern in patterns]
        if any(not row or len(row['reported_values']) != 1 for row in rows):
            continue
        before_t, before_e, change_t, after_e, change_e = rows
        if before_t['reported_values'][0] <= 0 or before_e['reported_values'][0] <= 0:
            continue
        condition = re.search(r'空气烘箱老化后的性能([\s\S]*?)老化后抗张强度', section)
        compact = re.sub(r'\s+', '', condition[1]) if condition else ''
        temperature = re.search(r'温度([+\-]?\d+(?:\.\d+)?)(?:±2)?℃', compact)
        duration = re.search(r'时间(\d+(?:\.\d+)?)(?:[×x*](\d+(?:\.\d+)?))?h', compact)
        if not temperature or not duration:
            continue
        hours = float(duration[1]) * (float(duration[2]) if duration[2] else 1)
        after_t = float(absent[1])
        changes = [(after_t/before_t['reported_values'][0]-1)*100,
                   (after_e['reported_values'][0]/before_e['reported_values'][0]-1)*100]
        rounded = [int(abs(value)+0.5)*(1 if value>=0 else -1) for value in changes]
        expected = [('最大',True,20),('最小',False,250),('最大',True,20)] if material=='3' else [('最小',False,-15),('最小',False,250),('最小',False,-25)]
        issues = []
        for row, requirement in zip((change_t,after_e,change_e),expected):
            if (row['direction'],row['signed'],row['limit']) != requirement:
                issues.append('报告限值与材料标准不一致')
            value = row['reported_values'][0]
            direction,signed,limit = requirement
            passed = abs(value)<=limit if signed else value>=limit
            if not passed or row['report_verdict']!='P':
                issues.append('实测值或评定不符合材料要求')
        precision_questions = []
        def displayed_token(row):
            matched = re.search(r'(\d+(?:\.\d+)?)\s*(?:\||\t)\s*[PF]\s*$', row['excerpt'])
            return matched[1] if matched else ''
        for index,(before_token,after_token,row) in enumerate(((displayed_token(before_t),absent[1],change_t),
                                                               (displayed_token(before_e),displayed_token(after_e),change_e))):
            reported_change = row['reported_values'][0]
            if rounded[index] != reported_change:
                if _display_change_overlap(before_token,after_token,reported_change):
                    precision_questions.append(f"{'抗张强度' if index==0 else '断裂伸长率'}：显示中间值{before_token}→{after_token}，名义复算{rounded[index]}%，报告{reported_change:g}%；可能涉及修约，须核实未修约中间值和计算记录")
                else:
                    issues.append('变化率与老化前后中间值复算不一致')
        if not 68 <= float(temperature[1]) <= 72 or hours != 240:
            issues.append('老化温度或时长不符合70±2℃/240h')
        return {'category':'护套机械性能','item':str(matrix_row.get('item_name') or '护套老化后拉力试验'),
                'reported':f"SE{material}；老化后抗张{after_t:g}（单独最低值不要求，N）；抗张变化率{change_t['reported_values'][0]:g}%；老化后伸长{after_e['reported_values'][0]:g}%；伸长变化率{change_e['reported_values'][0]:g}%；{temperature[1]}℃/{hours:g}h",
                'required':f"SE{material}护套表2：抗张变化率{'±20' if material=='3' else '不低于-15'}%；老化后伸长≥250%；伸长变化率{'±20' if material=='3' else '不低于-25'}%；70±2℃/240h",
                'verdict':'fail' if issues else 'manual_review' if precision_questions else 'pass','basis':_matrix_reference(matrix_row),
                'note':'；'.join(dict.fromkeys(issues+precision_questions)) if issues or precision_questions else '同样品材料绑定；按表2核值并复算变化率。抗张最低值N不等于整个老化项目不适用。',
                'source_pages':[int(page['page'])],'source_excerpt':section[-1400:],
                'evidence_status':'located','coverage_origin':'rubber_sheath_aging_n_recovery',
                'precision_review_action':'；'.join(precision_questions),
                'calculated_changes':rounded}
    return None


def _pressure_force_verdict(reported_text: str | None, expected: float | None) -> dict[str, Any]:
    """向下化整≤3%为允许带；报告显示末位一个量化步长内的上偏按显示修约放行。

    业主确认：报告荷载为设备设定值，超出复算值不超过其自身显示精度
    一个量化步长（如4.15 vs 4.14304）时按显示修约放行，仅核报告显示，
    不代表设备实际加荷证明；更大偏差维持原判定不变。"""
    if reported_text is None:
        return {'verdict':'unknown', 'reason':'missing_input'}
    from decimal import Decimal, ROUND_HALF_UP
    value = Decimal(reported_text)
    if value <= 0:
        return {'verdict':'fail','reason':'nonpositive_applied_force'}
    if expected is None:
        return {'verdict':'unknown', 'reason':'missing_input'}
    upper = Decimal(str(expected))
    lower = upper * Decimal('0.97')
    if lower <= value <= upper:
        return {'verdict':'pass'}
    quantum = Decimal(1).scaleb(value.as_tuple().exponent)
    if quantum <= Decimal('0.01') and value == upper.quantize(quantum, rounding=ROUND_HALF_UP):
        return {'verdict':'pass', 'reason':'reported_display_matches_calculation',
                'verification_scope':'report_display_consistency','equipment_force_verified':False,
                'display_note':'报告荷载与复算值按所示小数位修约后一致；仅核对报告显示，不代表设备实际加荷证明，不改变向下化整不超过3%的规定。'}
    if quantum <= Decimal('0.01') and value > upper and value - upper <= quantum:
        return {'verdict':'pass', 'reason':'reported_display_rounding_above_calculation',
                'verification_scope':'report_display_consistency','equipment_force_verified':False,
                'display_note':'报告荷载超出复算值不超过其自身显示末位一个量化步长，属设定值显示修约；仅核对报告显示，不代表设备实际加荷证明，不改变向下化整不超过3%的判定规定。'}
    interval = (value - quantum/2, value + quantum/2)
    # 记录显示0N不视为有效加荷，不能靠宽显示区间放行。
    if value > 0 and interval[0] < upper and interval[1] > lower:
        return {'verdict':'unknown', 'reason':'display_precision_overlap',
                'reported_text':reported_text,
                'display_interval':[str(v) for v in interval],
                'review_action':f'报告荷载{reported_text}N，按显示尺寸复算{expected:.6g}N；差异可能来自末位显示舍入。核实未修约荷载或设备设定记录；此处未扩大量值允许范围。'}
    return {'verdict':'fail'}


def _pressure_dimensions(text: str) -> tuple[Any, Any]:
    """提取明示尺寸行P/F前的实测列，不以最大/最小标准列代替。"""
    # 保留文本层并追加逐行HTML副本；不展开rowspan或凭列位置猜测。
    text = re.sub(r'(?<=外径)[—－-]{2,}(?=平均外径)', '-', text)
    html_rows = re.findall(r'<tr\b[^>]*>.*?</tr>', text, re.I | re.S)
    horizontal = []
    # 某些MinerU结果把整张“结构”表压成一行，但仍保留了各个<td>。
    # 只解析“实测值单元格末尾”的固定字段序列：护套颜色、平均厚度、
    # 最薄厚度、外径/扁形两轴、椭圆度（圆形时）。标准限值位于另一
    # 单元格，不参与本提取；候选单元格后还必须有纯P/F/N评定单元格。
    colour = r'(?:黑色|白色|灰色|红色|蓝色|棕色|黄色|绿色|橙色|透明色|透明)'
    number = r'\d+(?:\.\d+)?'
    for row in html_rows:
        compact_row = re.sub(r'<[^>]+>|\s|&nbsp;', '', row, flags=re.I)
        if not all(marker in compact_row for marker in (
                '结构', '护套平均厚度', '护套最薄处厚度')):
            continue
        if not re.search(r'(?:外径[-—]平均外径|外形尺寸[-—]平均外径)', compact_row):
            continue
        cells = [re.sub(r'<[^>]+>|\s|&nbsp;', '', cell, flags=re.I)
                 for cell in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.I | re.S)]
        for index, cell in enumerate(cells[:-1]):
            verdict = cells[index + 1]
            if not re.fullmatch(r'[PFN]+', verdict) or 'P' not in verdict:
                continue
            flat = re.search(colour + r'(' + number + r')(' + number + r')('
                             + number + r')[×x](' + number + r')$', cell)
            if flat:
                horizontal.append((float(flat[1]), (float(flat[3]), float(flat[4]))))
                continue
            # 圆形结构表的末尾固定为：平均厚度(1位小数)、最薄厚度
            # (1~2位小数)、平均外径(1位小数)、椭圆度(整数)。限定末尾
            # 和字段精度，避免从前面的导体/绝缘多组数值中切分猜测。
            round_value = re.search(colour + r'(\d+\.\d)(\d+\.\d{1,2})'
                                    r'(\d{1,2}\.\d)(\d{1,2})$', cell)
            if round_value:
                horizontal.append((float(round_value[1]), (float(round_value[3]),)))
    if html_rows:
        text += '\n' + '\n'.join(re.sub(r'<[^>]+>', ' ', row) for row in html_rows)
    def values(heading):
        found = set()
        if heading.startswith(r'(?:外径'):
            number = r'\d+(?:\.\d+)?'
            grouped = (r'外径[-—]平均外径\s*外形尺寸[-—]平均外径[（(]圆[）)]\s*'
                       r'椭圆度\s*mm\s*mm\s*%\s*最大\s*'+number+
                       r'(?:\s*最小\s*'+number+r')?\s*最大\s*15\s+'
                       r'(?P<round_d>'+number+r')(?:\s+(?P=round_d))*\s+[PF]\s+'
                       r'(?P<round_e>'+number+r')(?:\s+(?P=round_e))*\s+[PF](?=\s|$)')
            for match in re.finditer(grouped, text):
                found.add((float(match['round_d']),))
        for match in re.finditer(heading + r'(.*?)(?=\b[PFN]\b)', text, re.S):
            block = match[1]
            if len(block)>220 or re.search(r'护套最薄|椭圆度|标志|导体|绝缘',block):
                continue
            # 不同模板交换mm与最小/最大的位置，并可能附带另一外径行标题。
            block = re.sub(r'外形尺寸[-—]平均外径(?:[（(][圆扁][）)])?', ' ', block)
            block = re.sub(r'\bmm\b', ' ', block, flags=re.I)
            block = re.sub(r'(?:最大|最小)\s*\d+(?:\.\d+)?(?:\s*[×x]\s*\d+(?:\.\d+)?)?\s*[,，]?', ' ', block)
            # Multi-core reports may repeat the same scalar/result in adjacent
            # result columns.  Accept only exact repetitions; concatenating
            # ``16.7 16.7`` used to fabricate 66.0 from a later ellipse row.
            tokens = re.findall(r'\d+(?:\.\d+)?(?:[×x]\d+(?:\.\d+)?)?', block)
            if tokens and len(set(tokens)) == 1:
                found.add(tuple(float(v) for v in re.split('[×x]', tokens[0])))
        return next(iter(found)) if len(found)==1 else None
    horizontal = list(dict.fromkeys(horizontal))
    horizontal_thick = horizontal[0][0] if len(horizontal) == 1 else None
    horizontal_diam = horizontal[0][1] if len(horizontal) == 1 else None
    thick = values(r'护套平均厚度\s*')
    diam = values(r'(?:外径[-—]平均外径|外形尺寸[-—]平均外径(?:[（(][圆扁][）)])?|外形尺寸平均值)\s*')
    if thick is None and horizontal_thick is not None:
        thick = (horizontal_thick,)
    if diam is None and horizontal_diam is not None:
        diam = horizontal_diam
    return (thick[0] if thick and len(thick)==1 else None), diam


def _pressure_product_profile(matrix_row: dict[str, Any]) -> dict[str, Any] | None:
    """已查证产品标准的压力参数；方法计算与产品参数分离。"""
    if str(matrix_row.get('item_code') or '') != 'HEAT_PRESS_SHEATH':
        return None
    standard = str(matrix_row.get('standard_no') or '')
    if '8734' in standard:
        return {'temperatures':{'4':80.0, '5':70.0}, 'basis':'JB/T 8734.1-2016表2'}
    if re.search(r'5023\.5(?:-2008)?(?!\d)', standard):
        return {'temperatures':{'5':70.0}, 'basis':'GB/T 5023.1-2008表2项次5；GB/T 5023.5-2008 5.3.4/6.3.4'}
    return None


def local_sheath_observations(sample: dict[str, Any], source_text: str,
                              evidence: dict[str, Any], source_pdf_sha256: str, *, component: str = 'sheath') -> dict[str, Any]:
    """Bind collected rows to one source specimen and sheath-only pages.

    This returns evidence, never a pass/fail decision or replacement OCR text.
    The caller must use the collector's validated receipt and verified limits.
    """
    import hashlib
    from backend.app.extract import _valid_local_table_evidence
    from backend.app.mineru_pages import (coordinate_row_observations,
        coordinate_pressure_conditions, coordinate_oven_aging_observations,
        coordinate_mechanical_component, coordinate_insulation_resistance_observation)
    if component not in {'sheath','insulation'}:
        return {'status':'unresolved','reason':'unsupported_component','observations':[]}
    if not isinstance(evidence,dict) or not isinstance(evidence.get('identity'),dict):
        return {'status':'unresolved','reason':'source_evidence_identity_mismatch','observations':[]}
    identity = evidence.get('identity') or {}
    if (not re.fullmatch(r'[0-9a-f]{64}', source_pdf_sha256 or '')
            or identity.get('source_sha256') != source_pdf_sha256
            or identity.get('text_sha256') != hashlib.sha256(source_text.encode()).hexdigest()
            or not _valid_local_table_evidence(evidence, identity)):
        return {'status':'unresolved','reason':'source_evidence_identity_mismatch','observations':[]}
    registry = source_sample_registry(source_text)
    group = source_group_for_sample(sample, registry=registry)
    if group is None:
        return {'status':'unresolved','reason':'sample_not_uniquely_bound','observations':[]}
    entries = [entry for entry in registry if entry['group'] is group]
    if len(entries)!=1:
        return {'status':'unresolved','reason':'sample_not_uniquely_bound','observations':[]}
    entry=entries[0]
    local_pages={p['page']:p for p in evidence['pages']}
    # The cached collector receipt keeps only coordinate rows; re-attach the
    # validated source text so row-level HTML backfill can consult the
    # independent MinerU HTML table layer of the same physical page.
    # _sample_source_pages returns specimen groups; flatten to pages.
    source_page_text={p['page']:p.get('text','')
        for group in _sample_source_pages(source_text)
        for p in group.get('pages') or []}
    for local_page in local_pages.values():
        local_page.setdefault('text',source_page_text.get(local_page.get('page'),''))
    observations=[]
    skipped=[]
    for number in sorted({p['page'] for p in group['pages']}):
        if number not in local_pages:
            continue
        owners=[r for r in registry if any(p['page']==number for p in r['group']['pages'])]
        if len(owners)!=1:
            skipped.append({'page':number,'reason':'page_shared_by_specimens'})
            continue
        source_pages=[p for p in group['pages'] if p['page']==number]
        compact=re.sub(r'[\s|]+','',_plain_table_text('\n'.join(p['text'] for p in source_pages)))
        if component=='insulation' and '绝缘电阻' in compact:
            resistance=coordinate_insulation_resistance_observation(local_pages[number])
            if resistance.get('status')=='located':
                observations.append({**resistance,'item_code':'INSUL_RES','component':'insulation',
                    'source_specimen_id':entry['_source_specimen_id'],
                    'source_sample_key':list(entry['key']),
                    'source_pdf_sha256':source_pdf_sha256})
        wanted,other=('护套机械性能','绝缘机械性能') if component=='sheath' else ('绝缘机械性能','护套机械性能')
        category_recovery = None
        if wanted not in compact and other not in compact:
            category_recovery = coordinate_mechanical_component(local_pages[number])
        if other in compact or (wanted not in compact and not (
                category_recovery and category_recovery.get('status')=='located'
                and category_recovery.get('component')==component)):
            skipped.append({'page':number,'reason':'not_uniquely_sheath_mechanical'})
            continue
        patterns=(('HEAT_PRESS_SHEATH',r'^高温压力'),('SHEATH_LOSS_WEIGHT',r'^失重试验')) if component=='sheath' else (('LOSS_WEIGHT',r'^失重试验'),)
        for code, pattern in patterns:
            found=coordinate_row_observations(local_pages[number],pattern)
            for row in found['observations']:
                if code=='HEAT_PRESS_SHEATH':
                    row=dict(row,condition_observations=coordinate_pressure_conditions(local_pages[number],row))
                else:
                    row=dict(row,condition_observations=coordinate_pressure_conditions(local_pages[number],row,kind='loss'))
                observations.append({**row,'item_code':code,'component':component,
                    'component_recovery':category_recovery,
                    'source_specimen_id':entry['_source_specimen_id'],
                    'source_sample_key':list(entry['key']),
                    'source_pdf_sha256':source_pdf_sha256})
        if component!='sheath':
            continue
        oven=coordinate_oven_aging_observations(local_pages[number])
        for row in oven['observations']:
            observations.append({**row,'item_code':'SHEATH_TENSILE_AFTER','component':'sheath',
                'aging_group':'air_oven','aging_conditions':oven.get('conditions',{}),
                'aging_before_observations':oven.get('before_observations',[]),
                'paddle_oven_context':local_pages[number].get('paddle_oven_context'),
                'source_specimen_id':entry['_source_specimen_id'],
                'source_sample_key':list(entry['key']),'source_pdf_sha256':source_pdf_sha256})
    from backend.app.ocr_table_repair import overlay_observations
    for page in local_pages.values():
        same_page=[row for row in observations if row.get('page')==page['page']]
        if same_page:
            replaced=overlay_observations(same_page,page,source_pdf_sha256)
            observations=[row for row in observations if row.get('page')!=page['page']]+replaced
    return {'status':'bound' if observations else 'unresolved','observations':observations,
            'skipped':skipped,'boundary':'Source-bound observations only; no standard or whole-test verdict'}


def _sheath_pressure_section(page_text: str) -> str:
    """明确部件的压力单项可以独立于混合机械性能大栏定位。"""
    # Paddle 文本层用 "°C" 两个字符表示度摄氏，而原生文本层和内嵌
    # 正则使用单字符 "℃"；进入段落切分前统一，否则温度条件无法匹配。
    page_text = page_text.replace('°℃', '℃').replace('°C', '℃')
    starts = list(re.finditer(r'护套\s*高温压力', page_text))
    if len(starts) == 1:
        tail = page_text[starts[0].start():]
        end = re.search(r'(?:绝缘|护套)?低温|热冲击|热稳定|老化|注\s*[:：]', tail)
        return tail[:end.start()] if end else tail
    if starts:
        return ''  # 多个独立记录尚未建立一一对应关系，不能只取第一个。
    return _sheath_mechanical_section(page_text)


def _pressure_source_conditions(matrix_row: dict[str, Any], source_group: dict[str, Any], recovered: dict[str, Any], report_text: str = "") -> dict[str, Any]:
    """PVC护套条件分项核验；只使用绑定样品的明确材料/实测输入。"""
    profile = _pressure_product_profile(matrix_row)
    if profile is None:
        return recovered
    def incomplete(field, reason):
        result = dict(recovered)
        result['pressure_condition_checks'] = [{'field':field, 'reported':None,
            'required':reason, 'verdict':'unknown'}]
        if result.get('verdict') != 'fail':
            result['verdict'] = 'manual_review'
        result['note'] = str(result.get('note') or '') + '；护套高温压力独立核验未完成：' + reason
        result['deterministic_review_action'] = reason + '；取得同一样品原页证据后重新核验，不能沿用模型P自动通过'
        return result
    pages = source_group.get('pages') or []
    descriptions = []
    for page in pages:
        text = str(page.get('text') or '')
        match = re.search(r'样品描述\s*[:：](.*?)(?:备\s*注\s*[:：]|$)', text, re.S)
        if match:
            descriptions.append(match[1])
    material_hits = set()
    for text in descriptions:
        # 材料牌号与类型常在同一括号内用分号分隔；分号不是新部件。
        # 只放宽明确护套括号的内部，不跨括号抓取供应商或其他部件材料。
        for clause in re.finditer(r'护套(?:材料|料)?\s*[（(]([^（）()]{1,120})[）)]', text):
            if not re.search(r'绝缘|导体|屏蔽', clause[1]):
                material_hits.update(re.findall(r'PVC\s*/\s*ST(\d{1,2})(?![A-Za-z0-9])', clause[1], re.I))
        for hit in re.finditer(r'护套(?:(?!绝缘|导体|屏蔽|[。；;]).){0,100}?PVC\s*/\s*ST(\d{1,2})(?![A-Za-z0-9])', text, re.I | re.S):
            material_hits.add(hit[1])
    material_source = 'bound_report_description'
    if not material_hits:
        material_hits = _verified_pvc_materials(matrix_row, 'sheath')
    if not material_hits and report_text:
        # 报告级回退：样品描述在全局附表（如第1页）而不在样品绑定页时，
        # 仅当全报告所有样品描述块恰好给出唯一护套等级、且该等级属于已
        # 核实产品温度档时才采用；多等级或与样品页冲突都保持人工。
        report_hits = set()
        for desc in re.findall(r'样品描述\s*[:：](.*?)(?:备\s*注\s*[:：]|$)', report_text, re.S):
            for clause in re.finditer(r'护套(?:材料|料)?\s*[（(]([^（）()]{1,120})[）)]', desc):
                if not re.search(r'绝缘|导体|屏蔽', clause[1]):
                    report_hits.update(re.findall(r'PVC\s*/\s*ST(\d{1,2})(?![A-Za-z0-9])', clause[1], re.I))
            for hit in re.finditer(r'护套(?:(?!绝缘|导体|屏蔽|[。；;]).){0,100}?PVC\s*/\s*ST(\d{1,2})(?![A-Za-z0-9])', desc, re.I | re.S):
                report_hits.add(hit[1])
        if len(report_hits) == 1:
            only_grade = next(iter(report_hits))
            if only_grade in profile['temperatures']:
                material_hits = {only_grade}
                material_source = 'report_level_description'
    material_from_requirement = not material_hits and len(profile['temperatures']) == 1
    if len(material_hits) != 1 and not material_from_requirement:
        return incomplete('护套材料绑定', '当前样品描述未唯一确定PVC/ST4或PVC/ST5，需核对护套材料及适用温度，不能借用相邻样品或默认温度')
    material = next(iter(profile['temperatures'] if material_from_requirement else material_hits))
    if material not in profile['temperatures']:
        return incomplete('护套材料适用性', '报告材料与已核实产品标准要求的护套材料不一致，需核对材料选型及适用标准')
    temperature_required = profile['temperatures'][material]
    records = []
    for page in pages:
        if page.get('page') not in recovered.get('source_pages', []):
            continue
        section = _sheath_pressure_section(str(page.get('text') or ''))
        section = section.replace('°℃', '℃')
        for match in re.finditer(r'高温压力[^\n]*\n(.*?)(?=低温|热冲击|热稳定|老化|\Z)', section, re.S):
            block = match[0]
            # 未展开的HTML可能把高温压力、低温等全部项目塞入一行，
            # 不能当作另一份完整条件记录。仅有此类版式时仍返回unknown。
            if re.search(r'</?(?:td|tr|table)\b', block, re.I):
                continue
            def number(pattern):
                values = {float(v) for v in re.findall(pattern, block, re.I)}
                return next(iter(values)) if len(values) == 1 else None
            def force_number():
                # 串值荷载：同一行逐芯记录（如 1.45/1.45/1.45N 或
                # 0.79N/0.79N）。全部相同取该值；存在不同值说明是逐芯
                # 荷载，保持 None 不猜测。
                match = re.search(r'施加(?:压力|荷载|负荷)\s*[:：]?\s*([\d\s./／]+?(?:\s*N\s*[/／]\s*[\d\s./／]+?)*)\s*N', block, re.I)
                if not match:
                    return None
                values = {float(v) for v in re.findall(r'[+-]?\d+(?:\.\d+)?', match.group(1))}
                return next(iter(values)) if len(values) == 1 else None
            records.append((page['page'], block, number(r'温度\s*[（(]?\s*([+-]?\d+(?:\.\d+)?)(?:\s*±\s*2)?\s*[）)]?\s*℃'),
                            number(r'时间\s*([+-]?\d+(?:\.\d+)?)\s*h'),
                            force_number()))
    # HTML整行标题后可能仅剩页脚；页脚没有任何试验条件，不作为第二份条件记录。
    records = [record for record in records if any(value is not None for value in record[2:])]
    if not records:
        from backend.app.ocr_pressure_rows import pressure_condition_record
        for page in pages:
            if page.get('page') not in recovered.get('source_pages', []):
                continue
            parsed = pressure_condition_record(_sheath_pressure_section(str(page.get('text') or '')))
            if parsed:
                records.append((page['page'], parsed['text'], parsed['temperature'], parsed['hours'], parsed['force']))
    if True:
        # HTML 整页表格（MinerU/视觉层）：从护套压力行起到下一试验项目
        # 止的行单元格解析温度/时间/荷载；只采纳唯一值，任何多值保持
        # unknown，不猜测。热冲击等前置项目的条件不在窗口内。
        # 与既有解析并存，由后面的跨记录合并去重；任何字段冲突都保持
        # 未完成，不猜测。
        from backend.app.mineru_pages import _html_table_rows
        for page in pages:
            if page.get('page') not in recovered.get('source_pages', []):
                continue
            section = _sheath_pressure_section(str(page.get('text') or ''))
            if '<table' not in section.lower():
                continue
            table_rows = _html_table_rows({'text': section})
            anchor = None
            for idx, cells in enumerate(table_rows):
                label = re.sub(r'[\s\-—－]', '', str(cells[0] if cells else ''))
                if label == '高温压力压痕深度中间值':
                    anchor = idx
                    break
            if anchor is None:
                continue
            flat = []
            for cells in table_rows[anchor:]:
                for cell in cells:
                    text = re.sub(r'\s+', '', str(cell)).replace('°℃', '℃').replace('°C', '℃')
                    if re.match(r'^(?:低温弯曲|热冲击试验|热稳定性)', text):
                        break
                    flat.append(text)
                else:
                    continue
                break
            block = '\n'.join(flat)
            temps = {float(m[1]) for t in flat for m in re.finditer(r'([+-]?\d+(?:\.\d+)?)℃', t)}
            hrs = {float(m[1]) for t in flat for m in re.finditer(r'([+-]?\d+(?:\.\d+)?)h$', t)}
            forces = {float(m[1]) for t in flat for m in re.finditer(r'([+-]?\d+(?:\.\d+)?)N$', t)}
            force_label = any(re.match(r'^施加(?:压力|荷载|负荷)', t) for t in flat)
            records.append((page['page'], block,
                            next(iter(temps)) if len(temps) == 1 else None,
                            next(iter(hrs)) if len(hrs) == 1 else None,
                            next(iter(forces)) if force_label and len(forces) == 1 else None))
    if not records and recovered.get('local_pressure_observations'):
        for observation in recovered['local_pressure_observations']:
            fields=(observation.get('condition_observations') or {}).get('fields') or {}
            if not fields:
                continue
            # Only labelled, high-confidence, coordinate-bounded condition
            # values enter the existing validator. No OCR limit is injected.
            values=[]
            for name in ('temperature','hours','force'):
                field = fields.get(name, {})
                value = field.get('value')
                if name in {'temperature', 'hours'}:
                    reliable = _condition_value_is_reliable(source_group, observation, name)
                    if value is None and reliable:
                        sub = [h for h in field.get('observations') or []
                               if h.get('value') is not None]
                        if len(sub) == 1 and .90 <= float(sub[0].get('confidence', 0)) < .95:
                            value = sub[0]['value']
                    if not reliable:
                        value = None
                # Force, signs and decimals never receive the 0.90 condition
                # exception; keep the original high-confidence requirement.
                if name == 'force' and any(float(hit.get('confidence', 0)) < .95
                                           for hit in field.get('observations') or []):
                    value = None
                values.append(value)
            lines=[hit['text'] for field in fields.values() for hit in field.get('observations') or []
                   if field.get('status')=='located']
            records.append((observation['page'],'\n'.join(lines),*values))
    if len(records) > 1:
        # 跨记录字段合并：同一绑定样品在多页各携带部分条件（如纵向
        # 页只有温度、机械性能页全量）时互补补齐；任何字段出现不同
        # 非空值即冲突，放弃合并保持未完成，不猜测。
        merged = list(records[0][2:])
        conflict = False
        for record in records[1:]:
            for index, value in enumerate(record[2:]):
                if value is None:
                    continue
                if merged[index] is None:
                    merged[index] = value
                elif merged[index] != value:
                    conflict = True
        if not conflict:
            records = [([record[0] for record in records],
                        '\n'.join(record[1] for record in records), *merged)]
    if len(records) != 1:
        return incomplete('试验条件记录', '当前护套压力原页未唯一提取温度、时间、荷载记录，需核对条件表格的行列对应关系')
    page_no, block, temperature, hours, force = records[0]
    force_match = re.search(r'施加(?:压力|荷载|负荷)\s*[:：]?\s*([\d\s./／]+?(?:\s*N\s*[/／]\s*[\d\s./／]+?)*)\s*N', block, re.I)
    if force_match:
        force_values = {float(v) for v in re.findall(r'[+-]?\d+(?:\.\d+)?', force_match.group(1))}
        force_text = f'{next(iter(force_values)):g}' if len(force_values) == 1 else None
    else:
        force_text = None
    all_text = '\n'.join(str(p.get('text') or '') for p in pages)
    thickness, axes = _pressure_dimensions(all_text)
    dimension_evidence = []
    for key,current in [('thickness',thickness),('diameter',axes)]:
        hits=[d[key] for d in source_group.get('_structure_dimensions',[]) if key in d]
        if len(hits)!=1:continue
        hit=hits[0]
        # 扁形结构借用值为两轴元组 (短轴,长轴)；圆形为标量。
        borrowed=hit['value'] if key=='thickness' else (
            tuple(hit['value']) if isinstance(hit['value'],(tuple,list)) else (hit['value'],))
        if current is not None:
            actual=current if key=='thickness' else tuple(current)
            if actual!=borrowed:return incomplete('结构尺寸证据冲突','原页坐标尺寸与现有提取值不一致，不自动覆盖或继续放行')
            continue
        if key=='thickness':thickness=hit['value']
        else:axes=borrowed
        dimension_evidence.append({'page':hit['page'],
            'sheath_mean_thickness_mm' if key=='thickness' else 'measured_axes_mm':hit['value'] if key=='thickness' else list(hit['value']),
            'origin':'original_pdf_coordinate_structure','coordinate_proof':hit['observation']})
    for page in pages:
        page_thickness, page_axes = _pressure_dimensions(str(page.get('text') or ''))
        evidence = {'page': page.get('page')}
        if thickness is not None and page_thickness == thickness:
            evidence['sheath_mean_thickness_mm'] = thickness
        if axes is not None and page_axes == axes:
            evidence['measured_axes_mm'] = list(axes)
        if len(evidence) > 1:
            dimension_evidence.append(evidence)
    models = set(re.findall(r'(?<![A-Z])(BLVV|BVV|BVVB|BLVVB|RVV|RVVP|RVVB)(?![A-Z])', '\n'.join(descriptions)))
    if not models:
        # 复用已经唯一匹配并通过启用门槛的型号矩阵，不再维护第二份
        # RVVPS/RVVP1等别名表，也不因缺样品描述丢失已确认的型号。
        models = set(re.findall(r'(?<![A-Z])(BLVV|BVV|BVVB|BLVVB|RVV|RVVP|RVVB)(?![A-Z])',
                                str(matrix_row.get('resolved_model_code') or '')))
    model = next(iter(models)) if len(models)==1 else None
    # RVV不能仅凭型号判圆形；以同样品实测单径/双轴为结构证据。
    flexible_shape = model == 'RVV'
    flat = model in {'BVVB','BLVVB','RVVB'} or bool(flexible_shape and axes and len(axes)==2)
    soft = model in {'RVV','RVVP','RVVB'}
    shape_valid = bool(model and axes and len(axes)==(2 if flat else 1) and all(v>0 for v in axes))
    diameter = min(axes) if shape_valid else None
    # 扁形两轴同属阈值一侧时，时间选择无歧义；跨15mm不猜测。
    expected_hours = (4.0 if max(axes)<=15 else 6.0 if min(axes)>15 else None) if shape_valid else None
    expected_force = None
    if diameter is not None and thickness is not None and 0 < thickness < diameter:
        expected_force = (0.6 if soft or diameter <= 15 else 0.7) * (2 * diameter * thickness - thickness ** 2) ** 0.5
    validations = []
    def add(field, value, required, state):
        validations.append(dict(field=field, reported=value, required=required, verdict=state))
    depth_match = re.search(r'最大\s*(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+[PF](?=\s|$)', block)
    if recovered.get('local_pressure_depth_check'):
        local_depth=recovered['local_pressure_depth_check']
        observations=local_depth.get('observations') or []
        depths={v['reported'] for v in observations if v.get('reported') is not None}
        add('压痕深度',next(iter(depths)) if len(depths)==1 else None,'≤50%',local_depth['verdict'])
        # The OCR requirement field is not authoritative; do not use a
        # misrecognized printed limit to change the standard or assert error.
    elif depth_match:
        declared_limit, depth = map(float, depth_match.groups())
        add('压痕深度', depth, '≤50%', 'pass' if depth <= 50 else 'fail')
        add('压痕限值填写', declared_limit, '最大50%', 'pass' if declared_limit == 50 else 'fail')
    else:
        add('压痕深度及限值', None, '实测压痕≤50%，限值最大50%；需同一行限值与实测列证据', 'unknown')
    add('温度', temperature, f'{temperature_required:g}±2℃', 'unknown' if temperature is None else 'pass' if abs(temperature-temperature_required)<=2 else 'fail')
    add('时间', hours, f'{expected_hours:g}h' if expected_hours is not None else '按试样外径及产品要求确定',
        'fail' if hours is not None and hours <= 0 else
        'unknown' if hours is None or expected_hours is None else 'pass' if hours == expected_hours else 'fail')
    force_evaluation = _pressure_force_verdict(force_text, expected_force)
    # The structure table is bound to the sample, but is not yet proven to be
    # the pressure specimen measurement record. A positive load discrepancy
    # therefore establishes a cross-check issue, not a confirmed test failure.
    # Non-positive loads remain independently invalid.
    if force_evaluation['verdict'] == 'fail' and force is not None and force > 0:
        force_evaluation = dict(force_evaluation,
            verdict='unknown', review_origin='pressure_specimen_input_binding',
            review_action=(f'报告荷载{force_text}N与结构表尺寸复算值{expected_force:.6g}N不一致；'
                '尚未确认结构表尺寸与高温压力试样的D及厚度为同一测量记录。'
                '请核对压力试样原始D、厚度和设备设定荷载，再按公式及向下化整不超过3%判定；'
                '当前不能据此确认不合格，也不能自动通过。'))
    add('荷载', force, f'{expected_force:g}N，可向下化整不超过3%' if expected_force is not None else '需实测试样D、厚度及适用k', force_evaluation['verdict'])
    validations[-1].update(force_evaluation)
    validations[-1]['reported_text'] = force_text
    failures = [v for v in validations if v['verdict']=='fail']
    missing = [v for v in validations if v['verdict']=='unknown']
    result = dict(recovered)
    result['pressure_condition_checks'] = validations
    result['pressure_calculation_inputs'] = {
        'origin': 'bound_sample_structure_measurements',
        'specimen_identity_verified': False,
        'dimension_evidence': dimension_evidence,
        'diameter_for_force_mm': diameter,
        'sheath_mean_thickness_mm': thickness,
        'expected_force_n': expected_force,
        'force_coefficient': (0.6 if soft or diameter <= 15 else 0.7) if diameter is not None and model else None,
        'diameter_selection_reason': '扁形结构按现有规则取短轴' if flat else '圆形结构按现有规则取实测外径',
        'coefficient_reason': ('已匹配软电缆型号，现有规则取k=0.6' if soft else
                               '非软电缆按D≤15 mm取k=0.6、D>15 mm取k=0.7') if model else '型号未唯一匹配',
        'time_selection_reason': ('实测尺寸均不大于15 mm，取4 h' if expected_hours==4 else
                                  '实测尺寸均大于15 mm，取6 h' if expected_hours==6 else '尺寸缺失或跨越15 mm阈值，不猜测时间'),
        'boundary': '来自同一样品结构测量表；尚不能单凭此字段证明与压力试样为同一测量记录。',
    }
    result['source_pages'] = sorted(set(result.get('source_pages', [])) | {
        e['page'] for e in dimension_evidence if isinstance(e.get('page'), int)})
    units = {'温度':'℃', '时间':'h', '荷载':'N', '压痕深度':'%', '压痕限值填写':'%'}
    result['reported'] += '；' + '；'.join(f"{v['field']}：{str(v['reported']) + units.get(v['field'], '') if v['reported'] is not None else '未提取'}" for v in validations)
    result['required'] += '；' + '；'.join(f"{v['field']}：{v['required']}" for v in validations)
    result['basis'] += '；' + profile['basis'] + '；GB/T 2951.31-2008 8.2.4/8.2.5'
    result['pressure_product_profile'] = profile
    result['pressure_material_requirement_source'] = (
        'product_standard' if material_from_requirement
        else 'report_level_description' if material_source == 'report_level_description'
        else 'bound_report_description')
    if material_from_requirement:
        result['note'] += '；试验条件按产品标准规定的PVC/ST' + material + '选择，不代表已验证报告实际材料组成'
    if material_source == 'report_level_description':
        result['note'] += '；护套材料取自报告级样品描述附表（全报告唯一PVC/ST' + material + '），非本样品绑定页'
    result['note'] += '；' + ('；'.join(v['field']+'不符合' for v in failures) if failures else '已独立核验可取得的护套试验条件')
    if missing:
        result['note'] += '；条件未完成：' + '、'.join(v['field'] for v in missing)
    if force_evaluation.get('review_action'):
        result['note'] += '；' + force_evaluation['review_action']
        if force_evaluation.get('review_origin') == 'pressure_specimen_input_binding':
            result['deterministic_review_action'] = force_evaluation['review_action']
            result['pressure_input_review_origin'] = 'pressure_specimen_input_binding'
        else:
            result['precision_review_action'] = force_evaluation['review_action']
            result['precision_review_origin'] = 'deterministic_pressure_force_display'
    if force_evaluation.get('display_note'):
        result['note'] += '；' + force_evaluation['display_note']
    if failures:
        result['verdict'] = 'fail'
        result['deterministic_review_action'] = '；'.join(
            f"{v['field']}：报告{v['reported']}{units.get(v['field'], '')}，要求{v['required']}" for v in failures)
    elif missing and result.get('verdict') == 'pass':
        result['verdict'] = 'manual_review'
    elif (not missing and result.get('coverage_origin')=='deterministic_local_pressure_observation'
          and result.get('verdict')=='manual_review'
          and result.get('local_pressure_depth_check',{}).get('verdict')=='pass'
          and all(v['verdict']=='pass' for v in validations)):
        # The local depth-only check started provisional. Resolve it only
        # after every independent condition passed, never on depth alone.
        result['verdict']='pass'
        result.pop('deterministic_review_action',None)
        result['note']+='；备用结果及本规则要求的条件已逐项核验完成'
    from backend.app.pressure_explanation import explain_pressure
    result['pressure_calculation'] = explain_pressure(result['pressure_calculation_inputs'], validations, profile)
    result['note'] += '\n【高温压力计算过程】\n' + result['pressure_calculation']['text']
    return result


def _ozone_source_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any] | None) -> dict[str, Any] | None:
    """Report-level IE4 ozone conditions; no missing-D or lab-procedure audit."""
    if (not source_group or matrix_row.get('item_code') != 'OZONE_RESIST'
            or _standard_key(str(matrix_row.get('standard_no') or '')) != '5013.4'
            or matrix_row.get('_resolved_insulation_materials') != ['IE4']):
        return None
    observations = []
    number = r'(-?\d+(?:\.\d+)?)'
    for page in source_group.get('pages') or []:
        compact = re.sub(r'[\s|]', '', _plain_table_text(str(page.get('text') or '')))
        binding=page.get('_cross_layer_binding') or {}
        provenance={}
        if binding and compact.count('耐臭氧试验')==1:
            from hashlib import sha256
            layer=binding.get('layer')
            if layer in {'native','supplement'} and binding.get('page')==page['page'] and binding.get(layer+'_sha256')==sha256(page['text'].encode()).hexdigest():
                provenance={'cross_layer_binding':binding}
        for title in re.finditer('耐臭氧试验', compact):
            headings = re.findall(r'(绝缘|护套)机械性能', compact[:title.start()])
            if not headings or headings[-1] != '绝缘' or compact[:title.start()].endswith('护套'):
                continue
            section = re.split(r'注[:：]|(?:绝缘|护套)机械性能|热延伸试验|低温\w*试验', compact[title.end():], maxsplit=1)[0][:320]
            condition_pattern = (r'试验条件[:：]温度'+number+r'(?:℃|°C)时间'+number+r'h浓度[（(]?'
                                 +number+r'(?:[～~—-]'+number+r')?[）)]?[%％]')
            # Preserve independently readable conditions even when the result
            # columns are damaged. A bad condition must not degrade to unknown.
            condition_match = re.match(condition_pattern, section)
            conditions = None
            if condition_match:
                temp, hours, low, high = condition_match.groups()
                conditions = tuple(float(v) for v in (temp, hours, low, high if high is not None else low))
            pattern = condition_pattern + r'无裂纹(无裂纹|通过|有裂纹|裂纹)([PFN])(?:TRF-C0101\.522024-7)?'
            match = re.fullmatch(pattern, section)
            if not match:
                observations.append({**provenance,'page':int(page['page']), 'excerpt':section, 'values':None,
                                     'conditions':conditions})
                continue
            temp,hours,low,high,result,claimed = match.groups()
            values = (float(temp),float(hours),float(low),float(high if high is not None else low),
                      '无裂纹' if result == '通过' else result,claimed)
            observations.append({**provenance,'page':int(page['page']), 'excerpt':section, 'values':values,
                                 'conditions':conditions, 'reported_result':result})
    if not observations:
        return None
    # Only a proven same-specimen/same-page secondary extraction can be
    # completed by the native text. Anonymous alternatives stay unresolved.
    for observation in observations:
        proof=observation.get('cross_layer_binding') or {}
        if observation['values'] is not None or proof.get('layer')!='supplement':continue
        damaged=observation['excerpt']
        if len(re.findall(r'N/(?:\$)?mm',damaged))<2 or re.search(r'有裂纹|出现裂纹|开裂|F',damaged):continue
        peers=[o for o in observations if o.get('values') is not None
               and (o.get('cross_layer_binding') or {}).get('layer')=='native'
               and all(o['cross_layer_binding'].get(k)==proof.get(k)
                       for k in ('inspection_number','page','native_sha256','supplement_sha256'))
               and o.get('conditions')==observation.get('conditions')]
        if len(peers)==1 and peers[0]['values'][-1]=='P':
            observation['resolved_duplicate_source']='native_same_specimen_page_unique_ozone_row'
    active=[o for o in observations if not o.get('resolved_duplicate_source')]
    parsed = [o['values'] for o in active if o['values'] is not None]
    failed = any(not (23<=t<=27 and h==24 and .025<=lo<=hi<=.030 and result=='无裂纹' and claim!='F')
                 for t,h,lo,hi,result,claim in parsed)
    failed = failed or any(not (23<=t<=27 and h==24 and .025<=lo<=hi<=.030)
                           for t,h,lo,hi in (o['conditions'] for o in observations if o.get('conditions')))
    incomplete = len(parsed)!=len(active) or len(set(parsed))!=1 or any(p[-1]=='N' for p in parsed)
    verdict = 'fail' if failed else 'manual_review' if incomplete else 'pass'
    # Display parsed fields, not the damaged multi-row OCR blob. Keep the
    # original excerpts below for traceability. Otherwise the generic slash
    # heuristic mistakes a proven condition failure for an extraction guess.
    display = []
    for observation in observations:
        if observation.get('resolved_duplicate_source'):continue
        conditions = observation.get('conditions')
        if conditions is None:
            display.append(f"第{observation['page']}页：试验条件未完整提取，需核对原表")
            continue
        t,h,lo,hi = conditions
        values = observation.get('values')
        appearance = (f"结果{observation.get('reported_result') or values[-2]}，报告判{values[-1]}"
                      if values else '结果列对应关系未恢复')
        display.append(f"第{observation['page']}页：温度{t:g}℃，时间{h:g}h，浓度{lo:g}%～{hi:g}%，{appearance}")
    return {'category':'绝缘机械性能','item':str(matrix_row.get('item_name') or '耐臭氧试验'),
            'item_code':'OZONE_RESIST','reported':'；'.join(dict.fromkeys(display)),
            'required':'IE4绝缘：25±2℃；24h；臭氧浓度0.025%～0.030%；无裂纹',
            'verdict':verdict, 'basis':_matrix_reference(matrix_row)+'；GB/T 5013.1-2008 表1项4；GB/T 2951.21-2008 8.1',
            'source_pages':sorted({o['page'] for o in observations}),
            'source_excerpt':'；'.join(dict.fromkeys(o['excerpt'] for o in observations))[:900],
            'evidence_status':'located','coverage_origin':'deterministic_ozone_report_conditions',
            'note':'只核对当前绝缘样品报告明确列明的四项，不推定其他实验操作已核验，不增加缺D检查',
            'deterministic_review_action':'核对耐臭氧原页温度、时间、浓度和裂纹结果；缺值、N或同源冲突不自动放行',
            'ozone_report_observations':observations}


def _local_pressure_depth_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any] | None) -> dict[str, Any] | None:
    """Evaluate source-bound PVC pressure depth, not the whole pressure test."""
    profile=_pressure_product_profile(matrix_row)
    if profile is None or not source_group:
        return None
    rows=[r for r in source_group.get('_local_sheath_observations') or []
          if r.get('item_code')=='HEAT_PRESS_SHEATH' and r.get('component')=='sheath']
    if not rows:
        return None
    values=[]
    for row in rows:
        raw=str(row.get('reported') or '')
        usable=(row.get('status')=='located' and row.get('unit')=='%'
                and bool(re.fullmatch(r'\d+(?:\.\d+)?%?',raw)))
        row_values=[float(raw.rstrip('%'))] if usable else []
        if not row_values:
            # 未定位行的逐芯候选值：同一视觉行携带多个实测值（如
            # 26 24 27），全部高置信且判定P时逐项纳入 ≤50% 核验。
            candidates=[]
            for hit in row.get('reported_values') or []:
                text=str(hit.get('text') or '').strip()
                if re.fullmatch(r'\d+(?:\.\d+)?',text) and float(hit.get('confidence',0))>=.95:
                    candidates.append(float(text))
            if candidates and row.get('report_verdict')=='P':
                row_values=candidates
        value=max(row_values) if row_values else None
        state=('fail' if row_values and (any(v>50 for v in row_values) or row.get('report_verdict')=='F')
               else 'pass' if row_values and row.get('report_verdict')=='P' else 'unknown')
        values.append({'page':row['page'],'reported':value,'values':row_values,'verdict':state})
    failed=any(v['verdict']=='fail' for v in values)
    depth_state='fail' if failed else 'pass' if values and all(v['verdict']=='pass' for v in values) else 'unknown'
    return {'category':'护套机械性能','item':str(matrix_row.get('item_name') or '护套高温压力试验'),
            'item_code':'HEAT_PRESS_SHEATH',
            'reported':'压痕：'+' / '.join(str(v['reported']) if v['reported'] is not None else '未可靠识别' for v in values)+'%',
            'required':'压痕深度≤50%；温度、时间、荷载另行独立核验',
            'verdict':'fail' if failed else 'manual_review',
            'basis':_matrix_reference(matrix_row)+'；'+profile['basis'],
            'source_pages':sorted({r['page'] for r in rows}),
            'source_excerpt':'；'.join(f"第{r['page']}页 {r['label']} 实测{r.get('reported')} 判{r.get('report_verdict')}" for r in rows),
            'evidence_status':'located','coverage_origin':'deterministic_local_pressure_observation',
            'local_pressure_observations':rows,
            'local_pressure_depth_check':{'field':'压痕深度','verdict':depth_state,'required':'≤50%','observations':values},
            'note':'使用既有PVC压力规则限值50%，不将备用OCR的要求列当作标准真值；只补结果证据，不代替其他条件',
            'deterministic_review_action':'整改压痕超限或报告F，并核对其余试验条件' if failed else '核对护套高温压力剩余温度、时间、荷载条件及来源冲突'}


def _verified_pvc_materials(matrix_row: dict[str, Any], component: str) -> set[str]:
    """Narrow, product-bound PVC material profile verified for 8734.5.

    This is a product-standard fact, not an OCR guess.  Keep the mapping
    deliberately small until other models/parts have the same source review.
    """
    standard = re.sub(r'\s+', '', str(matrix_row.get('standard_no') or '')).upper()
    model = re.sub(r'^Z[A-Z]?[-]?', '', re.sub(
        r'[^A-Z0-9-]', '', str(matrix_row.get('resolved_model_code') or '').upper(),
    ))
    if standard == 'JB/T8734.5-2016':
        if component == 'insulation' and model in {'RVP', 'RVP-90', 'RVVP', 'RVVP1', 'RVVPS'}:
            return {'E'} if model == 'RVP-90' else {'D'}
        if component == 'sheath' and model in {'RVVP', 'RVVP1', 'RVVPS'}:
            return {'5'}
    return set()


def _local_pvc_sheath_materials(source_group: dict[str, Any], matrix_row: dict[str, Any] | None = None) -> set[str]:
    """Prefer explicit material; otherwise use a narrow verified model fact."""
    materials = set()
    for page in source_group.get('pages') or []:
        description = re.search(r'样品描述\s*[:：](.*?)(?:备\s*注\s*[:：]|$)', str(page.get('text') or ''), re.S)
        if not description:
            continue
        from backend.app.ocr_readable import material_clauses
        for clause in material_clauses(description[1], '护套'):
            materials.update(re.findall(r'PVC\s*/\s*ST(\d{1,2})(?![A-Za-z0-9])', clause, re.I))
    return materials or (_verified_pvc_materials(matrix_row or {}, 'sheath') if matrix_row else set())


def _condition_value_is_reliable(source_group: dict[str, Any], row: dict[str, Any], field: str) -> bool:
    """Use 0.90-0.95 only for labelled conditions corroborated by Paddle text.

    Numeric results, signs, changes and decimal values remain on their existing
    stricter gates.  This exception is limited to temperature/time labels on
    the same physical page and requires agreement from the independent primary
    OCR text.
    """
    condition = ((row.get('condition_observations') or row.get('aging_conditions') or {})
                 .get('fields', {}).get(field) if 'fields' in (row.get('condition_observations') or {})
                 else (row.get('aging_conditions') or {}).get(field)) or {}
    observations = condition.get('observations') or []
    if not observations:
        return False
    value = condition.get('value')
    if all(float(hit.get('confidence', 0)) >= .95 for hit in observations):
        return condition.get('status') == 'located' and type(value) in (int, float)
    if (len(observations) != 1 or not .90 <= float(observations[0].get('confidence', 0)) < .95):
        return False
    # 唯一且处于 0.90-0.95 的坐标命中：当同页独立主文本对同一值达成一致
    # （跨源共识）时可采信。字段本身可以仅因置信度阈值被标 unresolved，
    # 数值从观测记录中取。
    if type(value) not in (int, float):
        value = observations[0].get('value')
    if type(value) not in (int, float):
        return False
    page = next((p for p in source_group.get('pages') or [] if p.get('page') == row.get('page')), None)
    if not page:
        return False
    plain = _plain_table_text(str(page.get('text') or ''))
    plain = re.sub(r'\$\s*pm\s*\$|\\pm|\bpm\b', '±', plain, flags=re.I)
    plain = plain.replace('$', '')
    compact = re.sub(r'\s+', '', plain)
    value = f"{float(value):g}"
    if field == 'temperature':
        return bool(re.search(r'温度[^\d+-]{0,16}' + re.escape(value)
                              + r'(?:±2)?[）)]?(?:℃|°C|°℃)', compact, re.I))
    if field == 'hours':
        return bool(re.search(r'时间[^\d]{0,12}' + re.escape(value) + r'h\b', compact, re.I))
    return False


def _local_pvc_sheath_profile(matrix_row: dict[str, Any]) -> dict[str, Any] | None:
    """Explicitly reviewed product/edition profiles; no cross-family fallback.

    GB/T 5023.1-2008 table 2 (printed pp8-10) independently verifies ST5
    oven/loss values and the before/after median change definition. This
    profile intentionally excludes ST9/ST10 and unreviewed product parts.
    """
    standard = re.sub(r'\s+', '', str(matrix_row.get('standard_no') or '')).upper()
    if re.fullmatch(r'JB/T8734\.[1-6]-2016', standard):
        return {'materials':{'4','5'}, 'basis':'JB/T 8734.1-2016 表2'}
    if standard == 'GB/T5023.5-2008':
        return {'materials':{'5'}, 'basis':'GB/T 5023.1-2008 表2'}
    return None



_TL_AFTER_LABELS = {
    '老化后抗张强度中间值': 'tensile_strength',
    '老化前后抗张强度变化率': 'tensile_change',
    '老化后断裂伸长率中间值': 'elongation',
    '老化前后断裂伸长率中间值': 'elongation',
    '老化前后断裂伸长率变化率': 'elongation_change',
    '失重试验失重': 'loss',
    '失重试验': 'loss',
}
_TL_AFTER_REQS = {
    'tensile_strength': {'最小10.0', '最小12.5'},
    'tensile_change': {'最大±20', '最大20'},
    'elongation': {'最小150', '最小125'},
    'elongation_change': {'最大±20', '最大20'},
}
_TL_AFTER_UNITS = {
    'tensile_strength': {'N/mm2', 'N/mm^2'},
    'tensile_change': {'%'},
    'elongation': {'%'},
    'elongation_change': {'%'},
    'loss': {'mg/cm2', 'mg/cm^2'},
}
_TL_MARKERS = (set(_TL_AFTER_LABELS) | {
    '空气烘箱老化后的性能', '非污染试验老化后的性能', '交货状态原始性能',
    '热冲击试验', '低温弯曲试验', '低温试验'})


def _tl_clean(text: str) -> str:
    return re.sub(r'\s+', '', str(text or '')).replace('²', '2').replace('％', '%').replace('°C', '℃')


def _tl_key(text: str) -> str:
    return re.sub(r'[\s\-—－·]', '', str(text or ''))


def _tl_record_from_cells(cells: list) -> dict | None:
    if not cells:
        return None
    # rowspan 类别列（护套/绝缘机械性能，OCR可能叠字“机机”）不是行标签
    if re.fullmatch(r'(?:护套|绝缘)机+械性能', _tl_key(cells[0])):
        cells = cells[1:]
        if not cells:
            return None
    key = _tl_key(cells[0])
    if key not in _TL_MARKERS and not key.startswith(('老化前', '老化后', '老化前后', '失重试验')):
        return None
    rest = [_tl_clean(c) for c in cells[1:]]
    reqs = [c for c in rest if re.fullmatch(r'(?:最小|最大)±?\d+(?:\.\d+)?', c)]
    vals = [c for c in rest if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', c)]
    verd = [c for c in rest if re.fullmatch(r'[PF]', c)]
    units = [c for c in rest if c in {'%', 'N/mm2', 'N/mm^2', 'mg/cm2', 'mg/cm^2'}]
    return {'key': key, 'req': reqs[0] if len(reqs) == 1 else None,
            'values': vals,
            'value': vals[0] if len(vals) == 1 else None,
            'verdict': verd[0] if len(verd) == 1 else None,
            'unit': units[0] if len(units) == 1 else '',
            'raw': ' '.join([key] + rest)}


def _tl_records_from_plain(section: str) -> list:
    lines = [l.strip() for l in section.splitlines()]
    records = []
    i, n = 0, len(lines)
    while i < n:
        if not lines[i]:
            i += 1
            continue
        key = _tl_key(lines[i])
        if key in _TL_MARKERS or key.startswith(('老化前', '老化后', '老化前后', '失重试验')):
            j = i + 1
            window = []
            while j < n and j < i + 8:
                if not lines[j]:
                    j += 1
                    continue
                k2 = _tl_key(lines[j])
                if k2 in _TL_MARKERS or k2.startswith(('老化前', '老化后', '老化前后', '失重试验', '空气烘箱', '非污染', '试验条件', '低温', '热冲击', '注')):
                    break
                window.append(_tl_clean(lines[j]))
                j += 1
            reqs = [w for w in window if re.fullmatch(r'(?:最小|最大)±?\d+(?:\.\d+)?', w)]
            vals = [w for w in window if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', w)]
            verd = [w for w in window if re.fullmatch(r'[PF]', w)]
            units = [w for w in window if w in {'%', 'N/mm2', 'N/mm^2', 'mg/cm2', 'mg/cm^2'}]
            records.append({'key': key, 'req': reqs[0] if len(reqs) == 1 else None,
                            'values': vals,
                            'value': vals[0] if len(vals) == 1 else None,
                            'verdict': verd[0] if len(verd) == 1 else None,
                            'unit': units[0] if len(units) == 1 else '',
                            'raw': _tl_clean(lines[i]) + ' ' + ' '.join(window)})
            i = j
        else:
            i += 1
    return records


_TL_SEP = r'(?:\s|\||<[^>]*>)*'


def _tl_category_pattern(name: str) -> str:
    # OCR偶发叠字（绝缘机机械性能）：机字允许重复
    return _TL_SEP.join(re.escape(ch) for ch in name).replace(
        re.escape('机') + _TL_SEP, re.escape('机') + '+' + _TL_SEP)


def _tl_component_section(page_text: str, component: str) -> str:
    own = _tl_category_pattern('绝缘机械性能' if component == 'insulation' else '护套机械性能')
    other = _tl_category_pattern('护套机械性能' if component == 'insulation' else '绝缘机械性能')
    start = re.search(own, page_text)
    if not start:
        return ''
    # 类别命中点在 <td> 内：向两侧扩展到完整 <table>，否则行解析拿不到
    t0 = page_text.rfind('<table', 0, start.start())
    t1 = page_text.find('</table>', start.end())
    if t0 != -1 and t1 != -1:
        return page_text[t0:t1 + len('</table>')]
    section = page_text[start.start():]
    end = re.search(other, section)
    return section[:end.start()] if end else section


def _tl_component_records(page_text: str, component: str) -> list:
    section = _tl_component_section(page_text, component)
    if not section:
        return []
    if '<table' in section.lower():
        import html as _html
        from backend.app.mineru_pages import _html_table_rows
        out = []
        lead = re.search(r"<table\b[^>]*>(.*?)<tr\b", section, re.I | re.S)
        if lead and lead.group(1).strip():
            frag = [re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', _html.unescape(p))).strip()
                    for p in re.split(r'</t[dh]>', lead.group(1)) if p.strip()]
            rec = _tl_record_from_cells(frag)
            if rec:
                out.append(rec)
        for cells in _html_table_rows({'text': section}):
            rec = _tl_record_from_cells(cells)
            if rec:
                out.append(rec)
        return out
    return _tl_records_from_plain(section)


def _report_level_materials(report_text: str, component: str) -> set:
    """全报告样品描述块中的唯一材料等级；多等级或缺失返回原样空/多。"""
    grades = set()
    zh = {'sheath': '护套', 'insulation': '绝缘'}.get(component, component)
    for desc in re.findall(r'样品描述\s*[:：](.*?)(?:备\s*注\s*[:：]|$)', report_text or '', re.S):
        from backend.app.ocr_readable import material_clauses
        for clause in material_clauses(desc, zh):
            if component == 'sheath':
                grades.update(re.findall(r'PVC\s*/\s*ST(\d{1,2})(?![A-Za-z0-9])', clause, re.I))
            else:
                grades.update(re.findall(r'PVC\s*/\s*([CDE])(?![A-Za-z0-9])', clause, re.I))
    return grades


def _text_layer_oven_rows(source_group: dict) -> list | None:
    """坐标绑定不可用时，从样品绑定页文本/HTML行重建空气烘箱老化观测。

    老化后四项按标签+要求+单位+唯一值+评定逐行对应；老化前两项按
    要求列识别（兼容Paddle把交货状态数值错位到标题行的版式）。
    任何字段不唯一都返回 None，不猜测。
    """
    for page in source_group.get('pages') or []:
        records = _tl_component_records(str(page.get('text') or ''), 'sheath')
        starts = [i for i, r in enumerate(records) if r['key'] == '空气烘箱老化后的性能']
        if len(starts) != 1:
            continue
        s = starts[0]
        ends = [i for i, r in enumerate(records) if i > s and (
            r['key'] == '非污染试验老化后的性能'
            or r['key'].startswith(('失重试验', '热冲击', '低温')))]
        e = ends[0] if ends else len(records)
        window = records[s + 1:e]
        after = {}
        complete = True
        for field in ('tensile_strength', 'tensile_change', 'elongation', 'elongation_change'):
            matches = [r for r in window
                       if _TL_AFTER_LABELS.get(r['key']) == field
                       and r['value'] is not None and r['verdict'] in {'P', 'F'}
                       and r['req'] in _TL_AFTER_REQS[field]
                       and r['unit'] in _TL_AFTER_UNITS[field]]
            if len(matches) != 1:
                complete = False
                break
            after[field] = matches[0]
        if not complete:
            continue
        before = records[:s]
        bt = [r for r in before if r['unit'] in {'N/mm2', 'N/mm^2'}
              and r['req'] in {'最小10.0', '最小12.5'}
              and r['value'] is not None and r['verdict'] in {'P', 'F'}]
        be = [r for r in before if r['unit'] == '%'
              and r['req'] in {'最小150', '最小125'}
              and r['value'] is not None and r['verdict'] in {'P', 'F'}]
        if len(bt) != 1 or len(be) != 1:
            continue
        wcond = ' '.join(r.get('raw', '') for r in records[s:e])
        temps = {float(v) for v in re.findall(r'老化条件：温度\s*([+-]?\d+(?:\.\d+)?)\s*℃', wcond)}
        hrs = {float(v) for v in re.findall(r'时间\s*([+-]?\d+(?:\.\d+)?)\s*h', wcond)}
        temp = next(iter(temps)) if len(temps) == 1 else None
        hours = next(iter(hrs)) if len(hrs) == 1 else None
        conditions = {'temperature': {'status': 'located' if temp is not None else 'unresolved', 'value': temp},
                      'hours': {'status': 'located' if hours is not None else 'unresolved', 'value': hours}}
        before_obs = [{'measurement': 'tensile_strength', 'status': 'located', 'unit': 'N/mm2',
                       'reported': bt[0]['value'], 'report_verdict': bt[0]['verdict'],
                       'page': page.get('page')},
                      {'measurement': 'elongation', 'status': 'located', 'unit': '%',
                       'reported': be[0]['value'], 'report_verdict': be[0]['verdict'],
                       'page': page.get('page')}]
        unit_map = {'tensile_strength': 'N/mm2', 'tensile_change': '%',
                    'elongation': '%', 'elongation_change': '%'}
        rows = []
        for field, rec in after.items():
            rows.append({'measurement': field, 'status': 'located', 'unit': unit_map[field],
                         'reported': rec['value'], 'report_verdict': rec['verdict'],
                         'page': page.get('page'), 'aging_group': 'air_oven',
                         'aging_conditions': conditions,
                         'aging_before_observations': before_obs,
                         'source_specimen_id': 'text-layer-bound',
                         'source_pdf_sha256': '', 'text_layer_origin': True})
        return rows
    return None


def _text_layer_loss_row(source_group: dict, code: str, component: str) -> dict | None:
    """坐标绑定不可用时，从样品绑定页文本/HTML行重建失重观测行。"""
    for page in source_group.get('pages') or []:
        records = _tl_component_records(str(page.get('text') or ''), component)
        cands = [r for r in records
                 if _TL_AFTER_LABELS.get(r['key']) == 'loss'
                 and 1 <= len(r.get('values') or []) <= 12
                 and (r['verdict'] in {'P', 'F'} or r['verdict'] is None)
                 and r['unit'] in _TL_AFTER_UNITS['loss']]
        if len(cands) != 1:
            continue
        c = cands[0]
        return {'item_code': code, 'component': component, 'status': 'located',
                'unit': 'mg/cm2',
                'reported': c['values'][0] if len(c['values']) == 1 else '',
                'reported_values': [{'text': v} for v in c['values']],
                'vector_status': 'located_values_only',
                'report_verdict': c['verdict'],
                'page': page.get('page'), 'label': c['key'],
                'result_column_binding': {'status': 'complete', 'expected_columns': len(c['values'])},
                'text_layer_origin': True}
    return None

def _local_pvc_oven_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any] | None, report_text: str = "") -> dict[str, Any] | None:
    """JB/T 8734.1-2016 table 2; OCR requirement cells are never limits.

    Local after-ageing evidence can establish a defect independently, but cannot
    release the entire test until before/after change consistency is verified.
    """
    if not source_group or str(matrix_row.get('item_code') or '').upper() != 'SHEATH_TENSILE_AFTER':
        return None
    profile = _local_pvc_sheath_profile(matrix_row)
    if profile is None:
        return None  # Other editions/families need their own verified profiles.
    rows = [r for r in source_group.get('_local_sheath_observations', [])
            if r.get('item_code') == 'SHEATH_TENSILE_AFTER'
            and r.get('component') == 'sheath' and r.get('aging_group') == 'air_oven']
    if not rows:
        # 坐标绑定不可用（如缺评定表头）时，回退样品绑定页文本/HTML行。
        alt = _text_layer_oven_rows(source_group)
        if not alt:
            return None
        rows = alt
    from backend.app.ocr_oven_bridge import recover_oven_rows
    rows = recover_oven_rows(source_group, rows)
    if not any(r.get('status') == 'located' for r in rows):
        alt = _text_layer_oven_rows(source_group)
        if alt:
            rows = alt
    from backend.app.text_layer_witness import adopt_oven_witness
    for _r in rows:
        adopt_oven_witness(_r, source_group)
    materials = _local_pvc_sheath_materials(source_group, matrix_row)
    if len(materials) != 1 and report_text:
        _profile = _local_pvc_sheath_profile(matrix_row)
        _grades = _report_level_materials(report_text, 'sheath')
        if _profile and len(_grades) == 1 and next(iter(_grades)) in _profile['materials']:
            materials = _grades
    checks = []
    material = next(iter(materials)) if len(materials) == 1 else None
    checks.append({'field':'护套材料绑定', 'reported':sorted(materials),
                   'required':'当前样品明确'+ '或'.join('PVC/ST'+m for m in sorted(profile['materials'])),
                   'verdict':'pass' if material in profile['materials'] else 'unknown'})
    unique_group = len({(r.get('page'), r.get('source_specimen_id'), r.get('source_pdf_sha256')) for r in rows}) == 1
    names = {'tensile_strength':'老化后抗张强度', 'tensile_change':'抗张强度变化率',
             'elongation':'老化后断裂伸长率', 'elongation_change':'伸长率变化率'}
    if material in profile['materials'] and unique_group:
        limits = {'tensile_strength':12.5 if material=='4' else 10.0,
                  'elongation':125 if material=='4' else 150,
                  'tensile_change':20, 'elongation_change':20}
        for field, name in names.items():
            selected = [r for r in rows if r.get('measurement') == field]
            row = selected[0] if len(selected) == 1 else {}
            raw = str(row.get('reported') or '').rstrip('%')
            unit = re.sub(r'\s+', '', str(row.get('unit') or '')).replace('²','2')
            valid_unit = unit in {'N/mm2','N/mm^2'} if field=='tensile_strength' else unit in {'%','％'}
            reliable = row.get('status') == 'located' and valid_unit and bool(re.fullmatch(r'[+-]?\d+(?:\.\d+)?',raw))
            value = float(raw) if reliable else None
            conforms = (abs(value)<=limits[field] if field.endswith('change') else value>=limits[field]) if reliable else False
            state = ('pass' if conforms and row.get('report_verdict')=='P' else 'fail') if reliable and row.get('report_verdict') in {'P','F'} else 'unknown'
            checks.append({'field':name,'reported':value,'required':('±' if field.endswith('change') else '≥')+str(limits[field]),'verdict':state})
        # All four measurements carry the same source-bound condition receipt.
        conditions = rows[0].get('aging_conditions') or {}
        same_conditions = all(r.get('aging_conditions') == conditions for r in rows)
        for field, name, required in [('temperature','老化温度','80±2℃'),('hours','老化时间','168h')]:
            condition = conditions.get(field) or {}
            value = condition.get('value')
            probe = dict(rows[0], aging_conditions=conditions)
            reliable = (same_conditions and condition.get('status')=='located'
                        and type(value) in (int,float)
                        and _condition_value_is_reliable(source_group, probe, field))
            good = (78<=value<=82 if field=='temperature' else value==168) if reliable else False
            # 标准固定条件：报告未单独举证时按标准默认条件视为满足，仅报告明示冲突才判 fail。
            checks.append({'field':name,'reported':value if reliable else None,'required':required,
                           'verdict':('pass' if good else 'fail') if reliable else 'pass'})
    else:
        checks.append({'field':'老化组与限值绑定','reported':None,'required':'唯一空气老化组和适用材料','verdict':'unknown'})
    before_rows = rows[0].get('aging_before_observations') or []
    same_before = all(r.get('aging_before_observations') == before_rows for r in rows)
    for field, change_field, name in [('tensile_strength','tensile_change','抗张强度变化率复算'),
                                      ('elongation','elongation_change','伸长率变化率复算')]:
        before = [r for r in before_rows if r.get('measurement')==field]
        after = [r for r in rows if r.get('measurement')==field]
        change = [r for r in rows if r.get('measurement')==change_field]
        records = [part[0] for part in (before,after,change) if len(part)==1]
        reliable = same_before and unique_group and len(records)==3 and all(
            r.get('status')=='located' and re.fullmatch(r'[+-]?\d+(?:\.\d+)?',str(r.get('reported') or '').rstrip('%'))
            and r.get('page')==rows[0].get('page') for r in records)
        units=[re.sub(r'\s+','',str(r.get('unit') or '')).replace('²','2') for r in records]
        expected={'N/mm2','N/mm^2'} if field=='tensile_strength' else {'%','％'}
        reliable = reliable and all(u in expected for u in units[:2]) and units[-1] in {'%','％'}
        state='unknown'
        reported=None
        if reliable:
            raw=[str(r['reported']).rstrip('%') for r in records]
            a,b,c=map(float,raw)
            reported=f'{raw[0]}→{raw[1]}；报告变化率{raw[2]}%'
            if a>0:
                calculated=(b-a)/a*100
                reported=(f'({raw[1]}−{raw[0]})÷{raw[0]}×100%={calculated:+.2f}%；'
                          f'报告填写{raw[2]}%')
                # Match the displayed precision; ambiguous source rounding is
                # retained for review instead of becoming a false defect.
                resolution=10**(-len(raw[2].split('.')[1])) if '.' in raw[2] else 1
                if abs(calculated-c)<resolution/2:
                    state='pass'
                elif not _display_change_overlap(raw[0],raw[1],c):
                    state='fail'
            else:
                state='fail'
        checks.append({'field':name,'reported':reported,
                       'required':'(老化后中间值-老化前中间值)/老化前中间值×100%，考虑显示精度',
                       'verdict':state,
                       'reason':'display_precision_overlap' if reliable and state=='unknown' and a>0
                           and _display_change_overlap(raw[0],raw[1],c) else None})
    failed = [c['field'] for c in checks if c['verdict']=='fail']
    unknown = [c['field'] for c in checks if c['verdict']=='unknown']
    return present_material_check({'category':'护套机械性能','item':str(matrix_row.get('item_name') or '护套老化后拉力试验'),
            'reported':'；'.join(c['field']+'：'+str(c['reported']) for c in checks if c['reported'] is not None),
            'required':profile['basis']+'对应护套材料列；不采用OCR要求列作为限值',
            'verdict':'fail' if failed else ('manual_review' if unknown else 'pass'), 'basis':profile['basis']+' 1.2及注a',
            'source_pages':sorted({r['page'] for r in rows}), 'source_excerpt':'；'.join(str(r.get('reported')) for r in rows),
            'evidence_status':'located','coverage_origin':'local_pvc_oven_evidence',
            'item_code':'SHEATH_TENSILE_AFTER','local_oven_observations':rows,
            'aging_condition_checks':checks,
            'note':'异常项：'+('、'.join(failed) or '暂无确定异常')+'；证据待核对：'+'、'.join(unknown),
            'deterministic_review_action':('整改'+'、'.join(failed)+'；' if failed else '')+('核对'+'、'.join(unknown) if unknown else '')})


def _local_insulation_resistance_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any] | None) -> dict[str, Any] | None:
    """Verified GB/T 5023.5 table-10 profile for RVV 5x0.75 only."""
    if not source_group or str(matrix_row.get('item_code') or '').upper()!='INSUL_RES':
        return None
    standard=re.sub(r'\s+','',str(matrix_row.get('standard_no') or '')).upper()
    model=re.sub(r'[\s()（）-]','',str(matrix_row.get('resolved_model_code') or '')).upper()
    rows=[r for r in source_group.get('_local_insulation_observations') or []
          if r.get('item_code')=='INSUL_RES' and r.get('component')=='insulation']
    if len(rows)!=1:
        return None
    row=rows[0]; key=list(row.get('source_sample_key') or [])
    if (standard!='GB/T5023.5-2008' or 'RVV' not in model or len(key)!=3
            or key[1]!='300/500' or key[2] not in {'5×0.75','5×0.75mm²'}):
        return None
    values=row.get('reported_values') or []
    reliable=(row.get('status')=='located' and row.get('unit')=='MΩ·km'
              and row.get('report_verdict') in {'P','F','N'} and len(values)==3
              and all(type(v) in (int,float) and v>=0 for v in values))
    if not reliable:
        return None
    limit=0.011  # GB/T 5023.5-2008 表10，60227 IEC 53，5×0.75 mm²
    passed=all(v+1e-12>=limit for v in values) and row['report_verdict']=='P'
    return {'category':'电性能','item':str(matrix_row.get('item_name') or '绝缘电阻'),
        'reported':'/'.join(f'{v:g}' for v in values)+' MΩ·km；报告判'+row['report_verdict'],
        'required':f'70℃绝缘电阻最小{limit:g} MΩ·km','verdict':'pass' if passed else 'fail',
        'basis':'GB/T 5023.5-2008 表10 项次1.4','source_pages':[int(row['page'])],
        'source_excerpt':'；'.join(map(str,values)),'evidence_status':'located',
        'coverage_origin':'local_insulation_resistance_evidence','item_code':'INSUL_RES',
        'note':'程序从唯一绝缘电阻行恢复五芯实测值；MQ·km仅在该单位格规范为MΩ·km，标准限值来自已核验产品表'}


def _mechanical_source_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any] | None, report_text: str = "") -> dict[str, Any] | None:
    recovered = _mechanical_measurement_source_evidence(matrix_row, source_group)
    oven = _local_pvc_oven_evidence(matrix_row, source_group, report_text)
    if oven and (recovered is None or recovered.get('verdict') != 'fail'):
        if recovered:
            oven['previous_source_check'] = recovered
        recovered = oven
    local = _local_pressure_depth_evidence(matrix_row, source_group)
    if local and (recovered is None or (local['verdict']=='fail' and recovered.get('verdict')!='fail')):
        # An explicit local failure must outrank an old recovered/model pass.
        # Existing independent failures remain; never silently erase them.
        if recovered:
            local['previous_source_check']=recovered
        recovered=local
    elif local and recovered and any(row.get('table_repair_proof') for row in local.get('local_pressure_observations', [])):
        # A repaired scalar is not the whole test. Carry its independently bound
        # depth/conditions into the next validator; preserve an existing failure.
        recovered=dict(recovered,local_pressure_observations=local['local_pressure_observations'],
                       local_pressure_depth_check=local['local_pressure_depth_check'])
    if recovered is None and _pressure_product_profile(matrix_row) is not None:
        # 模型列出了该项目并判P，不能代替确定性的原页数值证据。
        reason = '未从当前样品护套原页恢复压痕实测值、限值及评定，请核对原表列对应关系并补齐独立核验'
        return {'category':'护套机械性能', 'item':str(matrix_row.get('item_name') or '护套高温压力试验'),
            'reported':'未取得可验证的护套压力结果', 'required':'按适用产品和方法标准核对压痕、温度、时间与荷载',
            'verdict':'manual_review', 'basis':_matrix_reference(matrix_row),
            'source_pages':[], 'source_excerpt':'', 'evidence_status':'not_located',
            'coverage_origin':'deterministic_source_recovery', 'note':reason,
            'deterministic_review_action':reason,
            'pressure_condition_checks':[{'field':'原页结果证据','reported':None,'required':reason,'verdict':'unknown'}]}
    if recovered and source_group:
        return _pressure_source_conditions(matrix_row, source_group, recovered, report_text)
    return recovered


def _mechanical_measurement_source_evidence(
    matrix_row: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """确定性补齐护套拉力和护套高温压力；不完整或N判定仍转人工。"""
    if not source_group:
        return None
    from backend.app.ocr_table_repair import measurement_group
    source_group = measurement_group(source_group, str(matrix_row.get('item_code') or '').upper())
    aging_n = _rubber_sheath_aging_n_evidence(matrix_row, source_group)
    if aging_n:
        return aging_n
    item_code = str(matrix_row.get("item_code") or "").upper()
    if item_code == 'HEAT_PRESS_SHEATH':
        old_layout = []
        for page in source_group.get('pages') or []:
            original = _sheath_pressure_section(str(page.get('text') or ''))
            matches = list(re.finditer(
                r'护套\s*高温压力[-—－]{1,3}压痕\s*\n\s*最大\s*\n\s*'
                r'(\d+(?:\.\d+)?)\s*\n\s*(\d+(?:\.\d+)?)\s*\n\s*([PF])\s*\n\s*'
                r'深度[-—－]{1,3}中间值\s*[（(]\s*[%％]\s*[）)]', original))
            for m in matches:
                old_layout.append((page, original, m))
        if len(old_layout) == 1:
            page, original, match = old_layout[0]
            limit, value = float(match[1]), float(match[2])
            return {'category':'护套机械性能','item':str(matrix_row.get('item_name') or '护套高温压力试验'),
                'reported':f'护套压痕深度中间值：{value:g}%；报告判{match[3]}',
                'required':f'报告限值最大{limit:g}%', 'verdict':'pass' if value<=limit and match[3]=='P' else 'fail',
                'basis':_matrix_reference(matrix_row), 'source_pages':[int(page['page'])],
                'source_excerpt':original[:900], 'evidence_status':'located',
                'coverage_origin':'deterministic_source_recovery',
                'note':'明确护套单项标题、限值、单一实测列、评定及标题续行完整对应；未借用绝缘压力数据'}
        # 紧凑模板：标签单行（高温压力-压痕深度-中间值）+ 限值/实测/评定
        # 逐行排列、OCR单位格为空。仅当样品护套节内恰好一条此类行时采用；
        # 单位格缺失不推测成分，压痕深度以%计（GB/T 2951.31），限值不大于100。
        compact_layout = []
        for page in source_group.get('pages') or []:
            original_compact = _sheath_pressure_section(str(page.get('text') or ''))
            matches = list(re.finditer(
                r'高温压力[-—－]{1,3}压痕深度[-—－]{1,3}中间值\s*\n\s*'
                r'最大\s*(\d+(?:\.\d+)?)\s*\n\s*(\d+(?:\.\d+)?)\s*\n\s*([PF])(?=\s*\n|\s*$)',
                original_compact))
            for m in matches:
                compact_layout.append((page, original_compact, m))
        if len(compact_layout) == 1:
            page, original_compact, match = compact_layout[0]
            limit, value = float(match[1]), float(match[2])
            if limit <= 100 and value <= limit and match[3] == 'P':
                return {'category':'护套机械性能','item':str(matrix_row.get('item_name') or '护套高温压力试验'),
                    'reported':f'护套压痕深度中间值：{value:g}%；报告判{match[3]}',
                    'required':f'报告限值最大{limit:g}%', 'verdict':'pass',
                    'basis':_matrix_reference(matrix_row), 'source_pages':[int(page['page'])],
                    'source_excerpt':original_compact[:900], 'evidence_status':'located',
                    'coverage_origin':'deterministic_source_recovery',
                    'note':'明确护套压力行：标签、限值、单一实测与评定逐行对应；OCR单位格为空，压痕深度按方法标准以%计；未借用绝缘压力数据'}
        # HTML 表形态：MinerU/视觉层把整页渲染成 <table> 时，按行单元格
        # 解析同一护套压力行；逐芯多值、缺评定或跨页多行都不采用。
        if len(compact_layout) != 1:
            from backend.app.mineru_pages import _html_table_rows
            html_layout = []
            for page in source_group.get('pages') or []:
                section_html = _sheath_pressure_section(str(page.get('text') or ''))
                if '<table' not in section_html.lower():
                    continue
                parsed_rows = _html_table_rows({'text': section_html})
                for idx, cells in enumerate(parsed_rows):
                    if not cells:
                        continue
                    label = re.sub(r'[\s\-—－]', '', str(cells[0]))
                    if label != '高温压力压痕深度中间值':
                        continue
                    rest = [re.sub(r'\s+', '', str(c)) for c in cells[1:]]
                    limits = [c for c in rest if re.fullmatch(r'最大\d+(?:\.\d+)?', c)]
                    values = [c for c in rest if re.fullmatch(r'\d+(?:\.\d+)?', c)]
                    verdicts = [c for c in rest if re.fullmatch(r'[PF]', c)]
                    if not (len(limits) == 1 and len(values) == 1 and len(verdicts) == 1):
                        # Paddle 错位形态：数值错位到下一行，且常与
                        # “试验条件：温度 …”文字同 row；下一行必须是条件
                        # 载体而非另一测量项目，任何多值都不采用。
                        nxt = parsed_rows[idx + 1] if idx + 1 < len(parsed_rows) else []
                        nlabel = re.sub(r'[\s\-—－]', '', str(nxt[0])) if nxt else ''
                        if nxt and '条件' in nlabel:
                            rest = [re.sub(r'\s+', '', str(c)) for c in nxt[1:]]
                            limits = [c for c in rest if re.fullmatch(r'最大\d+(?:\.\d+)?', c)]
                            values = [c for c in rest if re.fullmatch(r'\d+(?:\.\d+)?', c)]
                            verdicts = [c for c in rest if re.fullmatch(r'[PF]', c)]
                    if len(limits) == 1 and len(values) == 1 and len(verdicts) == 1:
                        html_layout.append((page, section_html,
                            float(limits[0].lstrip('最大')), float(values[0]), verdicts[0]))
            if len(html_layout) == 1:
                page, section_html, limit, value, verdict = html_layout[0]
                if limit <= 100 and value <= limit and verdict == 'P':
                    return {'category':'护套机械性能','item':str(matrix_row.get('item_name') or '护套高温压力试验'),
                        'reported':f'护套压痕深度中间值：{value:g}%；报告判{verdict}',
                        'required':f'报告限值最大{limit:g}%', 'verdict':'pass',
                        'basis':_matrix_reference(matrix_row), 'source_pages':[int(page['page'])],
                        'source_excerpt':section_html[:900], 'evidence_status':'located',
                        'coverage_origin':'deterministic_source_recovery',
                        'note':'明确护套压力行（HTML表）：标签、限值、单一实测与评定同 row 对应；压痕深度按方法标准以%计；未借用绝缘压力数据'}
    source_group = dict(source_group, pages=[dict(page, text=section)
        for page in source_group.get('pages') or []
        if (section := _sheath_mechanical_section(str(page.get('text') or '')))])
    patterns: tuple[str, ...]
    if item_code == "SHEATH_TENSILE_BEFORE":
        patterns = (
            r"老化前抗张强度\s*[-—－一]{0,3}\s*中间值",
            r"老化前断裂伸长率\s*[-—－一]{0,3}\s*中间值",
        )
    elif item_code == "SHEATH_TENSILE_AFTER":
        patterns = (
            r"老化后抗张强度\s*[-—－一]{0,3}\s*中间值",
            r"老化前后抗张强度变化率",
            r"老化(?:前后|后)断裂伸长率\s*[-—－一]{0,3}\s*中间值",
            r"老化前后断裂伸长率变化率",
        )
    elif item_code == "HEAT_PRESS_SHEATH":
        patterns = (r"高温压力\s*[-—－]{0,3}\s*压痕深度\s*[-—－]{0,3}\s*中间值",)
    else:
        return None

    structured_sheath_pass = any(
        "<table" in str(page.get("text") or "").lower()
        and "护套机械性能" in re.sub(r"\s+", "", str(page.get("text") or ""))
        and bool(re.search(r">\s*P{2,}\s*<", str(page.get("text") or ""), re.I))
        for page in source_group.get("pages") or []
    )

    # A column of P is the report's assertion, not an independent numerical
    # verdict. Always attempt the actual measurements first. If extraction
    # fails, return None so the matrix records a located evidence gap.
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        plain = _plain_table_text(page_text)
        if not _is_sheath_mechanical_page(page_text) and not (
            item_code == "HEAT_PRESS_SHEATH" and "护套高温压力" in re.sub(r"\s+", "", plain)
        ):
            continue
        vertical_measurements: list[dict[str, Any]] = []
        if "<table" not in page_text.lower():
            vertical_patterns = {
                "SHEATH_TENSILE_BEFORE": (r"老化前抗张强度", r"老化前断裂[伸仲]长率"),
                "SHEATH_TENSILE_AFTER": (
                    r"老化后抗张强度", r"老化前后抗张强度变化率",
                    r"老化(?:前后|后)断裂[伸仲]长率\s*[-—－一]{0,3}\s*中间值", r"老化前后断裂[伸仲]长率变化率",
                ),
                "HEAT_PRESS_SHEATH": (r"高温压力(?:试验)?\s*[-—－一]*\s*压痕深度",),
            }.get(item_code, patterns)
            vertical_measurements = [
                measured for pattern in vertical_patterns
                if (measured := _vertical_rapidocr_measurement(page_text, pattern))
            ]
            if len(vertical_measurements) == len(vertical_patterns):
                passed = all(measured["passed"] for measured in vertical_measurements)
                reported_series = [
                    tuple(measured.get("reported_values") or [])
                    for measured in vertical_measurements
                ]
                # 纵向OCR可能把同一整列数值复制到四个独立项目上。
                # 这种重复串列不能据此判不合格或要求人工；继续扫描同一样品
                # 的MinerU结构化原页，由完整项目标题、限值和P/F/N兜底。
                malformed_repeated_series = bool(
                    len(vertical_measurements) >= 2
                    and reported_series
                    and len(reported_series[0]) > 1
                    and len(set(reported_series)) == 1
                )
                if not passed and malformed_repeated_series:
                    continue
                return {
                    "category": "护套机械性能",
                    "item": str(matrix_row.get("item_name") or ""),
                    "reported": "；".join(
                        f"{measured['heading']}："
                        + "/".join(f"{value:g}" for value in measured["reported_values"])
                        for measured in vertical_measurements
                    ),
                    "required": "；".join(
                        f"{measured['heading']}：{measured['direction']}"
                        f"{'±' if measured['signed'] else ''}{measured['limit']:g}"
                        for measured in vertical_measurements
                    ),
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序从RapidOCR纵向表中确定性补齐，并独立复算限值、实测值和P/F评定",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": "；".join(
                        measured["excerpt"] for measured in vertical_measurements
                    )[:900],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
        if item_code == "SHEATH_TENSILE_BEFORE":
            vertical_before = re.search(
                r"老化前抗张强度\s*[-—－一]{0,3}\s*中间值.{0,180}?"
                r"最小\s*(?:\|\s*)?([0-9]+(?:\.[0-9]+)?)\s*\|\s*([0-9]+(?:\.[0-9]+)?)"
                r".{0,220}?老化前断裂伸长率\s*[-—－一]{0,3}\s*中间值.{0,180}?"
                r"最小\s*(?:\|\s*)?([0-9]+(?:\.[0-9]+)?)\s*\|\s*([0-9]+(?:\.[0-9]+)?)",
                plain,
                re.I | re.S,
            )
            vertical_before_reordered = re.search(
                r"([0-9]+(?:\.[0-9]+)?)\s*(?:\|\s*){1,4}"
                r"老化前抗张强度\s*[-—－一]{0,3}\s*中间值\s*\|\s*"
                r"N/mm[²2]?\s*最小\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*P\s*\|\s*"
                r"老化前断裂伸长率\s*[-—－一]{0,3}\s*中间值\s*\|\s*%\s*\|\s*"
                r"最小\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*P",
                plain,
                re.I | re.S,
            )
            if not vertical_before and vertical_before_reordered and structured_sheath_pass:
                strength, strength_limit, elongation_limit, elongation = map(
                    float, vertical_before_reordered.groups()
                )
                passed = strength + 1e-9 >= strength_limit and elongation + 1e-9 >= elongation_limit
                return {
                    "category": "护套机械性能",
                    "item": str(matrix_row.get("item_name") or ""),
                    "reported": f"老化前抗张强度：{strength:g}；老化前断裂伸长率：{elongation:g}",
                    "required": f"抗张强度最小{strength_limit:g}；断裂伸长率最小{elongation_limit:g}",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序从纵向OCR重排行提取实测值，并由同页MinerU结构化P评定交叉确认",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": vertical_before_reordered.group(0)[:700],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
            if vertical_before and structured_sheath_pass:
                strength_limit, strength, elongation_limit, elongation = map(float, vertical_before.groups())
                passed = strength + 1e-9 >= strength_limit and elongation + 1e-9 >= elongation_limit
                return {
                    "category": "护套机械性能",
                    "item": str(matrix_row.get("item_name") or ""),
                    "reported": f"老化前抗张强度：{strength:g}；老化前断裂伸长率：{elongation:g}",
                    "required": f"抗张强度最小{strength_limit:g}；断裂伸长率最小{elongation_limit:g}",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "纵向OCR提取实测值，并由同页MinerU结构化P评定交叉确认",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": vertical_before.group(0)[:700],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
        if item_code == "HEAT_PRESS_SHEATH" and _is_sheath_mechanical_page(page_text):
            vertical_pressure = re.search(
                r"高温压力\s*[-—－]{0,3}\s*压痕深度\s*[-—－]{0,3}\s*中间值"
                r".{0,420}?(最大|最小)\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*"
                r"([0-9]+(?:\.[0-9]+)?)\s*\|\s*([PF])(?:\s*\||$)",
                plain,
                re.I | re.S,
            )
            if vertical_pressure:
                direction, limit_text, reported_text, verdict = vertical_pressure.groups()
                limit, reported_value = float(limit_text), float(reported_text)
                passed = (
                    reported_value <= limit + 1e-9 if direction == "最大"
                    else reported_value + 1e-9 >= limit
                ) and verdict.upper() == "P"
                return {
                    "category": "护套机械性能",
                    "item": str(matrix_row.get("item_name") or ""),
                    "reported": f"护套高温压力压痕深度：{reported_value:g}",
                    "required": f"护套高温压力压痕深度：{direction}{limit:g}",
                    "verdict": "pass" if passed else "fail",
                    "basis": _matrix_reference(matrix_row),
                    "note": "程序从护套机械性能纵向表确定性补齐",
                    "source_pages": [int(page["page"])],
                    "source_excerpt": vertical_pressure.group(0)[:700],
                    "evidence_status": "located",
                    "coverage_origin": "deterministic_source_recovery",
                }
        # MinerU常用rowspan把绝缘/护套高温压力合在一个项目单元格，
        # 护套的限值、实测值和P单独位于紧接的第二<tr>。
        if item_code == "HEAT_PRESS_SHEATH" and "<tr" in page_text.lower():
            combined = re.search(
                r"(<tr\b[^>]*>.*?护套高温压力.*?</tr>)\s*(<tr\b[^>]*>.*?</tr>)",
                page_text,
                re.I | re.S,
            )
            if combined:
                second_cells = [
                    re.sub(r"\s+", "", re.sub(r"<[^>]+>", " ", cell))
                    for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", combined.group(2), re.I | re.S)
                ]
                requirement_index = next((
                    index for index, value in enumerate(second_cells)
                    if re.search(r"(?:最大|最小)\s*[0-9]+(?:\.[0-9]+)?", value)
                ), None)
                verdict_index = next((
                    index for index in range(len(second_cells) - 1, -1, -1)
                    if second_cells[index].upper() in {"P", "F", "N"}
                ), None)
                if requirement_index is not None and verdict_index is not None and verdict_index > requirement_index:
                    requirement = re.search(
                        r"(最大|最小)\s*([0-9]+(?:\.[0-9]+)?)", second_cells[requirement_index]
                    )
                    reported_values = [
                        float(value)
                        for cell in second_cells[requirement_index + 1:verdict_index]
                        for value in re.findall(r"(?<![A-Za-z0-9.])[0-9]+(?:\.[0-9]+)?", cell)
                    ]
                    if requirement and reported_values:
                        direction, limit_text = requirement.groups()
                        limit = float(limit_text)
                        passed = all(
                            value <= limit + 1e-9 if direction == "最大" else value + 1e-9 >= limit
                            for value in reported_values
                        ) and second_cells[verdict_index].upper() == "P"
                        return {
                            "category": "护套机械性能",
                            "item": str(matrix_row.get("item_name") or ""),
                            "reported": "护套高温压力压痕深度：" + "/".join(f"{value:g}" for value in reported_values),
                            "required": f"护套高温压力压痕深度：{direction}{limit:g}",
                            "verdict": "pass" if passed else "fail",
                            "basis": _matrix_reference(matrix_row),
                            "note": "程序从绝缘/护套合并高温压力表的护套子行确定性补齐",
                            "source_pages": [int(page["page"])],
                            "source_excerpt": _plain_table_text(combined.group(1) + combined.group(2))[:900],
                            "evidence_status": "located",
                            "coverage_origin": "deterministic_source_recovery",
                        }
        expected_semantics = {
            "SHEATH_TENSILE_BEFORE": (("最小", False), ("最小", False)),
            "SHEATH_TENSILE_AFTER": (
                ("最小", False), ("最大", True),
                ("最小", False), ("最大", True),
            ),
            "HEAT_PRESS_SHEATH": (("最大", False),),
        }[item_code]
        groups = []
        for pattern, (expected_direction, expected_signed) in zip(patterns, expected_semantics):
            candidates = _tabular_measurements(plain, pattern)
            # 重复表头串列会产生同名但方向/正负号语义不符的第二条。
            # 只接受方向/正负号语义一致的唯一测量；歧义保留缺证据，
            # 不把原表整组P当成独立复算，也不伪造不合格。
            matched = [
                value for value in candidates
                if (
                    value.get("direction") == expected_direction
                    and bool(value.get("signed")) == expected_signed
                ) or (
                    value.get("report_verdict") == "N"
                    and value.get("direction") == "不判定"
                    and value.get("limit") is None
                )
            ]
            groups.append(matched if len(matched) == 1 else [])
        if any(not values for values in groups):
            # Try another source page; the caller distinguishes an evidence
            # gap from an actual failed measurement when no page is parseable.
            continue
        measurements = [measurement for values in groups for measurement in values]
        passed = all(measurement["passed"] for measurement in measurements)
        reported = "；".join(
            f"{measurement['heading']}：" + "/".join(f"{value:g}" for value in measurement["reported_values"])
            for measurement in measurements
        )
        required = "；".join(
            f"{measurement['heading']}："
            + (
                "标准不要求单项判定（N）"
                if measurement["limit"] is None
                else f"{measurement['direction']}{'±' if measurement['signed'] else ''}{measurement['limit']:g}"
            )
            for measurement in measurements
        )
        return {
            "category": "护套机械性能",
            "item": str(matrix_row.get("item_name") or ""),
            "reported": reported,
            "required": required,
            "verdict": "pass" if passed else "fail",
            "basis": _matrix_reference(matrix_row),
            "note": "程序从护套机械性能表确定性补齐；外网模型未返回该必审项目",
            "source_pages": [int(page["page"])],
            "source_excerpt": "；".join(measurement["excerpt"] for measurement in measurements)[:900],
            "evidence_status": "located",
            "coverage_origin": "deterministic_source_recovery",
        }
    return None


def _same_page_loss_row_corroboration(
    source_group: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate a coordinate loss row against one exact row on the same page.

    The coordinate collector remains the primary column-binding evidence.  A
    native text row or a clean MinerU HTML row must independently agree on the
    component, unit, 2.0 limit, result-column count/order and P/F verdict.  A
    collapsed MinerU table is not parsed; disagreement or ambiguity returns no
    evidence and therefore keeps manual review.
    """
    label_key = re.sub(r"[\s—－-]", "", str(row.get("label") or ""))
    if not is_loss_label(row.get("label")):
        return None
    cells = row.get("cells") or []
    label_cells = [
        cell for cell in cells
        if is_loss_label(cell.get("text"))
    ]
    if len(label_cells) != 1 or float(label_cells[0].get("confidence") or 0) < .85:
        return None
    verdict = str(row.get("report_verdict") or "").upper()
    verdict_sources = row.get("verdict_sources") or []
    if (
        verdict not in {"P", "F"}
        or len(verdict_sources) != 1
        or float(verdict_sources[0].get("confidence") or 0) < .90
    ):
        return None
    binding = row.get("result_column_binding") or {}
    expected = binding.get("expected_columns")
    values = row.get("reported_values") or []
    if binding.get("status") != "complete" or type(expected) is not int or not 1 <= expected <= 12:
        # Column geometry unresolved (e.g. a low-confidence witness broke
        # unanimity): an exact multi-value HTML row on the same page can still
        # corroborate the whole vector when every value independently agrees.
        if len(values) < 2:
            return None
        expected = len(values)
    if (
        len(values) != expected
        or any(
            not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", str(value.get("text") or ""))
            or float(value.get("confidence") or 0) < .90
            for value in values
        )
    ):
        return None
    coordinate_values = tuple(float(value["text"]) for value in values)
    page_number = row.get("page")
    component = row.get("component")
    if type(page_number) is not int or component not in {"insulation", "sheath"}:
        return None

    coordinate_row_text = " ".join(str(cell.get("text") or "") for cell in cells)
    if not re.search(r"最大\s*2(?:\.0+)?(?:\D|$)", coordinate_row_text):
        return None
    coordinate_unit = re.sub(r"[\s$^{}\\]", "", str(row.get("unit") or "")).replace("²", "2")
    explicit_units = {
        re.sub(r"[\s$^{}\\]", "", str(source.get("unit_token") or "")).replace("²", "2")
        for source in (row.get("merged_unit_sources") or [])
    }
    explicit_units.discard("")
    if coordinate_unit and coordinate_unit not in {"mg/cm", "mg/cm2"}:
        return None
    if explicit_units and explicit_units != {"mg/cm2"}:
        return None

    expected_signature = (coordinate_values, "mg/cm2", 2.0, verdict)
    signatures: set[tuple[tuple[float, ...], str, float, str]] = set()
    sources: set[str] = set()
    wanted_component = "绝缘机械性能" if component == "insulation" else "护套机械性能"
    other_component = "护套机械性能" if component == "insulation" else "绝缘机械性能"

    for page in source_group.get("pages") or []:
        if page.get("page") != page_number:
            continue
        page_text = str(page.get("text") or "")
        if "<table" in page_text.lower():
            # Supplemental signatures remain subject to every coordinate, sample,
            # unit-conflict and value-confidence gate above and final consensus below.
            for evidence in loss_signatures(page_text, expected, component):
                signatures.add(evidence['signature'])
                sources.add(evidence['source'])
            for table_row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page_text, re.I | re.S):
                rendered = [
                    _plain_table_text(cell)
                    for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", table_row, re.I | re.S)
                ]
                labels = [
                    index for index, value in enumerate(rendered)
                    if is_loss_label(value)
                ]
                if len(labels) != 1:
                    continue
                tail = rendered[labels[0] + 1:]
                if len(tail) < 4:
                    continue
                source_unit = re.sub(r"[\s$^{}\\]", "", tail[0]).replace("²", "2")
                requirement = re.fullmatch(r"最大\s*([0-9]+(?:\.[0-9]+)?)", tail[1])
                source_verdict = re.sub(r"\s+", "", tail[-1]).upper()
                value_texts = [re.sub(r"\s+", "", value) for value in tail[2:-1]]
                if (
                    source_unit != "mg/cm2"
                    or requirement is None
                    or float(requirement.group(1)) != 2.0
                    or source_verdict not in {"P", "F"}
                    or len(value_texts) != expected
                    or any(not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value) for value in value_texts)
                ):
                    continue
                signatures.add((tuple(float(value) for value in value_texts), source_unit, 2.0, source_verdict))
                sources.add("ocr_exact_html_row")
            continue

        compact = re.sub(r"[\s|]", "", _plain_table_text(page_text))
        if wanted_component not in compact or other_component in compact:
            continue
        lines = [re.sub(r"\s+", "", line) for line in page_text.splitlines() if line.strip()]
        label_indexes = [
            index for index, line in enumerate(lines)
            if is_loss_label(line)
        ]
        if len(label_indexes) != 1:
            continue
        start = label_indexes[0]
        window = lines[start:start + 18]
        requirement_index = next((
            index for index, line in enumerate(window[1:], 1)
            if re.fullmatch(r"最大2(?:\.0+)?", line)
        ), None)
        if requirement_index is None:
            continue
        unit_block = "".join(window[1:requirement_index])
        unit_block = unit_block.replace("²", "2").replace("^", "")
        if "mg/cm2" not in unit_block:
            continue
        source_values: list[float] = []
        source_verdict = ""
        for line in window[requirement_index + 1:]:
            if re.fullmatch(r"[PF]", line, re.I):
                source_verdict = line.upper()
                break
            if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", line):
                source_values = []
                break
            source_values.append(float(line))
            if len(source_values) > expected:
                source_values = []
                break
        if len(source_values) == expected and source_verdict:
            signatures.add((tuple(source_values), "mg/cm2", 2.0, source_verdict))
            sources.add("native_same_page_exact_row")

    if signatures != {expected_signature}:
        return None
    return {
        "status": "corroborated",
        "page": page_number,
        "values": list(coordinate_values),
        "unit": "mg/cm2",
        "limit": 2.0,
        "verdict": verdict,
        "sources": sorted(sources),
        "basis": "same_sample_component_page_coordinate_and_source_row_exact_agreement",
    }


def _local_pvc_loss_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any] | None, report_text: str = "") -> dict[str, Any] | None:
    """Source-bound ST4/ST5 loss, including the independently verified conditions."""
    code=str(matrix_row.get('item_code') or '').upper()
    if not source_group or code not in {'SHEATH_LOSS_WEIGHT','LOSS_WEIGHT'}:
        return None
    insulation=code=='LOSS_WEIGHT'
    profile=_local_pvc_sheath_profile(matrix_row)
    if insulation and profile:
        profile={'materials':{'C','D','E'} if '8734' in str(matrix_row.get('standard_no')) else {'D'},
                 'basis':profile['basis'].replace('表2','表1')}
    if profile is None:return None
    component='insulation' if insulation else 'sheath'
    component_name='绝缘' if insulation else '护套'
    prefix='PVC/' if insulation else 'PVC/ST'
    rows=[r for r in source_group.get('_local_'+component+'_observations',[])
          if r.get('item_code')==code and r.get('component')==component]
    if not any(r.get('status')=='located' for r in rows):
        alt=_text_layer_loss_row(source_group,code,component)
        if alt: rows=[alt]
    if not rows:return None
    from backend.app.text_layer_witness import adopt_loss_witness
    for _r in rows:
        adopt_loss_witness(_r, source_group)
    materials=_local_pvc_sheath_materials(source_group, matrix_row)
    if insulation:
        materials=set()
        for page in source_group.get('pages') or []:
            description=re.search(r'样品描述\s*[:：](.*?)(?:备\s*注\s*[:：]|$)',str(page.get('text') or ''),re.S)
            if not description:continue
            from backend.app.ocr_readable import material_clauses
            for clause in material_clauses(description[1], '绝缘'):
                materials.update(re.findall(r'PVC\s*/\s*([CDE])(?![A-Za-z0-9])',clause,re.I))
        if not materials:
            materials = _verified_pvc_materials(matrix_row, 'insulation')
        if len(materials)!=1 and report_text:
            _grades=_report_level_materials(report_text,'insulation')
            if len(_grades)==1 and _grades<=profile['materials']:
                materials=_grades
    if not insulation and len(materials)!=1 and report_text:
        _grades=_report_level_materials(report_text,'sheath')
        if len(_grades)==1 and _grades<=profile['materials']:
            materials=_grades
    material_ok=len(materials)==1 and materials<=profile['materials']
    checks=[{'field':component_name+'材料绑定','reported':sorted(materials),'required':'明确'+'或'.join(prefix+m for m in sorted(profile['materials'])),
             'verdict':'pass' if material_ok else 'unknown'}]
    row=rows[0] if len(rows)==1 else {}
    raw=str(row.get('reported') or '')
    unit=re.sub(r'\s+','',str(row.get('unit') or '')).replace('²','2')
    corroboration = _same_page_loss_row_corroboration(source_group, row) if row else None
    verdict_known = row.get('report_verdict') in {'P','F'} or bool(row.get('text_layer_origin') and row.get('report_verdict') is None)
    reliable=material_ok and (
        row.get('status')=='located' or (
            corroboration is not None
            and (row.get('result_column_binding') or {}).get('expected_columns') == 1
        )
    ) and (unit in {'mg/cm2','mg/cm^2'} or corroboration is not None) and bool(re.fullmatch(r'[+-]?\d+(?:\.\d+)?',raw))
    value=float(raw) if reliable else None
    vector=row.get('reported_values') or []
    vector_values=[]
    vector_complete=False
    binding=row.get('result_column_binding') or {}
    if (insulation and material_ok and (
        row.get('vector_status')=='located_values_only' or corroboration is not None
    ) and (unit in {'mg/cm2','mg/cm^2'} or corroboration is not None) and verdict_known):
        vector_values=[float(c['text']) for c in vector]
        expected_count=binding.get('expected_columns')
        vector_complete=binding.get('status')=='complete' and type(expected_count) is int and len(vector_values)==expected_count and 1<expected_count<=12
    state='unknown'
    if reliable and verdict_known:
        if (value>2 and row.get('text_layer_origin') and corroboration is None
                and row.get('report_verdict')!='F'):
            # 文本层单读数无共识证据且与报告自评矛盾（457样品1绝缘失重
            # "30"实为OCR坏读）：不独立判 fail，转人工核对原页。
            state='unknown'
        else:
            # A negative reading needs source/method confirmation, not a made-up
            # lower acceptance limit. An explicit F or >2 is independently actionable.
            state='fail' if value>2 or row['report_verdict']=='F' else ('pass' if value>=0 else 'unknown')
            if insulation and state=='pass' and not (binding.get('status')=='complete' and binding.get('expected_columns')==1):
                state='unknown'
    elif vector_values:
        corroborated=(corroboration or {}).get('status')=='corroborated'
        state='fail' if any(v>2 for v in vector_values) or row['report_verdict']=='F' else ('pass' if (vector_complete or corroborated) and all(v>=0 for v in vector_values) else 'unknown')
        value=vector_values
    checks.append({'field':'失重实测值','reported':value,'required':'≤2.0mg/cm²','verdict':state})
    conditions=(row.get('condition_observations') or {}).get('fields') or {}
    expected_temperature=115 if insulation and materials=={'E'} else 80
    expected_hours=240 if insulation and materials=={'E'} else 168
    for field,name,required in [('temperature','失重温度',f'{expected_temperature}±2℃'),('hours','失重时间',f'{expected_hours}h')]:
        condition=conditions.get(field) or {}
        value=condition.get('value')
        reliable=(material_ok and condition.get('status')=='located'
                  and type(value) in (int,float)
                  and _condition_value_is_reliable(source_group, row, field))
        good=(abs(value-expected_temperature)<=2 if field=='temperature' else value==expected_hours) if reliable else False
        # 标准固定条件：报告未单独举证时按标准默认条件视为满足，仅报告明示冲突才判 fail。
        checks.append({'field':name,'reported':value if reliable else None,'required':required,
                       'verdict':('pass' if good else 'fail') if reliable else 'pass'})
    failed=[c['field'] for c in checks if c['verdict']=='fail']
    unknown=[c['field'] for c in checks if c['verdict']=='unknown']
    return present_material_check({'category':component_name+'机械性能','item':str(matrix_row.get('item_name') or component_name+'失重试验'),
            'item_code':code,'coverage_origin':'local_pvc_loss_evidence',
            'reported':'；'.join(c['field']+'：'+str(c['reported']) for c in checks if c['reported'] is not None),
            'required':'、'.join(prefix+m for m in sorted(profile['materials']))+f'失重≤2.0mg/cm²；{expected_temperature}±2℃、{expected_hours}h',
            'basis':profile['basis']+' 项2','verdict':'fail' if failed else ('manual_review' if unknown else 'pass'),
            'source_pages':sorted({r['page'] for r in rows}),'source_excerpt':str(row.get('label') or '')+' '+raw,
            'evidence_status':'located','loss_condition_checks':checks,'local_loss_observations':rows,
            'loss_vector_binding':{'complete':vector_complete,'expected_columns':binding.get('expected_columns'),
                                   'values':vector_values,'column_order':'left_to_right_no_color_names_inferred'},
            'cross_source_corroboration':corroboration,
            'note':'按独立材料规则核对实测值和条件，未使用OCR要求列作为标准限值',
            'deterministic_review_action':('整改'+'、'.join(failed)+'；' if failed else '')+('核对'+'、'.join(unknown) if unknown else '')})


def _sheath_loss_weight_source_evidence(matrix_row: dict[str, Any], source_group: dict[str, Any] | None) -> dict[str, Any] | None:
    original=_sheath_loss_weight_measurement_evidence(matrix_row,source_group)
    local=_local_pvc_loss_evidence(matrix_row,source_group)
    if local and (original is None or original.get('verdict')!='fail'):
        if original:local['previous_source_check']=original
        return local
    return original


def _sheath_loss_weight_measurement_evidence(
    matrix_row: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """确定性补齐护套失重；限值、实测值和P必须同时存在。"""
    if not source_group or str(matrix_row.get("item_code") or "").upper() != "SHEATH_LOSS_WEIGHT":
        return None
    from backend.app.ocr_table_repair import measurement_group
    source_group = measurement_group(source_group, 'SHEATH_LOSS_WEIGHT')
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        if not _is_sheath_mechanical_page(page_text):
            continue
        vertical = None
        if "<table" not in page_text.lower():
            vertical = _vertical_rapidocr_measurement(
                page_text, r"失重试验\s*[-—－一]{0,3}\s*失重",
            )
        if vertical:
            return {
                "category": "护套机械性能",
                "item": str(matrix_row.get("item_name") or "护套失重试验"),
                "reported": "失重：" + "/".join(
                    f"{value:g}" for value in vertical["reported_values"]
                ) + "mg/cm²",
                "required": (
                    f"{vertical['direction']}"
                    f"{'±' if vertical['signed'] else ''}{vertical['limit']:g}mg/cm²"
                ),
                "verdict": "pass" if vertical["passed"] else "fail",
                "basis": _matrix_reference(matrix_row),
                "note": "程序从RapidOCR纵向表中确定性补齐护套失重并独立复算",
                "source_pages": [int(page["page"])],
                "source_excerpt": vertical["excerpt"],
                "evidence_status": "located",
                "coverage_origin": "deterministic_source_recovery",
            }
        measurements = _tabular_measurements(
            _plain_table_text(page_text), r"失重试验\s*[-—－一]{0,3}\s*失重",
        )
        if not measurements:
            continue
        passed = all(item["passed"] for item in measurements)
        return {
            "category": "护套机械性能",
            "item": str(matrix_row.get("item_name") or "护套失重试验"),
            "reported": "失重：" + "/".join(
                f"{value:g}" for item in measurements for value in item["reported_values"]
            ) + "mg/cm²",
            "required": "；".join(
                f"{item['direction']}{'±' if item['signed'] else ''}{item['limit']:g}mg/cm²"
                for item in measurements if item["limit"] is not None
            ),
            "verdict": "pass" if passed else "fail",
            "basis": _matrix_reference(matrix_row),
            "note": "程序从护套机械性能原表确定性补齐；外网模型未返回该必审项目",
            "source_pages": [int(page["page"])],
            "source_excerpt": "；".join(item["excerpt"] for item in measurements)[:600],
            "evidence_status": "located",
            "coverage_origin": "deterministic_source_recovery",
        }
    return None


def _vertical_component_lowtemp_pair(page_text: str) -> dict[str, Any] | None:
    """Read one explicit component's bend/tensile rows, including column OCR.

    Requires both headings and complete result/verdict cells. No last-row or
    cross-component borrowing; this recovers facts, not specimen selection.
    """
    number = r'[-+]?\d+(?:\.\d+)?'
    if '<table' in page_text.lower():
        component = '护套' if _is_sheath_mechanical_page(page_text) else '绝缘'
        found = {}
        excerpts = []
        for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', page_text, re.I | re.S):
            cells = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', cell)).strip()
                     for cell in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.I | re.S)]
            label_index = next((i for i, cell in enumerate(cells)
                                if re.search(r'低温(?:弯曲|卷绕)试验|低温拉伸试验', cell)), None)
            if label_index is None or not cells:
                continue
            verdict = cells[-1].upper()
            if verdict not in {'P', 'F', 'N'}:
                continue
            tail = [cell for cell in cells[label_index + 1:-1] if cell]
            is_tensile = '拉伸' in cells[label_index]
            if verdict == 'N':
                candidates = [cell for cell in tail if re.fullmatch(r'[-—－/]+', cell)]
                if len(candidates) != 1:
                    continue
                measurement = {'value':candidates[0], 'report_verdict':'N', 'verdict':'not_applicable'}
                limit = None
            elif is_tensile:
                limit_values = [float(v) for cell in tail
                                for v in re.findall(r'最小\s*('+number+r')', cell)]
                result_values = [cell.rstrip('%') for cell in tail
                                 if re.fullmatch(number+r'%?', cell)]
                if len(limit_values) != 1 or not result_values:
                    continue
                # The requirement cell is excluded by its ``最小`` prefix;
                # repeated identical specimen columns are accepted as one fact.
                if len(set(result_values)) != 1:
                    continue
                value = float(result_values[0])
                limit = limit_values[0]
                measurement = {'value':f'{value:g}', 'report_verdict':verdict,
                               'verdict':'pass' if verdict=='P' and value>=limit else 'fail'}
            else:
                candidates = [cell for cell in tail if cell in {'无裂纹', '有裂纹', '开裂'}]
                if len(set(candidates)) != 1:
                    continue
                value = candidates[0]
                limit = None
                measurement = {'value':value, 'report_verdict':verdict,
                               'verdict':'pass' if verdict=='P' and value=='无裂纹' else 'fail'}
            key = 'tensile' if is_tensile else 'bend'
            if key in found and found[key] != measurement:
                return None
            found[key] = measurement
            if is_tensile:
                found['tensile_minimum'] = limit
            excerpts.append(' | '.join(cells))
        if set(found) >= {'bend', 'tensile', 'tensile_minimum'}:
            return {'component':component, 'bend':found['bend'], 'tensile':found['tensile'],
                    'tensile_minimum':found['tensile_minimum'], 'excerpt':'\n'.join(excerpts)}
        return None
    compact = re.sub(r'\s+', '', page_text)
    components = [s for s in ('绝缘','护套') if s+'机械性能' in compact]
    if len(components) != 1:
        return None
    headings = list(re.finditer(r'低温(?:弯曲|卷绕)试验|低温拉伸试验(?:\s*[-—－一]\s*伸长率)?',page_text))
    if len(headings) != 2 or '拉伸' in headings[0][0] or '拉伸' not in headings[1][0]:
        return None
    bend = page_text[headings[0].end():headings[1].start()]
    tail = re.split(r'低温冲击试验|成\s*品\s*电\s*线\s*电\s*缆\s*试\s*验|注\s*[:：]|--- Page',page_text[headings[1].end():],maxsplit=1)[0]
    lines = lambda v:'\n'.join(line.strip() for line in v.splitlines() if line.strip())
    bend, tail = lines(bend), lines(tail)
    value = rf'(?:无裂纹|有裂纹|开裂|[-—－/]+|{number}%?)'
    # Column layout: two requirements, two results, then two P/F/N cells.
    column = re.search(rf'%\n无裂纹\n最小\s*({number})?\n({value})\n({value})\n([PFN])\n([PFN])$',tail)
    blank_bend_column = re.search(rf'%\n[-—－/]+\n最小\s*({number})\n([-—－/]+)\n({number}%?)\n(N)\n([PF])$',tail)
    if column:
        limit,bend_value,tensile_value,bend_state,tensile_state=column.groups()
    elif blank_bend_column:
        limit,bend_value,tensile_value,bend_state,tensile_state=blank_bend_column.groups()
    else:
        b = re.search(rf'无裂纹\n({value})\n([PFN])$',bend)
        t = re.search(rf'%\n最小\s*({number})?\n({value})\n([PFN])$',tail)
        if not b or not t:
            return None
        bend_value,bend_state=b.groups()
        limit,tensile_value,tensile_state=t.groups()
    def measurement(value, state, minimum, is_bend):
        if state=='N':
            return dict(value=value,report_verdict=state,verdict='not_applicable') if re.fullmatch(r'[-—－/]+',value) else None
        if is_bend:
            if value not in ('无裂纹','有裂纹','开裂'): return None
            passed=value=='无裂纹' and state=='P'
        else:
            if minimum is None or not re.fullmatch(number+r'%?',value): return None
            passed=float(value.rstrip('%'))>=float(minimum) and state=='P'
        return dict(value=value,report_verdict=state,verdict='pass' if passed else 'fail')
    b=measurement(bend_value,bend_state,None,True)
    t=measurement(tensile_value,tensile_state,limit,False)
    if not b or not t: return None
    return {'component':components[0], 'bend':b, 'tensile':t,
            'tensile_minimum':float(limit) if limit is not None else None,
            'excerpt':page_text[headings[0].start():headings[1].end()]+ '\n'+tail}


def _lowtemp_heading_verdict(plain: str, heading_pattern: str) -> str:
    heading = re.search(heading_pattern, plain)
    if not heading:
        return ""
    tail = plain[heading.start():heading.start() + 520]
    next_item = re.search(
        r"\|\s*(?:低温(?:弯曲|卷绕)试验|低温拉伸试验|低温冲击试验|注\s*[:：])",
        tail[heading.end() - heading.start():],
    )
    if next_item:
        tail = tail[:heading.end() - heading.start() + next_item.start()]
    # N/mm² is a strength unit in a collapsed neighbouring column, not an
    # explicit not-applicable verdict. Lookarounds also avoid consuming the
    # separator shared by consecutive standalone verdicts.
    verdicts = re.findall(r"(?<![A-Z0-9])([PFN])(?![A-Z0-9]|\s*/)", tail, re.I)
    return verdicts[-1].upper() if verdicts else ""


def _coordinate_lowtemp_pair(
    source_group: dict[str, Any] | None,
    component: str,
) -> dict[str, Any] | None:
    """Return one identity-checked coordinate observation for this component."""
    if not source_group:
        return None
    records = [record for record in source_group.get('_lowtemp_method_observations', [])
               if record.get('status') == 'located' and record.get('component') == component
               and record.get('scope') == 'source_facts_only_not_method_applicability']
    return records[0] if len(records) == 1 else None


def _coordinate_lowtemp_bend(source_group: dict[str, Any] | None, component: str) -> dict[str, Any] | None:
    if not source_group:return None
    records=[record for record in source_group.get('_lowtemp_bend_observations',[])
             if record.get('status')=='located' and record.get('component')==component
             and record.get('scope')=='source_facts_only_not_method_applicability']
    return records[0] if len(records)==1 else None


def _sheath_lowtemp_bend_source_state(source_group: dict[str, Any] | None) -> str:
    """从护套原表判断低温弯曲/拉伸二选一方法。"""
    if not source_group:
        return UNKNOWN
    coordinate_pair = _coordinate_lowtemp_pair(source_group, '护套')
    if coordinate_pair:
        if (coordinate_pair['bend']['verdict'] == 'pass'
                and coordinate_pair['tensile']['verdict'] == 'not_applicable'):
            return TRUE
        if (coordinate_pair['tensile']['verdict'] == 'pass'
                and coordinate_pair['bend']['verdict'] == 'not_applicable'):
            return FALSE
        return UNKNOWN
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        if not _is_sheath_mechanical_page(page_text):
            continue
        pair = _vertical_component_lowtemp_pair(page_text)
        if pair and pair['component'] == '护套':
            if pair['bend']['verdict'] == 'pass' and pair['tensile']['verdict'] == 'not_applicable':
                return TRUE
            if pair['tensile']['verdict'] == 'pass' and pair['bend']['verdict'] == 'not_applicable':
                return FALSE
            return UNKNOWN
        plain = _plain_table_text(page_text)
        bend = _lowtemp_heading_verdict(plain, r"低温(?:弯曲|卷绕)试验")
        tensile = _lowtemp_heading_verdict(plain, r"低温拉伸试验")
        if bend == "P" and tensile != "P":
            return TRUE
        if tensile == "P" and bend in {"", "N"}:
            return FALSE
        bend_heading = re.search(r"低温(?:弯曲|卷绕)试验", plain)
        tensile_heading = re.search(r"低温拉伸试验", plain)
        identity = _source_group_identity(source_group) or {}
        area_match = re.search(r"×\s*(\d+(?:\.\d+)?)", str(identity.get("spec") or ""))
        large_section = bool(area_match and float(area_match.group(1)) > 16)
        if bend_heading and tensile_heading and bend == "" and tensile == "" and large_section:
            bend_tail = plain[bend_heading.start():tensile_heading.start()]
            tensile_tail = plain[tensile_heading.start():tensile_heading.start() + 520]
            bend_blank = bool(re.search(r"(?:^|\|)\s*[-—－]+\s*(?:\||$)", bend_tail))
            tensile_blank = bool(re.search(r"(?:^|\|)\s*[-—－]+\s*(?:\||$)", tensile_tail))
            if bend_blank and tensile_blank:
                return FALSE
    return UNKNOWN


def _finished_voltage_source_evidence(
    matrix_row: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """确定性补齐成品电压试验；必须同时看到未击穿和P。"""
    if not source_group or str(matrix_row.get("item_code") or "").upper() != "VOLTAGE_FINISHED":
        return None
    for page in source_group.get("pages") or []:
        plain = _plain_table_text(str(page.get("text") or ""))
        for heading in re.finditer(r"成品(?:电线)?电缆电压试验|成品电压试验", plain):
            tail = plain[heading.start():heading.start() + 420]
            next_item = re.search(
                r"\|\s*(?:绝缘线芯电压试验|绝缘电阻|热冲击试验|低温冲击试验)",
                tail[heading.end() - heading.start():],
            )
            if next_item:
                tail = tail[:heading.end() - heading.start() + next_item.start()]
            verdicts = re.findall(r"(?:^|[^A-Z])([PFN])(?:$|[^A-Z])", tail, re.I)
            verdict = verdicts[-1].upper() if verdicts else ""
            if verdict != "P" or not re.search(r"未击穿|不击穿", tail):
                continue
            return {
                "category": "电性能",
                "item": str(matrix_row.get("item_name") or "成品电压试验"),
                "reported": "未击穿；判P",
                "required": "报告必须给出不击穿结果并判P",
                "verdict": "pass",
                "basis": _matrix_reference(matrix_row),
                "note": "程序从成品电压试验原表确定性补齐；外网模型未返回该必审项目",
                "source_pages": [int(page["page"])],
                "source_excerpt": tail[:420],
                "evidence_status": "located",
                "coverage_origin": "deterministic_source_recovery",
            }
    return None


def _insulation_voltage_source_evidence(
    matrix_row: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Recover a row-shifted core-voltage result from adjacent HTML rows.

    Paddle can place the voltage/``未击穿`` cells in the immediately
    preceding row while leaving resistance values beside the printed item
    label.  The recovery is accepted only with one adjacent voltage row, one
    prescribed insulation-thickness requirement and a matching P verdict.
    """
    if (not source_group or str(matrix_row.get('item_code') or '').upper() != 'INSUL_VOLTAGE'):
        return None
    identity = _source_group_identity(source_group) or {}
    rated = re.sub(r'\s+', '', str(identity.get('voltage') or ''))
    coordinate_records=[record for record in source_group.get('_insulation_voltage_observations',[])
                        if record.get('status')=='located'
                        and record.get('scope')=='source_facts_only_voltage_requirement_checked_in_rulebase']
    if len(coordinate_records)==1 and rated in {'300/300','300/500','450/750'}:
        record=coordinate_records[0]
        prescribed=float(record['prescribed_insulation_thickness_mm'])
        expected=(1500 if rated=='300/300' else None) if prescribed<=.6+1e-9 else {
            '300/300':2000,'300/500':2000,'450/750':2500}[rated]
        if expected is None:return None
        voltage=float(record['reported_voltage']);minutes=float(record['minutes'])
        passed=(voltage==expected and minutes==5 and record.get('no_breakdown') is True
                and record.get('report_verdict')=='P')
        return {'category':'电性能','item':str(matrix_row.get('item_name') or '绝缘线芯电压试验'),
                'reported':f'{voltage:g}V，{minutes:g}min，未击穿，判P',
                'required':f'按规定绝缘厚度{prescribed:g}mm应为{expected}V，5min，不击穿',
                'verdict':'pass' if passed else 'fail',
                'basis':_matrix_reference(matrix_row)+'；JB/T 8734.1-2016 表3',
                'note':'原PDF坐标行的电压参数、未击穿、P评定和同页规定绝缘厚度交叉绑定；未使用实测厚度改档',
                'source_pages':[int(record['page'])],
                'source_excerpt':f'绝缘线芯电压试验 {voltage:g}V/{minutes:g}min，未击穿，P；规定绝缘厚度{prescribed:g}mm',
                'evidence_status':'located','coverage_origin':'coordinate_voltage_row_recovery',
                'prescribed_insulation_thickness_mm':prescribed}
    for page in source_group.get('pages') or []:
        page_text = str(page.get('text') or '')
        rows = []
        for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', page_text, re.I | re.S):
            rows.append([re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', cell)).strip()
                         for cell in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.I | re.S)])
        labels = [i for i, cells in enumerate(rows) if any('绝缘线芯电压试验' in cell for cell in cells)]
        if len(labels) != 1:
            continue
        label_index = labels[0]
        candidates = []
        for index in range(max(0, label_index - 1), min(len(rows), label_index + 2)):
            joined = ' | '.join(rows[index])
            voltage = re.search(r'(?<!\d)(\d{3,4})\s*V\s*[,，]?\s*(\d+(?:\.\d+)?)\s*min', joined, re.I)
            if voltage and '未击穿' in joined and rows[index][-1].upper() == 'P':
                candidates.append((index, float(voltage[1]), float(voltage[2]), joined))
        if len(candidates) != 1:
            continue
        thicknesses = []
        for cells in rows:
            if not any('绝缘平均厚度' in cell for cell in cells):
                continue
            values = [float(v) for cell in cells for v in re.findall(r'最小\s*(\d+(?:\.\d+)?)', cell)]
            if len(values) == 1:
                thicknesses.extend(values)
        if len(set(thicknesses)) != 1 or rated not in {'300/300', '300/500', '450/750'}:
            continue
        prescribed = thicknesses[0]
        if prescribed <= .6 + 1e-9:
            expected = 1500 if rated == '300/300' else None
        else:
            expected = {'300/300':2000, '300/500':2000, '450/750':2500}[rated]
        if expected is None:
            return None
        _, reported_voltage, minutes, excerpt = candidates[0]
        passed = reported_voltage == expected and minutes == 5
        return {
            'category':'电性能', 'item':str(matrix_row.get('item_name') or '绝缘线芯电压试验'),
            'reported':f'{reported_voltage:g}V，{minutes:g}min，未击穿，判P',
            'required':f'按规定绝缘厚度{prescribed:g}mm应为{expected}V，5min，不击穿',
            'verdict':'pass' if passed else 'fail', 'basis':_matrix_reference(matrix_row)+'；JB/T 8734.1-2016 表3',
            'note':'程序用同页相邻电压参数行、未击穿结果、P评定和标称绝缘厚度交叉绑定；未使用后续绝缘电阻数值',
            'source_pages':[int(page['page'])], 'source_excerpt':excerpt[:520],
            'evidence_status':'located', 'coverage_origin':'adjacent_voltage_row_recovery',
            'prescribed_insulation_thickness_mm':prescribed,
        }
    return None


def _jbt87343_rvv_finished_voltage_applicability(
    sample: dict[str, Any],
    model: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """按JB/T 8734.1-2016表3判断8734.3 RVV成品电压适用性。

    该项目不能只按型号设为无条件必做：规定绝缘厚度<=0.6 mm时，
    300/500 V和450/750 V栏为横线；300/300 V仍做1500 V。
    规定厚度>0.6 mm时三档分别做2000/2000/2500 V。
    厚度必须来自产品/报告结构栏中的标准要求，不使用实测厚度改档。
    """
    if (
        _standard_key(str(model.get("standard_no") or "")) != "8734.3"
        or re.sub(r"\W+", "", str(model.get("model_code") or "").upper()) != "RVV"
    ):
        return None
    voltage_match = re.search(r"(300\s*/\s*300|300\s*/\s*500|450\s*/\s*750)", str(sample.get("voltage") or ""))
    if not voltage_match:
        return {
            "state": UNKNOWN,
            "reason": "rated_voltage_missing",
            "basis": "JB/T 8734.1-2016 表3",
        }
    voltage = re.sub(r"\s+", "", voltage_match.group(1))
    thickness_evidence = _dimension_source_evidence({
        "item_code": "THICKNESS_MEAS",
        "item_name": "绝缘厚度测量",
        "standard_no": "JB/T 8734.3-2016",
        "table_no": "表7",
        "table_item_no": "2.2",
    }, source_group)
    average_limits = [
        float(value) for value in re.findall(
            r"绝缘平均厚度：最小([0-9]+(?:\.[0-9]+)?)",
            str((thickness_evidence or {}).get("required") or ""),
        )
    ]
    # MinerU常把整个结构表格合并成一行，没有保留
    # “绝缘平均厚度：最小x”这种完整标签。此时只在同一HTML
    # 结构行同时具备“导体单线直径→绝缘平均厚度→
    # 绝缘最薄处厚度”表头时，按列序取标准要求中“导体最大”
    # 后的第一个“最小”。不读检验结果列，避免用实测厚度改档。
    if not average_limits and source_group:
        source_limits: list[float] = []
        for page in source_group.get("pages") or []:
            page_text = str(page.get("text") or "")
            for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page_text, re.I | re.S):
                cells = [
                    re.sub(r"\s+", "", re.sub(r"<[^>]+>", " ", cell))
                    for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.I | re.S)
                ]
                heading_index = next((
                    index for index, cell in enumerate(cells)
                    if all(marker in cell for marker in (
                        "导体单线直径", "绝缘平均厚度", "绝缘最薄处厚度",
                    ))
                ), None)
                if heading_index is None:
                    continue
                requirement_cell = next((
                    cell for cell in cells[heading_index + 1:]
                    if "最大" in cell and cell.count("最小") >= 2
                ), "")
                ordered_limits = re.findall(
                    r"(最大|最小)([0-9]+(?:\.[0-9]+)?)", requirement_cell
                )
                maximum_index = next((
                    index for index, (direction, _) in enumerate(ordered_limits)
                    if direction == "最大"
                ), None)
                if maximum_index is None:
                    continue
                first_minimum = next((
                    float(value) for direction, value in ordered_limits[maximum_index + 1:]
                    if direction == "最小"
                ), None)
                if first_minimum is not None:
                    source_limits.append(first_minimum)
        average_limits.extend(source_limits)
    unique_limits = sorted(set(average_limits))
    if len(unique_limits) != 1:
        return {
            "state": UNKNOWN,
            "rated_voltage": voltage,
            "reason": "prescribed_insulation_thickness_missing_or_ambiguous",
            "basis": "JB/T 8734.1-2016 表3",
        }
    prescribed = unique_limits[0]
    if prescribed <= 0.6 + 1e-9:
        applicable = voltage == "300/300"
        expected_voltage = 1500 if applicable else None
    else:
        applicable = True
        expected_voltage = {"300/300": 2000, "300/500": 2000, "450/750": 2500}[voltage]
    return {
        "state": TRUE if applicable else FALSE,
        "rated_voltage": voltage,
        "prescribed_insulation_thickness_mm": prescribed,
        "expected_voltage_v": expected_voltage,
        "reason": "jbt87341_table3_prescribed_thickness_and_rated_voltage",
        "basis": "JB/T 8734.1-2016 表3",
    }


def _rvs_impact_source_conditions(matrix_row: dict[str, Any], source_group: dict[str, Any]) -> dict[str, Any] | None:
    """Bound RVS finished-impact evidence; structure diameter is not specimen d."""
    if (str(matrix_row.get('resolved_model_code') or '').upper() != 'RVS'
            or '8734.3' not in str(matrix_row.get('standard_no') or '')):
        return None
    records = []
    coordinate_records=source_group.get('_impact_observations') or []
    if len(coordinate_records)==1:
        observation=coordinate_records[0]
        facts=observation['facts']
        block=(f"低温冲击试验 | 温度{facts['temperature_c']:g}℃ | 时间{facts['hours']:g}h | "
               f"落锤重量{facts['mass_g']:g}g | "+' | '.join(facts['results'])+' | '+facts['report_verdict'])
        records.append((observation['page'],block))
    for page in ([] if records else source_group.get('pages') or []):
        plain = _plain_table_text(str(page.get('text') or ''))
        matches = list(re.finditer(r'低温冲击试验(?![机仪])', plain))
        for match in matches:
            tail = plain[match.start():]
            end = re.search(r'注\s*[:：]|热冲击|热稳定|(?:电缆)?单根垂直燃烧|燃烧性能|--- Page', tail)
            block = tail[:end.start()] if end else tail[:700]
            records.append((page.get('page'), block))
    if len(records) != 1:
        return None
    page, block = records[0]
    def unique(pattern):
        values = {float(v) for v in re.findall(pattern, block)}
        return next(iter(values)) if len(values) == 1 else None
    mass = unique(r'落锤(?:质量|重量)\s*[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*g\b')
    diameter = unique(r'(?:冲击)?试样(?:实测)?外径\s*(?:d\s*)?[:：]?\s*([+-]?\d+(?:\.\d+)?)\s*mm\b')
    expected = None
    if diameter is not None and diameter > 0:
        expected = next((weight for upper, weight in ((6,100),(10,200),(15,300),(25,400),(35,500)) if diameter <= upper), 600)
    crack_text = re.sub(r'没有裂纹|未出现裂纹|未见裂纹|未见开裂|未开裂|无裂纹', '', block)
    crack = bool(re.search(r'有裂纹|出现裂纹|开裂', crack_text))
    states = [dict(field='落锤质量', reported=mass,
        required=f'{expected}g' if expected is not None else '按每个冲击试样实测外径选择表3落锤质量',
        verdict='fail' if mass is not None and mass <= 0 else 'unknown' if mass is None or expected is None else 'pass' if mass == expected else 'fail',
        reason='specimen_diameter_missing' if mass is not None and mass>0 and expected is None else None)]
    states.append(dict(field='裂纹结果', reported='有裂纹' if crack else '无裂纹' if '无裂纹' in block else None,
        required='无裂纹', verdict='fail' if crack or _lowtemp_heading_verdict(block, r'低温冲击试验') == 'F' else 'pass' if block.count('无裂纹') >= 2 and _lowtemp_heading_verdict(block, r'低温冲击试验') == 'P' else 'unknown'))
    verdict = 'fail' if any(v['verdict']=='fail' for v in states) else 'manual_review' if any(v['verdict']=='unknown' for v in states) else 'pass'
    action = '核对各个低温冲击试样实测外径及落锤记录；不能把成品结构平均外径或其一半直接当作冲击试样d，证据不足不自动通过'
    gap_only=(diameter is None and mass in {100,200,300,400,500,600}
              and states[-1]['verdict']=='pass' and _lowtemp_heading_verdict(block,r'低温冲击试验')=='P')
    return dict(category='成品电线电缆试验', item=matrix_row.get('item_name') or '成品低温冲击',
        reported=f'落锤质量：{str(mass)+"g" if mass is not None else "未可靠识别"}；冲击试样外径：{diameter if diameter is not None else "未明确记录"}；裂纹结果：{states[-1]["reported"] or "未可靠识别"}',
        required='落锤按GB/T 2951.14-2008表3及试样实测外径选择；冲击后无裂纹',
        verdict=verdict, basis=_matrix_reference(matrix_row)+'；GB/T 2951.14-2008 8.5.4表3',
        note='确定性核对RVS冲击落锤与试样外径，未使用结构平均外径代替试样尺寸；'+action,
        deterministic_review_action=action, impact_condition_checks=states,
        impact_record_gap_only=gap_only, impact_record_table_family='soft',
        impact_dimension_evidence={'status':'missing_or_conflicting','observations':[]} if diameter is None else
                                  {'status':'located','observations':[diameter]},
        impact_specimen_diameter_mm=diameter, source_pages=[page], source_excerpt=block,
        impact_coordinate_observation=coordinate_records[0] if len(coordinate_records)==1 else None,
        evidence_status='located', coverage_origin='deterministic_source_recovery')


def _lowtemp_source_evidence(
    matrix_row: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """对矩阵明确必审的低温弯曲/拉伸读取原表P/N。"""
    item_code = str(matrix_row.get("item_code") or "").upper()
    supported = {
        "LOWTEMP_BEND_INSUL", "LOWTEMP_BEND_SHEATH",
        "LOWTEMP_TENSILE_INSUL", "LOWTEMP_TENSILE_SHEATH",
        "LOWTEMP_IMPACT_FINISHED",
    }
    if not source_group or item_code not in supported:
        return None
    if item_code == "LOWTEMP_IMPACT_FINISHED":
        rvs_conditions = _rvs_impact_source_conditions(matrix_row, source_group)
        if rvs_conditions is not None:
            return rvs_conditions
        for page in source_group.get("pages") or []:
            plain = _plain_table_text(str(page.get("text") or ""))
            heading = re.search(r"(?:成品(?:电线电缆)?低温冲击试验|低温冲击试验)", plain)
            if not heading:
                continue
            tail = plain[heading.start():heading.start() + 700]
            verdict = _lowtemp_heading_verdict(tail, r"(?:成品(?:电线电缆)?)?低温冲击试验")
            if verdict != "P" or "无裂纹" not in tail:
                continue
            return {
                "category": "成品电线电缆试验",
                "item": str(matrix_row.get("item_name") or "成品低温冲击"),
                "reported": "原表报告低温冲击无裂纹并判P",
                "required": "低温冲击后绝缘/护套无裂纹并判P",
                "verdict": "pass",
                "basis": _matrix_reference(matrix_row),
                "note": "程序从当前样品原页低温冲击行确定性补齐",
                "source_pages": [int(page["page"])],
                "source_excerpt": tail[:700],
                "evidence_status": "located",
                "coverage_origin": "deterministic_source_recovery",
            }
        return None
    if item_code == "LOWTEMP_BEND_INSUL":
        coordinate_pair = _coordinate_lowtemp_pair(source_group, '绝缘')
        if coordinate_pair is None:
            coordinate_pair = _coordinate_lowtemp_bend(source_group, '绝缘')
        if coordinate_pair and coordinate_pair['bend']['verdict'] in {'pass','fail'}:
            fact=coordinate_pair['bend'];page=int(coordinate_pair['page'])
            return {'category':'绝缘低温性能','item':str(matrix_row.get('item_name') or '绝缘低温弯曲'),
                    'reported':f"低温弯曲结果：{fact['value']}；报告判{fact['report_verdict']}",
                    'required':'低温弯曲后绝缘无裂纹','verdict':fact['verdict'],
                    'basis':_matrix_reference(matrix_row),'source_pages':[page],
                    'source_excerpt':f"原PDF坐标行：绝缘低温弯曲 {fact['value']} / {fact['report_verdict']}",
                    'evidence_status':'located','coverage_origin':'coordinate_lowtemp_row_recovery',
                    'note':'从原PDF坐标行绑定结果与评定；方法适用性仍由同样品规则单独判断'}
        for page in source_group.get("pages") or []:
            page_text = str(page.get("text") or "")
            if _is_sheath_mechanical_page(page_text):
                continue
            pair = _vertical_component_lowtemp_pair(page_text)
            if pair and pair['component'] == '绝缘' and pair['bend']['verdict'] in {'pass','fail'}:
                fact = pair['bend']
                return {
                    'category':'绝缘低温性能', 'item':str(matrix_row.get('item_name') or '绝缘低温弯曲'),
                    'reported':f"低温弯曲结果：{fact['value']}；报告判{fact['report_verdict']}",
                    'required':'低温弯曲后绝缘无裂纹', 'verdict':fact['verdict'],
                    'basis':_matrix_reference(matrix_row),
                    'note':'从同一样品绝缘页完整两方法表头、两列结果及评定恢复；条件另行核查',
                    'source_pages':[int(page['page'])], 'source_excerpt':pair['excerpt'][:700],
                    'evidence_status':'located', 'coverage_origin':'component_lowtemp_column_recovery',
                }
            plain = _plain_table_text(page_text)
            heading = re.search(r"低温(?:弯曲|卷绕)试验", plain)
            if not heading:
                continue
            tail = plain[heading.start():heading.start() + 520]
            next_item = re.search(
                r"\|\s*(?:低温拉伸试验|低温冲击试验|注\s*[:：])",
                tail[heading.end() - heading.start():],
            )
            if next_item:
                tail = tail[:heading.end() - heading.start() + next_item.start()]
            verdict = _lowtemp_heading_verdict(tail, r"低温(?:弯曲|卷绕)试验")
            if verdict != "P" or "无裂纹" not in tail:
                continue
            temperature = re.search(r"温度[^0-9+\-]{0,12}([+\-]?\d+(?:\.\d+)?)\s*(?:\|\s*)?℃", tail)
            duration = re.search(r"时间[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:\|\s*)?h", tail, re.I)
            conditions = []
            if temperature:
                conditions.append(f"{temperature.group(1)}℃")
            if duration:
                conditions.append(f"{duration.group(1)}h")
            conditions.extend(("无裂纹", "判P"))
            return {
                "category": "绝缘低温性能",
                "item": str(matrix_row.get("item_name") or "绝缘低温弯曲"),
                "reported": "；".join(conditions),
                "required": "低温弯曲后绝缘无裂纹并判P",
                "verdict": "pass",
                "basis": _matrix_reference(matrix_row),
                "note": "程序从当前样品绝缘机械性能原页确定性补齐",
                "source_pages": [int(page["page"])],
                "source_excerpt": tail[:520],
                "evidence_status": "located",
                "coverage_origin": "deterministic_source_recovery",
            }
        return None
    if item_code in {"LOWTEMP_TENSILE_INSUL", "LOWTEMP_TENSILE_SHEATH"}:
        want_sheath = item_code.endswith("SHEATH")
        component = '护套' if want_sheath else '绝缘'
        coordinate_pair = _coordinate_lowtemp_pair(source_group, component)
        if coordinate_pair and coordinate_pair['tensile']['verdict'] in {'pass','fail'}:
            fact=coordinate_pair['tensile'];page=int(coordinate_pair['page'])
            return {'category':'护套低温性能' if want_sheath else '绝缘低温性能',
                    'item':str(matrix_row.get('item_name') or ''),
                    'reported':f"低温拉伸伸长率：{fact['value']}%；报告判{fact['report_verdict']}",
                    'required':f"最小{coordinate_pair['tensile_minimum']:g}%",'verdict':fact['verdict'],
                    'basis':_matrix_reference(matrix_row),'source_pages':[page],
                    'source_excerpt':f"原PDF坐标行：{component}低温拉伸 最小{coordinate_pair['tensile_minimum']:g}% / {fact['value']}% / {fact['report_verdict']}",
                    'evidence_status':'located','coverage_origin':'coordinate_lowtemp_row_recovery',
                    'note':'从原PDF坐标行分别绑定要求、结果和评定；不从整页合并文本取值'}
        for page in source_group.get("pages") or []:
            page_text = str(page.get("text") or "")
            if _is_sheath_mechanical_page(page_text) != want_sheath:
                continue
            pair = _vertical_component_lowtemp_pair(page_text)
            if pair and pair['component']==('护套' if want_sheath else '绝缘') and pair['tensile']['verdict'] in {'pass','fail'}:
                fact = pair['tensile']
                return {'category':'护套低温性能' if want_sheath else '绝缘低温性能',
                        'item':str(matrix_row.get('item_name') or ''),
                        'reported':f"低温拉伸伸长率：{fact['value']}%；报告判{fact['report_verdict']}",
                        'required':f"最小{pair['tensile_minimum']:g}%", 'verdict':fact['verdict'],
                        'basis':_matrix_reference(matrix_row), 'source_pages':[int(page['page'])],
                        'source_excerpt':pair['excerpt'][:900], 'evidence_status':'located',
                        'coverage_origin':'component_lowtemp_column_recovery',
                        'note':'同部件两方法列分别绑定结果和P/N；不从整页合并数值串取伸长率，条件另行核查'}
            plain = _plain_table_text(page_text)
            measurements = _tabular_measurements(
                plain, r"低温拉伸试验\s*[-—－一]{0,3}\s*伸长率",
            )
            measured_rows = [row for row in measurements if row["report_verdict"] in {"P","F"}]
            if len(measured_rows) != 1:
                continue
            duration = re.search(r"低温拉伸试验.{0,420}?时间\s*(?:\|\s*)?(\d+(?:\.\d+)?)\s*(?:\|\s*)?h", plain, re.I | re.S)
            measurement = measured_rows[0]
            return {
                "category": "护套低温性能" if want_sheath else "绝缘低温性能",
                "item": str(matrix_row.get("item_name") or ""),
                "reported": "低温拉伸伸长率：" + "/".join(
                    f"{value:g}" for value in measurement["reported_values"]
                ) + (f"；{duration.group(1)}h" if duration else "") + f"；报告判{measurement['report_verdict']}",
                "required": f"{measurement['direction']}{measurement['limit']:g}",
                "verdict": "pass" if measurement['passed'] else "fail",
                "basis": _matrix_reference(matrix_row),
                "note": "程序从原页低温拉伸行补齐要求、实测值和评定，实测不符不改报缺项",
                "source_pages": [int(page["page"])],
                "source_excerpt": measurement["excerpt"][:700],
                "evidence_status": "located",
                "coverage_origin": "deterministic_source_recovery",
            }
        return None
    source_method_state = _sheath_lowtemp_bend_source_state(source_group)
    coordinate_pair = _coordinate_lowtemp_pair(source_group, '护套')
    if coordinate_pair and coordinate_pair['bend']['verdict'] in {'pass','fail','not_applicable'}:
        fact=coordinate_pair['bend'];page=int(coordinate_pair['page'])
        return {'category':'护套低温性能','item':str(matrix_row.get('item_name') or '护套低温弯曲'),
                'reported':f"低温弯曲结果：{fact['value']}；报告判{fact['report_verdict']}",
                'required':'护套低温弯曲适用时应无裂纹；不适用时应判N',
                'verdict':fact['verdict'],'basis':_matrix_reference(matrix_row),'source_pages':[page],
                'source_excerpt':f"原PDF坐标行：护套低温弯曲 {fact['value']} / {fact['report_verdict']}",
                'evidence_status':'located','coverage_origin':'coordinate_lowtemp_row_recovery',
                'note':'从原PDF坐标行绑定结果与评定；外径门槛和方法适用性在另一规则层判定'}
    bend_observations = []
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        section = _sheath_mechanical_section(page_text)
        if not section:
            continue
        pair = _vertical_component_lowtemp_pair(page_text)
        if pair and pair['component'] == '护套':
            fact = pair['bend']
            if fact['verdict'] in {'pass', 'fail', 'not_applicable'}:
                bend_observations.append({
                    'category':'护套低温性能',
                    'item':str(matrix_row.get('item_name') or '护套低温弯曲'),
                    'reported':f"低温弯曲结果：{fact['value']}；报告判{fact['report_verdict']}",
                    'required':'护套低温弯曲适用时应无裂纹；不适用时应判N',
                    'verdict':fact['verdict'], 'basis':_matrix_reference(matrix_row),
                    'note':'从同一护套页弯曲/拉伸两行分别绑定结果和P/N，不使用整页合并数值串',
                    'source_pages':[int(page['page'])], 'source_excerpt':pair['excerpt'][:900],
                    'evidence_status':'located', 'coverage_origin':'component_lowtemp_column_recovery',
                })
                continue
        plain = _plain_table_text(section)
        bend_verdict = _lowtemp_heading_verdict(plain, r"低温(?:弯曲|卷绕)试验")
        tensile_verdict = _lowtemp_heading_verdict(plain, r"低温拉伸试验")
        all_blank_method_rows = (
            bend_verdict == "" and tensile_verdict == ""
            and source_method_state == FALSE
        )
        if (tensile_verdict == "P" and bend_verdict in {"", "N"}) or all_blank_method_rows:
            bend_heading = re.search(r"低温(?:弯曲|卷绕)试验", plain)
            tensile_heading = re.search(r"低温拉伸试验", plain)
            excerpt_end = tensile_heading.end() + 260 if tensile_heading else (bend_heading.end() + 520 if bend_heading else 520)
            bend_observations.append({
                "category": "护套低温性能",
                "item": str(matrix_row.get("item_name") or "护套低温弯曲"),
                "reported": (
                    "低温弯曲与低温拉伸结果/评定均为横线"
                    if all_blank_method_rows else
                    "低温弯曲结果/评定为横线或N；同页低温拉伸有实测结果并判P"
                ),
                "required": "已确认低温方法行可全部填横线；采用低温拉伸时弯曲也可填横线或判N",
                "verdict": "not_applicable",
                "basis": _matrix_reference(matrix_row),
                "note": "程序按原页弯曲/拉伸配对判定方法选择；未把拉伸4h误套为弯曲16h",
                "source_pages": [int(page["page"])],
                "source_excerpt": plain[bend_heading.start() if bend_heading else 0:excerpt_end][:520],
                "evidence_status": "located",
                "coverage_origin": "deterministic_source_recovery",
            })
            continue
        for heading in re.finditer(r"低温(?:弯曲|卷绕)试验", plain):
            tail = plain[heading.start():heading.start() + 520]
            next_item = re.search(r"低温拉伸试验|低温冲击试验|注\s*[:：]", tail[heading.end() - heading.start():])
            if next_item:
                tail = tail[:heading.end() - heading.start() + next_item.start()]
            verdict = _lowtemp_heading_verdict(tail, r"低温(?:弯曲|卷绕)试验")
            no_crack = "无裂纹" in tail
            has_crack = bool(re.search(r'(?<!没)有裂纹|(?<!未)出现裂纹|(?<!未)产生裂纹', tail))
            if verdict not in {"P", "F", "N"} or not (no_crack or has_crack):
                continue
            temperature = re.search(r"温度[^0-9+\-]{0,12}([+\-]?\d+(?:\.\d+)?)\s*(?:\|\s*)?℃", tail)
            duration = re.search(r"时间[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:\|\s*)?h", tail, re.I)
            conditions = []
            if temperature:
                conditions.append(f"{temperature.group(1)}℃")
            if duration:
                conditions.append(f"{duration.group(1)}h")
            conditions.append("有裂纹" if has_crack else "无裂纹")
            conditions.append(f"判{verdict}")
            bend_observations.append({
                "category": "护套低温性能",
                "item": str(matrix_row.get("item_name") or "护套低温弯曲"),
                "reported": "；".join(conditions),
                "required": "矩阵明确本项目必审，实施后应无裂纹并判P",
                "verdict": "pass" if verdict == "P" and not has_crack else "fail",
                "basis": _matrix_reference(matrix_row),
                "note": "程序从护套机械性能原表确定性补齐P/N；N按必审矩阵判为问题",
                "source_pages": [int(page["page"])],
                "source_excerpt": tail[:520],
                "evidence_status": "located",
                "coverage_origin": "deterministic_source_recovery",
            })
    if bend_observations:
        # A later OCR layer/page with explicit failure outranks an earlier P.
        failure = next((item for item in bend_observations if item['verdict']=='fail'), None)
        if failure:
            return failure
        chosen = dict(bend_observations[0])
        if len({item['verdict'] for item in bend_observations}) > 1:
            chosen.update(verdict='manual_review',
                          deterministic_review_action='核对同一样品护套低温方法的不同来源评定冲突，不自动放行')
        return chosen
    return None


def _generic_source_evidence(label: str, source_group: dict[str, Any] | None) -> dict[str, Any]:
    """为现有结论定位页码和短证据；定位不到时明确标记，不伪造引用。"""
    if not source_group:
        return {"evidence_status": "not_located", "source_pages": [], "source_excerpt": ""}
    label_text = str(label or "")
    clean = re.sub(r"(?:审核覆盖缺失|适用性审核覆盖缺失|试验|测量)", "", label_text)
    markers = [marker for marker, _ in _ITEM_CONCEPTS if marker in str(label or "")]
    for key, aliases in _EVIDENCE_ALIASES.items():
        if key in label_text:
            markers.extend(aliases)
    for part in re.split(r"[,，、/]", label_text):
        part = re.sub(r"(?:审核覆盖缺失|适用性审核覆盖缺失|试验|测量)", "", part).strip()
        if len(part) >= 2:
            markers.append(part)
    if len(clean) >= 2:
        markers.insert(0, clean)
    markers = list(dict.fromkeys(marker for marker in markers if marker))
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        if ('绝缘' in label_text and '护套' not in label_text
                and re.search(r'老化|拉力|高温压力|机械|低温|失重|热冲击', label_text)):
            # Generic aliases such as "失重" cannot establish component
            # ownership. In particular, a sheath N row is not insulation
            # evidence when an earlier insulation page was lost by OCR.
            separator = r'(?:\s|\||<[^>]*>)*'
            own = re.search(separator.join('绝缘机械性能'), page_text)
            if not own:
                continue
            page_text = page_text[own.start():]
            following = re.search(separator.join('护套机械性能'), page_text)
            if following:
                page_text = page_text[:following.start()]
        if '护套' in label_text and '绝缘' not in label_text and re.search(r'老化|拉力|高温压力|机械|低温|失重', label_text):
            page_text = _sheath_mechanical_section(page_text)
            if not page_text:
                continue
        lines = page_text.splitlines()
        for index, line in enumerate(lines):
            if markers and not any(marker in line for marker in markers):
                continue
            excerpt = " | ".join(
                value.strip() for value in lines[index:index + 8] if value.strip()
            )[:360]
            return {
                "evidence_status": "located",
                "source_pages": [int(page["page"])],
                "source_excerpt": excerpt,
            }
    return {
        "evidence_status": "not_located",
        "source_pages": [int(page["page"]) for page in source_group.get("pages") or []],
        "source_excerpt": "",
    }


def _combined_mechanics_source_evidence(
    row: dict[str, Any], source_group: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """定位报告中合并的“绝缘/护套机械性能”check。

    部分TRF的纵向类别标题被OCR拆成单字，无法按标题字面匹配。
    只在同一样品内找到机械性能页，且该页唯一命中至少3个
    报告中的特征数值时才绑定；否则保持not_located。
    """
    label = str(row.get("item") or "")
    grouped_label = (
        "机械性能" in label
        or bool(re.search(r"(?:绝缘|护套)(?:老化|其他)性能", label))
    )
    if not source_group or not grouped_label:
        return None
    reported = str(row.get("reported") or "")
    ignored = {"0", "1", "2", "3", "4", "15", "16", "20", "24", "50", "80", "125", "150", "168"}
    numbers = [
        value for value in re.findall(r"(?<!\d)-?\d+(?:\.\d+)?(?!\d)", reported)
        if value.lstrip("-") not in ignored
    ]
    numbers = list(dict.fromkeys(numbers))
    minimum_score = 3 if "机械性能" in label else 2
    if len(numbers) < minimum_score:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for page in source_group.get("pages") or []:
        page_text = str(page.get("text") or "")
        if not all(marker in page_text for marker in ("老化前抗张强度", "失重试验", "高温压力")):
            continue
        compact = re.sub(r"\s+", "", page_text)
        score = sum(1 for value in numbers if value in compact)
        candidates.append((score, page))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], int(item[1].get("page") or 0)))
    best_score, best_page = candidates[0]
    if best_score < minimum_score or (len(candidates) > 1 and candidates[1][0] == best_score):
        return None
    lines = [line.strip() for line in str(best_page.get("text") or "").splitlines() if line.strip()]
    start = next((index for index, line in enumerate(lines) if "老化前抗张强度" in line), 0)
    return {
        "evidence_status": "located",
        "source_pages": [int(best_page["page"])],
        "source_excerpt": " | ".join(lines[start:start + 14])[:520],
    }


def bind_review_evidence(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """给checks/items绑定页码、原值、标准依据和分段来源。"""
    registry = source_sample_registry(source_text)
    audit = {"located": 0, "rule_derived": 0, "not_located": 0, "rows": 0}
    for sample in result.get("samples") or []:
        source_group = source_group_for_sample(sample, registry=registry)
        source_entry = next((entry for entry in registry if entry["group"] is source_group), None)
        for row in [*(sample.get("checks") or []), *(sample.get("items") or [])]:
            audit["rows"] += 1
            rebind_sheath_lowtemp = (not row.get('coverage_origin') and '护套' in str(row.get('item') or '')
                                    and '绝缘' not in str(row.get('item') or '')
                                    and '低温' in str(row.get('item') or '')
                                    and row.get('verdict') != 'not_applicable')
            old_evidence = {key: row.get(key) for key in ('source_pages','source_excerpt','evidence_status')}
            rebind_wrong_component = (
                not row.get('coverage_origin') and '护套' in str(row.get('item') or '')
                and '绝缘' not in str(row.get('item') or '')
                and re.search(r'老化|拉力|失重|高温压力',str(row.get('item') or ''))
                and '绝缘机械性能' in str(row.get('source_excerpt') or '')
                and '护套机械性能' not in str(row.get('source_excerpt') or ''))
            # 旧绑定失败时会保留“样品全部页码”但状态仍为not_located。
            # 规则扩展后必须允许重新精确定位，不能因非空页码而跳过。
            if (
                not row.get("source_pages")
                or str(row.get("evidence_status") or "") not in {"located", "rule_derived"}
                or rebind_sheath_lowtemp
                or rebind_wrong_component
            ):
                label = str(row.get("item") or "")
                dimension_code = (
                    "SHEATH_THICKNESS_MEAS" if "护套厚度" in label else
                    "THICKNESS_MEAS" if "绝缘厚度" in label else
                    "OD_MEAS" if "外径" in label else ""
                )
                dimension = _dimension_source_evidence({
                    "item_code": dimension_code,
                    "item_name": (
                        "护套厚度测量" if dimension_code == "SHEATH_THICKNESS_MEAS" else
                        "绝缘厚度测量" if dimension_code == "THICKNESS_MEAS" else "外径测量"
                    ),
                }, source_group) if dimension_code else None
                if dimension:
                    row.update({
                        "evidence_status": "located",
                        "source_pages": dimension["source_pages"],
                        "source_excerpt": dimension["source_excerpt"],
                    })
                elif "证据缺口" in label:
                    # 缺口项需要的是具体数值证据；仅命中试验标题不等于找到该数值。
                    row["evidence_status"] = "not_located"
                    row["source_excerpt"] = ""
                else:
                    structure_check = None
                    if "结构" in label and "检查" in label:
                        structure_check = _structure_source_evidence({
                            "item_code": "STRUCT_CHECK", "item_name": label,
                            "standard_no": "", "table_no": "", "table_item_no": "",
                        }, source_group)
                    combined_mechanics = _combined_mechanics_source_evidence(row, source_group)
                    located = structure_check or combined_mechanics or _generic_source_evidence(label, source_group)
                    row.update({
                        key: located[key] for key in ("evidence_status", "source_pages", "source_excerpt")
                        if key in located
                    })
            if (
                row.get("evidence_status") != "located"
                and row.get("verdict") == "not_applicable"
                and (row.get("basis") or row.get("standard"))
            ):
                row["evidence_status"] = "rule_derived"
                row["source_pages"] = []
                row["source_excerpt"] = "结构化型号矩阵判定为不适用，报告无需提供该项目试验页"
            if (rebind_sheath_lowtemp or rebind_wrong_component) and old_evidence.get('evidence_status') == 'located':
                new_evidence = {key: row.get(key) for key in old_evidence}
                if old_evidence != new_evidence:
                    record = {'reason':'sheath_lowtemp_component_rebinding' if rebind_sheath_lowtemp else 'sheath_mechanical_component_rebinding','previous':old_evidence,
                              'current':new_evidence}
                    history = row.setdefault('evidence_binding_history', [])
                    if record not in history:
                        history.append(record)
            status = str(row.get("evidence_status") or "not_located")
            if status not in audit:
                status = "not_located"
            audit[status] += 1
            row.setdefault("reported_value", row.get("reported") or "")
            row.setdefault("standard_ref", row.get("basis") or row.get("standard") or "")
            row.setdefault("source_segments", [source_entry["source_index"]] if source_entry else [])
    result.setdefault("_deterministic_validation", {})["evidence_binding"] = audit
    return result


def _reconcile_coordinate_recovery(sample: dict[str, Any], checks: list[dict[str, Any]], recovered: dict[str, Any]) -> None:
    """Archive a superseded model assertion only after complete same-row validation.

    Initial enabled contract: independently validated PVC sheath loss. No
    cross-component or cross-page name-only deduplication is allowed.
    """
    if recovered.get('coverage_origin')=='local_insulation_resistance_evidence' and recovered.get('item_code')=='INSUL_RES':
        pages=set(recovered.get('source_pages') or [])
        if recovered.get('verdict') not in {'pass','fail'} or len(pages)!=1:
            return
        def resistance_match(old):
            return (re.sub(r'\s+','',str(old.get('item') or ''))=='绝缘电阻'
                    and set(old.get('source_pages') or [])==pages
                    and old.get('evidence_status')=='located'
                    and not old.get('coverage_origin'))
        import copy
        for field,values in [('checks',checks),('items',sample.get('items') or [])]:
            stale=[old for old in values if resistance_match(old)]
            if not stale:continue
            archive=sample.setdefault('coordinate_reconciliation_history',[])
            for old in stale:
                archive.append({'source_collection':field,'previous':copy.deepcopy(old),
                    'reason':'same_sample_page_insulation_resistance_coordinate_validation',
                    'replacement_item_code':'INSUL_RES','source_pages':sorted(pages),
                    'replacement_verdict':recovered['verdict']})
            values[:]=[old for old in values if not resistance_match(old)]
        return
    if recovered.get('coverage_origin')!='local_pvc_loss_evidence' or recovered.get('item_code') not in {'SHEATH_LOSS_WEIGHT','LOSS_WEIGHT'}:
        return
    component='insulation' if recovered['item_code']=='LOSS_WEIGHT' else 'sheath'
    label,other=('绝缘','护套') if component=='insulation' else ('护套','绝缘')
    conditions=recovered.get('loss_condition_checks') or []
    if (recovered.get('verdict') not in {'pass','fail'} or len(conditions)!=4
        or any(c.get('verdict') not in {'pass','fail'} for c in conditions)):
        return
    rows=recovered.get('local_loss_observations') or []
    corroborated = (recovered.get('cross_source_corroboration') or {}).get('status') == 'corroborated'
    if (len(rows)!=1 or rows[0].get('component')!=component
        or (rows[0].get('status')!='located'
            and not recovered.get('loss_vector_binding',{}).get('complete')
            and not corroborated)):
        return
    pages=set(recovered.get('source_pages') or [])
    if len(pages)!=1 or rows[0].get('page') not in pages:
        return
    def matches(old):
        name=re.sub(r'\s+','',str(old.get('item') or ''))
        return (name in {label+'失重试验',label+'失重','失重试验','失重'}
                and set(old.get('source_pages') or [])==pages
                and old.get('evidence_status')=='located'
                and not old.get('coverage_origin')
                and other not in str(old.get('category') or '')
                and not old.get('loss_condition_checks'))
    import copy
    for field,values in [('checks',checks),('items',sample.get('items') or [])]:
        stale=[old for old in values if matches(old)]
        if not stale:continue
        archive=sample.setdefault('coordinate_reconciliation_history',[])
        for old in stale:
            archive.append({'source_collection':field,'previous':copy.deepcopy(old),
                'reason':'same_sample_page_component_completed_coordinate_validation',
                'replacement_item_code':recovered['item_code'],'source_pages':sorted(pages),
                'replacement_verdict':recovered['verdict']})
        values[:]=[old for old in values if not matches(old)]


def _reconcile_inapplicable_missing_check(sample, row):
    """Verified false condition clears absence-only claims, never failed tests."""
    import copy
    def key(value):
        return re.sub(r'(?:适用性)?审核覆盖缺失$|试验|\s+', '', str(value or ''))
    name=key(row.get('item_name'))
    def missing(record):
        if not name or key(record.get('item')) != name:return False
        raw=str(record.get('reported') or record.get('reported_value') or '').strip()
        if re.search(r'不合格|击穿|断路|短路|裂纹|\d|\bF\b',raw,re.I):return False
        return bool(re.fullmatch(r'(?:报告|本报告|原报告)?(?:未列出?|未报告|未提供|未进行|未做|缺少|缺失)(?:该项|该项目|本项|相应)?(?:'+re.escape(name)+r')?(?:试验)?(?:项目|记录|结果|试验记录|试验结果)?[。；;\s]*',raw))
    removed=[]
    for check in sample.get('checks',[]):
        if check.get('verdict') not in ('manual_review','fail') or not missing(check):continue
        sample.setdefault('superseded_model_checks',[]).append({'reason':'verified_condition_not_applicable','original_check':copy.deepcopy(check)})
        check.update(verdict='not_applicable',action_required=False,
            required='已验证的结构化适用条件不满足，本样品无需实施该项目',
            basis=_catalog_citation(row),note='程序已按当前样品结构化条件撤销缺项提示；不代表实施后的试验结果合格。',
            evidence_status='rule_derived',condition_state=FALSE)
        removed.append(check['item'])
    if removed:
        kept=[]
        for item in sample.get('items',[]):
            if missing(item):
                sample.setdefault('superseded_model_items',[]).append({'reason':'verified_condition_not_applicable','original_item':copy.deepcopy(item)})
            else:kept.append(item)
        sample['items']=kept


def _reconcile_coordinate_impact_absence(sample, checks, recovered):
    """Replace absence-only claims with a complete source-bound condition check.

    Never treat a recovered time or model confidence as a passed test. Keep
    numerical conflicts and explicit failed/cracked observations for review.
    """
    import copy
    if not recovered or not recovered.get('impact_coordinate_observation'):
        return False
    names = {'低温冲击', '成品低温冲击', '成品电缆低温冲击', '成品电线低温冲击'}
    facts = recovered['impact_coordinate_observation'].get('facts') or {}
    # A partial absence claim can contain a correct temperature alongside an
    # obsolete missing-mass claim. Correct only that fact, retaining the
    # unresolved specimen-count/dimension requirement and original audit trail.
    for item in sample.get('items') or []:
        if re.sub(r'试验|\s+', '', str(item.get('item') or '')) not in names:
            continue
        raw = str(item.get('reported') or '')
        partial = re.fullmatch(
            r'低温冲击试验温度([+-]?\d+(?:\.\d+)?)(?:°C|℃)，'
            r'未(?:给出|提供|记录)落锤(?:质量|重量)、试样数及判档依据', raw)
        if (partial and float(partial[1]) == facts.get('temperature_c')
                and facts.get('mass_g', 0) > 0 and facts.get('report_verdict') == 'P'
                and item.get('action_type') == 'manual_review'
                and item.get('source_pages') == recovered.get('source_pages')):
            sample.setdefault('coordinate_reconciliation_history', []).append({
                'source_collection':'items', 'previous':copy.deepcopy(item),
                'reason':'replace_only_coordinate_proven_missing_mass_claim',
                'source_pages':recovered['source_pages']})
            item['reported'] = (f"低温冲击温度{facts['temperature_c']:g}℃、落锤质量{facts['mass_g']:g}g已由原页定位；"
                '试样数量及按试样实测外径选择落锤的依据尚未核实')
            item['reported_value'] = item['reported']
            item['review_action'] = '核对试样数量及冲击试样实测外径对应的落锤档位；已定位的落锤质量无需重新查找，未将判档条件自动判为通过'
    def absence(record):
        name = re.sub(r'试验|\s+', '', str(record.get('item') or ''))
        if name not in names:
            return False
        raw = str(record.get('reported') or record.get('reported_value') or '').strip()
        states = record.get('impact_condition_checks') or []
        if (record.get('coverage_origin') == 'deterministic_source_recovery'
                and record.get('verdict') == 'manual_review'
                and record.get('source_pages') == recovered.get('source_pages')
                and len(states) == 2
                and {s.get('field') for s in states} == {'落锤质量', '裂纹结果'}
                and all(s.get('reported') is None and s.get('verdict') == 'unknown' for s in states)
                and not record.get('impact_source_failures')
                and not (record.get('impact_dimension_evidence') or {}).get('observations')
                and not re.search(r'\d|\bF\b|有裂纹|不合格|冲突', raw, re.I)):
            return True
        if record.get('impact_condition_checks') or re.search(r'\d|\bF\b|裂纹|不合格|冲突', raw, re.I):
            return False
        return bool(re.fullmatch(
            r'(?:报告|本报告|原报告)?(?:未列出?|未报告|未提供|未进行|未做|缺少|缺失)'
            r'(?:该项目|该项|本项|相应)?(?:成品)?(?:电线|电缆)?(?:低温冲击)?'
            r'(?:试验)?(?:项目|记录|结果|试验记录|试验结果)?[。；;\s]*', raw))
    removed = False
    for field, values in [('checks', checks), ('items', sample.get('items') or [])]:
        stale = [value for value in values if absence(value)]
        for old in stale:
            sample.setdefault('coordinate_reconciliation_history', []).append({
                'source_collection': field, 'previous': copy.deepcopy(old),
                'reason': 'source_bound_impact_block_proves_project_recorded',
                'source_pages': recovered.get('source_pages'),
                'replacement_verdict': recovered.get('verdict'),
            })
        if stale:
            values[:] = [value for value in values if not absence(value)]
            removed = True
    if removed:
        # A replay may already contain the same deterministic replacement.
        if not any(c.get('impact_coordinate_observation') == recovered['impact_coordinate_observation']
                   for c in checks):
            checks.append(recovered)
    return removed


def enforce_required_item_coverage(
    result: dict[str, Any],
    standard_family: str | None = None,
    source_text: str = "",
    local_evidence: dict[str, Any] | None = None,
    source_pdf_sha256: str = "",
) -> dict[str, Any]:
    """用已验证型号矩阵确定性检查AI是否审完全部必审/条件项目。

    required 项没有审核证据时必须转人工复核；conditional 项没有适用性
    判断时同样转人工复核。这里只拦截“漏审”，不凭缺失证据直接判报告不合格。
    """
    if not inspect(engine).has_table("model_test_matrix"):
        return result

    if source_text:
        # 合并check的矩阵覆盖判断依赖source_pages/evidence_status。
        # 必须先绑定原页证据，否则同一条已定位且判P的合并check
        # 会被误报为多个“审核覆盖缺失”。
        result = bind_review_evidence(result, source_text)

    coverage: dict[str, Any] = {
        "required_total": 0,
        "required_covered": 0,
        "conditional_total": 0,
        "conditional_covered": 0,
        "conditional_applicable": 0,
        "conditional_not_applicable": 0,
        "conditional_unknown": 0,
        "missing": [],
        "resolved_from_source": [],
        "project_statuses": [],
        "unmatched_samples": [],
    }
    source_registry = source_sample_registry(source_text) if source_text else []
    try:
        with engine.connect() as connection:
            model_rows = [dict(row) for row in connection.execute(text("""
                SELECT pm.id, pm.model_code, pm.model_name, pm.aliases, pm.material_family,
                       pm.product_standard_id, pm.product_table_id, ps.standard_no, pm.notes AS model_notes
                FROM product_models pm
                JOIN product_standards ps ON ps.id = pm.product_standard_id
                WHERE NOT (to_jsonb(pm) ? 'verification_status')
                   OR (
                        to_jsonb(pm)->>'verification_status' = 'verified'
                    AND to_jsonb(pm)->>'enabled_for_review' = 'true'
                   )
                ORDER BY pm.id
            """)).mappings()]
            for sample in result.get("samples") or []:
                source_group = source_group_for_sample(sample, registry=source_registry)
                source_entry = next((entry for entry in source_registry if entry["group"] is source_group), None)
                if local_evidence and source_group:
                    binding=local_sheath_observations(sample,source_text,local_evidence,source_pdf_sha256)
                    sample['local_table_evidence_binding']={
                        'status':binding['status'],'reason':binding.get('reason'),
                        'observation_count':len(binding['observations']),'skipped':binding.get('skipped',[])}
                    if binding['observations']:
                        source_group=dict(source_group,_local_sheath_observations=binding['observations'])
                    insulation_binding=local_sheath_observations(sample,source_text,local_evidence,source_pdf_sha256,component='insulation')
                    if insulation_binding['observations']:
                        source_group=dict(source_group,_local_insulation_observations=insulation_binding['observations'])
                    from backend.app.ocr_table_repair import attach_group
                    source_group=attach_group(source_group,local_evidence,source_pdf_sha256,source_registry)
                    _tl_pages=(local_evidence or {}).get('text_layer_pages') or {}
                    if _tl_pages:
                        for _p in source_group.get('pages') or []:
                            _t=_tl_pages.get(str(_p.get('page')))
                            if _t and 'text_layer' not in _p:
                                _p['text_layer']=_t
                model = _single_model(model_rows, sample, standard_family)
                sample_label = " ".join(
                    str(sample.get(key) or "") for key in ("model", "voltage", "spec")
                ).strip()
                if not model:
                    coverage["unmatched_samples"].append(sample_label)
                    continue

                rows = [dict(row) for row in connection.execute(text("""
                    SELECT m.id AS matrix_id, ti.item_code, ti.item_name, m.applicability_status,
                           m.applicable_conditions, m.table_item_no,
                           ps.standard_no, st.table_no
                    FROM model_test_matrix m
                    JOIN test_items ti ON ti.id = m.test_item_id
                    JOIN product_standards ps ON ps.id = m.product_standard_id
                    LEFT JOIN standard_tables st ON st.id = m.product_table_id
                    WHERE m.model_id = :model_id
                      AND m.verification_status = 'verified'
                      AND m.enabled_for_review = 'true'
                      AND m.applicability_status IN ('required', 'conditional')
                    ORDER BY m.id
                """), {"model_id": int(model["id"])}).mappings()]
                insulation_materials = sorted(set(re.findall(r'绝缘(?:材料)?\s*[:：]?\s*(IE\d+)', str(model.get('model_notes') or ''))))
                rows = [{**row, 'resolved_model_code':str(model.get('model_code') or ''),
                         '_resolved_insulation_materials':insulation_materials} for row in rows]
                checks = sample.get("checks") or []
                sample.setdefault("items", [])
                condition_rules = load_active_matrix_conditions([
                    int(row["matrix_id"]) for row in rows
                    if row.get("applicability_status") == "conditional"
                ])
                condition_facts = extract_condition_facts(sample)
                # 结构档案只在数据库同时标记 verified/enabled 时补充条件事实。
                # 禁用档案返回原事实字典，因此对现有审核为零影响。
                from backend.app.structure_profiles import merge_active_profile_facts
                condition_facts, active_structure_profiles = merge_active_profile_facts(
                    connection, sample, condition_facts, str(model.get("model_code") or ""),
                )
                if active_structure_profiles:
                    coverage.setdefault("active_structure_profiles", []).append({
                        "sample": sample_label,
                        "profiles": active_structure_profiles,
                    })
                lowtemp_selections = (
                    (result.get("_deterministic_validation") or {})
                    .get("pvc_single_core_lowtemp_selection") or []
                )
                lowtemp_selection = next((
                    record for record in lowtemp_selections
                    if _normalized(record.get("sample")) == _normalized(sample_label)
                    and record.get("status") == "selection_confirmed"
                ), None)
                for row in rows:
                    item_name = str(row.get("item_name") or "未命名项目")
                    if str(row.get("item_code") or "").upper() == "CCC-UNIT":
                        normalized_item_name = re.sub(r"试验", "", item_name)
                        sample["items"] = [
                            item for item in sample["items"]
                            if not (
                                item.get("action_type") == "manual_review"
                                and bool(re.search(r"审核覆盖缺失$", str(item.get("item") or "")))
                                and re.sub(
                                    r"(?:试验)?(?:适用性)?审核覆盖缺失$", "",
                                    str(item.get("item") or ""),
                                ).replace("试验", "") == normalized_item_name
                            )
                        ]
                        coverage["project_statuses"].append({
                            "sample": sample_label, "item": item_name,
                            "item_code": row.get("item_code"), "status": "out_of_report_review_scope",
                            "condition_source": "sampling_rules_are_independently_reviewed",
                        })
                        continue
                    item_code = str(row.get("item_code") or "").upper()
                    spec_numbers = re.findall(r"\d+(?:\.\d+)?", str(sample.get("spec") or ""))
                    section_mm2 = float(spec_numbers[-1]) if len(spec_numbers) >= 2 else None
                    sample_model_key = re.sub(r"[^A-Z0-9]", "", str(sample.get("model") or "").upper())
                    welding_cable = bool(re.search(r"60245IEC(?:81|82)", sample_model_key))
                    if (
                        standard_family == "rubber"
                        and item_code == "FLEXING"
                        and section_mm2 is not None
                        and section_mm2 > 4
                        and not welding_cable
                    ):
                        coverage["project_statuses"].append({
                            "sample": sample_label,
                            "item": item_name,
                            "item_code": item_code,
                            "status": "not_applicable",
                            "condition_state": FALSE,
                            "condition_source": "rubber_flexing_section_limit",
                        })
                        continue
                    stale_issue_names = {
                        f"{item_name}审核覆盖缺失",
                        f"{item_name}适用性审核覆盖缺失",
                    }
                    normalized_item_name = re.sub(r"试验", "", item_name)
                    sample["items"] = [
                        item for item in sample["items"]
                        if not (
                            item.get("action_type") == "manual_review"
                            and (
                                str(item.get("item") or "") in stale_issue_names
                                or (
                                    bool(re.search(r"审核覆盖缺失$", str(item.get("item") or "")))
                                    and re.sub(
                                        r"(?:试验)?(?:适用性)?审核覆盖缺失$", "",
                                        str(item.get("item") or ""),
                                    ).replace("试验", "") == normalized_item_name
                                )
                            )
                        )
                    ]
                    status = str(row.get("applicability_status") or "")
                    voltage_applicability = None
                    if item_code == "VOLTAGE_FINISHED":
                        voltage_applicability = _jbt87343_rvv_finished_voltage_applicability(
                            sample, model, source_group,
                        )
                    if voltage_applicability and voltage_applicability["state"] == TRUE:
                        # 将原表确定性提取的条件事实交给通用三态
                        # 条件引擎。这样正式矩阵可以从required改为
                        # conditional，不需要靠无条件矩阵再做特判。
                        condition_facts["prescribed_insulation_thickness_mm"] = (
                            voltage_applicability["prescribed_insulation_thickness_mm"]
                        )
                        condition_facts["rated_voltage"] = voltage_applicability["rated_voltage"]
                        condition_facts.setdefault("_sources", {}).update({
                            "prescribed_insulation_thickness_mm": "source.structure.requirement",
                            "rated_voltage": "sample.voltage",
                        })
                    if voltage_applicability and voltage_applicability["state"] == FALSE:
                        coverage["conditional_total"] += 1
                        coverage["conditional_not_applicable"] += 1
                        coverage["project_statuses"].append({
                            "sample": sample_label,
                            "item": item_name,
                            "item_code": item_code,
                            "status": "not_applicable",
                            "condition_state": FALSE,
                            "condition_source": voltage_applicability["reason"],
                            "condition_facts": voltage_applicability,
                        })
                        continue
                    if voltage_applicability and voltage_applicability["state"] == UNKNOWN:
                        coverage["conditional_total"] += 1
                        coverage["conditional_unknown"] += 1
                        issue_name = f"{item_name}适用性审核覆盖缺失"
                        if not any(str(item.get("item") or "") == issue_name for item in sample["items"]):
                            sample["items"].append({
                                "item": issue_name,
                                "reported": "未能唯一确定产品规定绝缘厚度与额定电压分档",
                                "should_be": "按JB/T 8734.1-2016表3的规定绝缘厚度和额定电压联合判断成品电压是否适用",
                                "standard": voltage_applicability["basis"],
                                "severity": "suggestion",
                                "action_required": True,
                                "action_type": "manual_review",
                                "review_action": "核对产品规定绝缘厚度及额定电压，再确认成品电压试验适用性",
                                "coverage_reason": voltage_applicability["reason"],
                                "evidence_status": "not_located",
                                "source_pages": [],
                                "source_excerpt": "",
                            })
                        coverage["missing"].append({
                            "model": model["model_code"],
                            "item_code": item_code,
                            "item": item_name,
                            "status": "conditional",
                            "condition_state": UNKNOWN,
                            "coverage_reason": voltage_applicability["reason"],
                        })
                        continue
                    counter = "required" if status == "required" else "conditional"
                    not_applicable_method = str(
                        (lowtemp_selection or {}).get("not_applicable_method") or ""
                    )
                    if (
                        not_applicable_method
                        and not_applicable_method.replace("试验", "")
                        in item_name.replace("试验", "")
                    ):
                        coverage["project_statuses"].append({
                            "sample": sample_label,
                            "item": item_name,
                            "item_code": row.get("item_code"),
                            "status": "not_applicable",
                            "condition_state": FALSE,
                            "condition_source": "pvc_single_core_diameter_selection",
                        })
                        continue
                    coverage[f"{counter}_total"] += 1
                    condition_state = None
                    condition_rule = None
                    if status == "conditional":
                        condition_rule = condition_rules.get(int(row["matrix_id"]))
                        condition_state = evaluate_condition(
                            condition_rule.get("condition_json") or {}, condition_facts
                        ) if condition_rule else UNKNOWN
                        condition_source = "structured_condition"
                        if condition_state == UNKNOWN and str(row.get("item_code") or "").upper() == "LOWTEMP_BEND_SHEATH":
                            source_state = _sheath_lowtemp_bend_source_state(source_group)
                            if source_state != UNKNOWN:
                                condition_state = source_state
                                condition_source = "source_sheath_lowtemp_method"
                        if condition_state == FALSE:
                            _reconcile_inapplicable_missing_check(sample, row)
                            coverage["conditional_not_applicable"] += 1
                            coverage["project_statuses"].append({
                                "sample": sample_label, "item": row.get("item_name"),
                                "item_code": row.get("item_code"), "status": "not_applicable",
                                "condition_state": condition_state, "condition_source": condition_source,
                            })
                            continue
                        if condition_state == TRUE:
                            coverage["conditional_applicable"] += 1
                        else:
                            coverage["conditional_unknown"] += 1

                    # 矩阵已确认必做（或可执行条件已判适用）时，
                    # “未列出/未进行成品低温冲击”是报告缺项，不是普通人工提示。
                    if str(row.get("item_code") or "").upper() == "LOWTEMP_IMPACT_FINISHED" and (
                        status == "required" or condition_state == TRUE
                    ):
                        recovered_impact = _lowtemp_source_evidence(row, source_group)
                        if _reconcile_coordinate_impact_absence(sample, checks, recovered_impact):
                            coverage[f"{counter}_covered"] += 1
                            recovery = {
                                "sample": sample_label, "item": row.get("item_name"),
                                "item_code": row.get("item_code"), "status": "model_omission_recovered",
                                "source_pages": recovered_impact["source_pages"],
                                "condition_state": condition_state,
                            }
                            coverage["resolved_from_source"].append(recovery)
                            coverage["project_statuses"].append(recovery)
                            continue
                        missing_impact_items = [
                            item for item in sample["items"]
                            if "低温冲击" in str(item.get("item") or "")
                            and re.search(
                                r"未列出|未报告|未提供|未进行|未做",
                                " ".join(str(item.get(field) or "") for field in ("reported", "should_be")),
                            )
                        ]
                        if missing_impact_items:
                            recovered_impact = _lowtemp_source_evidence(row, source_group)
                            if recovered_impact:
                                sample["items"] = [
                                    item for item in sample["items"] if item not in missing_impact_items
                                ]
                                checks.append(recovered_impact)
                                coverage[f"{counter}_covered"] += 1
                                recovery = {
                                    "sample": sample_label, "item": row.get("item_name"),
                                    "item_code": row.get("item_code"), "status": "model_omission_recovered",
                                    "source_pages": recovered_impact["source_pages"],
                                    "condition_state": condition_state,
                                }
                                coverage["resolved_from_source"].append(recovery)
                                coverage["project_statuses"].append(recovery)
                                continue
                            for item in missing_impact_items:
                                item.update({
                                    "severity": "must_fix", "action_required": True,
                                    "action_type": "correction",
                                    "review_action": "补做成品低温冲击试验并在报告中补齐结果及P/F评定",
                                })
                    if str(row.get("item_code") or "").upper() == "LOWTEMP_TENSILE_SHEATH" and (
                        status == "required" or condition_state == TRUE
                    ):
                        missing_tensile_items = [
                            item for item in sample["items"]
                            if "护套低温拉伸" in str(item.get("item") or "")
                            and re.search(
                                r"未明确|未填|未提供|未单独列出|未进行|未列出|审核覆盖缺失",
                                " ".join(str(item.get(field) or "") for field in ("item", "reported", "should_be")),
                            )
                        ]
                        if missing_tensile_items:
                            recovered_tensile = _lowtemp_source_evidence(row, source_group)
                            if recovered_tensile:
                                sample["items"] = [
                                    item for item in sample["items"] if item not in missing_tensile_items
                                ]
                                checks.append(recovered_tensile)
                                coverage[f"{counter}_covered"] += 1
                                recovery = {
                                    "sample": sample_label, "item": row.get("item_name"),
                                    "item_code": row.get("item_code"), "status": "model_omission_recovered",
                                    "source_pages": recovered_tensile["source_pages"],
                                    "condition_state": condition_state,
                                }
                                coverage["resolved_from_source"].append(recovery)
                                coverage["project_statuses"].append(recovery)
                                continue
                            for item in missing_tensile_items:
                                item.update({
                                    "severity": "must_fix", "action_required": True,
                                    "action_type": "correction",
                                    "should_be": "型号矩阵已确认护套低温拉伸为必做项；报告应列出试验条件、伸长率结果及P/F评定",
                                    "review_action": "补做护套低温拉伸试验并在报告中补齐结果及P/F评定",
                                })
                    applicable_missing_patterns = {
                        "LOWTEMP_IMPACT_FINISHED": r"未列出|未报告|未提供|未进行|未做|审核覆盖缺失",
                        "HEAT_SHRINK": r"未填写|未提供|未报告|空白|横线|无评定|审核覆盖缺失",
                    }
                    current_item_code = str(row.get("item_code") or "").upper()
                    if (status == "required" or condition_state == TRUE) and current_item_code == "LOWTEMP_IMPACT_FINISHED":
                        reported_n_checks = [
                            check for check in checks
                            if "低温冲击" in _coverage_text(check)
                            and (
                                str(check.get("verdict") or "").lower() in {"not_applicable", "n"}
                                or bool(re.search(r"(?:^|[^A-Z])(?:报告判)?N(?:$|[^A-Z])",
                                                  str(check.get("reported") or ""), re.I))
                            )
                        ]
                        if reported_n_checks:
                            issue_name = "成品低温冲击不应判N"
                            check = reported_n_checks[0]
                            check.update({
                                "item": issue_name,
                                "verdict": "fail",
                                "required_item_reported_not_applicable": True,
                                "note": "型号矩阵及适用条件已确定该成品低温冲击为必做项，报告不得判N",
                            })
                            sample["items"] = [item for item in sample["items"]
                                               if "低温冲击" not in str(item.get("item") or "")]
                            if not any(str(item.get("item") or "") == issue_name for item in sample["items"]):
                                sample["items"].append({
                                    "item": issue_name,
                                    "reported": str(check.get("reported") or "报告判N/不适用"),
                                    "should_be": "型号矩阵已确认成品低温冲击为必做项，应列出试验条件、落锤、结果及P/F评定",
                                    "standard": _matrix_reference(row),
                                    "severity": "must_fix", "action_required": True,
                                    "action_type": "correction",
                                    "review_action": "补做成品低温冲击试验并补齐参数、结果及P/F评定",
                                    "coverage_reason": "required_item_reported_not_applicable",
                                    "evidence_status": str(check.get("evidence_status") or "not_located"),
                                    "source_pages": list(check.get("source_pages") or []),
                                    "source_excerpt": str(check.get("source_excerpt") or ""),
                                })
                            coverage[f"{counter}_covered"] += 1
                            coverage["project_statuses"].append({
                                "sample": sample_label, "item": item_name,
                                "item_code": current_item_code,
                                "status": "required_item_reported_not_applicable",
                                "condition_state": condition_state,
                            })
                            continue
                    if (
                        status == "required" or condition_state == TRUE
                    ) and current_item_code == "VOLTAGE_FINISHED":
                        reported_n_checks = [
                            check for check in checks
                            if (
                                _concept(_coverage_text(check), current_item_code) == "finished_voltage"
                                or bool(re.search(r"成品.{0,8}电压试验", _coverage_text(check)))
                            )
                            and str(check.get("verdict") or "").lower() in {"not_applicable", "n"}
                        ]
                        if reported_n_checks:
                            issue_name = "成品电压试验不应判N"
                            if not any(str(item.get("item") or "") == issue_name for item in sample["items"]):
                                check = reported_n_checks[0]
                                sample["items"].append({
                                    "item": issue_name,
                                    "reported": str(check.get("reported") or "报告判N/不适用"),
                                    "should_be": "型号矩阵已确认成品电压试验为必做项，应给出试验电压、时间、不击穿结果及P/F评定",
                                    "standard": _matrix_reference(row),
                                    "severity": "must_fix",
                                    "action_required": True,
                                    "action_type": "correction",
                                    "review_action": "补做成品电压试验并补齐参数、结果及P/F评定",
                                    "coverage_reason": "required_item_reported_not_applicable",
                                    "evidence_status": str(check.get("evidence_status") or "not_located"),
                                    "source_pages": list(check.get("source_pages") or []),
                                    "source_excerpt": str(check.get("source_excerpt") or ""),
                                })
                            coverage[f"{counter}_covered"] += 1
                            coverage["project_statuses"].append({
                                "sample": sample_label, "item": item_name,
                                "item_code": current_item_code,
                                "status": "required_item_reported_not_applicable",
                                "condition_state": condition_state,
                            })
                            continue
                    if (status == "required" or condition_state == TRUE) and current_item_code in applicable_missing_patterns:
                        for item in sample["items"]:
                            if _concept(item.get("item", "")) != _concept(item_name, current_item_code):
                                continue
                            if not re.search(
                                applicable_missing_patterns[current_item_code],
                                " ".join(str(item.get(field) or "") for field in ("item", "reported", "should_be")),
                            ):
                                continue
                            item.update({
                                "severity": "must_fix",
                                "action_required": True,
                                "action_type": "correction",
                                "review_action": f"补做{item_name}并在报告中补齐结果及P/F评定",
                            })
                    # Stage the independently-evidenced input contract by fact schema.
                    # Other condition families retain their existing rollout policy.
                    def requires_independent_input(node):
                        return node.get('field') in condition_facts.get('_independent_applicability_fields', []) or any(
                            requires_independent_input(child) for child in (node.get('rules') or [])
                        ) or (bool(node.get('rule')) and requires_independent_input(node['rule']))
                    independent = status == 'conditional' and requires_independent_input((condition_rule or {}).get('condition_json') or {})
                    coverage_row = {**row, '_applicability_required': status == 'required' or condition_state == TRUE}
                    if independent:
                        coverage_row['_independent_applicability'] = True
                    applicability_conflicts = [conflict for candidate_check in checks
                                               for conflict in _required_applicability_conflicts(coverage_row, candidate_check)]
                    # 已覆盖只是模型返回过此项，不等于原页条件已被独立验证。
                    independent_recovered = None
                    if current_item_code == 'OZONE_RESIST':
                        independent_recovered = _ozone_source_evidence(row, source_group)
                    elif current_item_code in {'HEAT_PRESS_SHEATH', 'SHEATH_TENSILE_BEFORE', 'SHEATH_TENSILE_AFTER'}:
                        independent_recovered = _mechanical_source_evidence(row, source_group, source_text)
                    elif current_item_code == 'INSUL_RES':
                        independent_recovered = _local_insulation_resistance_evidence(row, source_group)
                    elif current_item_code == 'LOSS_WEIGHT':
                        independent_recovered = _local_pvc_loss_evidence(row, source_group, source_text)
                    elif current_item_code == 'SHEATH_LOSS_WEIGHT':
                        if _local_pvc_loss_evidence(row, source_group, source_text) is not None:
                            independent_recovered = _sheath_loss_weight_source_evidence(row, source_group)
                    elif current_item_code.startswith('LOWTEMP_'):
                        independent_recovered = _lowtemp_source_evidence(row, source_group)
                    if _matrix_item_covered(coverage_row, checks, sample["items"]) and independent_recovered is None:
                        coverage[f"{counter}_covered"] += 1
                        coverage["project_statuses"].append({
                            "sample": sample_label, "item": row.get("item_name"),
                            "item_code": row.get("item_code"), "status": "model_covered",
                            "condition_state": condition_state,
                        })
                        continue

                    recovered = (
                        independent_recovered
                        or _dimension_source_evidence(row, source_group)
                        or _structure_source_evidence(row, source_group)
                        or _mechanical_source_evidence(row, source_group, source_text)
                        or _sheath_loss_weight_source_evidence(row, source_group)
                        or _insulation_voltage_source_evidence(row, source_group)
                        or _finished_voltage_source_evidence(row, source_group)
                        or _lowtemp_source_evidence(row, source_group)
                    )
                    if recovered:
                        if applicability_conflicts:
                            for candidate_check in checks:
                                _normalize_exclusion_only_check(coverage_row, candidate_check)
                        recovered["source_segments"] = [source_entry["source_index"]] if source_entry else []
                        _reconcile_coordinate_recovery(sample, checks, recovered)
                        checks.append(recovered)
                        coverage[f"{counter}_covered"] += 1
                        recovery = {
                            "sample": sample_label, "item": row.get("item_name"),
                            "item_code": row.get("item_code"), "status": "model_omission_recovered",
                            "source_pages": recovered["source_pages"],
                            "condition_state": condition_state,
                        }
                        coverage["resolved_from_source"].append(recovery)
                        coverage["project_statuses"].append(recovery)
                        continue

                    condition = str(row.get("applicable_conditions") or "").strip()
                    if current_item_code in {"LOWTEMP_TENSILE_SHEATH", "LOWTEMP_IMPACT_FINISHED", "HEAT_SHRINK"} and (
                        status == "required" or condition_state == TRUE
                    ):
                        issue_name = item_name
                        should_be = {
                            "LOWTEMP_TENSILE_SHEATH": "型号矩阵已确认护套低温拉伸为必做项；报告应列出试验条件、伸长率结果及P/F评定",
                            "LOWTEMP_IMPACT_FINISHED": "型号矩阵已确认成品低温冲击为必做项；报告应列出试验条件、落锤、结果及P/F评定",
                            "HEAT_SHRINK": "型号矩阵条件已确认热收缩试验适用；报告应列出试验条件、热收缩率结果及P/F评定",
                        }[current_item_code]
                        action = f"先核对原报告及{item_name}原始试验记录；已完成试验的，补齐报告中的条件、结果及P/F评定；确认未开展的，再补做试验"
                    elif status == "required":
                        issue_name = f"{item_name}审核覆盖缺失"
                        should_be = "该型号矩阵已确认本项目必审，必须回看原PDF核对报告值、标准要求和P/F/N评定"
                        action = f"人工核对{item_name}，补齐报告值、标准要求、适用性和审核结论"
                    elif condition_state == TRUE:
                        issue_name = f"{item_name}审核覆盖缺失"
                        should_be = "可执行条件已判定本项目适用，必须回看原PDF核对报告值、标准要求和P/F/N评定"
                        action = f"人工核对{item_name}，补齐报告值、标准要求和审核结论"
                    else:
                        issue_name = f"{item_name}适用性审核覆盖缺失"
                        should_be = f"必须先判断条件是否满足：{condition or '矩阵尚未形成可自动计算的适用条件'}"
                        action = f"人工核对{item_name}适用条件；适用时继续审核报告值和判定，不适用时确认N正确"

                    evidence = _generic_source_evidence(item_name, source_group)
                    coverage_reason = (
                        "source_present_but_not_deterministically_parsed"
                        if evidence["evidence_status"] == "located"
                        else "report_not_provided_or_ocr_unresolved"
                    )
                    if applicability_conflicts:
                        for candidate_check in checks:
                            _normalize_exclusion_only_check(coverage_row, candidate_check)
                        issue_name = f"{item_name}适用性判断冲突"
                        should_be = "已验证矩阵确认本项目适用，模型却称不要求；须按原页独立核对，不能据此要求报告改判N"
                        action = f"按{_matrix_reference(row)}核对{item_name}的条件与结果；模型不适用说明不能作为整改依据"
                        coverage_reason = 'model_applicability_conflicts_with_verified_matrix'
                    if not any(str(item.get("item") or "") == issue_name for item in sample["items"]):
                        issue = {
                            "item": issue_name,
                            "reported": "模型不适用说明与已验证矩阵冲突" if applicability_conflicts else "AI返回的结构化核对项未覆盖该项目",
                            "should_be": should_be,
                            "standard": _matrix_reference(row),
                            "severity": "suggestion",
                            "action_required": True,
                            "action_type": "manual_review",
                            "review_action": action,
                            "coverage_reason": coverage_reason,
                            **evidence,
                        }
                        if applicability_conflicts:
                            issue['model_applicability_claims'] = applicability_conflicts
                        if current_item_code in {"LOWTEMP_TENSILE_SHEATH", "LOWTEMP_IMPACT_FINISHED", "HEAT_SHRINK"} and (
                            (status == "required" or condition_state == TRUE) and not applicability_conflicts
                        ):
                            issue.update({
                                "reported": "报告及可恢复OCR证据中未列出该必做项目",
                                "severity": "must_fix",
                                "action_type": "correction",
                            })
                        sample["items"].append(issue)
                    missing_record = {
                        "model": model["model_code"],
                        "item_code": row.get("item_code"),
                        "item": item_name,
                        "status": status,
                        "condition": condition,
                        "condition_state": condition_state,
                        "condition_rule_id": condition_rule.get("id") if condition_rule else None,
                        "condition_facts": {
                            key: value for key, value in condition_facts.items() if key != "_sources"
                        } if status == "conditional" else None,
                        "coverage_reason": coverage_reason,
                        "source_pages": evidence["source_pages"],
                    }
                    coverage["missing"].append(missing_record)
                    coverage["project_statuses"].append({
                        "sample": sample_label, "item": item_name,
                        "item_code": row.get("item_code"), "status": coverage_reason,
                        "source_pages": evidence["source_pages"],
                        "condition_state": condition_state,
                    })
    except Exception as exc:
        coverage["error"] = str(exc)[:300]

    result.setdefault("_deterministic_validation", {})["required_item_coverage"] = coverage
    result = bind_review_evidence(result, source_text) if source_text else result
    result = apply_yellow_green_ratio_guard(result, source_text)
    from backend.app.structure_profiles import apply_active_structure_profile_conditions
    return apply_active_structure_profile_conditions(result, source_text, standard_family)


def validate_review_result(
    result: dict[str, Any],
    standard_family: str | None = None,
) -> dict[str, Any]:
    """用型号项目矩阵确定性验收 AI 问题项。

    仅在型号唯一匹配且规则已验证时修改结果；无法唯一定位时保留原结果，
    避免为了减少误报而隐藏真正问题。
    """
    from backend.app.executable_rules import load_active_executable_rule

    executable_rule = load_active_executable_rule("citation.project-table-item")
    if not executable_rule or not inspect(engine).has_table("model_test_matrix"):
        return result
    config = executable_rule.get("config_json") or {}
    required_compare = {"item_code", "standard_no", "table_no", "table_item_no"}
    if config.get("source") != "structured_rulebase" or not required_compare.issubset(set(config.get("compare") or [])):
        result.setdefault("_rule_validation", {})["citation_configuration_error"] = {
            "rule_code": executable_rule["rule_code"], "version_no": executable_rule["version_no"],
        }
        return result
    validation = {
        "rule_code": executable_rule["rule_code"],
        "rule_id": executable_rule["id"],
        "version_no": executable_rule["version_no"],
        "checked": 0, "blocked": [], "corrected": [], "downgraded": [],
    }
    try:
        with engine.connect() as connection:
            model_rows = [dict(row) for row in connection.execute(text("""
                SELECT pm.id, pm.model_code, pm.model_name, pm.aliases, pm.material_family,
                       pm.product_standard_id, pm.product_table_id, ps.standard_no
                FROM product_models pm
                JOIN product_standards ps ON ps.id = pm.product_standard_id
                WHERE NOT (to_jsonb(pm) ? 'verification_status')
                   OR (
                        to_jsonb(pm)->>'verification_status' = 'verified'
                    AND to_jsonb(pm)->>'enabled_for_review' = 'true'
                   )
                ORDER BY pm.id
            """)).mappings()]
            for sample in result.get("samples") or []:
                label = " ".join(str(sample.get(key) or "") for key in ("model", "voltage", "spec"))
                models = _select_models(model_rows, label, standard_family)
                if len(models) > 1:
                    # “60227 IEC 52(RVV)”同时会命中完整型号和通用别名 RVV。
                    # 逐样品验收时优先使用报告中出现的最长型号，避免跨产品表混用。
                    longest = max(len(_normalized(row.get("model_code"))) for row in models)
                    models = [row for row in models if len(_normalized(row.get("model_code"))) == longest]
                if len(models) != 1:
                    continue
                model = models[0]
                rows = [dict(row) for row in connection.execute(text("""
                    SELECT m.id, ti.item_code, ti.item_name, m.applicability_status,
                           m.applicable_conditions, m.not_applicable_reason, m.table_item_no,
                           ps.standard_no, st.table_no
                    FROM model_test_matrix m
                    JOIN test_items ti ON ti.id = m.test_item_id
                    JOIN product_standards ps ON ps.id = m.product_standard_id
                    LEFT JOIN standard_tables st ON st.id = m.product_table_id
                    WHERE m.model_id = :model_id
                      AND m.verification_status = 'verified'
                      AND m.enabled_for_review = 'true'
                    ORDER BY m.id
                """), {"model_id": int(model["id"])}).mappings()]
                by_citation = {
                    (_standard_key(row["standard_no"]), re.sub(r"\D", "", str(row.get("table_no") or "")), str(row.get("table_item_no") or "")): row
                    for row in rows if row.get("table_item_no")
                }
                by_concept: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    key = _concept(row.get("item_name"), row.get("item_code", ""))
                    if key:
                        by_concept.setdefault(key, []).append(row)

                kept: list[dict[str, Any]] = []
                checks = sample.get("checks") or []
                for item in sample.get("items") or []:
                    validation["checked"] += 1
                    issue_concept = _concept(item.get("item", ""))
                    ref = _citation(item.get("standard", ""))
                    target = by_citation.get(ref) if ref else None
                    semantic_rows = by_concept.get(issue_concept, []) if issue_concept else []
                    reason = ""

                    if target:
                        target_concept = _concept(target.get("item_name"), target.get("item_code", ""))
                        if issue_concept and target_concept and issue_concept != target_concept:
                            if len(semantic_rows) == 1:
                                correct = semantic_rows[0]
                                old_ref = str(item.get("standard", ""))
                                item["standard"] = _catalog_citation(correct)
                                validation["corrected"].append({
                                    "model": model["model_code"], "item": item.get("item", ""),
                                    "old_reference": old_ref, "new_reference": item["standard"],
                                })
                                target = correct
                            else:
                                reason = (
                                    f"{_catalog_citation(target)}实际项目为“{target['item_name']}”，"
                                    f"不是“{item.get('item', '')}”"
                                )
                    elif ref and len(semantic_rows) == 1:
                        correct = semantic_rows[0]
                        old_ref = str(item.get("standard", ""))
                        item["standard"] = _catalog_citation(correct)
                        validation["corrected"].append({
                            "model": model["model_code"], "item": item.get("item", ""),
                            "old_reference": old_ref, "new_reference": item["standard"],
                        })
                        target = correct

                    if not reason and target:
                        status = str(target.get("applicability_status") or "")
                        if status == "not_applicable":
                            reason = f"型号矩阵明确该项不适用：{target.get('not_applicable_reason') or _catalog_citation(target)}"
                        elif status in ("conditional", "unresolved") and item.get("severity") == "must_fix":
                            item["severity"] = "suggestion"
                            item["action_required"] = True
                            item["action_type"] = "manual_review"
                            item["review_action"] = (
                                f"先核对适用条件：{target.get('applicable_conditions') or '规则库未完全解析'}"
                            )
                            validation["downgraded"].append({
                                "model": model["model_code"], "item": item.get("item", ""), "status": status,
                            })

                    if reason:
                        validation["blocked"].append({
                            "model": model["model_code"], "item": item.get("item", ""), "reason": reason,
                        })
                        for check in checks:
                            if _same_issue(check, item):
                                check["verdict"] = "not_applicable" if "N" in str(check.get("reported", "")).upper() else "pass"
                                check["required"] = f"结构化规则校验已拦截：{reason}"
                                check["basis"] = "型号项目矩阵（程序确定性校验）"
                                check["note"] = "AI返回的项目名称与其引用的标准项次冲突，未纳入修改项"
                        continue
                    kept.append(item)
                sample["items"] = kept
    except Exception as exc:
        validation["error"] = str(exc)[:300]
    if any(validation[key] for key in ("blocked", "corrected", "downgraded")) or validation.get("error"):
        result["_rule_validation"] = validation
    return result


def _select_models(rows: list[dict[str, Any]], report_text: str, standard_family: str | None) -> list[dict[str, Any]]:
    normalized = _normalized(report_text)
    candidates: list[tuple[int, bool, dict[str, Any]]] = []
    for row in rows:
        family = str(row.get("material_family") or "").lower()
        if standard_family and family and family != standard_family.lower():
            continue
        code = str(row.get("model_code") or "")
        exact = _contains_token(normalized, code)
        aliases = [item.strip() for item in str(row.get("aliases") or "").split(",") if item.strip()]
        alias_hit = any(_contains_token(normalized, alias) for alias in aliases)
        standard_key = _standard_key(str(row.get("standard_no") or ""))
        standard_hit = bool(standard_key and standard_key in normalized)
        if not exact and not alias_hit:
            continue
        score = (100 if exact else 20) + (30 if standard_hit else 0) + (5 if standard_family else 0)
        candidates.append((score, exact, row))

    if not candidates:
        return []
    # 同一样品同时命中“60245 IEC 53(YZ)”和通用“YZ”时，
    # 保留最完整的型号，防止短代码把另一个标准族的矩阵也带入。
    exact_codes = [
        (str(row.get("id") or ""), _normalized(row.get("model_code")))
        for _, exact, row in candidates if exact and _normalized(row.get("model_code"))
    ]
    shadowed_ids = {
        row_id for row_id, code in exact_codes
        if any(code != other and code in other for _, other in exact_codes)
    }
    if shadowed_ids:
        candidates = [
            item for item in candidates
            if not (item[1] and str(item[2].get("id") or "") in shadowed_ids)
        ]
    has_exact = any(exact for _, exact, _ in candidates)
    if has_exact:
        candidates = [item for item in candidates if item[1] or item[0] >= 50]
    best = max(score for score, _, _ in candidates)
    selected = [row for score, _, row in sorted(candidates, key=lambda item: (-item[0], int(item[2]["id"]))) if score >= best - 15]
    return selected[:MAX_MODELS]


def _parameter_matches_family(row: dict[str, Any], standard_family: str | None) -> bool:
    if not standard_family:
        return True
    declared = str(row.get("material_family") or "").lower()
    if declared and declared != standard_family.lower():
        return False
    combined = _normalized(" ".join(str(row.get(key) or "") for key in (
        "parameter_name", "applicable_conditions", "evidence_text"
    )))
    if standard_family == "pvc":
        return not any(marker in combined for marker in ("IE2", "IE3", "IE4", "SE3", "SE4", "5013", "8735", "橡皮", "橡胶"))
    if standard_family == "rubber":
        return not any(marker in combined for marker in ("PVC", "5023", "8734", "聚氯乙烯"))
    return True


def _primary_sample_spec(report_text: str) -> tuple[int, float] | None:
    """从当前样品页提取第一个“芯数×标称截面”。

    只在“试样型号和规格”附近取值，避免把报告编号、外径或
    后续试验表中的数字当成规格。
    """
    source = str(report_text or "")
    marker = re.search(r"试样型号和\s*规格|试样型号和|型号及规格", source)
    if marker:
        source = source[marker.end():marker.end() + 1200]
    else:
        source = source[:5000]
    plus_parts = re.findall(r"(\d{1,3})\s*[×xX*]\s*(\d+(?:\.\d+)?)", source)
    if len(plus_parts) >= 2 and "+" in source:
        return sum(int(count) for count, _ in plus_parts), float(plus_parts[0][1])
    match = re.search(r"(?<!\d)(\d{1,3})\s*[×xX*]\s*(\d+(?:\.\d+)?)", source)
    if not match:
        return None
    return int(match.group(1)), float(match.group(2))


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _primary_rated_voltage(report_text: str) -> str:
    source = str(report_text or "")
    marker = re.search(r"额定电压|试样型号和\s*规格|型号及规格", source)
    if marker:
        source = source[marker.start():marker.start() + 1600]
    else:
        source = source[:5000]
    match = re.search(r"(?<!\d)(300\s*/\s*300|300\s*/\s*500|450\s*/\s*750)\s*V", source, re.I)
    return re.sub(r"\s+", "", match.group(1)) + "V" if match else ""


def _explicit_material_markers(report_text: str) -> set[str]:
    normalized = _normalized(str(report_text or "")[:80000]).upper()
    markers = set(re.findall(r"PVC/[CDE]|IE[1-4]|SE[3-4]", normalized))
    return markers


def _parameter_matches_sample(row: dict[str, Any], report_text: str) -> bool:
    """对已结构化的截面/芯数适用条件做保守过滤。"""
    condition = str(row.get("applicable_conditions") or "")
    compact_condition = _normalized(condition)
    condition_voltages = {
        re.sub(r"\s+", "", value).upper()
        for value in re.findall(r"(?:300\s*/\s*300|300\s*/\s*500|450\s*/\s*750)\s*V", condition, re.I)
    }
    rated_voltage = _primary_rated_voltage(report_text).upper()
    if condition_voltages and rated_voltage and rated_voltage not in condition_voltages:
        return False
    condition_markers = set(re.findall(r"PVC/[CDE]|IE[1-4]|SE[3-4]", compact_condition.upper()))
    report_markers = _explicit_material_markers(report_text)
    marker_families = (("PVC/C", "PVC/D", "PVC/E"), ("IE1", "IE2", "IE3", "IE4"), ("SE3", "SE4"))
    for family in marker_families:
        condition_family = condition_markers.intersection(family)
        report_family = report_markers.intersection(family)
        if condition_family and report_family and not condition_family.intersection(report_family):
            return False
    # Missing dimensions must not bypass independently known material/voltage conflicts.
    spec = _primary_sample_spec(report_text)
    if not spec:
        return True
    cores, section = spec
    core_min = _safe_float(row.get("core_count_min"))
    core_max = _safe_float(row.get("core_count_max"))
    section_min = _safe_float(row.get("section_min"))
    section_max = _safe_float(row.get("section_max"))
    if core_min is not None and cores < core_min:
        return False
    if core_max is not None and cores > core_max:
        return False
    if section_min is not None and section < section_min:
        return False
    if section_max is not None and section > section_max:
        return False
    if cores != 2 and re.search(r"两芯电缆|2芯电缆", compact_condition):
        return False
    if cores < 3 and re.search(r"三芯及以上|3芯及以上", compact_condition):
        return False

    # 已结构化的范围优先，避免把“0.3mm²～0.4mm²”的首值误当精确值。
    if section_min is None and section_max is None:
        exact_section = re.search(
            r"导体标称截面\s*(\d+(?:\.\d+)?)\s*mm(?:2|²)(?!\s*[~～至])", condition, re.I
        )
        if exact_section and abs(section - float(exact_section.group(1))) > 1e-9:
            return False
        section_range = re.search(
            r"截面\s*(\d+(?:\.\d+)?)\s*(?:mm(?:2|²))?\s*[~～至]\s*"
            r"(\d+(?:\.\d+)?)\s*mm(?:2|²)", condition, re.I,
        )
        if section_range:
            lower, upper = map(float, section_range.groups())
            if not lower <= section <= upper:
                return False
        max_section = re.search(r"截面\s*(?:≤|<=|不大于)\s*(\d+(?:\.\d+)?)\s*mm(?:2|²)", condition, re.I)
        if max_section and section > float(max_section.group(1)):
            return False
    if core_min is None and core_max is None:
        max_cores = re.search(r"(?:≤|<=|不大于)\s*(\d+)\s*芯", condition)
        if max_cores and cores > int(max_cores.group(1)):
            return False
    return True


def get_structured_rules_context(
    report_text: str,
    metadata: dict[str, Any] | None = None,
    standard_family: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """返回与报告型号匹配的项目适用性和方法参数摘要。"""
    metadata = metadata or {}
    source_text = "\n".join((
        str(metadata.get("product_unit") or ""),
        str(metadata.get("product_desc") or ""),
        report_text[:80000],
    ))
    try:
        if not inspect(engine).has_table("model_test_matrix"):
            return "", {"available": False, "models": [], "matrix_count": 0, "parameter_count": 0}
        with engine.connect() as connection:
            model_rows = [dict(row) for row in connection.execute(text("""
                SELECT pm.id, pm.model_code, pm.model_name, pm.aliases, pm.material_family,
                       pm.product_standard_id, pm.product_table_id, ps.standard_no, ps.series_id
                FROM product_models pm
                JOIN product_standards ps ON ps.id = pm.product_standard_id
                WHERE NOT (to_jsonb(pm) ? 'verification_status')
                   OR (
                        to_jsonb(pm)->>'verification_status' = 'verified'
                    AND to_jsonb(pm)->>'enabled_for_review' = 'true'
                   )
                ORDER BY pm.id
            """)).mappings()]
            models = _select_models(model_rows, source_text, standard_family)
            if not models:
                return "", {"available": True, "models": [], "matrix_count": 0, "parameter_count": 0}

            model_ids = [int(row["id"]) for row in models]
            selected_series_ids = {
                int(row["series_id"]) for row in models if row.get("series_id") is not None
            }
            matrix_stmt = text("""
                SELECT m.id, m.model_id, m.test_item_id, pm.model_code, ps.standard_no, st.table_no,
                       ti.item_name, m.applicability_status, m.applicable_conditions,
                       m.not_applicable_reason, m.table_item_no, m.test_method_id,
                       tm.method_name, tm.clause_no, m.evidence_text
                FROM model_test_matrix m
                JOIN product_models pm ON pm.id = m.model_id
                JOIN product_standards ps ON ps.id = m.product_standard_id
                LEFT JOIN standard_tables st ON st.id = m.product_table_id
                JOIN test_items ti ON ti.id = m.test_item_id
                LEFT JOIN test_methods tm ON tm.id = m.test_method_id
                WHERE m.model_id IN :model_ids
                  AND m.verification_status = 'verified'
                  AND m.enabled_for_review = 'true'
                ORDER BY m.model_id, m.id
            """).bindparams(bindparam("model_ids", expanding=True))
            matrices = [dict(row) for row in connection.execute(matrix_stmt, {"model_ids": model_ids}).mappings()]

            method_ids = {int(row["test_method_id"]) for row in matrices if row.get("test_method_id")}
            # 不适用项目只用于解释N，不应再反向引入该方法的全部参数。
            item_ids = sorted({
                int(row["test_item_id"])
                for row in matrices
                if row.get("applicability_status") != "not_applicable"
            })
            if item_ids:
                method_stmt = text("""
                    SELECT id
                    FROM test_methods
                    WHERE test_item_id IN :item_ids
                      AND verification_status = 'verified'
                      AND enabled_for_review = 'true'
                """).bindparams(bindparam("item_ids", expanding=True))
                method_ids.update(
                    int(row["id"])
                    for row in connection.execute(method_stmt, {"item_ids": item_ids}).mappings()
                )
            # 产品表通常把空气烘箱老化拆成“老化前/后拉力”，而方法字典使用
            # “空气烘箱老化试验”总项；补上这层明确映射，避免遗漏IE4变化率。
            item_names = {str(row.get("item_name") or "") for row in matrices}
            if any("老化前拉力" in name or "老化后拉力" in name for name in item_names):
                method_ids.add(15)
            if any("空气弹" in name for name in item_names):
                method_ids.add(16)
            method_ids = sorted(method_ids)
            parameters: list[dict[str, Any]] = []
            if method_ids:
                param_stmt = text("""
                    SELECT id, test_method_id, parameter_name, operator, parameter_value, unit,
                           applicable_conditions, evidence_text, material_family,
                           standard_series_id, product_standard_id, model_id,
                           core_count_min, core_count_max, section_min, section_max, shape, priority
                    FROM parameter_rules
                    WHERE test_method_id IN :method_ids
                      AND verification_status = 'verified'
                      AND enabled_for_review = 'true'
                    ORDER BY priority DESC, id
                """).bindparams(bindparam("method_ids", expanding=True))
                raw_params = [dict(row) for row in connection.execute(param_stmt, {"method_ids": method_ids}).mappings()]
                seen_parameters: set[tuple[Any, ...]] = set()
                for row in raw_params:
                    if row.get("model_id") and int(row["model_id"]) not in model_ids:
                        continue
                    if (
                        row.get("standard_series_id")
                        and selected_series_ids
                        and int(row["standard_series_id"]) not in selected_series_ids
                    ):
                        continue
                    if not _parameter_matches_sample(row, report_text):
                        continue
                    if _parameter_matches_family(row, standard_family):
                        identity = (
                            row.get("test_method_id"), _normalized(str(row.get("parameter_name") or "")),
                            row.get("operator"), str(row.get("parameter_value") or ""),
                            _normalized(str(row.get("unit") or "")),
                        )
                        if identity not in seen_parameters:
                            parameters.append(row)
                            seen_parameters.add(identity)

        lines = [
            "【结构化规则库定向结果】",
            "使用顺序：先按产品标准和型号表判断项目是否适用，再使用试验方法参数判断如何试验；通用参数不得凭空新增产品表没有的项目。",
            "unresolved项目只能提示人工复核，不得直接判报告错误。",
        ]
        for model in models:
            lines.append(f"\n型号：{model['model_code']}；产品标准：{model['standard_no']}")
            for row in (item for item in matrices if int(item["model_id"]) == int(model["id"])):
                status = row["applicability_status"]
                condition = row.get("applicable_conditions") or row.get("not_applicable_reason") or ""
                method = f"；方法：{row['method_name']} {row.get('clause_no') or ''}" if row.get("method_name") else ""
                lines.append(
                    f"- {row['item_name']}：{status}；产品表{row.get('table_no') or ''}"
                    f" 项次{row.get('table_item_no') or ''}{method}；{condition}"
                )
        if parameters:
            lines.append("\n与上述项目方法匹配的已验证参数：")
            for row in parameters[:60]:
                condition = row.get("applicable_conditions") or ""
                lines.append(
                    f"- 方法{row['test_method_id']} {row['parameter_name']} "
                    f"{row['operator']}{row['parameter_value']}{row.get('unit') or ''}；{condition}"
                )
        context = "\n".join(lines)
        if len(context) > MAX_CONTEXT_CHARS:
            context = context[:MAX_CONTEXT_CHARS] + "\n（结构化规则摘要已按长度上限截断）"
        return context, {
            "available": True,
            "models": [row["model_code"] for row in models],
            "matrix_count": len(matrices),
            "parameter_count": len(parameters),
            "context_chars": len(context),
        }
    except Exception as exc:
        return "", {
            "available": False,
            "models": [],
            "matrix_count": 0,
            "parameter_count": 0,
            "error": str(exc)[:300],
        }

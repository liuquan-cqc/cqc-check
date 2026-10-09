"""8089通用可执行规则加载与确定性执行。仅运行已验证且已启用的最高版本。"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from hashlib import sha256
from typing import Any

from sqlalchemy import inspect, text

from backend.app.database import engine


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def load_active_executable_rules(rule_type: str | None = None) -> list[dict[str, Any]]:
    if not inspect(engine).has_table("executable_review_rules"):
        return []
    where = "AND rule_type = :rule_type" if rule_type else ""
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT id, rule_code, rule_type, title, scope_json, config_json,
                   evidence_ref, version_no, source_review_rule_id
            FROM executable_review_rules
            WHERE verification_status = 'verified' AND enabled_for_review = true
            {where}
            ORDER BY rule_code, version_no DESC, id DESC
        """), {"rule_type": rule_type} if rule_type else {}).mappings()
        active: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            row["scope_json"] = _json_object(row.get("scope_json"))
            row["config_json"] = _json_object(row.get("config_json"))
            active.setdefault(str(row["rule_code"]), row)
        return list(active.values())


def load_active_executable_rule(rule_code: str) -> dict[str, Any] | None:
    return next((row for row in load_active_executable_rules() if row["rule_code"] == rule_code), None)


def active_rules_fingerprint() -> str:
    """返回当前启用规则的稳定指纹，用于审核批次缓存失效。"""
    payload: dict[str, Any] = {"executable": load_active_executable_rules(), "conditional": []}
    if inspect(engine).has_table("matrix_condition_rules"):
        with engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT matrix_id, condition_json, evidence_ref, version_no
                FROM matrix_condition_rules
                WHERE verification_status = 'verified' AND enabled_for_review = true
                ORDER BY matrix_id, version_no DESC, id DESC
            """)).mappings()
            latest: dict[int, dict[str, Any]] = {}
            for raw in rows:
                row = dict(raw)
                row["condition_json"] = _json_object(row.get("condition_json"))
                latest.setdefault(int(row["matrix_id"]), row)
            payload["conditional"] = list(latest.values())
    from pathlib import Path
    package = Path(__file__).parent
    payload["deterministic_runtime"] = {
        name: sha256((package / name).read_bytes()).hexdigest()
        # Preflight caches contain normalized output, not just raw model JSON.
        # Code-only changes to normalization, parsing or request construction
        # must invalidate them even when the database rules have not changed.
        for name in ("structural_facts.py", "condition_rules.py", "structure_profiles.py", "rulebase.py",
                     "executable_rules.py", "privacy_preflight.py", "review.py", "llm.py")
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return sha256(encoded).hexdigest()


def _combined(row: dict[str, Any], fields: list[str]) -> str:
    return " ".join(str(row.get(field) or "") for field in fields)


def _source_range_observations(source: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Strict same-page vertical rows; limits come from rule data, not OCR."""
    import unicodedata
    observations = []
    component = str(config['component'])
    for page in re.finditer(r'^--- Page (\d+) \(text layer\) ---\s*\n(.*?)(?=^--- Page |\Z)', source, re.M | re.S):
        lines = [re.sub(r'\s+', '', unicodedata.normalize('NFKC', line)) for line in page[2].splitlines() if line.strip()]
        starts = [i for i,line in enumerate(lines) if line in config['section_starts']]
        if not starts:
            continue
        compact = ''.join(lines)
        components = [c for c in ('护套','绝缘') if c+'机械性能' in compact]
        if components != [component]:
            observations.append({'page':int(page[1]),'status':'ambiguous_component','values':{}})
            continue
        ends = [i for i,line in enumerate(lines) if line in config['section_ends']]
        if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
            observations.append({'page':int(page[1]),'status':'ambiguous_section','values':{}})
            continue
        section = lines[starts[0]+1:ends[0]]
        values = {}
        for metric in config['metrics']:
            positions = [i for i,line in enumerate(section) if line == metric['source_label']]
            if len(positions) != 1:
                continue
            row = section[positions[0]+1:positions[0]+6]
            # PDFs may place the marker and limit in one cell/line. Split only
            # an exact numeric limit, never a result or an ambiguous extra value.
            if len(row) >= 4:
                merged = re.fullmatch(re.escape(metric['source_limit_marker'])+r'((?:±|[+-])?\d+(?:\.\d+)?)',row[1])
                if merged:
                    row = [row[0],metric['source_limit_marker'],merged[1],row[2],row[3]]
            if len(row) != 5 or row[0] != metric['unit'] or row[1] != metric['source_limit_marker']:
                continue
            if not re.fullmatch(r'(?:±|[+-])?\d+(?:\.\d+)?',row[2]):
                continue
            if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',row[3]) or row[4] not in ('P','F','N'):
                continue
            values[metric['key']] = {'value':row[3],'reported_limit':row[2],'evaluation':row[4]}
        observations.append({'page':int(page[1]),'status':'complete' if len(values)==len(config['metrics']) else 'incomplete_rows','values':values})
    return observations


def _source_component_materials(description: str, component: str) -> set[str]:
    """Read only balanced component parentheses, excluding supplier text."""
    materials = set()
    normalized = description.replace('（','(').replace('）',')')
    for match in re.finditer(re.escape(component)+r'(?:材料)?\s*\(', normalized):
        start = match.end()
        depth = 1
        for end in range(start, min(len(normalized), start+200)):
            depth += (normalized[end]=='(') - (normalized[end]==')')
            if depth == 0:
                value = re.sub(r'\s+','',normalized[start:end]).upper()
                materials.update(re.findall(r'(?<![A-Z0-9])(?:SE[34]|IE[1-4]|PVC/[CDE])(?![A-Z0-9])',value))
                break
    return materials


def _source_range_exceeds(value: str, metric: dict[str, Any]) -> bool:
    from decimal import Decimal
    number, lower, upper = (Decimal(str(v)) for v in (value,metric['lower'],metric['upper']))
    if not all(v.is_finite() for v in (number,lower,upper)) or lower > upper:
        raise ValueError('Invalid numeric range')
    if not isinstance(metric.get('lower_inclusive'),bool) or not isinstance(metric.get('upper_inclusive'),bool):
        raise ValueError('Range inclusivity must be explicit')
    return (number < lower or number > upper or
            (number == lower and not metric['lower_inclusive']) or
            (number == upper and not metric['upper_inclusive']))


def apply_source_numeric_ranges(result: dict[str, Any], source_text: str, standard_family: str | None) -> dict[str, Any]:
    """Fail/hold unsupported passes using enabled, verified data-driven ranges.

    Never promotes a check to pass and never infers material from a model name.
    This guard compares recorded percentages, not their underlying arithmetic.
    """
    from backend.app.rulebase import source_sample_registry, source_group_for_sample, _plain_table_text
    rules = load_active_executable_rules('source_numeric_range')
    if not rules:
        return result
    registry = source_sample_registry(source_text)
    for rule in rules:
        scope, config = rule.get('scope_json') or {}, rule.get('config_json') or {}
        if scope.get('standard_family') != standard_family:
            continue
        # Invalid rule configuration is not silently interpreted as a pass.
        if config.get('parser') != 'native_vertical_single_value_v1' or config.get('component') not in ('护套','绝缘'):
            raise ValueError('Unsupported source numeric range configuration')
        metrics = config['metrics']
        if not metrics or len({m['key'] for m in metrics}) != len(metrics):
            raise ValueError('Missing or duplicate numeric range metrics')
        for metric in metrics:
            _source_range_exceeds(str(metric['lower']),metric)
        component = config['component']
        for sample in result.get('samples') or []:
            related = [c for c in sample.get('checks') or [] if c.get('item') in config['check_aliases']
                       and component in str(c.get('category') or '')]
            source = source_group_for_sample(sample,registry=registry)
            if source is None:
                # Identity reconciliation owns unbound samples. This material-
                # scoped rule cannot decide applicability without a source group.
                continue
            observations = _source_range_observations(str((source or {}).get('text') or ''),config) if source else []
            materials = set()
            for page in (source or {}).get('pages') or []:
                plain = _plain_table_text(str(page.get('text') or ''))
                for description in re.finditer(r'样品描述\s*[:：]([\s\S]*?)(?:备\s*注\s*[:：]|$)',plain):
                    materials.update(_source_component_materials(description[1],component))
            required_material = scope['material']
            if len(materials)==1 and required_material not in materials:
                continue
            complete = len(observations)==1 and observations[0]['status']=='complete'
            bad = [m for m in metrics if complete and _source_range_exceeds(observations[0]['values'][m['key']]['value'],m)]
            certain = materials=={required_material} and complete
            if certain and not bad:
                continue
            # Missing evidence is only a guard against an existing pass, not a
            # new blanket missing-test requirement or an applicability decision.
            if not (certain and bad) and not any(c.get('verdict')=='pass' for c in related):
                continue
            pages = sorted({o['page'] for o in observations})
            explanation = ('；'.join(m['source_label']+'为'+observations[0]['values'][m['key']]['value']+m['unit']+
                '，超出规则范围['+str(m['lower'])+','+str(m['upper'])+']'+m['unit'] for m in bad)
                if certain else '材料、原页试验段或单列数值不能唯一确认，不能沿用模型通过结论')
            verdict = 'fail' if certain else 'manual_review'
            proof = {'rule_code':rule['rule_code'],'rule_version':rule['version_no'],
                'scope':'recorded_numeric_range_not_underlying_arithmetic',
                'material_evidence':sorted(materials),'observations':observations}
            check = related[0] if len(related)==1 else None
            title = check['item'] if check else component+config['title']+'原页数值范围'
            if check is None:
                check={'item':title,'category':component+'机械性能','verdict':verdict,
                       'reported':explanation,'required':'按已验证规则上下限核对记录值','basis':rule['evidence_ref']}
                sample.setdefault('checks',[]).append(check)
            if 'source_numeric_range' not in check:
                sample.setdefault('superseded_model_checks',[]).append({'reason':'source_numeric_range','original_check':deepcopy(check)})
            if check.get('verdict') != 'fail':check['verdict']=verdict
            pages = pages or list(check.get('source_pages') or [])
            check.update(source_numeric_range=proof,source_pages=pages)
            if certain:
                check.update(reported=explanation,required='；'.join(m['source_label']+': ['+str(m['lower'])+','+str(m['upper'])+']'+m['unit'] for m in metrics),
                             basis=rule['evidence_ref'],evidence_status='located')
            if explanation not in str(check.get('note') or ''):
                check['note']='；'.join(filter(None,[str(check.get('note') or ''),explanation]))
            action={'item':title,'category':check['category'],'reported':explanation,'should_be':'核对试验原始记录及适用限值；不得将超限或证据不明记录判为通过',
                'standard':rule['evidence_ref'],'severity':'must_fix' if certain else 'suggestion',
                'action_required':True,'action_type':'correction' if certain else 'manual_review',
                'source_pages':pages,'source_numeric_range':proof}
            previous=[i for i in sample.get('items') or [] if (i.get('source_numeric_range') or {}).get('rule_code')==rule['rule_code']]
            if previous:
                previous[0].update(action)
            else:sample.setdefault('items',[]).append(action)
    return result


def _contains_any(value: str, markers: list[str]) -> bool:
    return any(marker and marker in value for marker in markers)


def _sample_identity_key(sample: dict[str, Any]) -> tuple[str, ...]:
    """归一化模型常见格式波动，避免同一物理样品跨分段重复。"""
    model = re.sub(r"[\s()（）]", "", str(sample.get("model") or "").upper())
    voltage = re.sub(r"[^0-9/]", "", str(sample.get("voltage") or "").upper())
    spec = re.sub(r"\s+", "", str(sample.get("spec") or "").lower())
    spec = spec.replace("x", "×").replace("*", "×").replace("mm2", "mm²")
    if spec and not spec.endswith("mm²") and re.fullmatch(r"\d+×\d+(?:\.\d+)?", spec):
        spec += "mm²"
    identity = (model, voltage, spec)
    specimen_id = str(sample.get("_source_specimen_id") or "")
    return identity + (specimen_id,) if specimen_id else identity


def _item_matches(item: dict[str, Any], match: dict[str, Any]) -> bool:
    subject = _combined(item, ["category", "item"])
    claim = _combined(item, ["item", "reported", "should_be", "standard", "note", "review_action"])
    subjects = [str(value) for value in match.get("subject_contains_any") or []]
    claims = [str(value) for value in match.get("claim_contains_any") or []]
    return (not subjects or _contains_any(subject, subjects)) and (not claims or _contains_any(claim, claims))


def _required_input_available(sample: dict[str, Any], spec: dict[str, Any]) -> bool:
    for field in spec.get("sample_fields") or []:
        value = sample.get(str(field))
        if value not in (None, ""):
            return True
    patterns = [re.compile(str(pattern), re.I) for pattern in spec.get("check_patterns") or []]
    for check in sample.get("checks") or []:
        evidence = _combined(check, ["item", "reported"])
        if any(pattern.search(evidence) for pattern in patterns):
            return True
    return False


def _input_guard_component_matches(entry: dict[str, Any], rule: dict[str, Any]) -> bool:
    """An insulation-input exception cannot suppress a sheath/combined claim."""
    code = str((rule.get('scope_json') or {}).get('test_item') or '')
    component = {'HEAT_PRESS':'绝缘', 'HEAT_PRESS_SHEATH':'护套'}.get(code)
    if component is None:
        return True
    label = _combined(entry, ['category','item'])
    opposite = '护套' if component == '绝缘' else '绝缘'
    evidence = _combined(entry, ['category','item','reported','should_be','required'])
    if opposite in evidence:
        return False
    # Explicit component evidence is required; don't infer from absent words.
    return component in label or component in _combined(entry, ['reported','should_be','required'])


def apply_required_input_guards(result: dict[str, Any]) -> dict[str, Any]:
    """缺少必要输入时只压制依赖该输入的推导，不隐藏其他真实问题。"""
    rules = load_active_executable_rules("required_input_guard")
    if not rules:
        return result
    validation: dict[str, Any] = {"rules": [], "suppressed": [], "checks_annotated": 0}
    for rule in rules:
        config = rule.get("config_json") or {}
        if config.get("missing_action") != "suppress_unsupported_claim":
            continue
        match = config.get("match") or {}
        required_inputs = config.get("required_inputs") or []
        if not match or not required_inputs:
            continue
        validation["rules"].append({
            "rule_code": rule["rule_code"], "rule_id": rule["id"], "version_no": rule["version_no"],
        })
        for sample_index, sample in enumerate(result.get("samples") or []):
            missing = [
                str(spec.get("field") or "") for spec in required_inputs
                if not _required_input_available(sample, spec)
            ]
            if not missing:
                continue
            kept = []
            for item in sample.get("items") or []:
                if _item_matches(item, match) and _input_guard_component_matches(item, rule):
                    # An item may carry other independent defects. Only remove
                    # a single-subject input-dependent claim, never a list.
                    label = str(item.get('item') or '')
                    if re.search(r'[、，,；;]|老化|失重|低温|热冲击|热延伸|电压|电阻|压痕|温度|时间', label):
                        kept.append(item)
                        continue
                    validation["suppressed"].append({
                        "sample_index": sample_index,
                        "item": str(item.get("item") or ""),
                        "missing_inputs": missing,
                        "rule_code": rule["rule_code"],
                    })
                    continue
                kept.append(item)
            sample["items"] = kept
            for check in sample.get("checks") or []:
                if not _input_guard_component_matches(check, rule):
                    continue
                if not _contains_any(_combined(check, ["category", "item"]), match.get("subject_contains_any") or []):
                    continue
                derivation = _combined(check, ["required", "basis", "note"])
                if not _contains_any(derivation, match.get("claim_contains_any") or []):
                    continue
                # Keep the complete multi-project requirement and prior verdict.
                # Record a bounded exception instead of replacing the whole row.
                annotation = {
                    'rule_code':rule['rule_code'], 'missing_inputs':missing,
                    'scope':dict(rule.get('scope_json') or {}),
                    'fallback':str(config.get('fallback_check') or ''),
                }
                annotations = check.setdefault('input_guard_exceptions', [])
                if annotation not in annotations:
                    annotations.append(annotation)
                note = str(config.get("guard_note") or "必要输入缺失，未执行相关推导")
                prior_note = str(check.get('note') or '')
                if note not in prior_note:
                    check['note'] = '；'.join(value for value in (prior_note,note) if value)
                validation["checks_annotated"] += 1
    if validation["rules"] and (validation["suppressed"] or validation["checks_annotated"]):
        result.setdefault("_deterministic_validation", {})["required_input_guards"] = validation
    return result


_MISSING_PROJECT = re.compile(r"未提供|未取得|未获取|未提取|未找到|没有提供|缺失|不完整")


def _project_key(value: Any) -> str:
    text_value = str(value or "").lower()
    text_value = re.sub(r"(?:数据|项目|试验|检验)?(?:覆盖)?缺失$", "", text_value)
    text_value = re.sub(r"(?:项目|试验|检验)$", "", text_value)
    return re.sub(r"[\s、，,;；:：()（）/\\_\-]+", "", text_value)


def _same_project(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or (min(len(left), len(right)) >= 4 and (left in right or right in left))


def _is_missing_record(row: dict[str, Any]) -> bool:
    return bool(_MISSING_PROJECT.search(_combined(row, [
        "item", "reported", "required", "should_be", "note", "review_action",
    ])))


def _has_evidence(row: dict[str, Any]) -> bool:
    verdict = str(row.get("verdict") or "")
    reported = str(row.get("reported") or "").strip()
    return verdict in {"pass", "fail", "not_applicable"} and bool(reported) and not _is_missing_record(row)


def _check_component_scope(row: dict[str, Any]) -> tuple[str, ...]:
    """只以检查项标题/类别界定部件；要求中提及另一部件不是身份。"""
    label = _combined(row, ['category', 'item'])
    # 仅对已接入独立原文复核的项目启用部件边界，其他项目保持原契约。
    if not re.search(r'高温压力|低温|老化|抗张|断裂伸长|拉力', label):
        return ()
    return tuple(component for component in ('绝缘', '护套') if component in label)


def _merge_segment_checks(rows: list[tuple[int, dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for segment, source in rows:
        row = deepcopy(source)
        key = _project_key(row.get("item"))
        component_scope = _check_component_scope(row)
        group = next((item for item in groups if _same_project(item["key"], key)
                      and item['component_scope'] == component_scope), None)
        if group is None:
            groups.append({"key": key, "component_scope":component_scope, "rows": [(segment, row)]})
        else:
            group["rows"].append((segment, row))

    merged: list[dict[str, Any]] = []
    for group in groups:
        candidates = group["rows"]
        evidence = [(segment, row) for segment, row in candidates if _has_evidence(row)]
        if evidence:
            candidates = evidence
        verdicts = {str(row.get("verdict") or "") for _, row in candidates if row.get("verdict")}
        segments = sorted({segment for segment, _ in candidates})
        if "pass" in verdicts and "fail" in verdicts:
            base = deepcopy(candidates[0][1])
            base["verdict"] = "manual_review"
            base["reported"] = "；".join(
                f"分段{segment}：{row.get('reported', '')} [{row.get('verdict', '')}]"
                for segment, row in candidates
            )
            base["note"] = "同一样品同一项目在不同分段出现相反结论，禁止自动选边"
            base["source_segments"] = segments
            merged.append(base)
            conflicts.append({"project": base.get("item", ""), "source_segments": segments})
            continue
        base = deepcopy(candidates[0][1])
        base["source_segments"] = segments
        reports: list[str] = []
        for _, row in candidates:
            value = str(row.get("reported") or "")
            if value and value not in reports:
                reports.append(value)
        if len(reports) > 1:
            base["reported"] = "；".join(reports)
        merged.append(base)
    return merged, conflicts


def apply_merge_policies(results: list[dict[str, Any]], legacy: dict[str, Any]) -> dict[str, Any]:
    """同样品同项目确定性合并；相反结论转人工，不自动判定对错。"""
    rule = load_active_executable_rule("merge.segment-conflict")
    if not rule or rule.get("rule_type") != "merge_policy":
        return legacy

    output = deepcopy(legacy)
    sample_rows: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = {}
    for segment, part in enumerate(results, start=1):
        for sample in part.get("samples") or []:
            key = _sample_identity_key(sample)
            sample_rows.setdefault(key, []).append((segment, sample))

    output_samples: list[dict[str, Any]] = []
    audit = {"rule_code": rule["rule_code"], "rule_id": rule["id"], "version_no": rule["version_no"],
             "samples": 0, "conflicts": [], "missing_suppressed": 0}
    for key, sources in sample_rows.items():
        base = deepcopy(sources[0][1])
        checks, conflicts = _merge_segment_checks([
            (segment, check) for segment, sample in sources for check in (sample.get("checks") or [])
        ])
        substantive_keys = [_project_key(check.get("item")) for check in checks if _has_evidence(check)]
        items: list[dict[str, Any]] = []
        item_groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for segment, sample in sources:
            for source in sample.get("items") or []:
                project = _project_key(source.get("item"))
                if _is_missing_record(source) and any(_same_project(project, value) for value in substantive_keys):
                    audit["missing_suppressed"] += 1
                    continue
                exact = tuple(str(source.get(field) or "") for field in (
                    "item", "reported", "should_be", "standard", "severity",
                ))
                if exact not in item_groups:
                    row = deepcopy(source)
                    row["source_segments"] = [segment]
                    item_groups[exact] = row
                    items.append(row)
                elif segment not in item_groups[exact]["source_segments"]:
                    item_groups[exact]["source_segments"].append(segment)
        for conflict in conflicts:
            items.append({
                "item": f"{conflict['project']}分段结论冲突",
                "reported": "不同分段同时出现符合与不符合结论",
                "should_be": "核对原始页面和试验数据后人工确认",
                "severity": "suggestion", "action_required": True,
                "review_action": "人工核对相反分段的原始证据",
                "source_segments": conflict["source_segments"],
            })
        base["checks"] = checks
        base["items"] = items
        output_samples.append(base)
        audit["samples"] += 1
        audit["conflicts"].extend({"sample": key, **conflict} for conflict in conflicts)
    output["samples"] = output_samples
    output.setdefault("_deterministic_validation", {})["merge_policy"] = audit
    return output


_LABELED_REPORT_NUMBER = re.compile(
    r"(?:报告编号|报告号|Report[ \t]*No\.?)\s*[:：]?[ \t]*([A-Z0-9][A-Z0-9 \t()/_-]{5,})",
    re.I,
)


def _normalized_report_number(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _report_number_hierarchy_consistent(text_value: str) -> bool:
    """识别主报告号与子报告号的合法层级，不把 /1-S…/6-S 当成冲突。"""
    # 数字子号必须由报告组成页明确列出，不能只凭公共前缀消除冲突。
    raw_values = {
        re.sub(r"[ \t]", "", match.group(1)).upper()
        for match in _LABELED_REPORT_NUMBER.finditer(text_value)
    }
    compositions = re.findall(
        r"--- Page\s+\d+[^\n]*---\s*([\s\S]*?)(?=\n--- Page\s+\d+|\Z)", text_value
    )
    for base in raw_values:
        if len(raw_values) <= 1 or len(base) < 8:
            continue
        if not all(value == base or re.fullmatch(re.escape(base) + r"-[1-9]\d{0,2}", value)
                   for value in raw_values):
            continue
        for page in compositions:
            if not all(word in page for word in ("报告组成", "报告内容", "编号")):
                continue
            listed = set(re.findall(r"(?<![A-Z0-9_-])" + re.escape(base)
                                    + r"(?:-[1-9]\d{0,2})?(?![A-Z0-9_-])", page.upper()))
            if raw_values <= listed:
                return True
    values = {
        _normalized_report_number(match.group(1))
        for match in _LABELED_REPORT_NUMBER.finditer(text_value)
        if _normalized_report_number(match.group(1))
    }
    if len(values) <= 1:
        return False
    for base in values:
        if len(base) < 8 or not base[-1].isalpha():
            continue
        stem, suffix = base[:-1], base[-1]
        if all(
            value == base or (
                value.endswith(suffix)
                and value[:-1].startswith(stem)
                and value[:-1][len(stem):].isdigit()
                and 1 <= len(value[:-1][len(stem):]) <= 3
            )
            for value in values
        ):
            return True
    suffixes = {value[-1] for value in values if value and value[-1].isalpha()}
    if len(suffixes) != 1:
        return False
    bodies = [value[:-1] for value in values]
    prefix = bodies[0]
    for body in bodies[1:]:
        while prefix and not body.startswith(prefix):
            prefix = prefix[:-1]
    remainders = [body[len(prefix):] for body in bodies]
    return bool(
        len(prefix) >= 8
        and len(set(remainders)) > 1
        and all(value.isdigit() and 1 <= len(value) <= 3 for value in remainders)
    )


def apply_report_chronology(result: dict[str, Any], source_text: str) -> dict[str, Any]:
    """在本地按原页明确日期字段兜底；不更正年份，不比较设备日期或跨页拼接。"""
    from datetime import date
    from html import unescape

    rule = "REPORT_CHRONOLOGY_V1"
    # 全报告问题独立存储，不制造第九个样品，也不挂到任意真实样品下。
    for field in ("report_checks", "report_items"):
        result[field] = [entry for entry in result.get(field) or [] if entry.get("rule_code") != rule]
    labels = ("收样日期", "完成日期", "签发日期")
    observations = []
    problems = []
    pages = re.finditer(r"--- Page\s+(\d+)[^\n]*---\s*([\s\S]*?)(?=\n--- Page\s+\d+|\Z)", source_text)
    for page in pages:
        identity = _LABELED_REPORT_NUMBER.search(page.group(2))
        if not result.get("report_no") or not identity or (
            _normalized_report_number(identity.group(1)) != _normalized_report_number(result["report_no"])
        ):
            # 只处理可绑定当前总报告的页面，旧报告附件及身份不明页不拼接。
            continue
        # 保留表格边界，避免从另一个单元格/下一项目借取日期。
        plain = unescape(re.sub(r"<[^>]+>", "|", page.group(2)))
        fields = {}
        for label in labels:
            tokens = re.findall(r"(?:^|[\n|])\s*" + label
                                + r"\s*[:：]?\s*[|]*\s*(\d{4}(?:[-/.年])\d{1,2}(?:[-/.月])\d{1,2}日?)(?!\d)", plain)
            if tokens:
                fields[label] = sorted(set(tokens))
        if len(fields) < 2:
            continue
        record = {"page": int(page.group(1)), "fields": fields}
        observations.append(record)
        parsed = {}
        ambiguous = False
        for label, tokens in fields.items():
            try:
                values = {date(*map(int, re.findall(r"\d+", token))) for token in tokens}
                if len(values) != 1:
                    ambiguous = True
                else:
                    parsed[label] = next(iter(values))
            except ValueError:
                ambiguous = True
        inversions = [f"{left}晚于{right}" for left, right in
                      ((labels[0], labels[1]), (labels[1], labels[2]), (labels[0], labels[2]))
                      if left in parsed and right in parsed and parsed[left] > parsed[right]]
        if ambiguous or inversions:
            problems.append({**record, "reason": "日期字段存在多个值或无效日期" if ambiguous else "；".join(inversions)})
    result.setdefault("_deterministic_validation", {})["report_chronology"] = {
        "rule_code": rule, "status": "manual_review" if problems else "checked" if observations else "not_evaluated",
        "observations": observations,
    }
    if not observations:
        return result
    selected = problems or observations
    excerpts = sorted({"；".join(label + "=" + "/".join(values) for label, values in entry["fields"].items())
                       for entry in selected})
    pages = sorted({entry["page"] for entry in selected})
    pairs = sorted({left + "→" + right for entry in selected for left, right in
                    ((labels[0], labels[1]), (labels[1], labels[2]), (labels[0], labels[2]))
                    if left in entry["fields"] and right in entry["fields"]})
    reported = "；".join(excerpts)
    required = "同一报告收样日期不应晚于完成日期，完成日期不应晚于签发日期；以原始记录核实，不推测正确年份"
    common = {"rule_code": rule, "scope": "report", "item": "报告日期先后关系",
              "reported": reported, "source_excerpt": reported, "source_pages": pages,
              "evidence_status": "located", "chronology_evaluated_pairs": pairs,
              "standard": "报告内部日期逻辑（非产品试验限值）"}
    result["report_checks"].append({**common, "category": "报告一致性", "required": required,
                                    "basis": common["standard"], "verdict": "manual_review" if problems else "pass"})
    if not problems:
        return result
    result["report_items"].append({**common, "severity": "suggestion", "action_type": "manual_review",
                                   "action_required": True, "should_be": required,
                                   "review_action": "核对原件及收样/完成记录，确认并更正日期矛盾或识别错误；不自动改写年份"})
    return result


def _privacy_logic_summary(checks: list[dict[str, Any]]) -> str:
    actionable = [item for item in checks if item.get("result") != "pass"]
    if not actionable:
        return ""
    lines = ["【单位内网内容复核线索（已脱敏，不得直接作为修改结论）】"]
    lines.extend(
        f"- {item.get('item', '逻辑一致性')}：{item.get('result', 'manual_review')}；{item.get('note', '')}"
        for item in actionable
    )
    return "\n".join(lines)


def normalize_privacy_logic_checks(preflight: dict[str, Any], source_text: str) -> dict[str, Any]:
    """消解编号假冲突，并对规则重建后的摘要再次脱敏和出口复检。"""
    if preflight.get("mode") == "disabled":
        return preflight
    checks = [deepcopy(item) for item in (preflight.get("logic_checks") or []) if isinstance(item, dict)]
    normalized: list[dict[str, Any]] = []
    replaced = False
    for item in checks:
        # 手写签名 OCR 误差大，不将审核/签名/批准人员一致性
        # 作为自动审核项，也不把这类内网模型输出发送给外网审核。
        from backend.app.privacy_preflight import _is_excluded_personnel_signature_check
        if _is_excluded_personnel_signature_check(item):
            continue
        if (
            _report_number_hierarchy_consistent(source_text)
            and item.get("item") == "报告编号一致性"
            and item.get("result") == "warning"
        ):
            if not replaced:
                normalized.append({
                    "item": "主报告/子报告编号层级",
                    "result": "pass",
                    "note": "识别为同一主报告号下的子报告编号，不构成编号冲突",
                })
                replaced = True
            continue
        normalized.append(item)
    preflight["logic_checks"] = normalized

    # prepare_privacy_preflight 已对原摘要脱敏；规则标准化会重建摘要，必须沿用
    # 同一批次黑名单再次脱敏，并在最终拼接后重新执行出口扫描。
    from backend.app.privacy_preflight import (
        PrivacyPreflightBlocked,
        redact_sensitive_text,
        scan_outbound_text,
    )

    companies = list(preflight.get("_privacy_company_names") or [])
    applications = list(preflight.get("_privacy_application_numbers") or [])
    redact_companies = bool(preflight.get("_privacy_redact_companies", True))
    redact_applications = bool(preflight.get("_privacy_redact_applications", True))
    markers = (
        "\n\n【单位内网内容复核线索（已脱敏，不得直接作为修改结论）】",
        "\n\n【单位内网逻辑预审（已脱敏，仅供复核）】",
    )
    candidate = str(preflight.get("sanitized_text") or "")
    base = candidate
    for marker in markers:
        base = base.split(marker, 1)[0]
    base, base_extra_counts = redact_sensitive_text(
        base,
        companies,
        applications,
        redact_companies=redact_companies,
        redact_applications=redact_applications,
    )
    summary, summary_counts = redact_sensitive_text(
        _privacy_logic_summary(normalized),
        companies,
        applications,
        redact_companies=redact_companies,
        redact_applications=redact_applications,
    )
    rebuilt = base + ("\n\n" + summary if summary else "")
    findings = scan_outbound_text(
        rebuilt,
        companies,
        applications,
        scan_companies=redact_companies,
        scan_applications=redact_applications,
    )
    blocked = bool(findings and preflight.get("_privacy_outbound_block_enabled", True))
    if preflight.get("mode") == "enforce" and blocked:
        types = "、".join(sorted({item["type"] for item in findings}))
        raise PrivacyPreflightBlocked(f"规则摘要重建后外发安全复检未通过：{types}")

    counts = dict(preflight.get("replacement_counts") or {})
    counts["company"] = int(counts.get("company") or 0) + base_extra_counts["company"] + summary_counts["company"]
    counts["application_no"] = int(counts.get("application_no") or 0) + base_extra_counts["application_no"] + summary_counts["application_no"]
    preflight["replacement_counts"] = counts
    preflight["outbound_findings"] = findings
    preflight["outbound_blocked"] = blocked
    preflight["sanitized_text"] = rebuilt
    if preflight.get("mode") == "enforce":
        preflight["external_text"] = rebuilt
    return preflight


def merge_privacy_logic_checks(privacy_batches: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """保留所有批次的唯一证据；显示预算不得丢弃非通过项。

    默认不截断。显式limit仅限制通过项占用的展示预算；非通过项即使
    超出预算也全部保留，且输出保持源顺序。不合并不同备注或不同结论。
    """
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for batch in privacy_batches:
        for source in batch.get("logic_checks") or []:
            if not isinstance(source, dict):
                continue
            from backend.app.privacy_preflight import _is_excluded_personnel_signature_check
            if _is_excluded_personnel_signature_check(source):
                continue
            item = deepcopy(source)
            key = tuple(str(item.get(field) or "").strip() for field in ("item", "result", "note"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    if limit is None or len(merged) <= limit:
        return merged
    remaining_passes = max(0, limit - sum(item.get("result") != "pass" for item in merged))
    selected: list[dict[str, Any]] = []
    for item in merged:
        if item.get("result") != "pass":
            selected.append(item)
        elif remaining_passes:
            selected.append(item)
            remaining_passes -= 1
    return selected

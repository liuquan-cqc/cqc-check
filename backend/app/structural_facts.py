"""Shared, conservative structural inputs. Report verdicts are not facts."""
from __future__ import annotations

import math
import re
from typing import Any


def parse_spec_groups(spec: str) -> list[dict[str, Any]]:
    value = str(spec or '').strip().translate(str.maketrans({'（':'(', '）':')', '＋':'+', '×':'*', 'x':'*', 'X':'*', 'G':'*', 'g':'*'}))
    value = re.sub(r'\s+', '', value)
    value = re.sub(r'(?:mm(?:²|2)|平方毫米)$', '', value, flags=re.I)
    if not value or re.search(r'[^0-9.*+()]', value):
        return []

    def parse(expr):
        # Split only at top-level plus, respecting parentheses.
        while expr.startswith('(') and expr.endswith(')'):
            depth = 0
            closes_at_end = True
            for index, char in enumerate(expr):
                depth += (char == '(') - (char == ')')
                if depth == 0 and index != len(expr)-1:
                    closes_at_end = False
                    break
            if not closes_at_end: break
            expr = expr[1:-1]
        depth, start, terms = 0, 0, []
        for index, char in enumerate(expr):
            depth += (char == '(') - (char == ')')
            if depth < 0: raise ValueError('parenthesis')
            if char == '+' and depth == 0:
                terms.append(expr[start:index]); start = index+1
        if depth: raise ValueError('parenthesis')
        if terms:
            terms.append(expr[start:])
            return [group for term in terms for group in parse(term)]
        grouped = re.fullmatch(r'(\d+)\*\((.+)\)', expr)
        if grouped:
            multiplier = int(grouped[1])
            if multiplier <= 0: raise ValueError('count')
            return [dict(core_count=multiplier*g['core_count'], section_mm2=g['section_mm2']) for g in parse(grouped[2])]
        if not re.fullmatch(r'\d+(?:\*\d+)*\*\d+(?:\.\d+)?', expr):
            raise ValueError('incomplete spec')
        numbers = expr.split('*')
        count = math.prod(int(n) for n in numbers[:-1])
        section = float(numbers[-1])
        if count <= 0 or not math.isfinite(section) or section <= 0: raise ValueError('positive inputs required')
        return [dict(core_count=count, section_mm2=section)]

    try:
        return parse(value)
    except (ValueError, OverflowError, RecursionError):
        return []


def spec_facts(spec: str) -> tuple[int | None, float | None]:
    groups = parse_spec_groups(spec)
    if not groups: return None, None
    sections = {group['section_mm2'] for group in groups}
    return sum(group['core_count'] for group in groups), next(iter(sections)) if len(sections) == 1 else None


def canonical_spec(spec: str) -> str:
    """Normalize spelling without flattening pair/group structure or taking prefixes."""
    if not parse_spec_groups(spec):
        return ''
    value = re.sub(r'\s+', '', spec).translate(str.maketrans(
        {'（':'(', '）':')', '＋':'+', '*':'×', 'x':'×', 'X':'×', 'G':'×', 'g':'×'}))
    value = re.sub(r'(?:mm(?:²|2)|平方毫米)$', '', value, flags=re.I)
    value = re.sub(r'\d+(?:\.\d+)?', lambda m: format(float(m[0]), '.15g'), value)
    return value + 'mm²'


def positive_number(value: Any, integer: bool = False) -> float | None:
    if isinstance(value, bool): return None
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:mm(?:²|2)?|毫米)?\s*', str(value or ''), re.I)
    if not match: return None
    number = float(match[1])
    if not math.isfinite(number) or number <= 0 or (integer and not number.is_integer()): return None
    return number


def filler_fact(sample: dict[str, Any]) -> tuple[str | None, str]:
    aliases = {
        'non_vulcanized_rubber':'non_vulcanized_rubber','非硫化橡皮':'non_vulcanized_rubber','非硫化橡胶':'non_vulcanized_rubber',
        'plastic':'plastic','塑料':'plastic','fiber':'fiber','纤维':'fiber',
        'none':'none','无':'none','无填充':'none',
    }
    raw = str(sample.get('filler_material') or '').strip().lower()
    hits = set()
    if raw:
        if raw not in aliases: return None, 'unknown:filler_material'
        hits.add(aliases[raw])
    text = '；'.join(str(sample.get(k) or '') for k in ('construction','description','sample_name'))
    # Ambiguous alternatives and negated material claims are not positive facts.
    if re.search(r'(?:或|可能|待定|不详|不确定).{0,15}(?:填充|填芯)|(?:填充|填芯).{0,12}(?:或|不详|待定)', text):
        return None, 'unknown:ambiguous_filler'
    for sentence in re.split(r'[；;。\n]', text):
        if re.search(r'(?:无|不含|未设)(?:任何)?(?:填充|填芯)', sentence): hits.add('none')
        for pattern, name in [(r'非硫化(?:橡皮|橡胶)(?:填充|填芯)','non_vulcanized_rubber'),(r'塑料(?:填充|填芯)','plastic'),(r'纤维(?:填充|填芯)','fiber')]:
            for match in re.finditer(pattern, sentence):
                prefix = sentence[max(0,match.start()-8):match.start()]
                if re.search(r'(?:无|不含|未用|不用|不采用|非)\s*$', prefix): continue
                hits.add(name)
    if len(hits) > 1: return None, 'conflict:filler_material'
    return (next(iter(hits)), 'sample.material_evidence') if hits else (None, 'unknown:filler_material')


def reconcile_facts(sample: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Validate independently of profile binding and preserve conflict tombstones."""
    result = dict(facts)
    sources = dict(result.get('_sources') or {})
    conflicts = dict(result.get('_fact_conflicts') or {})
    groups = parse_spec_groups(str(sample.get('spec') or ''))
    spec_core, spec_section = spec_facts(str(sample.get('spec') or ''))
    aliases = {
        'core_count': ('core_count','cores'),
        'nominal_section_mm2': ('nominal_section_mm2','section_mm2','section'),
        'outer_diameter_mm': ('outer_diameter_mm','diameter_mm'),
        'insulated_core_diameter_mm': ('insulated_core_diameter_mm','core_outer_diameter_mm'),
        'short_axis_mm': ('short_axis_mm',),
    }
    for field, keys in aliases.items():
        values, invalid = [], False
        for key in keys:
            if sample.get(key) is None or sample.get(key) == '': continue
            number = positive_number(sample[key], field == 'core_count')
            if number is None: invalid = True
            else: values.append(number)
        # Only single, explicitly named dimension values are independent inputs.
        for check in sample.get('checks') or []:
            item = str(check.get('item') or '').strip()
            target = None
            if re.fullmatch(r'(?:平均)?外径(?:[-—]平均外径)?(?:测量)?', item): target = 'outer_diameter_mm'
            elif re.fullmatch(r'绝缘线芯(?:平均)?外径(?:测量)?', item): target = 'insulated_core_diameter_mm'
            elif re.fullmatch(r'(?:外形尺寸[-—])?短轴', item): target = 'short_axis_mm'
            raw = str(check.get('reported') or '').strip()
            if target == field and re.fullmatch(r'[+-]?\d+(?:\.\d+)?\s*(?:mm)?', raw, re.I):
                number = positive_number(raw)
                if number is None: invalid = True
                else: values.append(number)
        spec_value = {'core_count':spec_core,'nominal_section_mm2':spec_section}.get(field)
        if spec_value is not None: values.append(spec_value)
        if result.get(field) is not None:
            number = positive_number(result[field], field == 'core_count')
            if number is None: invalid = True
            else: values.append(number)
        mixed = field == 'nominal_section_mm2' and groups and spec_section is None
        if invalid or len(set(values)) > 1 or mixed or field in conflicts or sources.get(field,'').startswith('conflict:'):
            result.pop(field,None)
            sources[field] = 'conflict:invalid_or_inconsistent_structural_input'
            conflicts[field] = sorted(set(values))
        elif values:
            result[field] = int(values[0]) if field == 'core_count' else values[0]
            sources.setdefault(field,'sample.validated')
    if groups: result['_spec_groups'] = groups
    material, material_source = filler_fact(sample)
    # Do not accept the legacy applicability boolean or a reported P/F/N.
    field = 'non_pollution_applicable_gbt5023_1_5_3_1'
    # Roll out independent applicability validation by fact schema, not report IDs.
    result['_independent_applicability_fields'] = [field]
    result.pop(field,None)
    result.pop('filler_material',None)
    sources.pop(field,None)
    if material is not None:
        result['filler_material'] = material
        result[field] = material == 'non_vulcanized_rubber'
        sources['filler_material'] = sources[field] = material_source
    elif material_source.startswith('conflict:'):
        conflicts[field] = []
        sources[field] = material_source
    result['_sources'] = sources
    if conflicts: result['_fact_conflicts'] = conflicts
    return result

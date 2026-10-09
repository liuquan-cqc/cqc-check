"""文字层证人：Paddle 主解析与 PDF 内嵌文字层矛盾时，采信文字层。

文字层是实验室软件生成 PDF 时写入的，与页面打印内容同源——比图像
OCR 少一道有损转录。证人只在该页文字层可用、目标行解析干净时采信；
矛盾本身不进审核结论，两个读数都记入 row['text_layer_witness'] 供追溯。
采信同时提供报告自评（P/F），文字层行尾的评定与数值同源，可完成
原页交叉确认。
"""
from __future__ import annotations

import re
from typing import Any

_LOSS_LABEL = r'失重试验[-—－]失重'
_LOSS_REQ = r'最大\s*2\.0'
_LOSS_UNIT = r'mg/cm'

_OVEN_FIELDS = {
    'tensile_strength': (r'老化后抗张强度[-—－]中间值', r'最小\s*1[02]\.[05]', r'N/mm'),
    'tensile_change': (r'老化前后抗张强度变化率', r'最大\s*±?\s*20', r'%'),
    'elongation': (r'老化后断裂伸长率[-—－]中间值', r'最小\s*1[25]0', r'%'),
    'elongation_change': (r'老化前后断裂伸长率变化率', r'最大\s*±?\s*20', r'%'),
}


def _row_values(tl_text: str, label_pat: str, req_pat: str, unit_pat: str,
                limit: int = 12) -> tuple[list[str], str | None] | None:
    """文字层中定位标签行，取要求列之后的数值（到 P/F 或无关行停）。

    返回 (数值列表, 行尾评定 P/F)；数值泛滥或无值时返回 None。
    """
    if not tl_text:
        return None
    m = re.search(label_pat, tl_text)
    if not m:
        return None
    window = tl_text[m.end():m.end() + 420]
    rm = re.search(req_pat, window)
    if not rm:
        return None
    if not re.search(unit_pat, window[:rm.end()]):
        return None
    vals: list[str] = []
    verdict: str | None = None
    for tok in re.split(r'\s+', window[rm.end():]):
        tok = tok.strip()
        if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', tok):
            vals.append(tok)
            if len(vals) >= limit:
                return None  # 数值泛滥：多半串了别的行，不采信
            continue
        if tok in {'P', 'F'}:
            verdict = tok
            break
        if tok.startswith('⟦'):
            break
        if tok:
            break  # 条件行/下一标签：停止
    if not vals:
        return None
    return vals, verdict


def _page_by_no(source_group: dict | None, page_no: Any) -> dict | None:
    for p in (source_group or {}).get('pages') or []:
        if p.get('page') == page_no:
            return p
    return None


def _adopt(row: dict, source_group: dict | None, vals: list[str],
           verdict: str | None = None) -> bool:
    prior = str(row.get('reported') or '')
    prior_vals = [c.get('text') for c in (row.get('reported_values') or []) if c.get('text')]
    if prior_vals == vals or (not prior_vals and prior and vals == [prior]):
        return False
    row['reported'] = vals[0] if len(vals) == 1 else ''
    row['reported_values'] = [{'text': v} for v in vals]
    row['vector_status'] = 'located_values_only'
    row['result_column_binding'] = {'status': 'complete', 'expected_columns': len(vals)}
    # 文字层是独立第二来源：采信即确认，恢复被 Paddle 冲突降级打掉的状态，
    # 否则行内自评补齐了行仍被当未定位证据。
    row['status'] = 'located'
    if verdict and row.get('report_verdict') not in {'P', 'F'}:
        # 文字层行尾评定与数值同源（实验室软件写入），可补足缺失的报告自评，
        # 使采信后的行完成原页交叉确认。
        row['report_verdict'] = verdict
    row['text_layer_witness'] = {
        'adopted': True, 'prior_reported': prior,
        'prior_values': prior_vals, 'values': vals, 'verdict': verdict,
    }
    return True


def adopt_loss_witness(row: dict, source_group: dict | None) -> bool:
    """失重行证人：mg/cm² 最大2.0 之后的数值列。"""
    page = _page_by_no(source_group, row.get('page'))
    tl = (page or {}).get('text_layer')
    if not tl:
        return False
    found = _row_values(tl, _LOSS_LABEL, _LOSS_REQ, _LOSS_UNIT)
    if not found:
        return False
    vals, verdict = found
    return _adopt(row, source_group, vals, verdict)


def adopt_oven_witness(row: dict, source_group: dict | None) -> bool:
    """烤箱老化后行证人：按测量名取文字层行数值。"""
    spec = _OVEN_FIELDS.get(str(row.get('measurement') or ''))
    if not spec:
        return False
    page = _page_by_no(source_group, row.get('page'))
    tl = (page or {}).get('text_layer')
    if not tl:
        return False
    found = _row_values(tl, spec[0], spec[1], spec[2])
    if not found:
        return False
    vals, verdict = found
    return _adopt(row, source_group, vals, verdict)

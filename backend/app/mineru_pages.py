"""从MinerU结构化结果恢复带真实页码的审核文本。"""
from __future__ import annotations

import html
import json
import math
import re
import zipfile
from backend.app.ocr_evidence_support import supported
from pathlib import Path
from typing import Any


def collapsed_measurement_pages(text: str) -> list[int]:
    """Find explicit physical pages with merged mechanical-test label cells.

    A trigger is only an OCR quality warning, never proof of a report defect.
    """
    pages = set()
    for part in re.split(r'(?=^--- Page \d+[^\n]*---\s*$)', text, flags=re.M):
        marker = re.match(r'--- Page (\d+)\b',part)
        if not marker:
            continue  # Never invent a physical page for unpaged MinerU output.
        for table in re.findall(r'<table\b[^>]*>.*?</table>',part,re.I|re.S):
            if not all(header in table for header in ('检测项目','标准要求','检验结果')):
                continue
            # A collapsed table can lose the mechanical labels altogether.
            # Detect structural damage independently of surviving item names.
            # This only schedules local evidence collection, never a verdict.
            table_rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.I|re.S)
            empty_rows = sum(not re.sub(r'<[^>]+>|\s|&nbsp;', '', row)
                             for row in table_rows)
            spans = [int(value) for value in re.findall(
                r'rowspan\s*=\s*[\"\']?(\d+)', table, re.I)]
            if (len(table_rows) >= 12 and empty_rows >= 6
                    and empty_rows / len(table_rows) >= .20
                    and max(spans, default=0) >= 12):
                pages.add(int(marker[1]))
            for cell in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>',table,re.I|re.S):
                labels = re.findall(r'老化前抗张强度|老化后抗张强度|老化前断裂伸长率|老化后断裂伸长率|失重试验|高温压力|热冲击试验|低温弯曲试验',cell)
                unique_labels = set(labels)
                # 一些模板的最后一段只有“热冲击+高温压力+低温”三项，
                # 不会达到旧的四项目阈值，但高温压力的温度、时间、荷载
                # 和结果仍挤在同一单元格。该形态同样需要坐标复核。
                pressure_triplet = (
                    '高温压力' in unique_labels and len(unique_labels) >= 3
                    and re.search(r'试验条件\s*[:：].*施加(?:压力|荷载|负荷)', cell, re.S)
                )
                if len(unique_labels) >= 4 or pressure_triplet:
                    pages.add(int(marker[1]))
                    break
    return sorted(p for p in pages if p>0)


def coordinate_mechanical_component(page: dict[str, Any]) -> dict[str, Any]:
    """Read an explicit vertical category, never infer it from a test name."""
    rows=page.get('rows') or []
    headers=[r for r in rows if re.sub(r'\s+','',str(r.get('text',''))) == '检测项目']
    if len(headers)!=1 or headers[0]['confidence']<.95:
        return {'status':'unresolved','reason':'category_header_missing'}
    limit=min(x[0] for x in headers[0]['box'])
    bottom=max(x[1] for x in headers[0]['box'])
    cells=sorted([(i,r) for i,r in enumerate(rows)
                  if max(x[0] for x in r['box'])<limit and min(x[1] for x in r['box'])>bottom],
                 key=lambda pair:sum(x[1] for x in pair[1]['box']))
    matches=[]
    for start in range(len(cells)-5):
        group=cells[start:start+6]
        token=''.join(r['text'] for _,r in group)
        if token not in ('绝缘机械性能','护套机械性能'):continue
        if any(r['confidence']<.95 for _,r in group):continue
        centers=[(sum(x[0] for x in r['box'])/4,sum(x[1] for x in r['box'])/4) for _,r in group]
        widths=[max(x[0] for x in r['box'])-min(x[0] for x in r['box']) for _,r in group]
        gaps=[b[1]-a[1] for a,b in zip(centers,centers[1:])]
        if (min(widths)>0 and max(x for x,y in centers)-min(x for x,y in centers)<=min(widths)*.6
                and min(gaps)>0 and max(gaps)/min(gaps)<=1.3):
            matches.append({'component':'insulation' if token.startswith('绝缘') else 'sheath',
                            'label':token,'source_indices':[i for i,_ in group]})
    if len(matches)!=1:
        return {'status':'unresolved','reason':'mechanical_category_not_unique'}
    return {'status':'located',**matches[0], 'basis':'six_explicit_aligned_category_glyphs'}


_HTML_BACKFILL_UNITS = {"%", "mg/cm2", "N/mm2"}


def _html_table_rows(page: dict[str, Any]) -> list[list[str]]:
    """Parse the page MinerU HTML tables once; plain text cells per row."""
    cached = page.get("_html_table_rows_cache")
    if cached is not None:
        return cached
    text = str(page.get("text") or "")
    parsed = []
    for table in re.findall(r"<table\b[^>]*>.*?</table>", text, re.I | re.S):
        for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(td))).strip()
                     for td in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
            if cells:
                parsed.append(cells)
    page["_html_table_rows_cache"] = parsed
    return parsed


def _html_row_backfill(page: dict[str, Any], label_text: str, result_texts: list[str]) -> dict[str, Any] | None:
    """Supply a missing unit/verdict cell from the independent HTML table layer.

    The coordinate row value sequence must appear verbatim, in order, in one
    HTML row carrying the same item label. Only non-numeric cells (unit
    and/or P/F verdict) are taken; measurement values are never manufactured
    or altered. Returns None when no HTML row agrees exactly.
    """
    if not result_texts:
        return None
    wanted = [re.sub(r"\s", "", str(t)) for t in result_texts]
    if any(not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", t) for t in wanted):
        return None
    label_compact = re.sub(r"[\s—－-]", "", str(label_text))
    if not label_compact:
        return None
    for row in _html_table_rows(page):
        compacts = [re.sub(r"[\s—－-]", "", c) for c in row]
        if label_compact not in compacts:
            continue
        tail = row[compacts.index(label_compact) + 1:]
        if len(tail) < 4:
            continue
        values = [re.sub(r"\s", "", c) for c in tail[2:-1]]
        if values != wanted:
            continue
        unit = re.sub(r"\s", "", tail[0]).replace("²", "2").replace("％", "%")
        if unit not in _HTML_BACKFILL_UNITS:
            unit = None
        verdict = re.sub(r"\s", "", tail[-1]).upper()
        if verdict not in {"P", "F"}:
            verdict = None
        if unit is None and verdict is None:
            continue
        return {"unit": unit, "verdict": verdict, "html_row": row}
    return None


def coordinate_row_observations(page: dict[str, Any], label_pattern: str) -> dict[str, Any]:
    """Bind local OCR measurements to explicit headers; never infer a standard limit.

    This is an evidence primitive, not a whole-test pass or component selector.
    The caller must independently bind specimen/component and verified rules.
    """
    cells = []
    for index, raw in enumerate(page.get('rows') or []):
        try:
            box = raw['box']
            if len(box) != 4 or any(len(point) != 2 for point in box):
                raise ValueError('invalid quadrilateral')
            xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
            confidence = float(raw['confidence'])
            if not all(math.isfinite(n) for n in [*xs, *ys, confidence]):
                raise ValueError('nonfinite box')
            if not 0 <= confidence <= 1 or max(xs) <= min(xs) or max(ys) <= min(ys):
                raise ValueError('degenerate box')
            raw_text=str(raw['text']).strip()
            # Keep a decimal point already recognized inside ONE OCR box.
            # Never join separated integers or add an absent decimal point.
            normalized_text=re.sub(r'(?<=\d)\.\s+(?=\d)', '.', raw_text)
            cells.append(dict(index=index, text=normalized_text, raw_text=raw_text, box=box, native_support=raw.get('native_support'),
                              left=min(xs), right=max(xs), top=min(ys), bottom=max(ys),
                              x=sum(xs)/4, y=sum(ys)/4, confidence=confidence))
        except (KeyError, TypeError, ValueError):
            return {'status':'unresolved', 'reason':'invalid_ocr_geometry', 'observations':[]}
    names = ('检测项目', '单位', '标准要求', '检验结果', '评定')
    headers = []
    for name in names:
        matches = [c for c in cells if re.sub(r'\s+', '', c['text']) == name]
        if len(matches) != 1 or matches[0]['confidence'] < .95:
            return {'status':'unresolved', 'reason':'ambiguous_or_missing_headers', 'observations':[]}
        headers.append(matches[0])
    if any(a['right'] >= b['left'] for a,b in zip(headers,headers[1:])):
        return {'status':'unresolved', 'reason':'overlapping_columns', 'observations':[]}
    height = max(h['bottom']-h['top'] for h in headers)
    if max(h['y'] for h in headers)-min(h['y'] for h in headers) > height:
        return {'status':'unresolved', 'reason':'headers_not_same_row', 'observations':[]}
    boundaries = [(a['right']+b['left'])/2 for a,b in zip(headers,headers[1:])]
    # A centred result header is shorter than its multi-core result area.
    # Midpoints between header TEXT boxes cut off the first/last result.
    # Use the neighbouring headers' inner edges as an exclusion envelope.
    # Any requirement/verdict span entering it remains an ambiguous row;
    # no box is split or numeric text borrowed from such a span.
    boundaries[2]=headers[2]['right']
    boundaries[3]=headers[4]['left']
    def column(cell):
        # Whole box must fit one column; an OCR span across columns is not split.
        for index, (left,right) in enumerate(zip([-math.inf,*boundaries],[*boundaries,math.inf])):
            if cell['left'] >= left and cell['right'] <= right:
                return index
        return None
    def expected_unit(label_text):
        # A dimensional constraint, not a replacement for missing OCR text.
        label_text = re.sub(r'\s+', '', label_text)
        if re.match(r'^失重试验', label_text):
            return 'mg/cm2'
        if re.match(r'^(?:高温压力|老化(?:前后|前|后).*变化率|老化(?:前|后)断裂伸长率)', label_text):
            return '%'
        if re.match(r'^老化(?:前|后)抗张强度', label_text):
            return 'N/mm2'
        return None

    def canonical_unit(text):
        # Do not repair missing letters, denominators or powers (N/m != N/mm2).
        return re.sub(r'\s+', '', text).replace('²', '2').replace('％', '%')

    def backfill_cell(col_index, text, anchor_label):
        # Geometry comes from the verified table headers and the label line;
        # the TEXT comes from the independent HTML row only. Never a value.
        header = headers[col_index]
        left = boundaries[col_index - 1] if col_index > 0 else -math.inf
        right = boundaries[col_index] if col_index < len(boundaries) else math.inf
        x0 = max(header['left'], left + 1.0)
        x1 = min(header['right'], right - 1.0)
        if x1 <= x0:
            x0, x1 = header['left'], header['right']
        y0, y1 = anchor_label['top'], anchor_label['bottom']
        box = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        return {'index': -1, 'text': text, 'raw_text': text, 'box': box,
                'native_support': None, 'html_backfill': True,
                'left': x0, 'right': x1, 'top': y0, 'bottom': y1,
                'x': (x0 + x1) / 2, 'y': (y0 + y1) / 2, 'confidence': 1.0}

    # The declared TESTED cores define result columns, not the cable's total
    # core count. Keep specimen selection compliance separate from OCR coverage.
    color_labels=[c for c in cells if re.fullmatch(r'受检(?:验)?绝缘线芯颜色',re.sub(r'\s+','',c['text']))
                  and c['right']<headers[1]['left'] and c['y']>max(h['y'] for h in headers)]
    core_columns=[]
    column_basis='declared_tested_core_colors'
    column_witnesses=[]
    if len(color_labels)==1 and color_labels[0]['confidence']>=.95:
        cl=color_labels[0]
        declared=sorted([c for c in cells if column(c)==3 and c['y']>max(h['y'] for h in headers)
                         and abs(c['y']-cl['y'])<1.2*(cl['bottom']-cl['top'])],key=lambda c:c['x'])
        if declared and all(c['confidence']>=.95 and re.fullmatch(r'棕|蓝|灰|黑|红|白|黄|绿|黄[/／-]?绿|橙|紫|粉红',c['text']) for c in declared):
            core_columns=declared
    if not color_labels:
        # Older templates have no colour subheader. Require unanimous column
        # geometry across at least three separate numeric mechanical rows.
        # These rows establish positions only, never supply the target value.
        witnesses=[]
        for anchor in cells:
            if (anchor['right']>=headers[1]['left'] or anchor['confidence']<.95
                or not re.match(r'^老化(?:前|后)(?:抗张强度|断裂伸长率)',anchor['text'])):continue
            # Use the target row's tight band: the wider skew-recovery band
            # can absorb neighbouring change-rate rows in dense templates.
            same=[c for c in cells if column(c)==3 and abs(c['y']-anchor['y'])<=.45*(anchor['bottom']-anchor['top'])]
            if not same or all(re.fullmatch(r'[/／—-]+',c['text']) for c in same):continue
            if any((c['confidence']<.95 and not supported(c)) for c in same):
                witnesses=[];break
            if any(not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',c['text']) for c in same):
                # Colour-labelled composites (紫:15.2灰:…) carry their own
                # per-core values inline; a merged box cannot establish column
                # geometry. Skip the row instead of discarding every witness.
                if all(re.search(r'[:：]|[一-鿿]',c['text']) for c in same):
                    continue
                witnesses=[];break
            witnesses.append((anchor,sorted(same,key=lambda c:c['x'])))
        if len(witnesses)>=3 and len({len(v) for _,v in witnesses})==1:
            first=witnesses[0][1]
            gap=min((b['x']-a['x'] for a,b in zip(first,first[1:])),default=boundaries[3]-boundaries[2])
            if 1<=len(first)<=12 and gap>0:
                # A page may carry two mechanical blocks (e.g. air-oven and
                # non-pollution aging) whose result columns sit at slightly
                # different x. Whole-page unanimity then fails even though each
                # block is internally consistent. Cluster witness rows by
                # column position; unanimity is required within the cluster,
                # and only a cluster of at least three rows may define columns.
                tolerance=gap*.20
                clusters=[]
                for anchor,row in sorted(witnesses,key=lambda w:w[1][0]['x']):
                    for cluster in clusters:
                        if all(abs(c['x']-r['x'])<tolerance for c,r in zip(cluster['row'],row)):
                            cluster['members'].append((anchor,row))
                            break
                    else:
                        clusters.append({'row':row,'members':[(anchor,row)]})
                cluster=max(clusters,key=lambda c:len(c['members']))
                if (len(cluster['members'])>=3
                        and all(abs(a['x']-b['x'])<tolerance
                                for _,row in cluster['members'] for a,b in zip(cluster['row'],row))):
                    core_columns=[dict(c,text=f'column_{i+1}') for i,c in enumerate(cluster['row'])]
                    column_basis='unanimous_mechanical_result_columns'
                    column_witnesses=[{'label_index':a['index'],'result_indices':[c['index'] for c in r]}
                                      for a,r in cluster['members']]

    def unit_support(label, unit, result_texts=None):
        expected = expected_unit(label['text'])
        observed = canonical_unit(unit['text'])
        if expected is not None and observed != expected:
            return False, 'measurement_unit_mismatch', []
        if unit.get('html_backfill'):
            return True, 'html_row_unit_backfill', []
        if unit['confidence'] >= .95 or supported(unit):
            return True, None, []
        # Symbol scores are not numeric-value scores. Recover only an exact
        # dimensional match corroborated by a high-confidence unit elsewhere
        # in this same table's unit column. Never borrow its measurement value.
        support = [c for c in cells if c['index'] != unit['index']
                   and column(c) == 1 and c['top'] > max(h['bottom'] for h in headers)
                   and c['confidence'] >= .95 and canonical_unit(c['text']) == expected]
        resampled_support=[]
        if expected is not None and observed == expected and unit['confidence'] >= .60:
            # A second rendering must read this very cell, not another sample
            # or row. Numeric results and signs are never upgraded by this path.
            try:
                width,height=float(page['width']),float(page['height'])
                target=(unit['left']/width,unit['top']/height,unit['right']/width,unit['bottom']/height)
                for alternate in page.get('unit_corroboration_pages') or []:
                    if alternate.get('page')!=page.get('page'):
                        continue
                    w,h=float(alternate['width']),float(alternate['height'])
                    overlapping=[]
                    for index,cell in enumerate(alternate.get('rows') or []):
                        xs,ys=[float(p[0])/w for p in cell['box']],[float(p[1])/h for p in cell['box']]
                        bounds=(min(xs),min(ys),max(xs),max(ys))
                        area=(bounds[2]-bounds[0])*(bounds[3]-bounds[1])
                        base=(target[2]-target[0])*(target[3]-target[1])
                        intersection=max(0,min(target[2],bounds[2])-max(target[0],bounds[0]))*max(0,min(target[3],bounds[3])-max(target[1],bounds[1]))
                        if area>0 and base>0 and intersection/min(area,base)>=.7 and intersection/max(area,base)>=.5:
                            overlapping.append((index,cell))
                    if len(overlapping)==1:
                        index,cell=overlapping[0]
                        if float(cell['confidence'])>=.95:
                            if canonical_unit(cell['text'])!=expected:
                                return False, 'conflicting_resampled_unit', []
                            resampled_support.append({'render_scale':alternate['render_scale'],'index':index})
            except (KeyError,TypeError,ValueError,ZeroDivisionError):
                pass
        if resampled_support:
            return True, 'exact_unit_with_same_cell_resampling', resampled_support
        if expected is not None and observed == expected and unit['confidence'] >= .60 and support:
            return True, 'exact_unit_with_same_table_corroboration', [c['index'] for c in support]
        if (expected is not None and observed == expected and unit['confidence'] >= .60
                and result_texts):
            # The independent HTML table layer read this very row (same label,
            # same exact value sequence) with the same unit text.
            hit = _html_row_backfill(page, label['text'], result_texts)
            if hit and hit.get('unit') and canonical_unit(hit['unit']) == observed:
                return True, 'exact_unit_with_html_row_corroboration', []
        return False, 'low_confidence_measurement', []
    observations = []
    for label in cells:
        # Item labels can be wider than their short centered header. Permit
        # that only to the left of the unit header, never into the result area.
        if label['x'] >= boundaries[0] or label['right'] >= headers[1]['left'] or label['top'] <= max(h['bottom'] for h in headers):
            continue
        if not re.search(label_pattern,label['text']):
            continue
        tolerance = (label['bottom']-label['top'])*.45
        aligned = [c for c in cells if abs(c['y']-label['y']) <= tolerance]
        row_binding = 'same_line'
        # Some loss tables vertically centre their values on the condition
        # line. Recover only a unique result line inside an explicit, short
        # trial block; never search past the next item or choose a nearest value.
        if re.match(r'^失重试验', label['text']) and not any(column(c)==3 for c in aligned):
            endings = [c for c in cells if c['y']>label['y'] and c['right']<headers[1]['left']
                       and re.match(r'^(?:低温|热冲击|热稳定|失重试验|高温压力|非污染|老化|曲挠|燃烧)',c['text'])]
            end = min(endings,key=lambda c:c['y']) if endings else None
            if end and end['confidence']>=.95:
                block = [c for c in cells if label['y']<c['y']<end['top']]
                result_cells = [c for c in block if column(c)==3]
                if result_cells:
                    anchor = result_cells[0]
                    if (anchor['y']-label['y'] <= 2.5*(label['bottom']-label['top'])
                        and all(abs(c['y']-anchor['y'])<=tolerance for c in result_cells)):
                        aligned = [c for c in block if abs(c['y']-anchor['y'])<=tolerance]
                        row_binding = 'unique_result_line_within_loss_block'
        results = [c for c in aligned if column(c)==3]
        column_binding={'status':'unverified','expected_columns':None,'labels':[]}
        if re.match(r'^失重试验',label['text']) and core_columns:
            edges=[boundaries[2],*[(a['x']+b['x'])/2 for a,b in zip(core_columns,core_columns[1:])],boundaries[3]]
            result_y=results[0]['y'] if row_binding!='same_line' and results else label['y']
            # Keep a complete tight row. A wider skew-search is only a
            # recovery attempt; extra neighbouring values make it incomplete.
            nearby=results if len(results)==len(core_columns) else [c for c in cells if column(c)==3 and abs(c['y']-result_y)<=1.2*(label['bottom']-label['top'])]
            buckets=[[c for c in nearby if left<c['left'] and c['right']<right] for left,right in zip(edges,edges[1:])]
            complete=(all(len(b)==1 for b in buckets) and sum(map(len,buckets))==len(nearby))
            # Do not silently drop an extra/misaligned OCR box to obtain pass.
            results=sorted(nearby,key=lambda c:c['left'])
            column_binding={'status':'complete' if complete else 'incomplete','expected_columns':len(core_columns),
                            'basis':column_basis,'witness_rows':column_witnesses,
                            'labels':[c['text'] for c in core_columns],
                            'source_indices':[c['index'] for c in core_columns],
                            'result_indices':[[c['index'] for c in b] for b in buckets]}
        units = [c for c in aligned if column(c)==1]
        # Some source PDFs wrap the observed exponent onto the next line.
        # Join only an explicit, unique high-confidence glyph inside the unit
        # column directly below its base. Never insert an unobserved power.
        wrapped_unit_sources=[]
        if len(units)==1 and canonical_unit(units[0]['text'])=='mg/cm':
            base=units[0]
            next_labels=[c for c in cells if c['y']>label['y']
                         and c['right']<headers[1]['left']
                         and re.match(r'^(?:失重试验|热冲击|高温压力|低温|老化)',c['text'])]
            end=min((c['top'] for c in next_labels),default=base['bottom'])
            candidates=[c for c in cells if column(c)==1
                        and base['bottom']<=c['top']<min(end,base['bottom']+(base['bottom']-base['top'])*.6)
                        and base['left']<=c['left'] and c['right']<=base['right']]
            if (base['confidence']>=.95 and len(candidates)==1
                    and candidates[0]['text'] in {'2','²'} and candidates[0]['confidence']>=.95
                    and candidates[0]['bottom']-candidates[0]['top'] <= (base['bottom']-base['top'])*.8):
                exponent=candidates[0]
                units=[dict(base,text='mg/cm2')]
                wrapped_unit_sources=[{'base_index':base['index'],'exponent_index':exponent['index'],
                                       'base_box':base['box'],'exponent_box':exponent['box'],
                                       'reason':'observed_wrapped_exponent_in_same_unit_column'}]
        # Exact unit + labelled requirement in ONE span across the two
        # adjacent columns. Preserve its full box/text; never manufacture a
        # numeric result, alter a unit, or split a span reaching result cells.
        merged_unit_sources=[]
        if not units:
            for c in aligned:
                token=re.fullmatch(r'(mg/cm[²2]|N/mm[²2]|[%％])\s*\|?\s*(?:最大|最小)(?:\s*[±+-]?\s*\d+(?:\.\d+)?)?',c['text'])
                if (token and (c['confidence']>=.95 or supported(c)) and boundaries[0]<c['left']<boundaries[1]
                    and boundaries[1]<c['right']<boundaries[2]):
                    merged_unit_sources.append({'index':c['index'],'text':c['raw_text'],'box':c['box'],'unit_token':token[1]})
                    units.append(dict(c,text=token[1]))
        states = [c for c in aligned if column(c)==4]
        verdict_binding = 'horizontal_band'
        if len(results)==1 and len(units)==1:
            # A scanned table row may slope across the page. Anchor the line
            # on this label AND its numeric result; the unit must independently
            # agree. Do not choose the nearest verdict or prefer P over N/F.
            value=results[0]
            distance=value['x']-label['x']
            slope=(value['y']-label['y'])/distance if distance>0 else math.inf
            expected_y=lambda c:label['y']+slope*(c['x']-label['x'])
            unit=units[0]
            if (abs(slope)<=.06 and unit['confidence']>=.95
                    and abs(unit['y']-expected_y(unit))<=.5*min(
                        label['bottom']-label['top'],unit['bottom']-unit['top'])):
                band=.35*min(label['bottom']-label['top'],value['bottom']-value['top'])
                states=[c for c in cells if column(c)==4 and abs(c['y']-expected_y(c))<=band]
                verdict_binding='label_result_line_with_unit_witness'
        html_backfill = None
        if (results and (len(units) != 1 or len(states) != 1)
                and re.match(r'^(?:失重试验|高温压力)', label['text'])):
            # The coordinate layer dropped a unit or verdict cell that the
            # independent HTML table layer read. Recover only that cell, and
            # only when the HTML row reproduces this row's exact value sequence.
            hit = _html_row_backfill(page, label['text'],
                                     [c['text'] for c in sorted(results, key=lambda c: c['left'])])
            if hit:
                used = False
                if len(units) != 1 and hit.get('unit'):
                    units = [backfill_cell(1, hit['unit'], label)]
                    used = True
                if len(states) != 1 and hit.get('verdict'):
                    states = [backfill_cell(4, hit['verdict'], label)]
                    used = True
                html_backfill = hit if used else None
        required = sorted([c for c in aligned if column(c)==2],key=lambda c:c['x'])
        reason = None
        unit_basis = None
        unit_support_indices = []
        if len(results)!=1 or len(states)!=1 or len(units)!=1:
            reason = 'missing_or_ambiguous_row_cells'
        elif html_backfill is None and any(c['confidence'] < .95 and not supported(c) for c in [label,*results,*states]):
            reason = 'low_confidence_measurement'
        elif not re.fullmatch(r'[+-]?\d+(?:\.\d+)?%?',results[0]['text']):
            reason = 'non_numeric_measurement'
        elif states[0]['text'] not in {'P','F','N'}:
            reason = 'unrecognized_report_verdict'
        else:
            unit_valid, unit_basis, unit_support_indices = unit_support(
                label, units[0], [c['text'] for c in sorted(results, key=lambda c: c['left'])])
            if not unit_valid:
                reason = unit_basis
        vector_unit_valid=False
        if len(results)>1 and len(units)==1:
            vector_unit_valid,unit_basis,unit_support_indices=unit_support(
                label,units[0],[c['text'] for c in sorted(results, key=lambda c: c['left'])])
        from backend.app.ocr_unit_trace import trace_unit_resampling
        unit_trace = (trace_unit_resampling(page, units[0])
                      if unit_basis == 'conflicting_resampled_unit' and len(units) == 1 else [])
        observations.append({
            'page':page.get('page'), 'status':'unresolved' if reason else 'located', 'reason':reason,
            'label':label['text'], 'reported':results[0]['text'] if len(results)==1 else None,
            'label_box':label['box'],
            'row_binding':row_binding,
            'result_column_binding':column_binding,
            'unit':units[0]['text'] if len(units)==1 else None,
            'unit_validation':unit_basis, 'unit_support_indices':unit_support_indices,
            'unit_resampling_trace':unit_trace,
            'report_verdict':states[0]['text'] if len(states)==1 else None,
            'verdict_binding':verdict_binding,
            'html_backfill':html_backfill,
            'verdict_sources':[{k:c[k] for k in ('index','text','box','confidence')} for c in states],
            'requirement_ocr':' '.join(c['text'] for c in required),
            'requirement_is_authoritative':False,
            'merged_unit_sources':merged_unit_sources,
            'wrapped_unit_sources':wrapped_unit_sources,
            'reported_values':[{'text':c['text'],'box':c['box'],'index':c['index'],'confidence':c['confidence']}
                               for c in sorted(results,key=lambda c:c['left'])],
            'vector_status':('located_values_only' if len(results)>1 and len(units)==1 and len(states)==1
                and vector_unit_valid and (html_backfill is not None or all(c['confidence']>=.95 or supported(c) for c in [label,*results,*states]))
                and all(re.fullmatch(r'[+-]?\d+(?:\.\d+)?',c['text']) for c in results)
                and all(a['right']<b['left'] for a,b in zip(sorted(results,key=lambda c:c['left']),sorted(results,key=lambda c:c['left'])[1:]))
                else 'not_verified'),
            'vector_completeness_verified':False,
            'cells':[{k:c[k] for k in ('index','text','box','confidence')} for c in aligned],
            'text_normalizations':[{'index':c['index'],'original':c['raw_text'],'normalized':c['text'],
                                    'reason':'whitespace_after_existing_decimal_point'}
                                   for c in aligned if c['raw_text']!=c['text']],
        })
    # A second render can corroborate the SAME row, never a neighbouring
    # sample, page, row, or different value. Missing primary values require
    # agreement of both independent render scales before recovery.
    if page.get('measurement_corroboration_pages'):
        alternates=[]
        for alternate in page['measurement_corroboration_pages']:
            if alternate.get('page')!=page.get('page'):continue
            clean={k:v for k,v in alternate.items() if k not in {'measurement_corroboration_pages','unit_corroboration_pages'}}
            alternates.append((alternate,coordinate_row_observations(clean,label_pattern)['observations']))
        normalized_label=lambda t:re.sub(r'[\s—-]','',str(t))
        def anchor(obs,receipt):
            hits=[r for r in receipt['rows'] if normalized_label(r['text'])==normalized_label(obs['label'])]
            # Repeated aging labels are not recovered by name alone.
            if len(hits)!=1:return None
            box=hits[0]['box'];h=float(receipt['height'])
            return sum(p[1] for p in box)/4/h,(max(p[1] for p in box)-min(p[1] for p in box))/h
        for i,observation in enumerate(observations):
            base=anchor(observation,page)
            if not base:continue
            support=[]
            for alternate,found in alternates:
                for candidate in found:
                    other=anchor(candidate,alternate)
                    if ((candidate.get('status')=='located' or (candidate.get('vector_status')=='located_values_only' and candidate.get('result_column_binding',{}).get('status')=='complete'))
                        and other and normalized_label(candidate['label'])==normalized_label(observation['label'])
                        and abs(base[0]-other[0])<=min(base[1],other[1])*.7):
                        support.append((alternate.get('render_scale'),candidate))
            if not support:continue
            def signature(o):
                try:
                    values=tuple(float(c['text']) for c in o['reported_values']) if len(o.get('reported_values') or [])>1 else (float(o['reported']),)
                    return (values,canonical_unit(o['unit']),o['report_verdict'])
                except (ValueError,TypeError,KeyError):return None
            signs={signature(c) for _,c in support}
            own=signature(observation)
            if len(signs)!=1 or None in signs or (own is not None and own not in signs):
                observation.update(status='unresolved',reason='conflicting_same_row_renderings',vector_status='not_verified')
                continue
            if observation['status']!='located' and (own in signs or len({s for s,_ in support})>=2):
                selected=dict(support[0][1])
                selected['label']=observation['label']
                selected['label_box']=observation['label_box']
                selected['primary_observation']=observation
                selected['coordinate_recovery']={'kind':'same_row_render_consensus','scales':[s for s,_ in support],
                                                 'primary_value_present':own is not None}
                observations[i]=selected
    return {'status':'located' if observations and all(o['status']=='located' for o in observations) else 'unresolved',
            'reason':None if observations else 'no_matching_row', 'observations':observations}


def coordinate_pressure_conditions(page: dict[str, Any], observation: dict[str, Any], *, kind: str = 'pressure') -> dict[str, Any]:
    """Read conditions below a pressure/loss row, ending at the next test."""
    if kind not in {'pressure','loss'}:
        return {'status':'unresolved','reason':'unsupported_condition_kind','fields':{}}
    rows=page.get('rows') or []
    labels=[c for c in rows if c.get('text')==observation.get('label')]
    units=[c for c in rows if re.sub(r'\s+','',str(c.get('text') or ''))=='单位']
    if len(labels)!=1 or len(units)!=1:
        return {'status':'unresolved','reason':'ambiguous_condition_anchor','fields':{}}
    center=lambda c:sum(p[1] for p in c['box'])/4
    left_boundary=min(p[0] for p in units[0]['box'])
    start=center(labels[0])
    candidates=[c for c in rows if center(c)>start and max(p[0] for p in c['box'])<left_boundary]
    ends=[c for c in candidates if re.match(r'^(?:低温|热冲击|热稳定|失重试验|高温压力|非污染|老化|曲挠|燃烧)',c['text'])]
    if not ends:
        return {'status':'unresolved','reason':'condition_end_not_located','fields':{}}
    end=min(ends,key=center)
    if end['confidence']<.95:
        return {'status':'unresolved','reason':'uncertain_condition_boundary','fields':{}}
    candidates=[c for c in candidates if center(c)<center(end)]
    number=r'([+-]?\d+(?:\.\d+)?)'
    patterns={'temperature':r'温度\s*'+number+r'(?:\s*±\s*2)?\s*℃',
              'hours':r'时间\s*'+number+r'(?:\s*[×xX*]\s*'+number+r')?\s*h',
              'force':r'施加(?:压力|荷载|负荷)\s*[:：]?\s*'+number+r'\s*N'}
    if kind=='loss':patterns.pop('force')
    fields={}
    for field,pattern in patterns.items():
        hits=[]
        for cell in candidates:
            text=cell['text'].replace('°℃','℃').replace('°C','℃').replace('°c','℃')
            # Strip only correctly paired parentheses around a temperature.
            # Unbalanced delimiters remain unrecognized, never silently fixed.
            text=re.sub(r'温度\s*(?:\(\s*('+number[1:-1]+r'(?:\s*±\s*2)?)\s*\)|（\s*('+number[1:-1]+r'(?:\s*±\s*2)?)\s*）)\s*℃',
                        lambda m:'温度'+(m[1] or m[2])+'℃',text)
            for match in re.finditer(pattern,text):
                value=float(match[1])
                if field=='hours' and match[2] is not None:value*=float(match[2])
                hits.append({'value':value,'reported_text':match[1],
                             'text':cell['text'],'box':cell['box'],'confidence':cell['confidence']})
        valid=len(hits)==1 and (hits[0]['confidence']>=.95 or any(supported(c) and c['box']==hits[0]['box'] for c in candidates))
        fields[field]={'status':'located' if valid else 'unresolved',
                       'value':hits[0]['value'] if valid else None,
                       'reported_text':hits[0]['reported_text'] if valid else None,'observations':hits}
    if page.get('measurement_corroboration_pages'):
        for field,current in fields.items():
            support=[]
            for alternate in page['measurement_corroboration_pages']:
                if alternate.get('page')!=page.get('page'):continue
                candidates=[r for r in alternate['rows'] if re.sub(r'[\s—-]','',r['text'])==re.sub(r'[\s—-]','',observation['label'])]
                if len(candidates)!=1:continue
                if abs(center(candidates[0])/alternate['height']-start/page['height'])>.012:continue
                clean={k:v for k,v in alternate.items() if k not in {'measurement_corroboration_pages','unit_corroboration_pages'}}
                alternate_fields=coordinate_pressure_conditions(clean,{'label':candidates[0]['text']},kind=kind).get('fields',{})
                found=alternate_fields.get(field,{})
                if found.get('status')=='located':support.append((alternate.get('render_scale'),found))
            values={f['value'] for _,f in support}
            own_values={o['value'] for o in current.get('observations',[])}
            if len(values)>1 or (len(values)==1 and own_values and own_values!=values):
                current.update(status='unresolved',value=None,reason='conflicting_same_cell_conditions')
            elif len(values)==1 and current['status']!='located' and (own_values==values or len({s for s,_ in support})>=2):
                fields[field]={**support[0][1],'primary_field':current,'coordinate_recovery':{'kind':'same_row_condition_consensus','scales':[s for s,_ in support]}}
    return {'status':'located' if all(f['status']=='located' for f in fields.values()) else 'unresolved',
            'fields':fields,'end_box':end['box']}


def coordinate_oven_aging_observations(page: dict[str, Any]) -> dict[str, Any]:
    """Select air-oven rows by explicit vertical section boundaries, not order."""
    rows=page.get('rows') or []
    units=[r for r in rows if re.sub(r'\s+','',str(r.get('text') or ''))=='单位']
    if len(units)!=1:
        return {'status':'unresolved','reason':'ambiguous_unit_header','observations':[]}
    left_limit=min(p[0] for p in units[0]['box'])
    labels=[r for r in rows if max(p[0] for p in r['box'])<left_limit]
    starts=[r for r in labels if re.fullmatch(r'空气烘箱老化后的性能',re.sub(r'\s+','',r['text']))]
    if len(starts)!=1 or starts[0]['confidence']<.95:
        return {'status':'unresolved','reason':'oven_section_not_unique','observations':[]}
    center=lambda r:sum(p[1] for p in r['box'])/4
    start=center(starts[0])
    ends=[r for r in labels if center(r)>start and re.match(
        r'^(?:非污染|失重试验|热冲击|高温压力|低温|耐.*油|热延伸)',re.sub(r'\s+','',r['text']))]
    if not ends:
        return {'status':'unresolved','reason':'oven_section_end_missing','observations':[]}
    end=min(ends,key=center)
    if end['confidence']<.95:
        return {'status':'unresolved','reason':'oven_section_end_uncertain','observations':[]}
    fields={'tensile_strength':r'^老化后抗张强度',
            'tensile_change':r'^老化前后抗张强度变化率',
            # 一些扫描模板把“老化后”识别为“老化前后”。这里只接受
            # 完整的“断裂伸长率-中间值”标题，并继续受空气烘箱区间约束，
            # 避免借到变化率行或后续非污染老化区间。
            'elongation':r'^老化(?:前后|后)断裂伸长率\s*[-—－一]{0,3}\s*中间值',
            'elongation_change':r'^老化前后断裂伸长率变化率'}
    observations=[]
    before_observations=[]
    for field, pattern in {'tensile_strength':r'^老化前抗张强度',
                           'elongation':r'^老化前断裂伸长率'}.items():
        found=coordinate_row_observations(page,pattern)
        selected=[r for r in found['observations']
                  if r.get('label_box') and center({'box':r['label_box']})<start]
        before_observations.append({**selected[0],'measurement':field} if len(selected)==1 else
            {'page':page.get('page'),'measurement':field,'status':'unresolved',
             'reason':'before_measurement_not_unique','reported':None})
    for field,pattern in fields.items():
        found=coordinate_row_observations(page,pattern)
        selected=[]
        for observation in found['observations']:
            anchor_box=observation.get('label_box')
            if anchor_box and start<center({'box':anchor_box})<center(end):
                selected.append(observation)
        if len(selected)!=1:
            observations.append({'page':page.get('page'),'measurement':field,'status':'unresolved',
                                 'reason':'oven_measurement_not_unique','reported':None})
        else:
            observations.append({**selected[0],'measurement':field,'aging_group':'air_oven',
                                 'section_start_box':starts[0]['box'],'section_end_box':end['box']})
    if page.get('measurement_corroboration_pages'):
        alternate_groups=[]
        for alternate in page['measurement_corroboration_pages']:
            if alternate.get('page')!=page.get('page'):
                continue
            clean={k:v for k,v in alternate.items()
                   if k not in {'measurement_corroboration_pages','unit_corroboration_pages'}}
            alternate_groups.append((alternate.get('render_scale'),coordinate_oven_aging_observations(clean)))
        for index,current in enumerate(observations):
            support=[]
            for scale,group in alternate_groups:
                matches=[r for r in group.get('observations') or []
                         if r.get('measurement')==current.get('measurement') and r.get('status')=='located']
                if len(matches)==1:support.append((scale,matches[0]))
            def signature(record):
                values=record.get('reported_values') or []
                unit=re.sub(r'\s+','',str(record.get('unit') or '')).replace('²','2')
                return (tuple(str(v.get('text')) for v in values),unit,record.get('report_verdict'))
            signatures={signature(record) for _,record in support}
            own=signature(current) if current.get('reported_values') else None
            if len(signatures)>1 or (len(signatures)==1 and own is not None and own not in signatures):
                current.update(status='unresolved',reason='conflicting_same_row_renderings')
            elif (len(signatures)==1 and current.get('status')!='located'
                  and (own in signatures or len({scale for scale,_ in support})>=2)):
                recovered={**support[0][1], 'label':current.get('label'),
                           'label_box':current.get('label_box'),
                           'measurement':current.get('measurement'),'aging_group':'air_oven',
                           'section_start_box':starts[0]['box'],'section_end_box':end['box'],
                           'primary_observation':current,
                           'coordinate_recovery':{'kind':'same_oven_measurement_consensus',
                                                  'scales':[scale for scale,_ in support]}}
                observations[index]=recovered
    measurement_labels=[r for r in labels if start<center(r)<center(end)
                        and any(re.search(pattern,r['text']) for pattern in fields.values())]
    first_measurement=min(map(center,measurement_labels)) if measurement_labels else start
    condition_cells=[r for r in labels if start<center(r)<first_measurement]
    conditions={}
    for field in ('temperature','hours'):
        hits=[]
        for cell in condition_cells:
            text=re.sub(r'\s+','',cell['text']).replace('°℃','℃').replace('°C','℃')
            pattern=(r'温度([+-]?\d+(?:\.\d+)?)(?:±(\d+(?:\.\d+)?))?℃'
                     if field=='temperature' else r'时间([+-]?\d+(?:\.\d+)?)(?:[×xX*]([+-]?\d+(?:\.\d+)?))?h')
            for match in re.finditer(pattern,text):
                value=float(match[1])
                if field=='hours' and match[2] is not None:value*=float(match[2])
                hits.append({'value':value,'tolerance':float(match[2]) if field=='temperature' and match[2] is not None else None,
                             'text':cell['text'],'box':cell['box'],'confidence':cell['confidence']})
        valid=len(hits)==1 and hits[0]['confidence']>=.95
        conditions[field]={'status':'located' if valid else 'unresolved',
                           'value':hits[0]['value'] if valid else None,'observations':hits}
    # 温度、时间也允许同一物理页的不同渲染倍率做同坐标共识恢复。
    # 主渲染已有值时必须与共识完全一致；主渲染缺值时至少需要两个
    # 独立倍率一致，任何冲突都保持 unresolved。
    if page.get('measurement_corroboration_pages'):
        supports={field:[] for field in conditions}
        for alternate in page['measurement_corroboration_pages']:
            if alternate.get('page') != page.get('page'):
                continue
            clean={k:v for k,v in alternate.items()
                   if k not in {'measurement_corroboration_pages','unit_corroboration_pages'}}
            found=coordinate_oven_aging_observations(clean).get('conditions') or {}
            for field in supports:
                if (found.get(field) or {}).get('status') == 'located':
                    supports[field].append((alternate.get('render_scale'), found[field]))
        for field,current in conditions.items():
            values={record.get('value') for _,record in supports[field]}
            own_values={record.get('value') for record in current.get('observations') or []}
            if len(values)>1 or (len(values)==1 and own_values and own_values != values):
                current.update(status='unresolved', value=None,
                               reason='conflicting_same_cell_conditions')
            elif (len(values)==1 and current.get('status')!='located'
                  and (own_values==values or len({scale for scale,_ in supports[field]})>=2)):
                conditions[field]={**supports[field][0][1], 'primary_field':current,
                    'coordinate_recovery':{'kind':'same_oven_condition_consensus',
                                           'scales':[scale for scale,_ in supports[field]]}}
    return {'status':'located' if all(r['status']=='located' for r in observations)
            and all(r['status']=='located' for r in conditions.values()) else 'unresolved',
            'observations':observations,'conditions':conditions,'before_observations':before_observations,
            'boundary':'Measurements and reported conditions only; no standard limits or ageing verdict'}


def coordinate_insulation_resistance_observation(page: dict[str, Any]) -> dict[str, Any]:
    """Strictly recover one 70 ℃ insulation-resistance row.

    The common OCR glyph ``Q`` is normalized to ``Ω`` only inside the unique
    insulation-resistance unit cell.  Report requirement text is retained as
    non-authoritative evidence; callers must apply a verified product profile.
    """
    cells=[]
    try:
        for index,raw in enumerate(page.get('rows') or []):
            box=raw['box']; xs=[float(p[0]) for p in box]; ys=[float(p[1]) for p in box]
            confidence=float(raw['confidence'])
            if (len(box)!=4 or any(len(p)!=2 for p in box)
                    or not all(math.isfinite(v) for v in [*xs,*ys,confidence])
                    or not 0<=confidence<=1 or max(xs)<=min(xs) or max(ys)<=min(ys)):
                raise ValueError
            cells.append({'index':index,'text':str(raw.get('text') or '').strip(),
                          'confidence':confidence,'box':box,'left':min(xs),'right':max(xs),
                          'top':min(ys),'bottom':max(ys),'x':sum(xs)/4,'y':sum(ys)/4})
    except (KeyError,TypeError,ValueError):
        return {'status':'unresolved','reason':'invalid_ocr_geometry','observations':[]}
    headers=[]
    for name in ('检测项目','单位','标准要求','检验结果','评定'):
        found=[c for c in cells if re.sub(r'\s+','',c['text'])==name]
        if len(found)!=1 or found[0]['confidence']<.95:
            return {'status':'unresolved','reason':'ambiguous_or_missing_headers','observations':[]}
        headers.append(found[0])
    if any(a['right']>=b['left'] for a,b in zip(headers,headers[1:])):
        return {'status':'unresolved','reason':'overlapping_columns','observations':[]}
    labels=[c for c in cells if c['top']>max(h['bottom'] for h in headers)
            and c['right']<headers[1]['left']
            and re.fullmatch(r'绝缘电阻[（(]70\s*℃[）)]',re.sub(r'\s+','',c['text']))]
    if len(labels)!=1 or labels[0]['confidence']<.90:
        return {'status':'unresolved','reason':'insulation_resistance_row_not_unique','observations':[]}
    label=labels[0]; tolerance=(label['bottom']-label['top'])*.55
    aligned=[c for c in cells if abs(c['y']-label['y'])<=tolerance]
    merged=[]
    for cell in aligned:
        token=re.sub(r'[\s|]+','',cell['text']).replace('Ω','Ω')
        match=re.fullmatch(r'M[QΩ][·•]?km最小([0-9]+(?:\.[0-9]+)?)',token,re.I)
        unit_header_width=headers[1]['right']-headers[1]['left']
        if (match and cell['confidence']>=.95 and headers[1]['left']-unit_header_width*.25<=cell['left']
                and cell['right']<=headers[3]['left']):
            merged.append((cell,float(match.group(1))))
    results=sorted([c for c in aligned if headers[2]['right']<=c['left']
                    and c['right']<=headers[4]['left']
                    and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',c['text'])],key=lambda c:c['left'])
    states=[c for c in aligned if c['left']>=headers[4]['left'] and c['text'] in {'P','F','N'}]
    if (len(merged)!=1 or not results or len(results)>12 or len(states)!=1
            or min(c['confidence'] for c in [*results,*states])<.95
            or any(a['right']>=b['left'] for a,b in zip(results,results[1:]))):
        return {'status':'unresolved','reason':'missing_or_ambiguous_row_cells','observations':[]}
    unit_cell,reported_limit=merged[0]
    values=[float(c['text']) for c in results]
    return {'status':'located','reason':None,'page':page.get('page'),
            'label':label['text'],'unit':'MΩ·km','reported_values':values,
            'reported_limit':reported_limit,'requirement_is_authoritative':False,
            'report_verdict':states[0]['text'],
            'source_indices':{'label':label['index'],'unit_requirement':unit_cell['index'],
                              'results':[c['index'] for c in results],'verdict':states[0]['index']},
            'normalizations':[{'index':unit_cell['index'],'original':unit_cell['text'],
                               'normalized':'MΩ·km','scope':'unique_insulation_resistance_unit'}]}


def _render_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type == "table":
        body = str(item.get("table_body") or "").strip()
        if not body:
            raise ValueError('MinerU表格正文缺失，不能用页眉或表格标题代替完整识别')
        caption = "\n".join(str(value).strip() for value in item.get("table_caption") or [] if str(value).strip())
        footnote = "\n".join(str(value).strip() for value in item.get("table_footnote") or [] if str(value).strip())
        return "\n".join(value for value in (caption, body, footnote) if value)
    if item_type == "image":
        caption = "\n".join(str(value).strip() for value in item.get("image_caption") or [] if str(value).strip())
        footnote = "\n".join(str(value).strip() for value in item.get("image_footnote") or [] if str(value).strip())
        return "\n".join(value for value in (caption, footnote) if value)

    text = str(item.get("text") or "").strip()
    if not text:
        return ""
    level = item.get("text_level")
    if item_type == "text" and isinstance(level, int) and 1 <= level <= 6:
        return f"{'#' * level} {text}"
    return text


def page_marked_markdown(content_list: list[dict[str, Any]]) -> str:
    """按page_idx分组，保留MinerU块顺序并加入一基页码标记。"""
    pages: dict[int, list[str]] = {}
    for item in content_list:
        if not isinstance(item, dict) or not isinstance(item.get("page_idx"), int):
            continue
        rendered = _render_item(item)
        if rendered:
            pages.setdefault(int(item["page_idx"]) + 1, []).append(rendered)
    return "\n\n".join(
        f"--- Page {page_no} (MinerU structured) ---\n" + "\n\n".join(pages[page_no])
        for page_no in sorted(pages)
    ).strip()


def _recover_missing_table_bodies(content_list: list[dict[str, Any]], model_pages: Any) -> None:
    """Only fill an empty export field from the same document/page/position.

    MinerU may retain a complete table in model.json while dropping table_body
    from content_list. Never replace existing bodies or guess between boxes.
    Both structures are newly decoded in memory; archive bytes are untouched.
    """
    if not isinstance(model_pages, list):
        return
    def box(value: Any, limit: float) -> list[float] | None:
        if not isinstance(value, list) or len(value) != 4:
            return None
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= limit for v in value):
            return None
        if value[0] >= value[2] or value[1] >= value[3]:
            return None
        return [float(v) for v in value]
    for item in content_list:
        if not isinstance(item, dict) or item.get('type') != 'table' or str(item.get('table_body') or '').strip():
            continue
        page = item.get('page_idx')
        target_box = box(item.get('bbox'), 1000)
        if type(page) is not int or not 0 <= page < len(model_pages) or target_box is None:
            continue
        candidates = model_pages[page]
        if not isinstance(candidates, list):
            continue
        matches = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get('type') != 'table':
                continue
            candidate_box = box(candidate.get('bbox'), 1)
            if candidate_box is None or candidate.get('angle', 0) != 0:
                continue
            if all(abs(left - right * 1000) <= 3 for left, right in zip(target_box, candidate_box)):
                matches.append(candidate)
        if len(matches) != 1:
            continue
        body = matches[0].get('content')
        if not isinstance(body, str) or not re.fullmatch(r'\s*<table\b[^>]*>.*</table>\s*', body, re.S | re.I):
            continue
        item['table_body'] = body


def page_marked_markdown_from_zip(archive_path: str | Path) -> str:
    """优先读取MinerU原始content_list，缺少结构化文件时明确报错。"""
    with zipfile.ZipFile(archive_path) as archive:
        candidates = [
            name for name in archive.namelist()
            if re.search(r"(?:^|/)[^/]+_content_list\.json$", name)
        ]
        if not candidates:
            raise ValueError("MinerU结果包缺少content_list.json，无法恢复真实页码")
        if len(candidates) != 1:
            raise ValueError('MinerU结果包包含多个文档，不能按文件名猜测报告归属')
        content_list = json.loads(archive.read(candidates[0]))
        if not isinstance(content_list, list):
            raise ValueError("MinerU content_list.json格式异常")
        model_name = candidates[0][:-len('_content_list.json')] + '_model.json'
        if archive.namelist().count(model_name) == 1:
            try:
                model_pages = json.loads(archive.read(model_name))
            except (ValueError, UnicodeError):
                model_pages = None
            _recover_missing_table_bodies(content_list, model_pages)
    if not isinstance(content_list, list):
        raise ValueError("MinerU content_list.json格式异常")
    text = page_marked_markdown(content_list)
    if not text:
        raise ValueError("MinerU content_list.json没有可用的页码内容")
    return text

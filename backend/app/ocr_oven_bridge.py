"""Same-page six-row consensus for single-result-column sheath air ageing.

Never standalone OCR evidence: caller supplies already PDF/sample-bound rows.
Never changes values, confidence, units, material or standard limits.
"""
import copy,re,hashlib
from backend.app.ocr_loss_bridge import plain,canonical_unit

FIELDS={'before_tensile':'老化前抗张强度中间值','before_elongation':'老化前断裂伸长率中间值',
        'tensile_strength':'老化后抗张强度中间值','tensile_change':'老化前后抗张强度变化率',
        'elongation':'老化后断裂伸长率中间值','elongation_change':'老化前后断裂伸长率变化率'}
def label(s):
    s=re.sub(r'[\s—－-]','',s)
    return s.replace('强度一中间值','强度中间值').replace('伸长率一中间值','伸长率中间值')
def span(attrs,name):
    match=re.search(name+r'\s*=\s*["\x27]?(\d+)',attrs,re.I)
    return int(match[1]) if match else 1
def table_groups(text):
    groups=[]
    for table in re.findall(r'<table\b[^>]*>.*?</table>',text,re.I|re.S):
        stage=None;found={};bad=False;header=False;result_span=1
        for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>',table,re.I|re.S):
            cells=[(attrs,plain(body)) for attrs,body in re.findall(r'<t[dh]\b([^>]*)>(.*?)</t[dh]>',tr,re.I|re.S)]
            ts=[v for _,v in cells];compact=''.join(ts).replace(' ','')
            if all(x in ts for x in ('检测项目','单位','标准要求','检验结果')):
                header=ts.count('检验结果')==1
                if header:
                    result_span=span(cells[ts.index('检验结果')][0],'colspan')
                    header=result_span in (1,2)
            if '交货状态原始性能' in compact:stage='before';continue
            if '空气烘箱老化后的性能' in compact:
                if stage!='before':bad=True
                stage='after';continue
            if stage=='after' and any(x in compact for x in ('非污染','失重试验','热冲击')):stage='ended'
            if stage not in ('before','after'):continue
            for j,(_,text_label) in enumerate(cells):
                normalized=label(text_label)
                # This complete elongation label variant is bounded by the air-oven section.
                if stage=='after' and normalized=='老化前后断裂伸长率中间值':normalized=FIELDS['elongation']
                keys=[k for k,v in FIELDS.items() if v==normalized and (k.startswith('before_')==(stage=='before'))]
                if not keys:continue
                key=keys[0];tail=cells[j+1:]
                if key in found or len(tail) not in (4,5):bad=True;continue
                if any(span(a,'rowspan')!=1 for a,_ in tail):bad=True;continue
                # One numeric cell may span exactly the same two layout columns
                # as the one result header. Never duplicate that numeric token.
                if any(span(a,'colspan')!=(result_span if k==len(tail)-2 else 1) for k,(a,_) in enumerate(tail)):bad=True;continue
                if len(tail)==5:
                    # Standard requirement may occupy two explicit cells.
                    # Merge only direction + numeric limit, never result cells.
                    req=re.sub(r'[\s$]','',tail[2][1]).replace(r'\pm','±')
                    if tail[1][1] not in ('最小','最大') or not re.fullmatch(r'±?\d+(?:\.\d+)?',req):bad=True;continue
                    tail=[tail[0],('',tail[1][1]+req),tail[3],tail[4]]
                unit=canonical_unit(tail[0][1]);value=re.sub(r'\s+','',tail[2][1]);verdict=tail[3][1].upper()
                expected='N/mm2' if key in ('before_tensile','tensile_strength') else '%'
                if unit!=expected or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',value) or verdict not in ('P','F'):bad=True;continue
                found[key]=(float(value),unit,verdict)
        if header and not bad and set(found)==set(FIELDS):groups.append(found)
    return groups

def recover_oven_rows(source_group,rows):
    original=copy.deepcopy(rows)
    if len(rows)!=4 or any(r.get('component')!='sheath' or r.get('aging_group')!='air_oven' for r in rows):return original
    identities={(r.get('page'),r.get('source_pdf_sha256'),r.get('source_specimen_id')) for r in rows}
    if len(identities)!=1:return original
    page,digest,specimen=next(iter(identities))
    if type(page) is not int or not re.fullmatch('[0-9a-f]{64}',str(digest)) or not specimen:return original
    before=rows[0].get('aging_before_observations') or []
    if len(before)!=2 or any(r.get('aging_before_observations')!=before for r in rows):return original
    pages=[p for p in source_group.get('pages',[]) if p.get('page')==page]
    if len(pages)!=1:return original
    groups=table_groups(pages[0].get('text',''))
    if not groups:
        contexts = [r.get('paddle_oven_context') for r in rows]
        context = contexts[0] if contexts else None
        if context and all(c == context for c in contexts):
            ident = context.get('identity', {})
            text = context.get('text', '')
            if (ident.get('source_sha256') == digest and ident.get('page') == page
                    and ident.get('kind') == 'oven_context'
                    and re.fullmatch('[0-9a-f]{64}', str(ident.get('image_sha256', '')))
                    and context.get('text_sha256') == hashlib.sha256(text.encode()).hexdigest()):
                groups = table_groups(text)
    if len(groups)!=1:return original
    expected=groups[0];records={};ratios=[]
    for r in list(before)+list(rows):
        key=r.get('measurement')
        if r in before:key={'tensile_strength':'before_tensile','elongation':'before_elongation'}.get(key)
        if key not in FIELDS or key in records or r.get('page')!=page:return original
        if r.get('status')!='located' and r.get('reason')!='low_confidence_measurement':return original
        if r.get('unit_validation')=='conflicting_resampled_unit':return original
        values=r.get('reported_values') or []
        if len(values)!=1 or float(values[0].get('confidence') or 0)<.60:return original
        value=str(values[0].get('text',''));unit=canonical_unit(r.get('unit',''));verdict=r.get('report_verdict')
        if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',value):return original
        if (float(value),unit,verdict)!=expected[key] or str(r.get('reported'))!=value:return original
        # Original coordinate row must explicitly contain the item, unit, value
        # and verdict in that left-to-right order. Relative positions tolerate
        # different raster scales without translating evidence to another row.
        base=r
        while isinstance(base.get('primary_observation'),dict):base=base['primary_observation']
        cells=base.get('cells') or []
        matches=[c for c in cells if c.get('text')==base.get('label') and c.get('confidence',0)>=.95]
        units=[c for c in cells if canonical_unit(c.get('text',''))==unit and c.get('confidence',0)>=.60]
        vals=base.get('reported_values') or [];vs=base.get('verdict_sources') or []
        if len(matches)!=1 or len(units)!=1 or len(vals)!=1 or len(vs)!=1 or vs[0].get('confidence',0)<.90:return original
        if str(vals[0].get('text'))!=value:return original
        def center(cell,axis):return sum(p[axis] for p in cell['box'])/4
        try:
            x=[center(c,0) for c in (matches[0],units[0],vals[0],vs[0])]
            if not 0<x[0]<x[1]<x[2]<x[3]:return original
            y=[center(c,1) for c in (units[0],vals[0],vs[0])]
            height=max(p[1] for p in vals[0]['box'])-min(p[1] for p in vals[0]['box'])
            if max(y)-min(y)>height*.7:return original
            ratios.append(x[2]/x[3])
        except (KeyError,TypeError,ValueError,ZeroDivisionError):return original
        records[key]=r
    if set(records)!=set(FIELDS) or max(ratios)-min(ratios)>.04:return original
    changed={k for k,r in records.items() if r.get('status')!='located'}
    if not changed:return original
    proof={'kind':'same_sample_page_six_row_ocr_consensus','source_sha256':digest,'page':page,
           'source_specimen_id':specimen,'fields':{k:list(v) for k,v in expected.items()},
           'note':'原页坐标与OCR完整老化组逐项一致；未修改原始数值或置信度'}
    upgraded=copy.deepcopy(records)
    for key in changed:
        upgraded[key]['prior_evidence_status']={k:upgraded[key].get(k) for k in ('status','reason')}
        upgraded[key].update(status='located',reason=None,cross_source_oven_corroboration=proof)
    new_before=[upgraded['before_tensile'],upgraded['before_elongation']]
    result=[]
    for row in original:
        row=upgraded[row['measurement']];row['aging_before_observations']=copy.deepcopy(new_before);result.append(row)
    return result

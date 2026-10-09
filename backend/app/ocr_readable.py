"""Isolated candidate: same-cell crop corroboration, never invented measurements."""
import copy
import hashlib
import re

VERSION = 'crop-corroboration-trial-20260916-v1'


def material_clauses(text, component):
    """Balanced nested parentheses or colon-led clause; never cross into another component/supplier."""
    for hit in re.finditer(re.escape(component)+r'(?:材料|料)?\s*([（(])', text):
        start=hit.end(); stack=[hit[1]]
        for pos in range(start, min(len(text), start+240)):
            ch=text[pos]
            if ch in '(（': stack.append(ch)
            elif ch in ')）':
                if not stack or (stack[-1],ch) not in [('(',')'),('（','）')]: break
                stack.pop()
                if not stack:
                    clause=text[start:pos]
                    if not re.search('绝缘|护套|导体|屏蔽|供应商|生产厂家',clause): yield clause
                    break
    # Colon-led form such as 「绝缘材料：PVC/D塑料，JR-70，生产厂：…」: the clause
    # runs to the next component mention or a sentence/segment delimiter.
    for hit in re.finditer(re.escape(component)+r'(?:材料|料)?\s*[:：]\s*([^；;。]*?)(?=(?:[，,]\s*)?(?:绝缘|护套|导体|屏蔽)材料|[；;。]|$)', text):
        clause=hit.group(1)
        if clause and not re.search('绝缘|护套|导体|屏蔽|供应商|生产厂家',clause): yield clause


def canonical(s):
    # Only typography normalization, not changing digits/signs/decimal points.
    return re.sub(r'\s+','',s).replace('°℃','℃').replace('°C','℃').replace('（','(').replace('）',')').replace('²','2')


def accept_cell(primary, attempts):
    """Two renderings, exact same text, located inside the original cell."""
    if len(attempts)!=2 or {a.get('scale') for a in attempts}!={3,4}:return None
    if any(a.get('confidence',0)<.95 or not a.get('geometry_ok') for a in attempts):return None
    tokens={canonical(a['text']) for a in attempts}
    if len(tokens)!=1 or tokens!={canonical(primary['text'])}:return None
    return min(a['confidence'] for a in attempts)


def strengthen_page(pdf_page, page, engine, outdir, budget=16):
    import fitz
    import numpy as np
    from pathlib import Path
    result=copy.deepcopy(page); changes=[]; attempts_log=[]
    sx=pdf_page.rect.width/page['width']; sy=pdf_page.rect.height/page['height']
    # A crop augments the original row at unchanged coordinates. It does not
    # introduce another row/column, value or synthetic complete page.
    for index,cell in enumerate(page['rows']):
        if not .60<=cell['confidence']<.95:continue
        if not re.search(r'温度|时间|mg/|N/mm|[%％]|施加|^[+-]?\d+(?:\.\d+)?$',cell['text']):continue
        if len(attempts_log)>=budget:break
        xs=[p[0]*sx for p in cell['box']];ys=[p[1]*sy for p in cell['box']]
        original=fitz.Rect(min(xs),min(ys),max(xs),max(ys))
        # Small padding preserves the whole text box, never a result-only digit.
        clip=fitz.Rect(original.x0-5,original.y0-3,original.x1+5,original.y1+3)&pdf_page.rect
        attempts=[]
        for scale in (3,4):
            pix=pdf_page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=clip,alpha=False)
            path=Path(outdir)/f'p{page["page"]}-cell{index}-s{scale}.png';pix.save(path)
            image=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,pix.n)
            rows,_=engine(image)
            if not rows or len(rows)!=1:
                attempts.append({'scale':scale,'reason':'split_or_empty','count':len(rows or [])});continue
            box,text,confidence=rows[0]
            # pix origin is integer-rounded; map back to source PDF coordinates.
            bounds=fitz.Rect(min(p[0] for p in box)/scale+pix.x/scale,
                             min(p[1] for p in box)/scale+pix.y/scale,
                             max(p[0] for p in box)/scale+pix.x/scale,
                             max(p[1] for p in box)/scale+pix.y/scale)
            intersection=(bounds&original).get_area()
            overlap=intersection/max(bounds.get_area(),original.get_area(),1)
            attempts.append({'scale':scale,'text':text,'confidence':float(confidence),'geometry_ok':overlap>=.65,'overlap':overlap})
        score=accept_cell(cell,attempts)
        evidence={'row':index,'original':cell,'attempts':attempts,'accepted':score is not None}
        attempts_log.append(evidence)
        if score is not None:
            result['rows'][index]={**cell,'confidence':score,'crop_corroboration':evidence}
            changes.append(index)
    result['crop_rebuild']={'version':VERSION,'source_page':page['page'],'changed_rows':changes,'attempts':attempts_log}
    return result


def readable_loss(check):
    """Display only. Raw fields remain available for audit and diagnosis."""
    details=[];pending=[]
    for f in (check.get('loss_condition_checks') or check.get('oven_condition_checks') or check.get('aging_condition_checks') or []):
        label=f['field'];v=f.get('reported')
        if '材料绑定' in label:
            component=label.replace('材料绑定','');prefix='PVC/' if component=='绝缘' else 'PVC/ST'
            values=[prefix+str(x) for x in (v or [])]
            if f['verdict']=='pass':details.append(component+'材料：'+'、'.join(values))
            else:pending.append(component+'材料类型'+('存在多个或不适用的识别结果（'+'、'.join(values)+'）' if values else '尚未可靠确认'))
        elif f['verdict']=='unknown':
            rows=check.get('local_loss_observations') or []
            reason=''
            oven_field={'老化后抗张强度':'tensile_strength','抗张强度变化率':'tensile_change',
                        '老化后断裂伸长率':'elongation','伸长率变化率':'elongation_change'}.get(label)
            oven=[r for r in check.get('local_oven_observations',[]) if r.get('measurement')==oven_field] if oven_field else []
            if len(oven)==1:
                r=oven[0];raw=str(r.get('reported') or '')
                if raw:
                    suffix=' N/mm²' if r.get('unit') in ('N/mm²','N/mm2','N/mm^2') else '%' if r.get('unit') in ('%','％') else ''
                    if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', raw.strip()):
                        reason='已读到 '+raw+suffix+'，但尚未完成原页交叉确认'
                    else:
                        # 坏读形态（如457的"9-%"）：不把不可靠原文当作读数展示。
                        reason='该位置识别结果不是可靠数值（'+raw+'），尚未完成原页交叉确认'
                    if str(r.get('reason','')).startswith('conflicting'):
                        reason+='（同一位置的识别结果不一致）'
                        alternatives = sorted({m['text'] for t in r.get('unit_resampling_trace', [])
                                               if t.get('unique_match') for m in t.get('matches', [])
                                               if m.get('confidence', 0) >= .95
                                               and m.get('text') in ('N/mm', 'N/mm²', 'N/mm2', 'mg/cm', 'mg/cm²', 'mg/cm2', '%')})
                        if alternatives:
                            reason += '；放大识别读为 ' + '、'.join(alternatives) + '，需确认单位及上标，不能自动补写'
                    elif r.get('reason')=='low_confidence_measurement':
                        reason+='（数值、负号或单位的识别证据不足）'
            elif label.endswith('变化率复算'):
                reason='老化前值、老化后值或报告变化率仍有未确认项，暂不能作出计算一致性结论'
            if len(rows)==1:
                row=rows[0]
                values=[v['text'] for v in row.get('reported_values',[]) if v.get('text')]
                if label=='失重实测值' and (values or row.get('reported')) and (not row.get('unit') or row.get('unit_validation') in {'low_confidence_measurement','conflicting_resampled_unit'}):
                    reason='已读到数值 '+ '、'.join(values or [str(row['reported'])])+'，但对应单位尚未可靠确认'
                    if row.get('unit_validation')=='conflicting_resampled_unit':reason+='（不同识别结果不一致）'
                elif label in {'失重温度','失重时间'}:
                    key='temperature' if label=='失重温度' else 'hours'
                    condition=(row.get('condition_observations') or {}).get('fields',{}).get(key,{})
                    hits=condition.get('observations') or []
                    if len(hits)==1 and hits[0].get('confidence',1)<.95:
                        reason='已识别到条件文字，但清晰度校验未通过，尚不能自动采信'
                if not reason and (row.get('result_column_binding') or {}).get('status')=='incomplete':
                    reason='结果列对应关系尚未确认，不能确定数值属于哪根线芯'
            pending.append(label+'：'+reason if reason else label+'尚未可靠确认')
        elif v is not None:
            vals=v if isinstance(v,list) else [v]
            fmt=lambda x:format(x,'g') if isinstance(x,(int,float)) else str(x)
            unit={'失重实测值':' mg/cm²','失重温度':' ℃','失重时间':' 小时',
                  '老化后抗张强度':' N/mm²','老化后断裂伸长率':'%',
                  '抗张强度变化率':'%','伸长率变化率':'%','老化温度':' ℃','老化时间':' 小时'}.get(label,'')
            details.append(label+'：'+'、'.join(fmt(x) for x in vals)+unit)
    pages='、'.join(map(str,check.get('source_pages',[])))
    return {'observed':'；'.join(details),'pending':'；'.join(pending),
            'next_action':('请核对原报告第'+pages+'页对应项目；这是识别证据未确认，不代表报告未填写。') if pending else '',
            'verdict':check.get('verdict')}


def present_material_check(check):
    """Presentation only; retain all structured evidence and verdicts."""
    display=readable_loss(check)
    check['reported_raw']=check.get('reported')
    if display['observed']:check['reported']=display['observed']
    elif display['pending']:check['reported']=display['pending']
    check['evidence_display']=display
    if check.get('verdict')=='manual_review' and display['pending']:
        check['deterministic_review_action']=display['pending']+'；请核对对应原页证据。'
    return check

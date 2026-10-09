"""Isolated coordinate-grounded table repair. Never imported by production.

The LLM selects field ownership; a spatial gate reconstructs accepted rows from
immutable source tokens. No gold values, standards, or calculated replacements
participate in adoption. This pilot supports single-specimen sheath tables only.
"""
import re
from mapping_shadow_core import number

VERSION = 'coordinate-repair-pilot-v3-bounded-block'
NP = {'np_strength':'after_strength','np_elongation':'after_elongation',
      'np_strength_change':'strength_change','np_elongation_change':'elongation_change'}

def center(block):
    return tuple(sum(p[i] for p in block['box'])/len(block['box']) for i in (0,1))

def compact(text):
    return re.sub(r'\s+', '', text).replace('²','2').replace('−','-')

def kind(text, phase):
    if '失重试验' in text:return 'loss'
    if '高温压力' in text and '压痕' in text:return 'pressure'
    metric='strength' if '抗张强度' in text else 'elongation' if '断裂伸长率' in text else None
    if metric is None:return None
    if '变化率' in text:base=metric+'_change'
    elif '中间值' in text:base=('before_' if phase=='before' else 'after_')+metric
    else:return None
    if phase=='np':return {v:k for k,v in NP.items()}.get(base)
    return base

def prepare(page):
    blocks=[dict(b,id=f'b{i}') for i,b in enumerate(page['rows'])]
    headers={}
    for name in ('检测项目','单位','标准要求','检验结果','评定'):
        hits=[b for b in blocks if compact(b['text'])==name]
        if len(hits)!=1:raise ValueError('ambiguous_column_header')
        headers[name]=center(hits[0])[0]
    xs=list(headers.values())
    if xs!=sorted(xs):raise ValueError('column_order')
    # Category text may be vertically split. Insulation/multiple tables rejected.
    category=''.join(b['text'].strip() for b in sorted(blocks,key=lambda b:center(b)[1])
                     if center(b)[0]<headers['检测项目']-100
                     and re.fullmatch(r'[护套绝缘机械性能]+',b['text'].strip()))
    if '护套机械性能' not in category or '绝缘' in category:raise ValueError('component_not_unique')
    labels={};phase=None
    for b in sorted(blocks,key=lambda b:center(b)[1]):
        if center(b)[0]>=headers['单位']-20:continue
        t=b['text']
        if '交货状态' in t:phase='before'
        if '空气烘箱' in t:phase='aging'
        if '非污染' in t:phase='np'
        k=kind(t,phase)
        if k:labels.setdefault(k,[]).append(b['id'])
    return blocks,headers,labels

def bounded_result_line(blocks,headers,label,result,key,tolerance):
    """Allow only two scalar trial blocks, never arbitrary rowspan or nearest value."""
    if key not in {'loss','pressure'}:return None
    y=center(label)[1];unit_x=headers['单位']
    ends=[b for b in blocks if center(b)[1]>y
          and max(p[0] for p in b['box'])<unit_x-15
          and re.match(r'^(?:低温|热冲击|热稳定|失重试验|高温压力|非污染|老化|曲挠|燃烧)',b['text'])]
    if not ends:return None
    end=min(ends,key=lambda b:center(b)[1]);bottom=min(p[1] for p in end['box'])
    # Explicit next-item boundary, short region, result strictly inside it.
    height=max(p[1] for p in label['box'])-min(p[1] for p in label['box'])
    if not y<center(result)[1]<bottom or bottom-y>8*height:return None
    lo=(headers['标准要求']+headers['检验结果'])/2
    hi=(headers['检验结果']+headers['评定'])/2
    region=[b for b in blocks if y-tolerance<=center(b)[1]<bottom]
    results=[b for b in region if lo<center(b)[0]<hi]
    if len(results)!=1 or results[0]['id']!=result['id']:return None
    # A genuine trial-condition line must occupy the label column between the
    # anchors. This is layout evidence, not inferred values/units/standard limits.
    conditions=[b for b in region if center(b)[0]<unit_x-20 and re.search(r'温度|时间',b['text'])]
    if not conditions:return None
    line=[b for b in region if abs(center(b)[1]-center(result)[1])<=tolerance]
    return line,{'kind':'unique_scalar_within_bounded_trial_block','start_id':label['id'],
                 'end_id':end['id'],'end_box':end['box'],'condition_ids':[b['id'] for b in conditions]}

def adopt(proposal,page,source_hash):
    if proposal.get('source_sha256')!=source_hash or proposal.get('page')!=page['page']:
        return {},{'_source':'source_identity_mismatch'}
    blocks,headers,labels=prepare(page);byid={b['id']:b for b in blocks}
    lo=(headers['标准要求']+headers['检验结果'])/2
    hi=(headers['检验结果']+headers['评定'])/2
    accepted={};rejected={};items=proposal.get('repairs',[])
    if not isinstance(items,list):return {},{'_schema':'invalid_repairs'}
    for k,expected_labels in labels.items():
        matches=[v for v in items if isinstance(v,dict) and v.get('item')==k]
        if len(matches)!=1:rejected[k]='missing_or_duplicate';continue
        v=matches[0]
        if v.get('status')!='candidate':rejected[k]='model_abstained';continue
        label=byid.get(v.get('label_id'));result=byid.get(v.get('result_id'))
        if len(expected_labels)!=1 or not label or label['id']!=expected_labels[0] or not result:
            rejected[k]='label_or_reference_mismatch';continue
        if v.get('raw_value')!=result['text']:
            rejected[k]='rewritten_value';continue
        value=number(compact(result['text']))
        if value is None:rejected[k]='ambiguous_numeric_token';continue
        y=center(label)[1]
        height=max(p[1] for p in label['box'])-min(p[1] for p in label['box'])
        tolerance=min(14,max(6,height*0.48))
        same=[b for b in blocks if abs(center(b)[1]-y)<=tolerance]
        results=[b for b in same if lo<center(b)[0]<hi]
        layout={'kind':'same_horizontal_line'}
        if len(results)!=1 or results[0]['id']!=result['id']:
            merged=bounded_result_line(blocks,headers,label,result,k,tolerance)
            if merged is None:
                rejected[k]='ambiguous_or_different_result_row';continue
            same,layout=merged
        unit_req=sorted([b for b in same if headers['单位']-50<center(b)[0]<lo],key=lambda b:center(b)[0])
        joined=''.join(compact(b['text']) for b in unit_req)
        joined=re.sub(r'^(mg/cm2|N/mm2|%)\|(?=最大|最小)',r'\1',joined)
        wanted='mg/cm2' if k=='loss' else 'N/mm2' if NP.get(k,k) in ('before_strength','after_strength') else '%'
        match=re.fullmatch(re.escape(wanted)+r'(最大|最小)(±?[+-]?\d+(?:\.\d+)?)',joined)
        verdicts=[b for b in same if center(b)[0]>hi and b['text'].strip() in ('P','F','N')]
        if not match or len(verdicts)!=1:
            rejected[k]='unit_requirement_or_verdict_unresolved';continue
        accepted[k]={'item':k,'label':label['text'],'unit':wanted.replace('2','²'),
            'requirement':''.join(match.groups()),'value':value,'raw_value':result['text'],
            'report_verdict':verdicts[0]['text'].strip(),'page':page['page'],'source_sha256':source_hash,
            'refs':[label['id']]+[b['id'] for b in unit_req]+[result['id'],verdicts[0]['id']],
            'boxes':{b['id']:b['box'] for b in [label,*unit_req,result,verdicts[0]]},
            'layout_proof':layout,'method':VERSION}
    return accepted,rejected

def normalized_view(accepted):
    lines=['护套机械性能'];last=None
    for key,row in accepted.items():
        phase='np' if key.startswith('np_') else 'aging' if key in ('after_strength','strength_change','after_elongation','elongation_change') else 'before' if key.startswith('before_') else 'other'
        if phase!=last:
            lines.append({'np':'非污染试验老化后的性能','aging':'空气烘箱老化后的性能','before':'交货状态原始性能','other':''}[phase]);last=phase
        lines.append('\t'.join([row['label'],row['unit'],row['requirement'],compact(row['raw_value']),row['report_verdict']]))
    return '\n'.join(lines)

def arithmetic(accepted):
    """Use candidate rulebase display-precision checks; never replace raw values."""
    from backend.app.rulebase import _display_change_overlap
    out=[]
    for pre,post,change in [('before_strength','after_strength','strength_change'),
                            ('before_elongation','after_elongation','elongation_change'),
                            ('before_strength','np_strength','np_strength_change'),
                            ('before_elongation','np_elongation','np_elongation_change')]:
        if change.startswith('np_') and not any(k.startswith('np_') for k in accepted):continue
        if not all(k in accepted for k in (pre,post,change)):
            out.append({'item':change,'status':'unknown_missing_evidence'});continue
        a,b,c=[accepted[k]['value'] for k in (pre,post,change)]
        if a==0:out.append({'item':change,'status':'unknown_zero_denominator'});continue
        calculated=(b-a)/a*100
        tokens=[compact(accepted[k]['raw_value']) for k in (pre,post,change)]
        resolution=10**(-len(tokens[2].split('.')[1])) if '.' in tokens[2] else 1
        status='consistent_at_display_precision' if abs(calculated-c)<resolution/2 else (
            'unresolved_rounding' if _display_change_overlap(tokens[0],tokens[1],c) else 'inconsistent')
        out.append({'item':change,'reported':c,'calculated':calculated,
                    'formula':f'({b:g}-{a:g})/{a:g}*100={calculated:.4f}%',
                    'status':status,'method':'candidate_display_precision_consistency'})
    return out

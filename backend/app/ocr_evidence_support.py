"""Offline trial: original visible PDF text and image OCR must agree at same box."""
import copy
import hashlib
import re

def canonical(text):
    text=re.sub(r'\s+', '', text).translate(str.maketrans({'²':'2','（':'(','）':')','：':':'})).replace('°℃','℃').replace('°C','℃')
    # A table separator between an explicit unit and requirement word is not
    # a sign/exponent/value. Never normalize bars elsewhere.
    return re.sub(r'^(mg/cm2|N/mm2|[%％])\|(?=最大|最小)',r'\1',text)

def supported(cell):
    proof=cell.get('native_support') or {}
    common=(proof.get('ocr_text')==cell.get('raw_text',cell.get('text')) and proof.get('box')==cell.get('box'))
    if proof.get('kind')=='same_pdf_crop_recognition_consensus':
        attempts=proof.get('attempts',[])
        return (common and len(attempts)==2 and {a.get('scale') for a in attempts}=={3,4}
                and len({a.get('padding',2) for a in attempts})==1
                and all(a.get('padding',2) in (0,2) for a in attempts)
                and not any(a.get('confidence',0)>=.95 and canonical(a.get('text',''))!=canonical(proof['ocr_text']) for a in proof.get('prior_attempts',[]))
                and all(a.get('confidence',0)>=.95 and canonical(a.get('text',''))==canonical(proof['ocr_text']) for a in attempts))
    return (proof.get('kind')=='same_pdf_visible_text_same_box'
            and proof.get('ocr_text')==cell.get('raw_text',cell.get('text'))
            and proof.get('box')==cell.get('box')
            and canonical(proof.get('native_text',''))==canonical(proof.get('ocr_text',''))
            and bool(proof.get('native_text')))

def corroborate(pdf_page, page, source_hash):
    import fitz
    result=copy.deepcopy(page)
    sx=pdf_page.rect.width/page['width'];sy=pdf_page.rect.height/page['height']
    traces=pdf_page.get_texttrace()
    for cell in result['rows']:
        if not .60<=cell['confidence']<.95:continue
        # This pilot only supplements units/conditions, never numeric result boxes.
        if not re.search(r'温度|时间|mg/',cell['text']):continue
        b=cell['box'];rect=fitz.Rect(min(v[0] for v in b)*sx-1,min(v[1] for v in b)*sy-1,
                                  max(v[0] for v in b)*sx+1,max(v[1] for v in b)*sy+1)
        chars=[];hidden=False
        for span in traces:
            for char in span['chars']:
                box=fitz.Rect(char[3]);center=(box.tl+box.br)/2
                if not rect.contains(center):continue
                if span.get('type')==3 or span.get('opacity',1)<=0:
                    hidden=True;continue
                chars.append((char[2][1],char[2][0],chr(char[0])))
        # PDF source order handles superscripts; exact equality forbids borrowing
        # nearby rows, different temperatures, missing decimals or other units.
        native=''.join(v[2] for v in chars)
        if hidden or canonical(native)!=canonical(cell['text']):continue
        cell['native_support']={'kind':'same_pdf_visible_text_same_box','source_sha256':source_hash,
                               'page':page['page'],'box':cell['box'],'ocr_text':cell['text'],'native_text':native}
    return result


def corroborate_crops(pdf_page,page,source_hash,engine,budget=16):
    """Recognize already located line directly; no detection re-splitting."""
    import fitz
    import numpy as np
    result=copy.deepcopy(page);logs=[]
    sx=pdf_page.rect.width/page['width'];sy=pdf_page.rect.height/page['height']
    for index,cell in enumerate(result['rows']):
        if len(logs)>=budget:break
        if supported(cell) or not .60<=cell['confidence']<.95:continue
        if not (re.search(r'温度|时间|mg\s*/|N\s*/|[%％]',cell['text']) or re.fullmatch(r'[+-]?\d+(?:\.\d+)?',canonical(cell['text']))):continue
        b=cell['box'];rect=fitz.Rect(min(v[0] for v in b)*sx-2,min(v[1] for v in b)*sy-2,
                                  max(v[0] for v in b)*sx+2,max(v[1] for v in b)*sy+2)&pdf_page.rect
        attempts=[]
        for scale in (3,4):
            pix=pdf_page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=rect,alpha=False)
            image=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,pix.n)
            rows,_=engine(image,use_det=False,use_cls=False)
            if not rows or len(rows)!=1 or len(rows[0])!=2 or not isinstance(rows[0][0],str):
                attempts.append({'scale':scale,'reason':'ambiguous_recognizer_output'});continue
            attempts.append({'scale':scale,'text':rows[0][0],'confidence':float(rows[0][1])})
        proof={'kind':'same_pdf_crop_recognition_consensus','source_sha256':source_hash,'page':page['page'],
               'box':cell['box'],'ocr_text':cell['text'],'attempts':attempts}
        proposal={**cell,'native_support':proof}
        accepted=supported(proposal)
        logs.append({'row':index,'original_text':cell['text'],'original_confidence':cell['confidence'],'attempts':attempts,'accepted':accepted})
        # Tight-box retry is restricted to explicit dimensional units. Do not
        # search crop variants until a desired numeric value/sign appears.
        if not accepted and re.fullmatch(r'(?:mg/cm[2²]|N/mm[2²]|[%％])',canonical(cell['text'])):
            tight=fitz.Rect(min(v[0] for v in b)*sx,min(v[1] for v in b)*sy,
                            max(v[0] for v in b)*sx,max(v[1] for v in b)*sy)&pdf_page.rect
            retry=[]
            for scale in (3,4):
                pix=pdf_page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=tight,alpha=False)
                im=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,pix.n)
                rr,_=engine(im,use_det=False,use_cls=False)
                if rr and len(rr)==1 and len(rr[0])==2 and isinstance(rr[0][0],str):
                    retry.append({'scale':scale,'padding':0,'text':rr[0][0],'confidence':float(rr[0][1])})
            # Any high-confidence contradictory unit remains unresolved.
            conflict=any(a.get('confidence',0)>=.95 and canonical(a.get('text',''))!=canonical(cell['text']) for a in attempts)
            alternate={**proof,'attempts':retry,'prior_attempts':attempts}
            if not conflict and supported({**cell,'native_support':alternate}):
                proof=alternate;accepted=True
            logs[-1].update(tight_retry=retry,accepted=accepted)
        if accepted:cell['native_support']=proof
    result['crop_trial_log']=logs
    return result

def evaluate(check,row,conditions):
    """Scoped loss-field replay, NOT the full production review pipeline."""
    result=copy.deepcopy(check)
    fields=result['loss_condition_checks']
    if any(f['verdict']!='pass' for f in fields if '材料绑定' in f['field']):return result
    for f in fields:
        if f['verdict']!='unknown':continue
        if f['field']=='失重实测值':
            binding=row.get('result_column_binding') or {}
            if (row.get('status')!='located' or row.get('unit') not in ['mg/cm2','mg/cm²']
                or row.get('report_verdict') not in ['P','F'] or binding.get('status')!='complete'
                or binding.get('expected_columns')!=1):continue
            if f.get('required')!='≤2.0mg/cm²':continue
            v=float(row['reported']);f['reported']=v
            f['verdict']='fail' if v>2 or row['report_verdict']=='F' else 'pass' if v>=0 else 'unknown'
        elif f['field'] in ['失重温度','失重时间']:
            key='temperature' if f['field']=='失重温度' else 'hours'
            hit=conditions.get('fields',{}).get(key,{})
            if hit.get('status')!='located':continue
            m=re.fullmatch(r'(\d+)±2℃' if key=='temperature' else r'(\d+)h',f['required'])
            if not m:continue
            v=hit['value'];expected=float(m[1]);f['reported']=v
            f['verdict']='pass' if (abs(v-expected)<=2 if key=='temperature' else v==expected) else 'fail'
    verdicts={f['verdict'] for f in fields}
    result['verdict']='fail' if 'fail' in verdicts or check['verdict']=='fail' else 'manual_review' if 'unknown' in verdicts else 'pass'
    return result


def enrich_verified_pages(receipt,pdf_path,engine_factory):
    """Ephemeral bounded proofs, bound to current file; never overwrite OCR."""
    from pathlib import Path
    import fitz
    source=Path(pdf_path)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    if digest!=receipt.get('identity',{}).get('source_sha256'):
        raise ValueError('supplemental source mismatch')
    clean=copy.deepcopy(receipt)
    def strip(page):
        page.pop('crop_trial_log',None)
        for cell in page.get('rows',[]):cell.pop('native_support',None)
        for key in ('unit_corroboration_pages','measurement_corroboration_pages'):
            for child in page.get(key,[]):strip(child)
    for page in clean['pages']:strip(page)
    enhanced=copy.deepcopy(clean);engine=None
    try:
        with fitz.open(source) as document:
            for i,page in enumerate(clean['pages']):
                actual=document[page['page']-1]
                page=corroborate(actual,page,digest)
                if any(.6<=c['confidence']<.95 and not supported(c) for c in page['rows']):
                    if engine is None:engine=engine_factory()
                    if engine is not None:page=corroborate_crops(actual,page,digest,engine)
                enhanced['pages'][i]=page
        if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:
            raise ValueError('source changed during supplemental recognition')
        enhanced['supplemental_schema']='same-source-unit-crop-v1'
        return enhanced
    except Exception:
        if hashlib.sha256(source.read_bytes()).hexdigest()!=digest:raise
        # Enhancement failure must preserve the existing conservative pipeline.
        clean['supplemental_status']='unavailable_original_evidence_preserved'
        return clean

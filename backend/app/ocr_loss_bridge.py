"""Strict supplemental HTML signatures; never an independent audit verdict.

Callers must bind the same original PDF, sample, component, physical page and
coordinate result columns. This module supplies only exact redundant evidence.
"""
import html
import re

NUMBER = re.compile(r'[+-]?\d+(?:\.\d+)?')

def is_loss_label(value):
    compact=re.sub(r'[\s—－-]','',str(value or ''))
    # OCR reads the separator as Chinese yi. Restrict to this exact item name;
    # never remove yi from arbitrary labels, values, sample names or units.
    return compact in ('失重试验','失重试验失重','失重试验一失重')

def plain(value):
    value=re.sub(r'<br\s*/?>',' ',value,flags=re.I)
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>',' ',value))).strip()

def canonical_unit(value):
    value=plain(value)
    # Allow explicit formatting wrappers only, not unknown symbols or exponents.
    value=re.sub(r'\\(?:mathrm|text)\{(mg|cm|N|mm)\}',r'\1',value)
    value=re.sub(r'[\s$]','',value)
    for stem,unit in [('mg/cm','mg/cm2'),('N/mm','N/mm2')]:
        if value.startswith(stem) and value[len(stem):] in ('2','²','^2','^{2}','^{{2}}'):
            return unit
    return '%' if value in ('%','％') else None

def loss_signatures(page_text, expected, component):
    """One data row or one explicit immediate condition continuation, never search.

    Reject result colspan/rowspan, duplicate labels, incomplete vectors and
    conflicting component labels. Values and signs remain literal.
    """
    if type(expected) is not int or not 1<=expected<=12:return []
    wanted='绝缘机械性能' if component=='insulation' else '护套机械性能'
    other='护套机械性能' if component=='insulation' else '绝缘机械性能'
    found=[]
    for table in re.findall(r'<table\b[^>]*>.*?</table>',page_text,re.I|re.S):
        compact=re.sub(r'\s+','',plain(table))
        if other in compact:continue
        rows=[]
        for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>',table,re.I|re.S):
            rows.append([(attrs,plain(body)) for attrs,body in re.findall(r'<t[dh]\b([^>]*)>(.*?)</t[dh]>',tr,re.I|re.S)])
        labels=[(i,j) for i,row in enumerate(rows) for j,(_,v) in enumerate(row)
                if is_loss_label(v)]
        if len(labels)!=1:continue
        i,j=labels[0];tail=rows[i][j+1:];kind='same_html_row'
        if not any(v for _,v in tail):
            if i+1>=len(rows):continue
            continuation=rows[i+1]
            nonempty=[k for k,(_,v) in enumerate(continuation) if v]
            if not nonempty:continue
            k=nonempty[0];label=continuation[k][1]
            # Exact condition-only line; not a different test or arbitrary prose.
            if not re.fullmatch(r'(?:试验条件\s*[:：]?\s*)?温度\s*[-+\d.±()（）\s℃°C]+',label):continue
            tail=continuation[k+1:];kind='immediate_condition_row'
        if len(tail)!=expected+3:continue
        if any(re.search(r'(?:colspan|rowspan)\s*=\s*["\x27]?([2-9]|\d{2,})',attrs,re.I) for attrs,_ in tail):continue
        unit=canonical_unit(tail[0][1]);requirement=re.fullmatch(r'最大\s*(\d+(?:\.\d+)?)',tail[1][1])
        values=[re.sub(r'\s+','',v) for _,v in tail[2:-1]]
        verdict=tail[-1][1].strip().upper()
        if (unit!='mg/cm2' or requirement is None or float(requirement[1])!=2.0
            or verdict not in ('P','F') or any(not NUMBER.fullmatch(v) for v in values)):continue
        found.append({'signature':(tuple(float(v) for v in values),unit,2.0,verdict),
                      'source':'ocr_exact_html_'+kind,'label_row':i,'data_row':i+(kind!='same_html_row'),
                      'component_explicit':wanted in compact})
    return found

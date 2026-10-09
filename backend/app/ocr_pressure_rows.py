"""Explicit one-label-per-row pressure conditions; never flatten merged tests."""
import html
import re


def plain(value):
    value = html.unescape(re.sub(r'<[^>]*>', '', value))
    value = re.sub(r'\$\s*\\pm\s*\$', '±', value)
    return re.sub(r'\s+', '', value).replace('°C', '℃').replace('°℃', '℃')


def pressure_condition_record(text):
    records = []
    for table in re.findall(r'<table\b[^>]*>.*?</table>', text, re.S | re.I):
        rows = [re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.S | re.I)
                for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S | re.I)]
        cells = [[plain(c) for c in row] for row in rows]
        starts = [i for i, row in enumerate(cells) if any(re.fullmatch(r'(?:护套)?高温压力[-—－]压痕深度[-—－]中间值', c) for c in row)]
        if len(starts) != 1: continue
        values = {}; lines = []; invalid = False
        # Retain the explicitly aligned result row together with its conditions.
        # Never borrow a limit/result from a neighbouring test or infer a unit.
        depth_row = list(cells[starts[0]])
        # Paddle may append empty layout columns. Only trailing empty cells
        # are removable; interior gaps or extra measurements stay ambiguous.
        while depth_row and not depth_row[-1]:
            depth_row.pop()
        raw_rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S | re.I)
        if (len(depth_row) == 5 and depth_row[1] == '%'
                and re.fullmatch(r'最大\d+(?:\.\d+)?', depth_row[2])
                and re.fullmatch(r'\d+(?:\.\d+)?', depth_row[3])
                and depth_row[4] in ('P', 'F')
                and not re.search(r'(?:rowspan|colspan)\s*=', raw_rows[starts[0]], re.I)):
            lines.append(' '.join(depth_row))
        for row in cells[starts[0]+1:]:
            nonempty = [c for c in row if c]
            if not nonempty: continue
            if re.search(r'低温|热冲击|热稳定|老化|失重|注[:：]', ''.join(nonempty)): break
            if len(nonempty) != 1:
                invalid = True; break
            line = nonempty[0]
            patterns = {'temperature': r'(?:试验条件[:：])?温度[（(]?([+-]?\d+(?:\.\d+)?)(?:±2)?[）)]?℃',
                        'hours': r'时间([+-]?\d+(?:\.\d+)?)h',
                        'force': r'施加(?:压力|荷载|负荷)[:：]?([+-]?\d+(?:\.\d+)?)N'}
            found = [(key, re.fullmatch(pattern, line)) for key, pattern in patterns.items()]
            found = [(key, match) for key, match in found if match]
            if len(found) != 1 or found[0][0] in values:
                invalid = True; break
            key, match = found[0]
            values[key] = float(match[1]); lines.append(line)
        if not invalid and set(values) == {'temperature', 'hours', 'force'}:
            records.append({'text': '\n'.join(lines), **values})
    return records[0] if len(records) == 1 else None

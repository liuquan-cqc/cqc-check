"""Structure-preserving plain rendering for PaddleOCR-VL HTML tables.

Raw HTML tables stay in the extracted text for the existing adapters
(bridge modules, identity comparisons, privacy preflight).  This module
renders an ADDITIONAL plain-line view after each ``</table>`` where
condition rows are explicitly attributed to their owning test item, so
text matchers do not have to infer ownership from adjacency alone.

The grid keeps shared cell objects for colspan/rowspan expansion, so
span repetitions collapse while legitimately repeated values in
adjacent columns (e.g. 无裂纹 | 无裂纹) are preserved.
"""
import re
from html.parser import HTMLParser

# 条件行以这些词开头（或含“条件”），且标准要求/检验结果/评定列均为空。
_CONDITION_START = re.compile(
    r'^(试验条件|试验温度|温度|时间|落锤|施加|载荷|老化|烘箱|加热|热处理)'
    r'|条件\s*[:：]',
)
_HEADER_CELLS = {
    'item': ('检测项目',),
    'unit': ('单位',),
    'required': ('标准要求',),
    'result': ('检验结果',),
    'verdict': ('单项评定', '评定'),
}


class _GridParser(HTMLParser):
    """Collect rows of cells; each cell is a dict shared by its spans."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            attrs = dict(attrs)
            colspan = str(attrs.get('colspan') or '1')
            rowspan = str(attrs.get('rowspan') or '1')
            self._cell = {
                'text': '',
                'colspan': int(colspan) if colspan.isdigit() else 1,
                'rowspan': int(rowspan) if rowspan.isdigit() else 1,
            }

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._cell is not None:
            self._cell['text'] = re.sub(r'\s+', ' ', self._cell['text']).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell['text'] += data


def _expand_grid(rows):
    """Expand colspan/rowspan into a rectangular grid of shared cell dicts."""
    grid = []
    pending = {}  # col -> [remaining_rows, cell]
    for row in rows:
        out = []
        c = 0
        index = 0
        while index < len(row) or c in pending:
            if c in pending:
                remaining, cell = pending[c]
                out.append(cell)
                remaining -= 1
                if remaining <= 0:
                    del pending[c]
                else:
                    pending[c] = [remaining, cell]
                c += 1
                continue
            cell = row[index]
            index += 1
            for _ in range(max(1, cell['colspan'])):
                out.append(cell)
                if cell['rowspan'] > 1:
                    pending[c] = [cell['rowspan'] - 1, cell]
                c += 1
        grid.append(out)
    while pending:
        last_col = max(pending)
        out = []
        for c in range(last_col + 1):
            if c in pending:
                remaining, cell = pending[c]
                out.append(cell)
                remaining -= 1
                if remaining <= 0:
                    del pending[c]
                else:
                    pending[c] = [remaining, cell]
            else:
                out.append(None)
        grid.append(out)
    return grid


def _text(cell):
    return cell['text'] if isinstance(cell, dict) else ''


def _find_header(grid):
    """Locate the standard header row; returns (row_index, colmap)."""
    for r, row in enumerate(grid):
        colmap = {}
        for c, cell in enumerate(row):
            text = _text(cell)
            for role, names in _HEADER_CELLS.items():
                if role not in colmap and text in names:
                    colmap[role] = c
        if 'item' in colmap and 'required' in colmap and 'result' in colmap:
            return r, colmap
    return None, None


def render_table_lines(html: str) -> list[str] | None:
    """Render a standard test table as structure-preserving plain lines.

    Returns None for tables without a recognizable standard header; the
    caller then leaves the original HTML untouched.
    """
    parser = _GridParser()
    try:
        parser.feed(html)
    except Exception:
        return None
    grid = _expand_grid(parser.rows)
    header_idx, colmap = _find_header(grid)
    if header_idx is None:
        return None
    header_width = len(grid[header_idx])
    item_col = colmap['item']
    value_cols = [colmap.get(role) for role in ('unit', 'required', 'result', 'verdict')]

    def shifted(row, col):
        # 表体可能比表头宽（检测项目格 colspan 更大），值列按宽度差平移；
        # item 列不位移：多余宽度正是来自它的 colspan。
        if col is None:
            return ''
        offset = max(0, len(row) - header_width)
        target = col if col == item_col else col + offset
        return _text(row[target]) if target < len(row) else ''

    def values_empty(row):
        return all(not shifted(row, col) for col in value_cols)

    def render_row(row):
        fields = []
        prev_id = None
        for cell in row:
            cid = id(cell) if isinstance(cell, dict) else None
            if cid is not None and cid == prev_id:
                fields.append('')  # 同一 colspan 格的重复位
            else:
                fields.append(_text(cell))
            prev_id = cid
        return ' | '.join(fields).rstrip(' |')

    lines = [render_row(grid[header_idx])]
    owner = None
    for r in range(header_idx + 1, len(grid)):
        row = grid[r]
        text = shifted(row, item_col)
        is_condition = (
            bool(text)
            and values_empty(row)
            and bool(_CONDITION_START.search(text))
            and owner is not None
        )
        if is_condition:
            lines.append(f'⟦条件→{owner}⟧ {text}')
            continue
        if text and not values_empty(row):
            owner = text
        lines.append(render_row(row))
    return lines


def annotate_html_tables(text: str) -> str:
    """Append a plain structure-preserving view after each standard table.

    The original ``<table>`` markup is preserved verbatim so existing
    adapters (bridges, identity comparisons, privacy preflight) continue
    to work unchanged.
    """
    def _replace(match):
        lines = render_table_lines(match.group(0))
        if not lines:
            return match.group(0)
        return match.group(0) + '\n' + '\n'.join(lines)

    return re.sub(r'<table\b[^>]*>.*?</table>', _replace, text or '', flags=re.I | re.S)

"""Diagnostic provenance only: never resolve a dimensional disagreement."""
import math


def _bounds(cell, width, height):
    width, height = float(width), float(height)
    if not math.isfinite(width + height) or min(width, height) <= 0:
        raise ValueError('invalid page dimensions')
    points = cell['box']
    if len(points) != 4:
        raise ValueError('invalid box')
    xs = [float(p[0]) / width for p in points]
    ys = [float(p[1]) / height for p in points]
    if not all(math.isfinite(v) for v in xs + ys):
        raise ValueError('invalid coordinate')
    return min(xs), min(ys), max(xs), max(ys)


def trace_unit_resampling(page, unit):
    """Repeat the existing geometric match, retaining all alternate readings."""
    try:
        target = _bounds(unit, page['width'], page['height'])
        base = (target[2] - target[0]) * (target[3] - target[1])
        if base <= 0:
            return []
        trace = []
        for alternate in page.get('unit_corroboration_pages') or []:
            if alternate.get('page') != page.get('page'):
                continue
            matches = []
            for index, cell in enumerate(alternate.get('rows') or []):
                bounds = _bounds(cell, alternate['width'], alternate['height'])
                area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                intersection = max(0, min(target[2], bounds[2]) - max(target[0], bounds[0])) * max(0, min(target[3], bounds[3]) - max(target[1], bounds[1]))
                if area > 0 and intersection / min(area, base) >= .7 and intersection / max(area, base) >= .5:
                    score = float(cell['confidence'])
                    if not math.isfinite(score):
                        continue
                    matches.append({'index': index, 'text': cell['text'], 'confidence': score,
                                    'normalized_box': list(bounds)})
            trace.append({'page': page['page'], 'render_scale': alternate.get('render_scale'),
                          'unique_match': len(matches) == 1, 'matches': matches})
        return trace
    except (KeyError, ValueError, TypeError, IndexError, OverflowError):
        return []

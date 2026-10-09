"""申请文字的规格段约束；只取各段的并集，不拼接跨段组合。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .parser import _clean_specification_fragment, SHAPE_WORDS


@dataclass(frozen=True)
class ScopeSegment:
    section_min: float | None
    section_max: float | None
    core_min: int | None
    core_max: int | None
    shapes: tuple[str, ...]
    special_expression: str = ""
    core_values: tuple[int, ...] = ()

    def contains(self, section, cores, shape):
        return (
            (self.section_min is None or section is not None and self.section_min <= section <= self.section_max)
            and (self.core_min is None or cores is not None and self.core_min <= cores <= self.core_max)
            and (not self.core_values or cores in self.core_values)
            and (not self.shapes or shape in self.shapes)
        )


def scope_segments(expression: str) -> list[ScopeSegment]:
    text = _clean_specification_fragment(expression)
    if not text:
        return []
    # 括号内的逗号连接芯数与形状，不能当作规格段分隔符。
    parts = []
    for fragment in re.split(r",(?![^()]*\))", text):
        if fragment.strip() in SHAPE_WORDS:
            if not parts:
                raise ValueError("形状备注缺少对应规格段，请把圆扁备注写在对应规格后")
            parts[-1] += " " + fragment.strip()
        else:
            parts.append(fragment)
    result = []
    for part in parts:
        compact = re.sub(r"\s+", "", part).replace("*", "×")
        if compact == "3×2×0.4+1×0.4":
            result.append(ScopeSegment(.4, .4, 7, 7, ("grouped",), compact))
            continue
        unequal = re.fullmatch(r"\((\d+)(?:-(\d+))?(?:芯)?\)×0\.75\+1×2(?:\.0)?", compact)
        if unequal:
            result.append(ScopeSegment(.75, .75, int(unequal[1])+1, int(unequal[2] or unequal[1])+1, ("special",), "unequal"))
            continue
        if "+" in part or re.search(r"\)\s*[×*]", part):
            raise ValueError(f"特殊结构尚无可靠解析规则：{part.strip()}，请转人工，不扩展为普通规格")
        shapes = tuple(shape for word, shape in SHAPE_WORDS.items() if word in part)
        cleaned = part.replace("任意规格一件", "").replace("任意规格", "")
        for word in SHAPE_WORDS:
            cleaned = cleaned.replace(word, "")
        core_match = re.search(r"\(([^()]*芯[^()]*)\)", cleaned)
        core_min = core_max = None
        special_expression = ""
        if core_match:
            core_text = core_match.group(1).strip().strip(" ,，")
            single_text = re.sub(r"芯$", "", core_text).strip()
            paired = re.fullmatch(r"(\d+)\s*[×*xX]\s*(\d+)", single_text)
            ranged = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", single_text)
            listed = re.fullmatch(r"\d+芯(?:\s*[,，、/\s]+\s*\d+芯)+", core_text)
            core_values = ()
            if paired:
                core_min = core_max = int(paired[1]) * int(paired[2])
                special_expression = f"{int(paired[1])}×{int(paired[2])}芯"
            elif ranged:
                core_min, core_max = int(ranged[1]), int(ranged[2] or ranged[1])
            elif listed:
                core_values = tuple(dict.fromkeys(int(value) for value in re.findall(r"\d+", core_text)))
                core_min, core_max = min(core_values), max(core_values)
            else:
                raise ValueError(f"芯数段无法可靠识别：{part.strip()}，请修改申请范围后重新识别")
            cleaned = cleaned[:core_match.start()] + cleaned[core_match.end():]
        else:
            core_values = ()
        cleaned = cleaned.strip(" ,()")
        section_min = section_max = None
        if cleaned:
            section_match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?", cleaned)
            if not section_match:
                raise ValueError(f"规格段无法可靠识别：{part.strip()}，请修改申请范围后重新识别")
            section_min, section_max = float(section_match[1]), float(section_match[2] or section_match[1])
        if section_min is not None or core_min is not None or shapes:
            result.append(ScopeSegment(section_min, section_max, core_min, core_max, shapes, special_expression, core_values))
    return result

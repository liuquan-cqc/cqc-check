"""按实际材料层分配覆盖项。不同层可搭配，同一层不可重复占用。"""
from copy import deepcopy
import re

LAYERS = {"copper_conductor": "conductor", "aluminum_conductor": "conductor",
          "insulation_normal": "insulation", "insulation_90": "insulation",
          "sheath_normal": "sheath", "sheath_90": "sheath", "outer_braid": "outer_braid"}
# J-70/JR-70软硬交叉使用为用户确认口径；未确认的牌号不自行推断。
BRAND_TYPES = {"J-70": {"PVC/C", "PVC/D"}, "JR-70": {"PVC/C", "PVC/D"},
               "J-90": {"PVC/E"}, "H-70": {"PVC/ST4"}, "HR-70": {"PVC/ST5"},
               "HII-90": {"PVC/ST10"}, "YG": {"YG"}}


def brands(group):
    supplied = group.get("material_brands") or []
    if supplied:
        return [str(b).upper().strip() for b in supplied]
    return re.findall(r"(?<![A-Z])(?:JR?-70|J-90|HII-90|HR?-70|PVC/[A-Z0-9]+|YG)(?![A-Z0-9])", str(group.get("group_code") or "").upper())


def eligible(sample, group, lookup):
    category = group.get("category")
    layer = LAYERS.get(category)
    if layer is None:
        return False
    model = lookup(sample["model_ref"])
    if group.get("model_refs") and sample["model_ref"] not in group["model_refs"]:
        return False
    if category in {"copper_conductor", "aluminum_conductor"}:
        return model.get("conductor") == category.removesuffix("_conductor")
    actual = model.get(layer)
    if not actual:
        return False
    if len(set(brands(group))) > 1:
        return False  # 多个牌号须拆成各自覆盖组，不能占用同一材料层冒充全部牌号。
    if category.endswith("_90") and model.get("temperature_class") != "90C":
        return False
    if category.endswith("_normal") and model.get("temperature_class") == "90C":
        return False
    for brand in brands(group):
        permitted = {brand} if brand.startswith("PVC/") else BRAND_TYPES.get(brand, set())
        if actual not in permitted:
            return False
    return True


def demands(groups):
    result = []
    for index, group in enumerate(groups):
        total, filed = int(group.get("total_items") or 0), int(group.get("cqc_filed_items") or 0)
        if not 0 <= filed <= total <= 999:
            raise ValueError("材料覆盖数量必须满足0≤备案数量≤总数≤999")
        for number in range(1, total - filed + 1):
            result.append((index, number))
    return result


def assignment(group_index, number, group):
    return {"group_index": group_index, "item_no": number, "group_code": group["group_code"],
            "category": group["category"], "layer": LAYERS.get(group["category"]), "material_brands": brands(group)}


def label(item, group):
    return f"{item['group_code']} 第{item['item_no']}/{int(group.get('total_items') or 0) - int(group.get('cqc_filed_items') or 0)}项"


def allocate(samples, groups, lookup, allow_add=True):
    result = deepcopy(samples)
    tokens = demands(groups)
    assignments = {}  # (样品序号, 材料层) -> (材料组序号, 覆盖项序号)
    unmet = []

    def place(token, visited):
        group = groups[token[0]]
        layer = LAYERS.get(group.get("category"))
        eligible_indexes = [i for i, sample in enumerate(result) if eligible(sample, group, lookup)]
        eligible_indexes.sort(key=lambda i: (i, layer) in assignments)
        for index in eligible_indexes:
            slot = (index, layer)
            if slot in visited:
                continue
            visited.add(slot)
            if slot not in assignments or place(assignments[slot], visited):
                assignments[slot] = token
                return True
        return False

    added_for = {}
    for token in tokens:
        if place(token, set()):
            continue
        group = groups[token[0]]
        compatible = [s for s in result if eligible(s, group, lookup)]
        if allow_add and compatible:
            # 优先复制可同时容纳其他材料类别的样品，不把新增层绑定在旧分配上。
            source = max(compatible, key=lambda s: sum(eligible(s, g, lookup) for g in groups))
            duplicate = deepcopy(source)
            duplicate["reasons"] = [*duplicate.get("reasons", []), f"材料层覆盖增样：{group['group_code']}"]
            duplicate["rule_refs"] = sorted({*duplicate.get("rule_refs", []), "COMMON-MATERIAL-COVERAGE-V1"})
            # 材料适用性基于该具体结构，不能复制成跨结构的任意规格范围。
            duplicate["any_spec"] = False
            result.append(duplicate)
            added_for[token[0]] = added_for.get(token[0], 0) + 1
            assert place(token, set())
        else:
            unmet.append(token)
    for sample in result:
        sample["material_coverage"] = []
        sample["material_assignments"] = []
    for (index, _), (group_index, number) in sorted(assignments.items()):
        item = assignment(group_index, number, groups[group_index])
        result[index]["material_assignments"].append(item)
        result[index]["material_coverage"].append(label(item, groups[group_index]))
        result[index]["any_spec"] = False
    analysis, warnings = [], []
    for index, group in enumerate(groups):
        required = int(group.get("total_items") or 0) - int(group.get("cqc_filed_items") or 0)
        missing = sum(t[0] == index for t in unmet)
        analysis.append({**group, "required_count": required, "assigned_count": required - missing,
                         "added_samples": added_for.get(index, 0), "status": "manual_review" if missing else "covered"})
        if missing:
            warnings.append(f"材料组“{group['group_code']}”缺少{missing}项适用材料层覆盖；请确认牌号、型号适用范围或增加合法样品，不能认定已覆盖。")
    return result, analysis, warnings, len(unmet)


def validate_assignments(samples, groups, lookup):
    expected = set(demands(groups))
    seen, gaps = set(), []
    for position, sample in enumerate(samples, 1):
        layers, labels = set(), []
        for item in sample.get("material_assignments") or []:
            token = (item.get("group_index"), item.get("item_no"))
            if token not in expected or token in seen:
                gaps.append(f"第{position}件材料覆盖项不存在或被重复分配")
                continue
            group = groups[token[0]]
            canonical = assignment(*token, group)
            if item != canonical or canonical["layer"] in layers or not eligible(sample, group, lookup):
                gaps.append(f"第{position}件材料层或牌号不适用，或同层重复占用")
                continue
            layers.add(canonical["layer"])
            seen.add(token)
            labels.append(label(canonical, group))
        if sorted(labels) != sorted(sample.get("material_coverage") or []):
            gaps.append(f"第{position}件材料显示文字与结构化分配不一致")
    if expected - seen:
        gaps.append(f"材料分配明细缺少{len(expected - seen)}项，请重新分配或重新生成方案")
    return gaps

"""配置驱动的确定性下样生成器。

规则文件负责描述型号和覆盖要求；本模块只实现通用 requirement kind，
不把“单元号等于某值”写进业务分支。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from itertools import combinations, product
from typing import Any, Callable, Iterable

from .rules import SamplingRuleError, SamplingRuleRepository, get_sampling_repository
from .scope import scope_segments
from . import materials


class SamplingError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    model_ref: str
    group_id: str
    section: float | None
    cores: int | None
    shape: str
    special_expression: str = ""

    @property
    def key(self) -> tuple[Any, ...]:
        return (self.model_ref, self.group_id, self.section, self.cores, self.shape, self.special_expression)


@dataclass
class AppliedModel:
    model: dict[str, Any]
    request: dict[str, Any]
    candidates: list[Candidate]
    section_sequence: list[float]
    core_sequence: list[int]


@dataclass
class CoverageTask:
    id: str
    kind: str
    rule_ref: str
    reason: str
    matches: Callable[[Candidate], bool]
    strict_matches: Callable[[Candidate], bool] | None = None
    target: dict[str, Any] = field(default_factory=dict)


SHAPE_TEXT = {"round": "圆形", "flat": "扁形", "twisted": "绞合", "parallel": "平行", "grouped": "成组"}
COLOR_TEXT = {"white": "白色", "black": "黑色"}


def _number(value: float | int | None) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _bounded_values(spec: dict[str, Any], low: float | int | None, high: float | int | None) -> list[Any]:
    if "values" in spec:
        values = list(spec["values"])
        if low is not None:
            values = [value for value in values if value >= low]
        if high is not None:
            values = [value for value in values if value <= high]
        return values
    minimum = spec.get("min") if low is None else max(spec.get("min", low), low)
    maximum = spec.get("max") if high is None else min(spec.get("max", high), high)
    if minimum is None or maximum is None or minimum > maximum:
        return []
    # 范围型来源没有完整档位时只生成真实申请边界，避免虚构中间规格。
    return [minimum] if minimum == maximum else [minimum, maximum]


class SamplingEngine:
    def __init__(self, repository: SamplingRuleRepository | None = None):
        self.repo = repository or get_sampling_repository()

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "fast")
        if mode not in {"fast", "merged"}:
            raise SamplingError("下样模式必须为 fast 或 merged")
        applications = payload.get("applications") or []
        if not applications:
            raise SamplingError("至少需要录入一个申请")
        application_numbers = {str(item.get("application_no") or "").strip() for item in applications}
        if mode == "fast" and len(application_numbers) != 1:
            raise SamplingError("快速模式只允许一个申请编号，但可包含多个产品单元")

        application_results = []
        all_samples: list[dict[str, Any]] = []
        rule_statuses: list[str] = []
        warnings: list[str] = []
        for index, application in enumerate(applications):
            result = self._generate_application(application, index)
            application_results.append(result)
            all_samples.extend(result["samples"])
            rule_statuses.append(result["rule_status"])
            warnings.extend(result["warnings"])

        material_analysis: list[dict[str, Any]] = []
        material_gaps = 0
        if mode == "merged":
            all_samples, material_analysis, material_warnings, material_gaps = self._apply_material_coverage(
                all_samples, payload.get("material_groups") or [], applications
            )
            warnings.extend(material_warnings)
        else:
            warnings.append("本方案未计算材料供应商数量导致的增样。")

        for position, sample in enumerate(all_samples, 1):
            sample["id"] = f"S{position:03d}"
        for index, app_result in enumerate(application_results):
            app_result["samples"] = [s for s in all_samples if s["application_index"] == index]
        verification = self.validate_manual_samples(payload, all_samples)
        material_gaps = verification["hard_gap_count"]
        warnings.extend(verification["gaps"])
        copy_text = self._copy_text(all_samples)
        draft_only = any(status != "enabled" for status in rule_statuses)
        if draft_only:
            warnings.insert(0, "当前单元规则均为老师审核前草案，结果仅供开发预览，不能最终确认。")
        warnings = list(dict.fromkeys(warnings))
        return {
            "rule_package_version": self.repo.version,
            "rule_status": "review_draft" if draft_only else "enabled",
            "validation_status": "has_gaps" if material_gaps else "draft_only" if draft_only else "valid",
            "final_confirmation_allowed": not draft_only and not material_gaps,
            "hard_gap_count": material_gaps,
            "gaps": verification["gaps"],
            "warnings": warnings,
            "applications": application_results,
            "samples": all_samples,
            "material_analysis": material_analysis,
            "copy_text": copy_text,
        }

    def validate_manual_samples(self, payload: dict[str, Any], samples: list[dict[str, Any]]) -> dict[str, Any]:
        """按申请分别验证结构，并校验颜色、材料层及显示文字；不覆盖原版本。"""
        applications = payload.get("applications") or []
        gaps, warnings = [], []
        prepared = {}
        for index, app in enumerate(applications):
            try:
                prepared[index] = self._prepare_application(app, index)
            except SamplingError as exc:
                gaps.append(str(exc))
        resolved = {index: [] for index in prepared}
        application_checks = []
        valid_samples = []
        for position, sample in enumerate(samples, 1):
            index = sample.get("application_index")
            if type(index) is not int or index not in prepared:
                gaps.append(f"第{position}件缺少有效申请归属")
                continue
            unit, applied, _, app_warnings = prepared[index]
            warnings.extend(app_warnings)
            if sample.get("application_no") != applications[index].get("application_no") or str(sample.get("unit_code")) != unit["unit_code"]:
                gaps.append(f"第{position}件申请编号或单元归属不一致")
                continue
            item = applied.get(sample.get("model_ref"))
            if not item:
                gaps.append(f"第{position}件型号未在该申请中申请")
                continue
            candidate = next((c for c in item.candidates if c.key == (
                sample.get("model_ref"), sample.get("group_id"), sample.get("section"),
                sample.get("cores"), sample.get("shape"), sample.get("special_expression") or ""
            )), None)
            if not candidate:
                gaps.append(f"第{position}件样品不在该申请合法规格组合内")
                continue
            if sample.get("model") != item.model["display_name"] or sample.get("voltage") != item.model["voltage"]:
                gaps.append(f"第{position}件型号名称或电压与目录不一致")
            if sample.get("color") not in {None, "", "white", "black"}:
                gaps.append(f"第{position}件颜色尚无自动规则，请人工确认")
            if sample.get("any_spec") and (sample.get("color") or sample.get("shape_explicit") or sample.get("material_assignments")):
                gaps.append(f"第{position}件设定颜色、圆扁或材料层后必须指定具体规格")
            canonical = self._specification_text(sample, item)
            if sample.get("specification") and sample["specification"] != canonical:
                gaps.append(f"第{position}件复制文字与结构化规格不一致")
            resolved[index].append((candidate, sample, item))
            valid_samples.append(sample)
        for index, (unit, applied, tasks, _) in prepared.items():
            rows = resolved[index]
            task_checks = []
            if not rows:
                gaps.append(f"申请{applications[index].get('application_no')}单元{unit['unit_code']}没有合法样品")
            for task in tasks:
                # 任意规格不是内部选定的单个候选：全范围都可满足任务才算覆盖。
                covered = any(all(task.matches(c) for c in item.candidates) if s.get("any_spec") else task.matches(c)
                              for c, s, item in rows)
                task_checks.append({"id": task.id, "kind": task.kind, "rule_ref": task.rule_ref, "reason": task.reason, "target": task.target, "covered": covered})
                if not covered:
                    gaps.append(f"申请{applications[index].get('application_no')}未覆盖：{task.reason}")
            if self._needs_color_pair(applied, unit):
                colors = {s.get("color") for _, s, item in rows if item.model.get("series") == "GB/T 5023"}
                if not {"white", "black"}.issubset(colors):
                    gaps.append(f"申请{applications[index].get('application_no')}单元{unit['unit_code']}缺少GB/T 5023一黑一白")
            application_checks.append({"application_no": applications[index].get("application_no"), "unit_code": unit["unit_code"],
                                       "unit_name": unit["unit_name"], "rule_status": unit["status"], "samples": [s for _, s, _ in rows], "coverage_tasks": task_checks})
        groups = payload.get("material_groups") or [] if payload.get("mode") == "merged" else []
        material_analysis = []
        gaps.extend(materials.validate_assignments(valid_samples, groups, self.repo.model))
        if groups:
            _, material_analysis, material_warnings, missing = materials.allocate(valid_samples, groups, self.repo.model, allow_add=False)
            if missing:
                gaps.extend(material_warnings)
        gaps = list(dict.fromkeys(gaps))
        return {
            "hard_gap_count": len(gaps),
            "validation_status": "has_gaps" if gaps else "draft_only",
            "final_confirmation_allowed": False,
            "gaps": gaps,
            "applications": application_checks,
            "material_analysis": material_analysis,
            "warnings": list(dict.fromkeys(warnings + ["规则仍处于审核稿，人工修改版本也不能最终确认。"])),
        }

    def normalize_manual_samples(self, payload, samples, previous_samples):
        """页面空白复制文字由后端重建；未修改的任意规格样品保持原范围。"""
        prepared = {i: self._prepare_application(app, i)[1] for i, app in enumerate(payload.get("applications") or [])}
        previous = {s.get("id"): s for s in previous_samples if s.get("id")}
        result = []
        dimensions = ("application_index", "application_no", "unit_code", "model_ref", "section", "cores", "shape", "color")
        for position, source in enumerate(samples, 1):
            sample = deepcopy(source)
            item = prepared.get(sample.get("application_index"), {}).get(sample.get("model_ref"))
            if item:
                old = previous.get(sample.get("id"))
                if old and sample.get("shape") != old.get("shape") and sample.get("shape") in {"round", "flat"}:
                    sample["shape_explicit"] = True
                if not sample.get("specification"):
                    if old and all(sample.get(k) == old.get(k) for k in dimensions):
                        sample["any_spec"] = bool(old.get("any_spec"))
                    candidates = [c for c in item.candidates if c.section == sample.get("section") and c.cores == sample.get("cores") and c.shape == sample.get("shape")]
                    if candidates:
                        chosen = next((c for c in candidates if c.group_id == sample.get("group_id")), candidates[0])
                        sample.update(group_id=chosen.group_id, special_expression=chosen.special_expression)
                if sample.get("color") or sample.get("shape_explicit") or sample.get("material_assignments"):
                    sample["any_spec"] = False
                sample.update(model=item.model["display_name"], voltage=item.model["voltage"])
                sample["specification"] = self._specification_text(sample, item)
            sample["id"] = f"S{position:03d}"
            result.append(sample)
        groups = (payload.get("material_groups") or []) if payload.get("mode") == "merged" else []
        if groups and all(sample.get("model_ref") in prepared.get(sample.get("application_index"), {}) for sample in result):
            result, _, _, _ = materials.allocate(result, groups, self.repo.model, allow_add=False)
            for sample in result:
                sample["specification"] = self._specification_text(sample, prepared[sample["application_index"]][sample["model_ref"]])
        return result

    def copy_text_for_samples(self, samples: list[dict[str, Any]]) -> str:
        """供人工版本保存时复用固定输出模板。"""
        return self._copy_text(samples)

    def _generate_application(self, application: dict[str, Any], index: int) -> dict[str, Any]:
        unit, applied, tasks, warnings = self._prepare_application(application, index)
        # 先按准确端点/组合端点求解，防止优化器主动制造中间档来同时替代两个边界。
        selected, _ = self._solve(applied, tasks, unit, strict_boundaries=True)
        selected = self._ensure_color_pair(selected, applied, unit)
        # 只有已经因型号、结构、温度、圆扁或颜色等要求入选的样品，才可按相邻档
        # 顺带承接边界并删除真正多余的端点样品；本阶段只做删除，不引入中间档。
        selected = self._relax_redundant_boundaries(selected, applied, tasks, unit)
        coverage = self._assigned_coverage_map(selected, tasks, applied, unit)
        samples = self._serialize_samples(selected, coverage, applied, unit, application, index)
        return {
            "application_no": application.get("application_no") or f"未编号申请{index + 1}",
            "unit_code": unit["unit_code"],
            "unit_name": unit["unit_name"],
            "rule_status": unit["status"],
            "samples": samples,
            "coverage_tasks": [self._task_out(task, coverage) for task in tasks],
            "warnings": warnings,
        }

    def _prepare_application(self, application: dict[str, Any], index: int):
        from .parser import unsupported_scope_conditions
        unsupported = unsupported_scope_conditions(str(application.get("raw_scope_text") or ""))
        if unsupported:
            raise SamplingError("；".join(unsupported))
        code = str(application.get("unit_code") or "").zfill(2)
        try:
            unit = self.repo.unit(code)
        except SamplingRuleError as exc:
            raise SamplingError(str(exc)) from exc
        model_inputs = application.get("models") or []
        if not model_inputs:
            raise SamplingError(f"申请{index + 1}未选择型号")
        applied: dict[str, AppliedModel] = {}
        for model_input in model_inputs:
            if model_input.get("unsupported_conditions"):
                raise SamplingError("；".join(model_input["unsupported_conditions"]))
            ref = str(model_input.get("model_ref") or "")
            try:
                model = self.repo.model(ref)
            except SamplingRuleError as exc:
                raise SamplingError(str(exc)) from exc
            if str(model["unit_code"]).zfill(2) != code:
                raise SamplingError(f"型号{model['display_name']}不属于单元{code}")
            if ref in applied:
                raise SamplingError(f"同一申请中型号重复：{model['display_name']}")
            candidates, sections, cores = self._model_candidates(model, model_input)
            if not candidates:
                raise SamplingError(f"型号{model['display_name']}的申请范围没有合法规格")
            applied[ref] = AppliedModel(model, model_input, candidates, sections, cores)
        tasks = self._build_tasks(unit, applied)
        if not tasks:
            raise SamplingError("当前申请没有形成可执行覆盖任务，请转人工确认")
        warnings = []
        if unit.get("open_questions"):
            warnings.append("该单元仍有未关闭的审核问题，未覆盖案例的申请应转人工复核。")
        return unit, applied, tasks, warnings

    def _model_candidates(self, model: dict[str, Any], request: dict[str, Any]):
        candidates = []
        try:
            segments = scope_segments(str(request.get("scope_expression") or ""))
        except ValueError as exc:
            raise SamplingError(f"{model['display_name']}：{exc}") from exc
        requested_shapes = set(request.get("shapes") or [])
        for group in model.get("specification_groups", []):
            special = str(group.get("special_expression") or "")
            if special and not request.get("include_special") and len(model["specification_groups"]) > 1:
                continue
            for segment in segments or [None]:
                if segment:
                    expected_special = segment.special_expression
                    if bool(expected_special) != bool(special):
                        continue
                    if expected_special and expected_special != special and not (expected_special == "unequal" and "0.75+1" in special):
                        continue
                bounds = {}
                for dimension, low_name, high_name in (("sections", "section_min", "section_max"), ("cores", "core_min", "core_max")):
                    lows = [v for v in [request.get(low_name), getattr(segment, low_name, None)] if v is not None]
                    highs = [v for v in [request.get(high_name), getattr(segment, high_name, None)] if v is not None]
                    low, high = max(lows) if lows else None, min(highs) if highs else None
                    values = _bounded_values(group[dimension], low, high)
                    if not values and group[dimension].get("unspecified") and low is None and high is None:
                        values = [None]
                    bounds[dimension] = values
                shapes = [s for s in group.get("shapes", []) if not requested_shapes or s in requested_shapes]
                for section, cores, shape in product(bounds["sections"], bounds["cores"], shapes):
                    if segment and not segment.contains(section, cores, shape):
                        continue
                    expression = special
                    if special and "0.75+1" in special:
                        expression = f"{int(cores)-1}×0.75+1×2.0"
                    candidates.append(Candidate(model["id"], group["id"], section, int(cores) if cores is not None else None, shape, expression))
        rows = sorted(set(candidates), key=lambda c: str(c.key))
        return rows, sorted({float(c.section) for c in rows if c.section is not None}), sorted({int(c.cores) for c in rows if c.cores is not None})

    def _build_tasks(self, unit: dict[str, Any], applied: dict[str, AppliedModel]) -> list[CoverageTask]:
        tasks: list[CoverageTask] = []
        handlers = {
            "per_model_section_endpoints": self._tasks_model_section_endpoints,
            "exact_corner_pair": self._tasks_corner_pair,
            "corner_pair": self._tasks_corner_pair,
            "compatible_corner_pair": self._tasks_corner_pair,
            "unit_section_endpoints": self._tasks_scope_section_endpoints,
            "subunit_section_endpoints": self._tasks_scope_section_endpoints,
            "attribute_presence": self._tasks_attribute_presence,
            "temperature_presence": self._tasks_temperature_presence,
            "subunit_representation": self._tasks_subunit_representation,
            "model_presence": self._tasks_model_presence,
            "exact_model_sample": self._tasks_exact_model_sample,
            "model_set_presence": self._tasks_model_set_presence,
            "shape_pair": self._tasks_shape_pair,
            "series_presence": self._tasks_series_presence,
            "series_sheathed_presence": self._tasks_series_sheathed_presence,
            "screening_group_presence": self._tasks_screening_groups,
            "unsheathed_representation": self._tasks_unsheathed,
            "selection_preference": self._tasks_selection_preference,
            "temperature_distribution": self._tasks_selection_preference,
        }
        for requirement in unit.get("requirements", []):
            if requirement.get("status") not in {"review_draft", "approved", "enabled"}:
                continue
            if not self._condition_applies(requirement.get("when") or {}, applied, unit):
                continue
            handler = handlers.get(requirement["kind"])
            if handler:
                tasks.extend(handler(requirement, applied, unit))
        # 同一规则和目标只保留一条，排序固定。
        unique = {task.id: task for task in tasks}
        return [unique[key] for key in sorted(unique)]

    def _condition_applies(self, when: dict[str, Any], applied: dict[str, AppliedModel], unit: dict[str, Any]) -> bool:
        refs = set(applied)
        models = [item.model for item in applied.values()]
        if when.get("model_applied") and when["model_applied"] not in refs:
            return False
        if when.get("all_models") and not set(when["all_models"]).issubset(refs):
            return False
        if when.get("any_models") and not refs.intersection(when["any_models"]):
            return False
        if when.get("subunit_applied") and not any(model.get("subunit") == when["subunit_applied"] for model in models):
            return False
        if when.get("subunits_applied") and not all(any(model.get("subunit") == value for model in models) for value in when["subunits_applied"]):
            return False
        if when.get("none_subunits_applied") and any(
            model.get("subunit") in set(when["none_subunits_applied"])
            for model in models
        ):
            return False
        if when.get("unit_applied") and str(when["unit_applied"]).zfill(2) != str(unit["unit_code"]).zfill(2):
            return False
        if when.get("any_standard_family") and not any(model.get("series") == when["any_standard_family"] for model in models):
            return False
        if when.get("any_temperature_class") and not any(model.get("temperature_class") == when["any_temperature_class"] for model in models):
            return False
        if when.get("any_unsheathed_model") and not any(not model.get("sheath") for model in models):
            return False
        if when.get("any_sheathed_model") and not any(model.get("sheath") for model in models):
            return False
        if when.get("any_screened_model") and not any(model.get("screening_group") for model in models):
            return False
        if when.get("series_contains_sheathed_model") and not any(model.get("sheath") for model in models):
            return False
        if when.get("series_applied") and not any(model.get("series") in when["series_applied"] for model in models):
            return False
        if when.get("contains_both"):
            subunit = when.get("subunit")
            scoped_items = [item for item in applied.values() if not subunit or item.model.get("subunit") == subunit]
            scoped = [item.model for item in scoped_items]
            values = set()
            for model in scoped:
                values.update({model.get("conductor_flexibility"), model.get("conductor")})
            for item in scoped_items:
                values.update(candidate.shape for candidate in item.candidates)
            if not set(when["contains_both"]).issubset(values):
                return False
        return True

    def _task(self, requirement, suffix, matcher, reason_suffix="", target=None, strict_matcher=None):
        return CoverageTask(
            id=f"{requirement['id']}:{suffix}", kind=requirement["kind"], rule_ref=requirement["id"],
            reason=requirement["reason"] + reason_suffix, matches=matcher,
            strict_matches=strict_matcher, target=target or {},
        )

    def _scope_refs(self, requirement, applied, unit) -> set[str]:
        params = requirement.get("parameters") or {}
        when = requirement.get("when") or {}
        if params.get("models"):
            refs = set(params["models"]).intersection(applied)
        elif params.get("model_ref"):
            refs = {params["model_ref"]}.intersection(applied)
        else:
            subunit = when.get("subunit") or when.get("subunit_applied")
            if subunit:
                refs = {ref for ref, item in applied.items() if item.model.get("subunit") == subunit}
            else:
                refs = set(applied)
        # 某些组合端点只允许在特定结构内比较。例如单元6的“有护套
        # 组合端点”不能把AV/AVR等无护套1芯产品纳入最小芯数计算。
        if params.get("sheathed_only"):
            refs = {ref for ref in refs if bool(applied[ref].model.get("sheath"))}
        if params.get("unsheathed_only"):
            refs = {ref for ref in refs if not applied[ref].model.get("sheath")}
        return refs

    def _tasks_model_section_endpoints(self, req, applied, unit):
        ref = req["parameters"]["model_ref"]
        item = applied.get(ref)
        if not item:
            return []
        tasks = []
        for label, value in (("min", min(item.section_sequence)), ("max", max(item.section_sequence))):
            tasks.append(self._task(req, label, lambda c, r=ref, v=value: c.model_ref == r and c.section == v,
                                   f"（{label}={_number(value)}mm²）", {"section": value, "endpoint": label}))
        return tasks

    def _corner_values(self, refs: set[str], applied: dict[str, AppliedModel]):
        candidates = [candidate for ref in refs for candidate in applied[ref].candidates if candidate.section is not None and candidate.cores is not None]
        max_core = max(candidate.cores for candidate in candidates)
        min_core = min(candidate.cores for candidate in candidates)
        small_corner = min((candidate for candidate in candidates if candidate.cores == max_core), key=lambda c: c.section)
        large_corner = max((candidate for candidate in candidates if candidate.cores == min_core), key=lambda c: c.section)
        return small_corner, large_corner

    def _tasks_corner_pair(self, req, applied, unit):
        refs = self._scope_refs(req, applied, unit)
        if not refs:
            return []
        split_tasks = (
            self._tasks_split_corner_dimensions(req, refs, applied, unit)
            if req.get("parameters", {}).get("split_dimensions")
            else []
        )
        small, large = self._corner_values(refs, applied)
        missing_section_tasks = []
        if req.get("parameters", {}).get("split_if_incompatible") and not split_tasks:
            # 锁定最多/最少芯数之后取截面，只得到两个合法组合端点，
            # 不保证覆盖该规则范围的截面极值。例如2芯最大2.5，
            # 但5芯还申请了6；必须保留原组合端点并补足6的覆盖。
            # 仅补两个角点之外的截面任务，复用既有准确选样及合法承接；
            # 不扩大到其他子单元，也不改写单元6先锁芯数的确认口径。
            corner_min = min(small.section, large.section)
            corner_max = max(small.section, large.section)
            missing_section_tasks = [
                task for task in self._tasks_split_corner_dimensions(req, refs, applied, unit)
                if task.target.get("dimension") == "section"
                and (task.target["section"] < corner_min or task.target["section"] > corner_max)
            ]
        exact = req["kind"] == "exact_corner_pair"
        directed_sources = {
            source["from"] for rule in unit.get("requirements", []) if rule["kind"] == "directed_representation"
            for source in rule.get("parameters", {}).get("allowed", [])
        }
        if any(
            rule["kind"] == "directed_representation"
            and rule.get("parameters", {}).get("direction") == "2.1_to_2.2_only"
            for rule in unit.get("requirements", [])
        ):
            directed_sources.update(ref for ref, item in applied.items() if item.model.get("subunit") == "2.1")
        if req["kind"] == "corner_pair" and (req.get("when") or {}).get("subunit_applied"):
            allowed_refs = refs | (directed_sources & set(applied))
        else:
            allowed_refs = refs

        required_preferences = req.get("parameters", {}).get("required_corner_model_refs_when_applied") or {}

        def matchers(target: Candidate, corner: str):
            def base_matches(candidate: Candidate) -> bool:
                return (
                    candidate.model_ref in allowed_refs
                    and (
                        (exact and candidate.model_ref == target.model_ref and candidate.section == target.section and candidate.cores == target.cores)
                        or (
                            not exact
                            and (
                                self._directed_corner_carries(candidate, target, corner, applied)
                                if candidate.model_ref in directed_sources
                                else self._carries(candidate, target, applied)
                            )
                        )
                    )
                )

            def strict_base_matches(candidate: Candidate) -> bool:
                return (
                    candidate.model_ref in allowed_refs
                    and candidate.section == target.section
                    and candidate.cores == target.cores
                    and (not exact or candidate.model_ref == target.model_ref)
                )

            preferred_refs = set(required_preferences.get(corner) or []).intersection(allowed_refs)
            capable_preferred_refs = {
                ref for ref in preferred_refs
                if any(base_matches(candidate) for candidate in applied[ref].candidates)
            }
            task_refs = capable_preferred_refs or allowed_refs
            strict_capable_preferred_refs = {
                ref for ref in preferred_refs
                if any(strict_base_matches(candidate) for candidate in applied[ref].candidates)
            }
            strict_task_refs = strict_capable_preferred_refs or allowed_refs
            return (
                lambda c: c.model_ref in task_refs and base_matches(c),
                lambda c: c.model_ref in strict_task_refs and strict_base_matches(c),
            )
        preferences = req.get("parameters", {}).get("corner_model_preferences") or {}
        small_matcher, small_strict_matcher = matchers(small, "max_cores_min_section")
        large_matcher, large_strict_matcher = matchers(large, "min_cores_max_section")
        for task in missing_section_tasks:
            # 原有准确组合端点本就必须送，可以顺带承接相邻截面极值。
            # 例如已必送0.75（41芯）可承接0.5，不能为新增检查机械增样；
            # 只允许准确角点或准确截面，不允许凭空挑中间档压缩样品数。
            exact_section = task.strict_matches
            task.strict_matches = lambda c, exact=exact_section, carries=task.matches: (
                exact(c) or (carries(c) and (small_strict_matcher(c) or large_strict_matcher(c)))
            )
        return [
            self._task(req, "max-cores-min-section", small_matcher,
                       f"（最大芯数{small.cores}芯、最小截面{_number(small.section)}mm²）",
                       {"cores": small.cores, "section": small.section, "corner": "max_cores_min_section", "preferred_model_refs": preferences.get("max_cores_min_section", [])},
                       strict_matcher=small_strict_matcher),
            self._task(req, "min-cores-max-section", large_matcher,
                       f"（最小芯数{large.cores}芯、最大截面{_number(large.section)}mm²）",
                       {"cores": large.cores, "section": large.section, "corner": "min_cores_max_section", "preferred_model_refs": preferences.get("min_cores_max_section", [])},
                       strict_matcher=large_strict_matcher),
        ] + split_tasks + missing_section_tasks

    def _tasks_split_corner_dimensions(self, req, refs, applied, unit):
        """将无法保证合法组合的两个角点拆成四个可合并尺寸任务。

        例如2.2同时申请BVV 0.75-185（1芯）和扁形2.5-10
        （2-3芯）时，不存在0.75（3芯）这一合法组合。此时必须分别
        覆盖最小截面0.75、最大芯数3、最大截面185和最小芯数1，
        再让合法样品尽量合并承担，而不能静默把最小截面抬到2.5。
        """
        candidates = [
            candidate for ref in refs for candidate in applied[ref].candidates
            if candidate.section is not None and candidate.cores is not None
        ]
        if not candidates:
            raise SamplingError(f"规则{req['id']}在实际申请范围内没有合法尺寸候选，请转人工确认")

        directed_sources = {
            ref for rule in unit.get("requirements", [])
            if rule.get("kind") == "directed_representation"
            and rule.get("parameters", {}).get("direction") == "2.1_to_2.2_only"
            for ref, item in applied.items()
            if item.model.get("subunit") == "2.1"
        }
        allowed_refs = refs | directed_sources
        dimensions = (
            ("min-section", "section", min(candidate.section for candidate in candidates), "min"),
            ("max-section", "section", max(candidate.section for candidate in candidates), "max"),
            ("min-cores", "cores", min(candidate.cores for candidate in candidates), "min"),
            ("max-cores", "cores", max(candidate.cores for candidate in candidates), "max"),
        )

        def relaxed_matches(candidate: Candidate, field: str, value, endpoint: str) -> bool:
            if candidate.model_ref not in allowed_refs:
                return False
            if candidate.model_ref in directed_sources:
                if field == "section":
                    if candidate.section is None or (value <= 10 and candidate.section > 10):
                        return False
                    targets = [item for item in candidates if item.section == value]
                    if not any(self._carries(candidate, target, applied, section_only=True) for target in targets):
                        return False
                    return candidate.section <= value if endpoint == "min" else candidate.section >= value
                if candidate.cores is None:
                    return False
                return candidate.cores <= value if endpoint == "min" else candidate.cores >= value
            if field == "section":
                target_rows = [item for item in candidates if item.section == value]
                return any(self._carries(candidate, target, applied, section_only=True) for target in target_rows)
            if candidate.cores is None:
                return False
            return any(self._standard_adjacent(candidate.cores, value, applied[t.model_ref].model, "cores", applied[candidate.model_ref].model)
                       for t in candidates if t.cores == value)

        tasks = []
        for suffix, field, value, endpoint in dimensions:
            label = "截面" if field == "section" else "芯数"
            unit_text = "mm²" if field == "section" else "芯"
            tasks.append(self._task(
                req,
                suffix,
                lambda c, f=field, v=value, e=endpoint: relaxed_matches(c, f, v, e),
                f"（{endpoint} {label}={_number(value)}{unit_text}）",
                {field: value, "endpoint": endpoint, "dimension": field},
                strict_matcher=lambda c, f=field, v=value: c.model_ref in refs and getattr(c, f) == v,
            ))
        return tasks

    def _tasks_scope_section_endpoints(self, req, applied, unit):
        refs = self._scope_refs(req, applied, unit)
        if not refs:
            return []
        if req.get("parameters", {}).get("endpoint_scope") == "unit":
            refs = set(applied)
        allowed_refs = set(applied) if req.get("parameters", {}).get("may_be_carried_by_unit_samples") else refs
        candidates = [candidate for ref in refs for candidate in applied[ref].candidates if candidate.section is not None]
        values = [candidate.section for candidate in candidates]
        tasks = []
        for label, value in (("min", min(values)), ("max", max(values))):
            target_candidates = [candidate for candidate in candidates if candidate.section == value]
            relaxed_matcher = lambda c, targets=target_candidates: c.model_ref in allowed_refs and any(self._carries(c, target, applied, section_only=True) for target in targets)
            strict_matcher = lambda c, v=value: c.model_ref in allowed_refs and c.section == v
            tasks.append(self._task(
                req, label,
                relaxed_matcher,
                f"（{label}={_number(value)}mm²）", {"section": value, "endpoint": label, "preferred_model_refs": req.get("parameters", {}).get(f"{label}_model_refs", [])},
                strict_matcher=strict_matcher,
            ))
        return tasks

    def _tasks_attribute_presence(self, req, applied, unit):
        attr = req["parameters"]["attribute"]
        subunit = (req.get("when") or {}).get("subunit")
        carrying_subunits = set(req.get("parameters", {}).get("may_be_carried_by_subunit_samples") or [])
        tasks = []
        for value in req["parameters"]["required_values"]:
            tasks.append(self._task(req, str(value), lambda c, a=attr, v=value, s=subunit: (
                applied[c.model_ref].model.get(a) == v
                and (
                    not s
                    or applied[c.model_ref].model.get("subunit") == s
                    or applied[c.model_ref].model.get("subunit") in carrying_subunits
                )
            ), f"（{value}）", {"attribute": attr, "value": value}))
        return tasks

    def _tasks_temperature_presence(self, req, applied, unit):
        temp = req["parameters"]["temperature_class"]
        preferred_series = req.get("parameters", {}).get("preferred_series") or []
        return [self._task(req, temp, lambda c: applied[c.model_ref].model.get("temperature_class") == temp,
                           f"（{temp}）", {"temperature_class": temp, "preferred_series": preferred_series})]

    def _tasks_subunit_representation(self, req, applied, unit):
        refs = self._scope_refs(req, applied, unit)
        return [self._task(req, "representative", lambda c, r=refs: c.model_ref in r, target={"model_refs": sorted(refs)})] if refs else []

    def _tasks_model_presence(self, req, applied, unit):
        ref = req["parameters"]["model_ref"]
        return [self._task(req, ref, lambda c, r=ref: c.model_ref == r, target={"model_ref": ref})] if ref in applied else []

    def _tasks_exact_model_sample(self, req, applied, unit):
        """要求确认案例中的某一型号承担明确规格，而非仅作软偏好。"""
        params = req.get("parameters") or {}
        ref = params["model_ref"]
        item = applied.get(ref)
        if not item:
            return []
        section = params.get("section")
        if section == "actual_min":
            section = min(item.section_sequence)
        elif section == "actual_max":
            section = max(item.section_sequence)
        cores = params.get("cores")
        if cores == "actual_min":
            cores = min(item.core_sequence)
        elif cores == "actual_max":
            cores = max(item.core_sequence)
        matching = [
            candidate for candidate in item.candidates
            if (section is None or candidate.section == section)
            and (cores is None or candidate.cores == cores)
        ]
        if not matching:
            return []
        return [self._task(
            req,
            ref,
            lambda c, r=ref, s=section, n=cores: c.model_ref == r and (s is None or c.section == s) and (n is None or c.cores == n),
            f"（{item.model['display_name']}确认规格）",
            {"model_ref": ref, "section": section, "cores": cores, "fix_specification": True},
        )]

    def _tasks_model_set_presence(self, req, applied, unit):
        refs = set(req["parameters"].get("models") or (req.get("when") or {}).get("any_models") or []).intersection(applied)
        return [self._task(req, ref, lambda c, r=ref: c.model_ref == r, f"（{applied[ref].model['display_name']}）", {"model_ref": ref}) for ref in sorted(refs)]

    def _tasks_shape_pair(self, req, applied, unit):
        refs = self._scope_refs(req, applied, unit)
        if req["parameters"].get("may_merge_with_unit_samples"):
            refs = set(applied)
        preferences = req.get("parameters", {}).get("shape_model_preferences") or {}
        return [self._task(req, shape, lambda c, r=refs, s=shape: c.model_ref in r and c.shape == s,
                           f"（{SHAPE_TEXT.get(shape, shape)}）", {"shape": shape, "preferred_model_refs": preferences.get(shape, [])}) for shape in req["parameters"]["required_shapes"]]

    def _tasks_series_presence(self, req, applied, unit):
        series = sorted({item.model.get("series") for item in applied.values()})
        return [self._task(req, value, lambda c, v=value: applied[c.model_ref].model.get("series") == v,
                           f"（{value}系列）", {"series": value}) for value in series]

    def _tasks_series_sheathed_presence(self, req, applied, unit):
        series = sorted({item.model.get("series") for item in applied.values() if item.model.get("sheath")})
        return [self._task(req, value, lambda c, v=value: applied[c.model_ref].model.get("series") == v and bool(applied[c.model_ref].model.get("sheath")),
                           f"（{value}系列有护套）", {"series": value, "sheathed": True}) for value in series]

    def _tasks_screening_groups(self, req, applied, unit):
        groups = req["parameters"]["groups"]
        tasks = []
        for group, refs in groups.items():
            scoped = set(refs).intersection(applied)
            if scoped:
                tasks.append(self._task(req, group, lambda c, r=scoped: c.model_ref in r,
                                        f"（屏蔽组{group}）", {"screening_group": group}))
        return tasks

    def _tasks_unsheathed(self, req, applied, unit):
        return [self._task(req, "unsheathed", lambda c: not applied[c.model_ref].model.get("sheath"), target={"sheathed": False})]

    def _tasks_selection_preference(self, req, applied, unit):
        """仅显式硬性分配生成覆盖任务；软偏好不得制造必送样品。"""
        if not req.get("hard_constraint"):
            return []
        preferred = req.get("parameters", {}).get("prefer") or {}
        sample_preferences = req.get("parameters", {}).get("sample_preferences") or {}
        tasks = []
        for subunit, temperature in preferred.items():
            refs = {ref for ref, item in applied.items() if item.model.get("subunit") == subunit and item.model.get("temperature_class") == temperature}
            if refs:
                preference = sample_preferences.get(subunit) or {}
                scoped_candidates = [candidate for ref in refs for candidate in applied[ref].candidates]
                preferred_cores = max((candidate.cores or 0 for candidate in scoped_candidates), default=None) if preference.get("cores") == "max" else None
                preferred_sections = [candidate.section for candidate in scoped_candidates if candidate.section is not None and (preferred_cores is None or candidate.cores == preferred_cores)]
                preferred_section = min(preferred_sections) if preferred_sections and preference.get("section") == "min" else None
                tasks.append(self._task(req, f"{subunit}-{temperature}", lambda c, r=refs: c.model_ref in r,
                                        f"（{subunit}选择{temperature}）", {"subunit": subunit, "temperature_class": temperature,
                                         "preferred_shape": preference.get("shape"), "preferred_cores": preferred_cores, "preferred_section": preferred_section}))
        return tasks

    def _carries(self, candidate: Candidate, target: Candidate, applied: dict[str, AppliedModel], section_only: bool = False) -> bool:
        section_ok = True
        if target.section is not None:
            if candidate.section is None:
                return False
            if target.section > 10 or candidate.section > 10:
                section_ok = candidate.section == target.section
            else:
                section_ok = self._standard_adjacent(candidate.section, target.section, applied[target.model_ref].model, "sections", applied[candidate.model_ref].model)
        if section_only:
            return section_ok
        core_ok = True
        if target.cores is not None:
            if candidate.cores is None:
                return False
            core_ok = self._standard_adjacent(candidate.cores, target.cores, applied[target.model_ref].model, "cores", applied[candidate.model_ref].model)
        return section_ok and core_ok

    @staticmethod
    def _standard_adjacent(value, target, model, dimension, source_model=None):
        if value == target:
            return True
        models = [model] + ([source_model] if source_model else [])
        specs = [g[dimension] for m in models for g in m["specification_groups"] if not g.get("special_expression")]
        # 标准序列必须独立于申请窗口。只有范围或来源不完整时，不猜相邻档。
        if not specs or any(s.get("sequence_status") != "exact" or not s.get("values") for s in specs):
            return False
        sequence = sorted({v for spec in specs for v in spec["values"]})
        return value in sequence and target in sequence and abs(sequence.index(value) - sequence.index(target)) == 1

    def _directed_corner_carries(
        self,
        candidate: Candidate,
        target: Candidate,
        corner: str,
        applied: dict[str, AppliedModel],
    ) -> bool:
        """2.1强制样品向2.2组合端点的单向承接。

        最多芯数/最小截面端允许芯数更大、截面更小的IEC 10样品承接；
        最少芯数/最大截面端方向相反。小截面仍须满足相邻档限制，且
        大于10mm²的样品不能跨界承接不大于10mm²的目标。
        """
        if candidate.section is None or candidate.cores is None:
            return False
        if target.section is None or target.cores is None:
            return False
        if target.section <= 10 and candidate.section > 10:
            return False
        if not self._carries(candidate, target, applied, section_only=True):
            return False
        if corner == "max_cores_min_section":
            return candidate.cores >= target.cores and candidate.section <= target.section
        if corner == "min_cores_max_section":
            return candidate.cores <= target.cores and candidate.section >= target.section
        return False

    @staticmethod
    def _task_matches(task: CoverageTask, candidate: Candidate, strict_boundaries: bool = False) -> bool:
        matcher = task.strict_matches if strict_boundaries and task.strict_matches is not None else task.matches
        return matcher(candidate)

    def _solve(
        self,
        applied: dict[str, AppliedModel],
        tasks: list[CoverageTask],
        unit: dict[str, Any],
        strict_boundaries: bool = False,
    ):
        candidates = [candidate for item in applied.values() for candidate in item.candidates]
        masks: dict[Candidate, int] = {}
        full = (1 << len(tasks)) - 1
        for candidate in candidates:
            mask = sum(
                1 << index for index, task in enumerate(tasks)
                if self._task_matches(task, candidate, strict_boundaries)
            )
            if mask:
                masks[candidate] = mask
        missing = [tasks[index].reason for index in range(len(tasks)) if not any(mask & (1 << index) for mask in masks.values())]
        if missing:
            raise SamplingError("当前申请无法满足硬性规则：" + "；".join(missing))

        # 相同覆盖能力只保留稳定排序最靠前的候选，显著缩小组合搜索空间。
        best_by_mask: dict[int, Candidate] = {}
        for candidate, mask in sorted(masks.items(), key=lambda item: (
            self._candidate_task_cost(item[0], tasks, applied, unit, strict_boundaries),
            self._candidate_sort(item[0], applied),
        )):
            best_by_mask.setdefault(mask, candidate)
        compact = list(best_by_mask.values())
        candidate_masks = {candidate: masks[candidate] for candidate in compact}
        choices = {
            index: sorted([candidate for candidate in compact if candidate_masks[candidate] & (1 << index)], key=lambda c: self._candidate_sort(c, applied))
            for index in range(len(tasks))
        }
        best: list[Candidate] | None = None

        def search(covered: int, selected: list[Candidate]):
            nonlocal best
            if covered == full:
                ordered = sorted(selected, key=lambda c: self._candidate_sort(c, applied))
                current_rank = (
                    len(ordered),
                    self._selection_cost(ordered, tasks, applied, unit, strict_boundaries),
                    [self._candidate_sort(c, applied) for c in ordered],
                )
                best_rank = (
                    len(best),
                    self._selection_cost(best, tasks, applied, unit, strict_boundaries),
                    [self._candidate_sort(c, applied) for c in best],
                ) if best is not None else None
                if best is None or current_rank < best_rank:
                    best = ordered
                return
            if best is not None and len(selected) >= len(best):
                return
            uncovered = [index for index in range(len(tasks)) if not covered & (1 << index)]
            task_index = min(uncovered, key=lambda index: len(choices[index]))
            ranked = sorted(choices[task_index], key=lambda c: (
                -(bin(candidate_masks[c] & ~covered).count("1")),
                self._candidate_task_cost(c, tasks, applied, unit, strict_boundaries),
                self._candidate_sort(c, applied),
            ))
            for candidate in ranked:
                if candidate in selected:
                    continue
                search(covered | candidate_masks[candidate], selected + [candidate])

        search(0, [])
        if best is None:
            raise SamplingError("未找到满足全部硬性规则的合法样品组合")
        best = self._improve_selection(best, candidates, tasks, applied, unit, strict_boundaries)
        return best, self._coverage_map(best, tasks)

    def _improve_selection(self, selected, candidates, tasks, applied, unit, strict_boundaries=False):
        """在不增样、不丢覆盖的前提下，将相邻档承接替换为更准确的端点。"""
        current = list(selected)
        changed = True
        while changed:
            changed = False
            current_cost = self._selection_cost(current, tasks, applied, unit, strict_boundaries)
            for index, original in enumerate(list(current)):
                for alternative in candidates:
                    if alternative in current or alternative == original:
                        continue
                    proposal = current[:index] + [alternative] + current[index + 1:]
                    if not all(
                        any(self._task_matches(task, candidate, strict_boundaries) for candidate in proposal)
                        for task in tasks
                    ):
                        continue
                    proposal_cost = self._selection_cost(proposal, tasks, applied, unit, strict_boundaries)
                    if (proposal_cost, [self._candidate_sort(c, applied) for c in sorted(proposal, key=lambda c: self._candidate_sort(c, applied))]) < (
                        current_cost, [self._candidate_sort(c, applied) for c in sorted(current, key=lambda c: self._candidate_sort(c, applied))]
                    ):
                        current = proposal
                        changed = True
                        break
                if changed:
                    break
        return sorted(current, key=lambda c: self._candidate_sort(c, applied))

    def _candidate_sort(self, candidate: Candidate, applied: dict[str, AppliedModel]):
        model_order = list(applied).index(candidate.model_ref)
        return (model_order, candidate.section if candidate.section is not None else -1, candidate.cores or 0, candidate.shape, candidate.group_id)

    def _candidate_task_cost(
        self,
        candidate: Candidate,
        tasks: list[CoverageTask],
        applied: dict[str, AppliedModel],
        unit: dict[str, Any],
        strict_boundaries: bool = False,
    ) -> int:
        """候选的近似排序成本；最终方案使用按任务分配后的成本。"""
        return sum(
            self._task_candidate_cost(candidate, task, applied)
            for task in tasks
            if self._task_matches(task, candidate, strict_boundaries)
        )

    def _task_candidate_cost(self, candidate: Candidate, task: CoverageTask, applied: dict[str, AppliedModel]) -> int:
        cost = 0
        target_section = task.target.get("section")
        target_cores = task.target.get("cores")
        if target_section is not None and candidate.section != target_section:
            cost += 4 if task.kind in {"exact_corner_pair", "corner_pair", "compatible_corner_pair"} else 2
        if target_cores is not None and candidate.cores != target_cores:
            cost += 2 if task.kind in {"exact_corner_pair", "corner_pair", "compatible_corner_pair"} else 1
        preferred_refs = task.target.get("preferred_model_refs") or []
        if preferred_refs and candidate.model_ref not in preferred_refs:
            cost += 6
        preferred_series = task.target.get("preferred_series") or []
        if preferred_series and applied[candidate.model_ref].model.get("series") not in preferred_series:
            cost += 6
        if task.target.get("preferred_shape") and candidate.shape != task.target["preferred_shape"]:
            cost += 3
        if task.target.get("preferred_cores") is not None and candidate.cores != task.target["preferred_cores"]:
            cost += 2
        if task.target.get("preferred_section") is not None and candidate.section != task.target["preferred_section"]:
            cost += 2
        return cost

    def _selection_cost(
        self,
        selected: list[Candidate],
        tasks: list[CoverageTask],
        applied,
        unit,
        strict_boundaries: bool = False,
    ) -> int:
        """每项任务只计入最佳承接样品，避免顺带匹配反而受到惩罚。"""
        total = 0
        for task in tasks:
            eligible = [
                candidate for candidate in selected
                if self._task_matches(task, candidate, strict_boundaries)
            ]
            if not eligible:
                raise SamplingError(f"候选方案未覆盖硬性规则：{task.reason}，请转人工确认")
            total += min(
                self._task_candidate_cost(candidate, task, applied)
                for candidate in eligible
            )
        return total

    def _coverage_map(self, selected: list[Candidate], tasks: list[CoverageTask]):
        return {candidate.key: [task for task in tasks if task.matches(candidate)] for candidate in selected}

    def _assigned_coverage_map(self, selected: list[Candidate], tasks: list[CoverageTask], applied, unit):
        """为每项要求指定实际承接样品，避免“顺带匹配”把任意规格错误固定。"""
        assigned = {candidate.key: [] for candidate in selected}
        for task in tasks:
            eligible = [candidate for candidate in selected if task.matches(candidate)]
            if not eligible:
                raise SamplingError(f"选样结果丢失覆盖任务：{task.reason}")
            chosen = min(eligible, key=lambda candidate: (
                self._task_candidate_cost(candidate, task, applied),
                self._candidate_sort(candidate, applied),
            ))
            assigned[chosen.key].append(task)
        return assigned

    def _needs_color_pair(self, applied: dict[str, AppliedModel], unit: dict[str, Any]) -> bool:
        return any(
            req["kind"] == "color_pair" and self._condition_applies(req.get("when") or {}, applied, unit)
            for req in unit.get("requirements", [])
        )

    def _color_pair_satisfied(self, selected: Iterable[Candidate], applied: dict[str, AppliedModel], unit: dict[str, Any]) -> bool:
        if not self._needs_color_pair(applied, unit):
            return True
        eligible = [
            candidate for candidate in selected
            if applied[candidate.model_ref].model.get("series") == "GB/T 5023"
        ]
        return len(eligible) >= 2

    def _relax_redundant_boundaries(
        self,
        selected: list[Candidate],
        applied: dict[str, AppliedModel],
        tasks: list[CoverageTask],
        unit: dict[str, Any],
    ) -> list[Candidate]:
        """只从准确边界方案中删除冗余样品，不主动引入中间档。

        `strict_matches`保证初始方案包含准确端点/组合端点。随后，若某件已经因
        其他要求入选的样品能够按相邻档承接边界，才允许删除多余端点；颜色数量
        也必须继续满足。这样可保留已确认的“结构样品顺带承接”案例，同时禁止
        为压缩件数凭空选择中间规格。
        """
        strict_selected = sorted(set(selected), key=lambda candidate: self._candidate_sort(candidate, applied))
        independent_tasks = [task for task in tasks if task.strict_matches is None]

        # 只有在准确端点方案中“唯一承担”某项独立要求的样品，
        # 才是合法的承接锚点。例如铝导体、扁形或特殊型号样品。
        # 若两个端点样品都能满足“本子单元有代表”，则该代表要求
        # 不能成为删掉另一端点的理由。
        anchor_task_ids: dict[tuple[Any, ...], set[str]] = {}
        for task in independent_tasks:
            matching = [candidate for candidate in strict_selected if task.matches(candidate)]
            if len(matching) == 1:
                anchor_task_ids.setdefault(matching[0].key, set()).add(task.id)

        # 单向承接来源本身是另一子单元的强制准确样品，也属于合法锚点。
        # 这允许IEC 10已经必送的两个端点参与2.2尺寸覆盖，但不会反向
        # 用2.2样品取消IEC 10自身的准确端点。
        directed_source_subunits = {
            "2.1" for rule in unit.get("requirements", [])
            if rule.get("kind") == "directed_representation"
            and rule.get("parameters", {}).get("direction") == "2.1_to_2.2_only"
        }
        for candidate in strict_selected:
            if applied[candidate.model_ref].model.get("subunit") in directed_source_subunits:
                anchor_task_ids.setdefault(candidate.key, set()).add("directed-source")

        def rank(subset: list[Candidate]):
            return (
                len(subset),
                self._selection_cost(subset, tasks, applied, unit),
                [self._candidate_sort(candidate, applied) for candidate in subset],
            )

        def best_relaxed_subset(
            pool: Iterable[Candidate],
            anchors: dict[tuple[Any, ...], set[str]],
        ) -> list[Candidate] | None:
            ordered_pool = sorted(set(pool), key=lambda candidate: self._candidate_sort(candidate, applied))
            for size in range(1, len(ordered_pool) + 1):
                matches_at_size: list[list[Candidate]] = []
                for subset_tuple in combinations(ordered_pool, size):
                    subset = list(subset_tuple)
                    if not self._color_pair_satisfied(subset, applied, unit):
                        continue
                    valid = True
                    for task in tasks:
                        if task.strict_matches is None:
                            covered = any(task.matches(candidate) for candidate in subset)
                        elif any(task.strict_matches(candidate) for candidate in subset):
                            covered = True
                        else:
                            covered = any(
                                task.matches(candidate) and bool(anchors.get(candidate.key))
                                for candidate in subset
                            )
                        if not covered:
                            valid = False
                            break
                    if not valid:
                        continue
                    matches_at_size.append(subset)
                if matches_at_size:
                    return min(matches_at_size, key=rank)
            return None

        current = best_relaxed_subset(strict_selected, anchor_task_ids) or strict_selected
        all_candidates = [candidate for item in applied.values() for candidate in item.candidates]

        # 已经承担独立任务的样品可以在合法申请范围内调整规格，以便顺带承接边界。
        # 颜色本身不作为调整锚点，避免仅因一黑一白又主动造出中间档。
        changed = True
        while changed:
            changed = False
            current_rank = rank(current)
            for index, original in enumerate(list(current)):
                original_anchor_ids = anchor_task_ids.get(original.key, set())
                if not original_anchor_ids:
                    continue
                anchor_tasks = [task for task in independent_tasks if task.id in original_anchor_ids]
                alternatives = [
                    candidate for candidate in all_candidates
                    if candidate not in current and any(task.matches(candidate) for task in anchor_tasks)
                ]
                for alternative in alternatives:
                    proposal_pool = current[:index] + [alternative] + current[index + 1:]
                    proposal_anchors = dict(anchor_task_ids)
                    transferred = {task.id for task in anchor_tasks if task.matches(alternative)}
                    proposal_anchors.pop(original.key, None)
                    if transferred:
                        proposal_anchors[alternative.key] = transferred
                    proposal = best_relaxed_subset(proposal_pool, proposal_anchors)
                    if proposal is None:
                        continue
                    proposal_rank = rank(proposal)
                    # 只有件数或覆盖成本实质改善时才调整独立样品规格；稳定排序
                    # 不能成为把既有代表规格换成较小芯数/截面的理由。
                    if proposal_rank[:2] < current_rank[:2]:
                        current = proposal
                        anchor_task_ids = proposal_anchors
                        changed = True
                        break
                if changed:
                    break
        return sorted(current, key=lambda candidate: self._candidate_sort(candidate, applied))

    def _ensure_color_pair(self, selected: list[Candidate], applied: dict[str, AppliedModel], unit: dict[str, Any]):
        needs_color = self._needs_color_pair(applied, unit)
        eligible = [candidate for candidate in selected if applied[candidate.model_ref].model.get("series") == "GB/T 5023"]
        if not needs_color or len(eligible) >= 2:
            return selected
        alternatives = [candidate for item in applied.values() for candidate in item.candidates if item.model.get("series") == "GB/T 5023" and candidate not in selected]
        while alternatives and len(eligible) < 2:
            color_rule = next((row for row in unit.get("requirements", []) if row.get("kind") == "color_pair"), None)
            params = color_rule.get("parameters", {}) if color_rule else {}
            preferred = [candidate for candidate in alternatives if not params.get("black_shape") or candidate.shape == params["black_shape"]] or alternatives
            if params.get("black_sort") == "cores_desc_section_asc":
                added = min(preferred, key=lambda c: (-(c.cores or 0), c.section if c.section is not None else float("inf"), self._candidate_sort(c, applied)))
            else:
                added = max(preferred, key=lambda c: (c.section or -1, c.cores or 0))
            selected = selected + [added]
            eligible.append(added)
            alternatives.remove(added)
        return sorted(selected, key=lambda c: self._candidate_sort(c, applied))

    def _color_pair_candidates(self, gbt, params):
        """序列化和规格优选共用同一颜色分配，避免优选后颜色换到另一件。"""
        white_pool = [c for c in gbt if not params.get("white_model_refs") or c.model_ref in params["white_model_refs"]]
        black_pool = [c for c in gbt if not params.get("black_model_refs") or c.model_ref in params["black_model_refs"]]
        if params.get("white_shape"):
            white_pool = [c for c in white_pool if c.shape == params["white_shape"]] or white_pool
        if params.get("black_shape"):
            black_pool = [c for c in black_pool if c.shape == params["black_shape"]] or black_pool
        small = min(white_pool or gbt, key=lambda c: (c.section if c.section is not None else float("inf"), -(c.cores or 0)))
        if params.get("black_sort") == "cores_desc_section_asc":
            large = min(black_pool or gbt, key=lambda c: (-(c.cores or 0), c.section if c.section is not None else float("inf")))
        else:
            large = max(black_pool or gbt, key=lambda c: (c.section if c.section is not None else -1, -(c.cores or 0)))
        if small == large:
            large = next(c for c in reversed(gbt) if c != small)
        return small, large

    def _serialize_samples(self, selected, coverage, applied, unit, application, application_index):
        selected = list(selected)
        corner_rule = next(
            (
                req for req in unit.get("requirements", [])
                if req.get("parameters", {}).get("output_corner_order")
                and self._condition_applies(req.get("when") or {}, applied, unit)
            ),
            None,
        )
        if corner_rule:
            corner_order = corner_rule["parameters"]["output_corner_order"]
            corner_rank = {name: index for index, name in enumerate(corner_order)}
            original_rank = {candidate.key: index for index, candidate in enumerate(selected)}

            def output_rank(candidate: Candidate):
                covered = coverage.get(candidate.key, [])
                priorities = [
                    corner_rank[task.target["corner"]]
                    for task in covered
                    if task.target.get("corner") in corner_rank
                ]
                return (
                    list(applied).index(candidate.model_ref),
                    min(priorities, default=len(corner_rank)),
                    original_rank[candidate.key],
                )

            selected.sort(key=output_rank)
        gbt = [c for c in selected if applied[c.model_ref].model.get("series") == "GB/T 5023"]
        color_map: dict[tuple[Any, ...], str] = {}
        color_rule = next((req for req in unit.get("requirements", []) if req["kind"] == "color_pair"), None)
        if len(gbt) == 1 and color_rule:
            color_map[gbt[0].key] = "white"
        if len(gbt) >= 2 and color_rule:
            params = color_rule.get("parameters") or {}
            small, large = self._color_pair_candidates(gbt, params)
            color_map[small.key] = "white"
            color_map[large.key] = "black"
        result = []
        for candidate in selected:
            item = applied[candidate.model_ref]
            covered = coverage.get(candidate.key, [])
            boundary_kinds = {"per_model_section_endpoints", "exact_corner_pair", "corner_pair", "compatible_corner_pair", "unit_section_endpoints", "subunit_section_endpoints", "shape_pair", "exact_model_sample"}
            any_spec = not any(task.kind in boundary_kinds or task.target.get("fix_specification") for task in covered)
            requested_shape = any(segment.shapes and segment.contains(candidate.section, candidate.cores, candidate.shape)
                                  for segment in scope_segments(str(item.request.get("scope_expression") or "")))
            shape_explicit = (any(task.kind == "shape_pair" for task in covered) or requested_shape) and candidate.shape in {"round", "flat"}
            # 用户确认：设定黑白或圆扁时必须落实到具体规格。这是输出
            # 约束，不新增该型号的独立双端点任务。
            if candidate.key in color_map or shape_explicit:
                any_spec = False
            sample = {
                "application_index": application_index,
                "application_no": application.get("application_no") or "",
                "unit_code": unit["unit_code"],
                "model_ref": candidate.model_ref,
                "model": item.model["display_name"],
                "voltage": item.model["voltage"],
                "group_id": candidate.group_id,
                "section": candidate.section,
                "cores": candidate.cores,
                "shape": candidate.shape,
                "special_expression": candidate.special_expression,
                "color": color_map.get(candidate.key),
                "any_spec": any_spec,
                "shape_explicit": shape_explicit,
                "coverage_task_ids": [task.id for task in covered],
                "rule_refs": sorted({task.rule_ref for task in covered}),
                "reasons": [task.reason for task in covered],
                "material_coverage": [],
            }
            if sample["color"] and color_rule:
                sample["rule_refs"] = sorted({*sample["rule_refs"], color_rule["id"]})
                sample["reasons"].append("满足GB/T 5023全单元一黑一白；指定颜色的样品落实到具体规格。")
            if requested_shape:
                sample["reasons"].append(f"本申请规格段限定{SHAPE_TEXT.get(candidate.shape, candidate.shape)}，按该段合法组合选样。")
            sample["specification"] = self._specification_text(sample, item)
            result.append(sample)
        # 单一合法规格也可以送两件不同颜色的实物，不能把候选规格去重
        # 误作样品件数去重。只有无法选择第二种合法规格时才走此分支。
        if len(gbt) == 1 and color_rule:
            original = next(sample for sample in result if sample.get("color") == "white")
            duplicate = {**deepcopy(original), "color": "black", "reasons": [*original["reasons"], "GB/T 5023单元一黑一白：同规格另送黑色一件"]}
            duplicate["specification"] = self._specification_text(duplicate, applied[duplicate["model_ref"]])
            result.insert(result.index(original) + 1, duplicate)
        return result

    def _specification_text(self, sample: dict[str, Any], item: AppliedModel | None = None):
        scope_expression = str(item.request.get("scope_expression") or "").strip() if item is not None else ""
        if sample.get("any_spec") and scope_expression and item is not None:
            original, _, _ = self._model_candidates(item.model, {"scope_expression": scope_expression, "include_special": item.request.get("include_special")})
            if set(original) != set(item.candidates):
                # 页面数值框收窄后，复制范围也必须取交集，不能回填旧原文。
                pieces = []
                for segment in scope_segments(scope_expression):
                    rows = [c for c in item.candidates if segment.contains(c.section, c.cores, c.shape)
                            and (bool(c.special_expression) == bool(segment.special_expression))]
                    if not rows:
                        continue
                    def span(values):
                        values = [v for v in values if v is not None]
                        if not values:
                            return ""
                        return _number(min(values)) if min(values) == max(values) else f"{_number(min(values))}-{_number(max(values))}"
                    text = span([c.section for c in rows])
                    if segment.special_expression:
                        if segment.special_expression.endswith("芯"):
                            text += f"({segment.special_expression})"
                        else:
                            raise SamplingError("特殊结构范围调整后请重新识别原文，不能输出扩大的任意规格")
                    elif max((c.cores or 0) for c in rows) > 1:
                        text += f"({span([c.cores for c in rows])}芯)"
                    pieces.append(text)
                scope_expression = ", ".join(pieces)
        if sample.get("any_spec") and scope_expression:
            base = scope_expression.removesuffix("任意规格一件").strip() + " 任意规格一件"
        elif sample.get("special_expression") and sample.get("any_spec") and item is not None:
            section_min, section_max = min(item.section_sequence), max(item.section_sequence)
            section_text = _number(section_min) if section_min == section_max else f"{_number(section_min)}-{_number(section_max)}"
            base = f"{section_text}({sample['special_expression']}) 任意规格一件"
        elif sample.get("special_expression"):
            expression = sample["special_expression"]
            base = f"{_number(sample.get('section'))}({expression})" if expression.endswith("芯") else expression
        elif sample.get("any_spec") and item is not None:
            if item.section_sequence:
                section_min, section_max = min(item.section_sequence), max(item.section_sequence)
                base = _number(section_min) if section_min == section_max else f"{_number(section_min)}-{_number(section_max)}"
            else:
                base = ""
            core_min, core_max = min(item.core_sequence), max(item.core_sequence)
            if core_max > 1:
                core_text = f"{core_min}芯" if core_min == core_max else f"{core_min}-{core_max}芯"
                base += f"({core_text})"
            base += (" " if base else "") + "任意规格一件"
        else:
            base = _number(sample.get("section"))
            if (sample.get("cores") or 0) > 1 or (item is not None and max(item.core_sequence or [1]) > 1):
                base += f"({sample['cores']}芯)"
        if sample.get("color"):
            base += f" {COLOR_TEXT[sample['color']]}"
        if sample.get("shape_explicit") and not (sample.get("any_spec") and SHAPE_TEXT.get(sample.get("shape"), "") in base):
            base += f" {SHAPE_TEXT.get(sample.get('shape'), sample.get('shape'))}"
        return base.strip()

    def _apply_material_coverage(self, samples, groups, applications):
        if not groups:
            return samples, [], ["合并覆盖模式尚未录入材料覆盖数量，本方案仅完成结构下样。"], 0
        try:
            result, analysis, warnings, gaps = materials.allocate(samples, groups, self.repo.model)
        except ValueError as exc:
            raise SamplingError(str(exc)) from exc
        applied_by_index = {index: self._prepare_application(app, index)[1] for index, app in enumerate(applications)}
        for sample in result:
            item = applied_by_index[sample["application_index"]][sample["model_ref"]]
            sample["specification"] = self._specification_text(sample, item)
        return result, analysis, warnings, gaps

    def _material_eligible(self, sample, group):
        return materials.eligible(sample, group, self.repo.model)

    def _copy_text(self, samples):
        template = self.repo.common.get("output_templates") or {}
        lines = [template.get("header", "---------检测费将由CQC统一收取，实验室不另收取-------------")]
        for sample in samples:
            lines.append(f"{sample['model']} {sample['voltage']} {sample['specification']};")
        lines.append(template.get("footer", "上述样品各一件，需要覆盖产品描述中全部供应商和牌号。"))
        return "\n".join(lines)

    def _task_out(self, task, coverage):
        sample_keys = [key for key, covered in coverage.items() if task in covered]
        return {"id": task.id, "kind": task.kind, "rule_ref": task.rule_ref, "reason": task.reason, "target": task.target, "covered": bool(sample_keys)}

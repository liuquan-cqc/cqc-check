"""管理员专用下样管理API。"""
from __future__ import annotations

import datetime
import math
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import String, cast
from sqlalchemy.orm import Session, selectinload

from backend.app import models
from backend.app.audit import add_audit
from backend.app.auth import require_permission, require_role
from backend.app.database import get_db
from backend.app.sampling import SamplingEngine, SamplingError
from backend.app.sampling.parser import parse_scope_text
from backend.app.sampling.material_parser import (
    extract_description_text,
    merge_recognition_results,
    recognize_material_groups,
)
from backend.app.sampling.rules import get_sampling_repository

router = APIRouter(prefix="/api/sampling", tags=["sampling"])


class ModelRangeIn(BaseModel):
    model_ref: str = Field(..., min_length=2, max_length=80)
    section_min: float | None = Field(None, gt=0)
    section_max: float | None = Field(None, gt=0)
    core_min: int | None = Field(None, ge=1, le=999)
    core_max: int | None = Field(None, ge=1, le=999)
    shapes: list[str] = Field(default_factory=list)
    include_special: bool = False
    scope_expression: str = Field("", max_length=4000)
    unsupported_conditions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.section_min is not None and self.section_max is not None and self.section_min > self.section_max:
            raise ValueError("最小截面不能大于最大截面")
        if self.core_min is not None and self.core_max is not None and self.core_min > self.core_max:
            raise ValueError("最小芯数不能大于最大芯数")
        return self


class ApplicationIn(BaseModel):
    application_no: str = Field(..., min_length=1, max_length=100)
    unit_code: str = Field(..., min_length=1, max_length=10)
    models: list[ModelRangeIn] = Field(..., min_length=1)
    raw_scope_text: str = Field("", max_length=20000)


class ParseScopeIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


class MaterialGroupIn(BaseModel):
    group_code: str = Field(..., min_length=1, max_length=100)
    category: str = Field(..., min_length=1, max_length=60)
    compatibility_group: str = Field("", max_length=100)
    total_items: int = Field(0, ge=0, le=999)
    cqc_filed_items: int = Field(0, ge=0, le=999)
    model_refs: list[str] = Field(default_factory=list)
    suppliers: list[dict[str, Any]] = Field(default_factory=list)
    material_brands: list[str] = Field(default_factory=list)
    recognition_notes: list[str] = Field(default_factory=list)
    recognition_source: str = Field("", max_length=1000)

    @model_validator(mode="after")
    def validate_filed(self):
        if self.cqc_filed_items > self.total_items:
            raise ValueError("CQC备案数量不能大于覆盖项总数")
        return self


class GenerateIn(BaseModel):
    mode: Literal["fast", "merged"] = "fast"
    applications: list[ApplicationIn] = Field(..., min_length=1)
    material_groups: list[MaterialGroupIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode(self):
        numbers = [item.application_no.strip() for item in self.applications]
        if self.mode == "fast" and len(set(numbers)) != 1:
            raise ValueError("快速模式只允许一个申请编号，但可包含多个产品单元")
        unit_keys = [(item.application_no.strip(), item.unit_code.zfill(2)) for item in self.applications]
        if len(unit_keys) != len(set(unit_keys)):
            raise ValueError("同一申请编号下不能重复录入同一个产品单元")
        return self


class ManualVersionIn(BaseModel):
    samples: list[dict[str, Any]] = Field(..., min_length=1)
    change_type: Literal["add", "replace", "delete", "edit", "enterprise_requested", "mixed"] = "edit"
    reason: str = Field(..., min_length=2, max_length=2000)

    @model_validator(mode="after")
    def validate_sample_types(self):
        for sample in self.samples:
            for key in ("id", "model_ref", "group_id", "shape", "model", "voltage", "special_expression", "application_no", "unit_code", "specification", "color"):
                if sample.get(key) is not None and not isinstance(sample[key], str):
                    raise ValueError(f"样品{key}必须是文字")
            for key in ("any_spec", "shape_explicit"):
                if sample.get(key) is not None and type(sample[key]) is not bool:
                    raise ValueError(f"样品{key}必须是布尔值")
            for key in ("material_coverage", "reasons", "rule_refs"):
                if sample.get(key) is not None and (not isinstance(sample[key], list) or not all(isinstance(v, str) for v in sample[key])):
                    raise ValueError(f"样品{key}必须是文字列表")
            for key in ("application_index", "cores"):
                if sample.get(key) is not None and type(sample[key]) is not int:
                    raise ValueError(f"样品{key}必须是整数")
            value = sample.get("section")
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError("样品截面必须是正数")
            for item in sample.get("material_assignments") or []:
                if not isinstance(item, dict) or type(item.get("group_index")) is not int or type(item.get("item_no")) is not int:
                    raise ValueError("材料分配明细格式无效")
        return self


def _payload(body: GenerateIn) -> dict[str, Any]:
    return body.model_dump()


def _version_out(row: models.SamplingPlanVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "version_no": row.version_no,
        "parent_version_id": row.parent_version_id,
        "source": row.source,
        "change_type": row.change_type,
        "change_reason": row.change_reason or "",
        "plan": row.plan_json or {},
        "coverage": row.coverage_json or {},
        "copy_text": row.copy_text,
        "validation_status": row.validation_status,
        "hard_gap_count": row.hard_gap_count,
        "rule_package_version": row.rule_package_version,
        "created_by": row.created_by,
        "creator_name": row.creator.username if row.creator else "",
        "created_at": row.created_at,
    }


def _task_out(row: models.SamplingTask, include_versions: bool = False) -> dict[str, Any]:
    current = next((version for version in row.versions if version.id == row.current_version_id), None)
    data = {
        "id": row.id,
        "task_no": row.task_no,
        "mode": row.mode,
        "application_nos": row.application_nos_json or [],
        "status": row.status,
        "current_version_id": row.current_version_id,
        "current_version_no": current.version_no if current else None,
        "sample_count": len((current.plan_json or {}).get("samples", [])) if current else 0,
        "validation_status": current.validation_status if current else "unknown",
        "hard_gap_count": current.hard_gap_count if current else 0,
        "created_by": row.created_by,
        "creator_name": row.creator.username if row.creator else "",
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if include_versions:
        data.update({
            "input": row.input_json or {},
            "versions": [_version_out(version) for version in reversed(row.versions)],
        })
    return data


def _generate(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return SamplingEngine().generate(payload)
    except SamplingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/catalog")
def catalog(_: models.User = Depends(require_permission("sampling.manage"))):
    repo = get_sampling_repository()
    return {"rule_package_version": repo.version, "units": repo.catalog()}


@router.post("/preview")
def preview(body: GenerateIn, _: models.User = Depends(require_permission("sampling.manage"))):
    return _generate(_payload(body))


@router.post("/parse-scope")
def parse_scope(body: ParseScopeIn, _: models.User = Depends(require_permission("sampling.manage"))):
    return parse_scope_text(body.text)


@router.post("/recognize-materials")
async def recognize_materials(
    files: list[UploadFile] = File(...),
    _: models.User = Depends(require_permission("sampling.manage")),
):
    """从产品描述中预填充材料覆盖项。

    识别结果不直接生成最终方案，必须在前端保留人工调整。
    """
    if not files:
        raise HTTPException(status_code=422, detail="请上传产品描述")
    if len(files) > 10:
        raise HTTPException(status_code=422, detail="一次最多上传10份产品描述")

    documents: list[dict[str, Any]] = []
    for upload in files:
        filename = (upload.filename or "未命名文件").strip()
        try:
            data = await upload.read()
            text, method, extraction_warnings = extract_description_text(filename, data)
            groups, recognition_warnings = recognize_material_groups(text, filename)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{filename}：{exc}") from exc
        finally:
            await upload.close()
        documents.append({
            "filename": filename,
            "extraction_method": method,
            "text_length": len(text),
            "groups": groups,
            "warnings": extraction_warnings + recognition_warnings,
        })
    return merge_recognition_results(documents)


@router.post("/tasks")
def create_task(
    body: GenerateIn,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("sampling.manage")),
):
    payload = _payload(body)
    result = _generate(payload)
    task_no = f"XY-{datetime.datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    task = models.SamplingTask(
        task_no=task_no,
        # 旧表结构仍保留非空列以兼容既有数据库；新界面和API不再采集企业名称。
        enterprise_name="",
        mode=body.mode,
        application_nos_json=list(dict.fromkeys(item.application_no.strip() for item in body.applications)),
        input_json=payload,
        status="draft",
        created_by=current.id,
    )
    db.add(task)
    db.flush()
    version = models.SamplingPlanVersion(
        task_id=task.id,
        version_no=1,
        source="generated",
        change_type="generate",
        change_reason="按当前下样规则生成初始方案",
        plan_json=result,
        coverage_json={"applications": result.get("applications", []), "material_analysis": result.get("material_analysis", [])},
        copy_text=result["copy_text"],
        validation_status=result["validation_status"],
        hard_gap_count=result["hard_gap_count"],
        rule_package_version=result["rule_package_version"],
        created_by=current.id,
    )
    db.add(version)
    db.flush()
    task.current_version_id = version.id
    add_audit(db, current, "下样管理", "创建下样任务", f"{task.task_no}，申请{'、'.join(task.application_nos_json)}，样品{len(result['samples'])}件", request)
    db.commit()
    return {"id": task.id, "task_no": task.task_no, "version_id": version.id, "result": result}


@router.get("/tasks")
def list_tasks(
    q: str = Query("", max_length=100),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("sampling.manage")),
):
    query = db.query(models.SamplingTask).options(
        selectinload(models.SamplingTask.creator),
        selectinload(models.SamplingTask.versions).selectinload(models.SamplingPlanVersion.creator),
    )
    if q.strip():
        keyword = f"%{q.strip()}%"
        query = query.filter(
            (models.SamplingTask.task_no.ilike(keyword)) |
            (cast(models.SamplingTask.application_nos_json, String).ilike(keyword))
        )
    rows = query.order_by(models.SamplingTask.id.desc()).limit(limit).all()
    return [_task_out(row) for row in rows]


@router.get("/tasks/{task_id}")
def task_detail(
    task_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("sampling.manage")),
):
    row = db.query(models.SamplingTask).options(
        selectinload(models.SamplingTask.creator),
        selectinload(models.SamplingTask.versions).selectinload(models.SamplingPlanVersion.creator),
    ).filter(models.SamplingTask.id == task_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="下样任务不存在")
    return _task_out(row, include_versions=True)


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_role("admin")),
):
    """删除下样任务及其全部方案版本，仅管理员可执行。"""
    task = db.query(models.SamplingTask).filter(models.SamplingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="下样任务不存在")

    task_no = task.task_no
    application_nos = list(task.application_nos_json or [])
    version_query = db.query(models.SamplingPlanVersion).filter(models.SamplingPlanVersion.task_id == task_id)
    version_count = version_query.count()
    # 先解除版本间的父子引用，兼容启用外键约束的正式数据库。
    version_query.update({models.SamplingPlanVersion.parent_version_id: None}, synchronize_session=False)
    version_query.delete(synchronize_session=False)
    db.delete(task)
    add_audit(
        db, current, "下样管理", "删除下样任务",
        f"任务：{task_no}；申请：{'、'.join(application_nos)}；删除方案版本：{version_count}", request,
    )
    db.commit()
    return {"ok": True, "task_no": task_no, "deleted_version_count": version_count}


@router.post("/tasks/{task_id}/versions")
def create_manual_version(
    task_id: int,
    body: ManualVersionIn,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("sampling.manage")),
):
    task = db.query(models.SamplingTask).options(selectinload(models.SamplingTask.versions)).filter(models.SamplingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="下样任务不存在")
    current_version = next((row for row in task.versions if row.id == task.current_version_id), None)
    if not current_version:
        raise HTTPException(status_code=409, detail="当前任务没有可编辑方案版本")
    engine = SamplingEngine()
    try:
        normalized = engine.normalize_manual_samples(task.input_json or {}, body.samples, (current_version.plan_json or {}).get("samples") or [])
        validation = engine.validate_manual_samples(task.input_json or {}, normalized)
    except (SamplingError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 即使重建了显示文字，也记录接口提交中的不一致，而非静默认定零缺口。
    for position, (source, sample) in enumerate(zip(body.samples, normalized), 1):
        if any(source.get(k) and source[k] != sample.get(k) for k in ("specification", "model", "voltage")):
            validation["gaps"].append(f"第{position}件显示文字与合法规格不一致，已按结构字段重建")
    validation["hard_gap_count"] = len(validation["gaps"])
    validation["validation_status"] = "has_gaps" if validation["gaps"] else "draft_only"
    plan = dict(current_version.plan_json or {})
    plan.update({
        "samples": normalized,
        "hard_gap_count": validation["hard_gap_count"],
        "validation_status": validation["validation_status"],
        "final_confirmation_allowed": False,
        "warnings": validation["warnings"],
        "copy_text": engine.copy_text_for_samples(normalized),
        "manual_gaps": validation["gaps"],
        "applications": validation["applications"],
        "material_analysis": validation["material_analysis"],
        "rule_package_version": engine.repo.version,
    })
    next_version = max((row.version_no for row in task.versions), default=0) + 1
    version = models.SamplingPlanVersion(
        task_id=task.id,
        version_no=next_version,
        parent_version_id=current_version.id,
        source="manual",
        change_type=body.change_type,
        change_reason=body.reason.strip(),
        plan_json=plan,
        coverage_json={"manual_validation": validation},
        copy_text=plan["copy_text"],
        validation_status=validation["validation_status"],
        hard_gap_count=validation["hard_gap_count"],
        rule_package_version=engine.repo.version,
        created_by=current.id,
    )
    db.add(version)
    db.flush()
    task.current_version_id = version.id
    task.status = "draft" if validation["hard_gap_count"] else "adjusted"
    add_audit(db, current, "下样管理", "调整下样方案", f"{task.task_no}，V{next_version}，{body.change_type}，缺口{validation['hard_gap_count']}项", request)
    db.commit()
    return {"version_id": version.id, "version_no": version.version_no, "result": plan}

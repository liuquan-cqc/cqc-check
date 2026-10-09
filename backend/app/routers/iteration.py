"""审核纠错、规则、版本和回归测试 API。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session, selectinload

from backend.app import models
from backend.app.audit import add_audit
from backend.app.auth import require_permission
from backend.app.config import get_settings
from backend.app.database import get_db
from backend.app.iteration import issue_count, rule_snapshot
from backend.app.knowledge import load_knowledge

router = APIRouter(prefix="/api/iteration", tags=["iteration"])


CORRECTION_TYPES = {
    "false_positive": "误报",
    "missed_issue": "漏报",
    "wrong_basis": "标准依据错误",
    "wrong_value": "数值或计算错误",
    "wrong_metadata": "基础信息识别错误",
    "conclusion_error": "审核结论错误",
}


class CorrectionCreate(BaseModel):
    report_id: int
    correction_type: str
    original_json: Optional[dict[str, Any]] = None
    corrected_json: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(..., min_length=2, max_length=4000)
    standard_ref: str = Field("", max_length=4000)


class CorrectionReview(BaseModel):
    status: Literal["approved", "rejected"]
    review_note: str = Field("", max_length=2000)
    create_rule: bool = True
    rule_title: str = Field("", max_length=255)
    rule_category: str = Field("人工纠错", max_length=60)
    applicable_conditions: str = Field("", max_length=4000)
    rule_text: str = Field("", max_length=6000)


class RuleIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=255)
    category: str = Field("其他", max_length=60)
    product_unit: str = Field("", max_length=255)
    applicable_conditions: str = Field("", max_length=4000)
    rule_text: str = Field(..., min_length=2, max_length=6000)
    standard_ref: str = Field("", max_length=4000)


class RuleUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=2, max_length=255)
    category: Optional[str] = Field(None, max_length=60)
    product_unit: Optional[str] = Field(None, max_length=255)
    applicable_conditions: Optional[str] = Field(None, max_length=4000)
    rule_text: Optional[str] = Field(None, min_length=2, max_length=6000)
    standard_ref: Optional[str] = Field(None, max_length=4000)
    status: Optional[Literal["draft", "active", "retired"]] = None


class VersionCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    description: str = Field("", max_length=2000)
    rule_ids: list[int] = Field(default_factory=list)


class RegressionCaseCreate(BaseModel):
    report_id: int
    name: str = Field(..., min_length=2, max_length=255)
    expected_conclusion: str = Field(..., min_length=1, max_length=100)
    expected_issue_count: int = Field(0, ge=0, le=999)
    notes: str = Field("", max_length=2000)


def _report_label(report: models.Report | None) -> str:
    if not report:
        return "已删除报告"
    return report.application_no or report.report_no or f"报告 #{report.id}"


def _correction_out(row: models.ReviewCorrection) -> dict[str, Any]:
    return {
        "id": row.id,
        "report_id": row.report_id,
        "report_label": _report_label(row.report),
        "company_name": row.report.company.name if row.report and row.report.company else "",
        "product_unit": row.report.product_unit if row.report else "",
        "correction_type": row.correction_type,
        "correction_type_text": CORRECTION_TYPES.get(row.correction_type, row.correction_type),
        "original_json": row.original_json or {},
        "corrected_json": row.corrected_json or {},
        "description": row.description,
        "standard_ref": row.standard_ref or "",
        "status": row.status,
        "submitted_by": row.submitted_by,
        "submitter_name": row.submitter.username if row.submitter else "",
        "reviewed_by": row.reviewed_by,
        "reviewer_name": row.reviewer.username if row.reviewer else "",
        "review_note": row.review_note or "",
        "created_at": row.created_at,
        "reviewed_at": row.reviewed_at,
    }


def _rule_out(row: models.ReviewRule, active_ids: set[int] | None = None) -> dict[str, Any]:
    data = rule_snapshot(row)
    data.update({
        "status": "active" if active_ids is not None and row.id in active_ids else row.status,
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    })
    return data


def _version_out(row: models.ReviewVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "version_no": row.version_no,
        "name": row.name,
        "description": row.description or "",
        "status": row.status,
        "rule_count": len(row.rules_json or []),
        "rules_json": row.rules_json or [],
        "metrics_json": row.metrics_json or {},
        "created_by": row.created_by,
        "published_by": row.published_by,
        "created_at": row.created_at,
        "published_at": row.published_at,
    }


@router.get("/summary")
def summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("iteration.manage")),
):
    active = db.query(models.ReviewVersion).filter(models.ReviewVersion.status == "active").first()
    last_run = db.query(models.RegressionRun).order_by(models.RegressionRun.id.desc()).first()
    active_metrics = active.metrics_json if active and isinstance(active.metrics_json, dict) else {}
    knowledge_version = str(active_metrics.get("knowledge_version") or "")
    if not knowledge_version:
        version_file = Path(get_settings().knowledge_dir) / "VERSION"
        try:
            knowledge_version = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            knowledge_version = "unknown"

    structured_version = str(active_metrics.get("structured_rulebase_version") or "")
    structured_name = str(active_metrics.get("structured_rulebase_name") or "")
    try:
        with db.begin_nested():
            structured = db.execute(text("""
                SELECT version_no, name
                FROM rule_versions
                ORDER BY id DESC
                LIMIT 1
            """)).mappings().first()
        if structured:
            structured_version = str(structured["version_no"] or structured_version)
            structured_name = str(structured["name"] or structured_name)
    except Exception:
        # 旧版SQLite测试库可能还没有结构化规则表，保留迭代快照中的版本信息。
        pass
    return {
        "pending_corrections": db.query(models.ReviewCorrection).filter(models.ReviewCorrection.status == "pending").count(),
        "draft_rules": db.query(models.ReviewRule).filter(models.ReviewRule.status == "draft").count(),
        "active_version": _version_out(active) if active else None,
        "regression_cases": db.query(models.RegressionCase).filter(models.RegressionCase.enabled.is_(True)).count(),
        "last_run": {
            "id": last_run.id, "total": last_run.total, "passed": last_run.passed,
            "failed": last_run.failed, "created_at": last_run.created_at,
        } if last_run else None,
        "component_versions": {
            "iteration_version": f"V{active.version_no}" if active else "基础版",
            "iteration_name": active.name if active else "",
            "knowledge_version": knowledge_version or "unknown",
            "structured_rulebase_version": structured_version or "unknown",
            "structured_rulebase_name": structured_name,
        },
    }


@router.post("/corrections")
def create_correction(
    body: CorrectionCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("reports.review")),
):
    if body.correction_type not in CORRECTION_TYPES:
        raise HTTPException(status_code=400, detail="纠错类型不合法")
    report = db.query(models.Report).filter(models.Report.id == body.report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    row = models.ReviewCorrection(
        report_id=report.id,
        correction_type=body.correction_type,
        original_json=body.original_json or {},
        corrected_json=body.corrected_json,
        description=body.description.strip(),
        standard_ref=body.standard_ref.strip(),
        submitted_by=current.id,
    )
    db.add(row)
    db.flush()
    add_audit(db, current, "审核迭代", "提交审核纠错", f"{_report_label(report)}，{CORRECTION_TYPES[body.correction_type]}", request)
    db.commit()
    return {"id": row.id, "status": row.status}


@router.get("/reports/{report_id}/corrections")
def report_corrections(
    report_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("reports.view")),
):
    rows = (
        db.query(models.ReviewCorrection)
        .options(selectinload(models.ReviewCorrection.report).selectinload(models.Report.company), selectinload(models.ReviewCorrection.submitter), selectinload(models.ReviewCorrection.reviewer))
        .filter(models.ReviewCorrection.report_id == report_id)
        .order_by(models.ReviewCorrection.id.desc())
        .all()
    )
    return [_correction_out(row) for row in rows]


@router.get("/corrections")
def list_corrections(
    status: str = "",
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("iteration.manage")),
):
    query = db.query(models.ReviewCorrection).options(
        selectinload(models.ReviewCorrection.report).selectinload(models.Report.company),
        selectinload(models.ReviewCorrection.submitter),
        selectinload(models.ReviewCorrection.reviewer),
    )
    if status:
        query = query.filter(models.ReviewCorrection.status == status)
    return [_correction_out(row) for row in query.order_by(models.ReviewCorrection.id.desc()).all()]


@router.put("/corrections/{correction_id}/review")
def review_correction(
    correction_id: int,
    body: CorrectionReview,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("iteration.manage")),
):
    row = db.query(models.ReviewCorrection).options(selectinload(models.ReviewCorrection.report)).filter(models.ReviewCorrection.id == correction_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="纠错记录不存在")
    row.status = body.status
    row.reviewed_by = current.id
    row.review_note = body.review_note.strip()
    row.reviewed_at = models._utc_now()
    created_rule = None
    if body.status == "approved" and body.create_rule:
        created_rule = db.query(models.ReviewRule).filter(models.ReviewRule.source_correction_id == row.id).first()
        if not created_rule:
            corrected = row.corrected_json or {}
            correct_text = body.rule_text.strip() or row.description
            if corrected:
                values = "；".join(f"{key}={value}" for key, value in corrected.items() if value not in (None, ""))
                if values:
                    correct_text = f"{correct_text}。正确处理：{values}"
            created_rule = models.ReviewRule(
                title=body.rule_title.strip() or f"{CORRECTION_TYPES.get(row.correction_type, '审核')}：{row.description[:80]}",
                category=body.rule_category.strip() or "人工纠错",
                product_unit=row.report.product_unit if row.report else "",
                applicable_conditions=body.applicable_conditions.strip(),
                rule_text=correct_text,
                standard_ref=row.standard_ref or "",
                source_correction_id=row.id,
                created_by=current.id,
                updated_by=current.id,
            )
            db.add(created_rule)
            db.flush()
    add_audit(db, current, "审核迭代", "确认审核纠错", f"纠错 #{row.id}：{body.status}", request)
    db.commit()
    return {"id": row.id, "status": row.status, "rule_id": created_rule.id if created_rule else None}


def _active_rule_ids(db: Session) -> set[int]:
    active = db.query(models.ReviewVersion).filter(models.ReviewVersion.status == "active").first()
    return {int(rule.get("id")) for rule in (active.rules_json or []) if rule.get("id")} if active else set()


@router.get("/rules")
def list_rules(db: Session = Depends(get_db), _: models.User = Depends(require_permission("iteration.manage"))):
    active_ids = _active_rule_ids(db)
    return [_rule_out(row, active_ids) for row in db.query(models.ReviewRule).order_by(models.ReviewRule.id.desc()).all()]


@router.post("/rules")
def create_rule(body: RuleIn, request: Request, db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    row = models.ReviewRule(**body.model_dump(), created_by=current.id, updated_by=current.id)
    db.add(row)
    db.flush()
    add_audit(db, current, "审核迭代", "新增审核规则", row.title, request)
    db.commit()
    return _rule_out(row, _active_rule_ids(db))


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleUpdate, request: Request, db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    row = db.query(models.ReviewRule).filter(models.ReviewRule.id == rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="规则不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    row.updated_by = current.id
    add_audit(db, current, "审核迭代", "更新审核规则", row.title, request)
    db.commit()
    db.refresh(row)
    return _rule_out(row, _active_rule_ids(db))


@router.get("/versions")
def list_versions(db: Session = Depends(get_db), _: models.User = Depends(require_permission("iteration.manage"))):
    return [_version_out(row) for row in db.query(models.ReviewVersion).order_by(models.ReviewVersion.version_no.desc()).all()]


@router.post("/versions")
def create_version(body: VersionCreate, request: Request, db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    rules = db.query(models.ReviewRule).filter(models.ReviewRule.id.in_(body.rule_ids), models.ReviewRule.status != "retired").all() if body.rule_ids else []
    if len(rules) != len(set(body.rule_ids)):
        raise HTTPException(status_code=400, detail="包含不存在或已停用的规则")
    next_no = (db.query(func.max(models.ReviewVersion.version_no)).scalar() or 0) + 1
    row = models.ReviewVersion(
        version_no=next_no, name=body.name.strip(), description=body.description.strip(),
        rules_json=[rule_snapshot(rule) for rule in rules], created_by=current.id,
    )
    db.add(row)
    db.flush()
    add_audit(db, current, "审核迭代", "创建审核版本", f"V{next_no} {row.name}", request)
    db.commit()
    return _version_out(row)


def _activate_version(db: Session, row: models.ReviewVersion, current: models.User):
    db.query(models.ReviewVersion).filter(models.ReviewVersion.status == "active", models.ReviewVersion.id != row.id).update({"status": "archived"})
    row.status = "active"
    row.published_by = current.id
    row.published_at = models._utc_now()
    included_ids = {int(rule.get("id")) for rule in (row.rules_json or []) if rule.get("id")}
    db.query(models.ReviewRule).filter(models.ReviewRule.status == "active").update({"status": "draft"})
    if included_ids:
        db.query(models.ReviewRule).filter(models.ReviewRule.id.in_(included_ids)).update({"status": "active"}, synchronize_session=False)


@router.post("/versions/{version_id}/publish")
def publish_version(version_id: int, request: Request, db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    row = db.query(models.ReviewVersion).filter(models.ReviewVersion.id == version_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")
    _activate_version(db, row, current)
    add_audit(db, current, "审核迭代", "发布审核版本", f"V{row.version_no} {row.name}", request)
    db.commit()
    return _version_out(row)


@router.post("/versions/{version_id}/rollback")
def rollback_version(version_id: int, request: Request, db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    row = db.query(models.ReviewVersion).filter(models.ReviewVersion.id == version_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")
    _activate_version(db, row, current)
    add_audit(db, current, "审核迭代", "回退审核版本", f"V{row.version_no} {row.name}", request)
    db.commit()
    return _version_out(row)


@router.get("/regression-cases")
def list_cases(db: Session = Depends(get_db), _: models.User = Depends(require_permission("iteration.manage"))):
    rows = db.query(models.RegressionCase).options(selectinload(models.RegressionCase.report)).order_by(models.RegressionCase.id.desc()).all()
    return [{
        "id": row.id, "report_id": row.report_id, "report_label": _report_label(row.report), "name": row.name,
        "expected_conclusion": row.expected_conclusion, "expected_issue_count": row.expected_issue_count,
        "enabled": row.enabled, "notes": row.notes or "", "created_at": row.created_at,
    } for row in rows]


@router.post("/regression-cases")
def create_case(body: RegressionCaseCreate, request: Request, db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    report = db.query(models.Report).filter(models.Report.id == body.report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    exists = db.query(models.RegressionCase).filter(models.RegressionCase.report_id == report.id).first()
    if exists:
        raise HTTPException(status_code=409, detail="该报告已在回归测试集中")
    row = models.RegressionCase(**body.model_dump(), created_by=current.id)
    db.add(row)
    add_audit(db, current, "审核迭代", "加入回归测试集", _report_label(report), request)
    db.commit()
    return {"id": row.id}


@router.put("/regression-cases/{case_id}/enabled")
def toggle_case(case_id: int, enabled: bool = Body(..., embed=True), db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    row = db.query(models.RegressionCase).filter(models.RegressionCase.id == case_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="测试样本不存在")
    row.enabled = enabled
    db.commit()
    return {"id": row.id, "enabled": row.enabled}


@router.get("/regression-runs")
def list_runs(db: Session = Depends(get_db), _: models.User = Depends(require_permission("iteration.manage"))):
    rows = db.query(models.RegressionRun).order_by(models.RegressionRun.id.desc()).limit(50).all()
    return [{
        "id": row.id, "version_id": row.version_id, "status": row.status, "total": row.total,
        "passed": row.passed, "failed": row.failed, "results_json": row.results_json,
        "created_at": row.created_at, "finished_at": row.finished_at,
    } for row in rows]


@router.post("/regression-runs")
def run_regression(request: Request, db: Session = Depends(get_db), current: models.User = Depends(require_permission("iteration.manage"))):
    active = db.query(models.ReviewVersion).filter(models.ReviewVersion.status == "active").first()
    cases = db.query(models.RegressionCase).options(selectinload(models.RegressionCase.report)).filter(models.RegressionCase.enabled.is_(True)).all()
    results = []
    passed = 0
    for case in cases:
        report = case.report
        actual_conclusion = report.conclusion if report else "报告已删除"
        actual_count = issue_count(report.result_json if report else None)
        ok = actual_conclusion == case.expected_conclusion and actual_count == case.expected_issue_count
        passed += int(ok)
        results.append({
            "case_id": case.id, "name": case.name, "report_id": case.report_id,
            "report_label": _report_label(report), "passed": ok,
            "expected_conclusion": case.expected_conclusion, "actual_conclusion": actual_conclusion,
            "expected_issue_count": case.expected_issue_count, "actual_issue_count": actual_count,
        })
    row = models.RegressionRun(
        version_id=active.id if active else None, total=len(cases), passed=passed,
        failed=len(cases) - passed, results_json=results, created_by=current.id,
        finished_at=models._utc_now(),
    )
    db.add(row)
    if active:
        active.metrics_json = {
            **(active.metrics_json if isinstance(active.metrics_json, dict) else {}),
            "total": row.total,
            "passed": row.passed,
            "failed": row.failed,
            "run_at": row.finished_at.isoformat(),
        }
    add_audit(db, current, "审核迭代", "执行回归校验", f"通过 {passed}/{len(cases)}", request)
    db.commit()
    return {"id": row.id, "total": row.total, "passed": row.passed, "failed": row.failed, "results_json": results}


# ===== 知识库浏览（只读）=====

@router.get("/knowledge")
def list_knowledge(
    _: models.User = Depends(require_permission("iteration.manage")),
):
    """返回知识库文件树。"""
    knowledge_dir = Path(get_settings().knowledge_dir)
    if not knowledge_dir.exists():
        return []

    tree: list[dict[str, Any]] = []
    for path in sorted(knowledge_dir.rglob("*.md")):
        relative = path.relative_to(knowledge_dir)
        parts = relative.parts
        current = tree
        for i, part in enumerate(parts):
            is_file = i == len(parts) - 1
            existing = next((c for c in current if c["name"] == part), None)
            if existing:
                current = existing.setdefault("children", [])
            else:
                node: dict[str, Any] = {
                    "name": part,
                    "path": str(relative.as_posix()),
                    "is_file": is_file,
                }
                if not is_file:
                    node["children"] = []
                current.append(node)
                current = node.setdefault("children", [])
    return tree


@router.get("/knowledge/{file_path:path}")
def read_knowledge(
    file_path: str,
    _: models.User = Depends(require_permission("iteration.manage")),
):
    """读取单个知识库 Markdown 文件内容。"""
    # 前置过滤：禁止路径中的目录遍历组件（HTTP层可能已做URL规范化，仍保留防御）
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(status_code=400, detail="非法路径")
    knowledge_dir = Path(get_settings().knowledge_dir).resolve()
    target = (knowledge_dir / file_path).resolve()
    # 二次校验：确保解析后仍在知识库目录内
    if not str(target).startswith(str(knowledge_dir)):
        raise HTTPException(status_code=400, detail="非法路径")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")
    return {"path": file_path, "content": content}


# ===== 结构化规则库浏览（只读）=====

@router.get("/rulebase")
def read_rulebase(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("iteration.manage")),
):
    """返回结构化规则库的参数规则、版本和变更日志。
    兼容旧版SQLite（无规则库表时返回空数据）。
    """
    result: dict[str, Any] = {"parameter_rules": [], "rule_versions": [], "rule_change_log": []}
    try:
        with db.begin_nested():
            rows = db.execute(text("""
                SELECT id, test_method_id, parameter_name, operator, parameter_value, unit,
                       applicable_conditions, priority, evidence_page, evidence_text,
                       verification_status, enabled_for_review
                FROM parameter_rules
                ORDER BY id
            """)).mappings().all()
            result["parameter_rules"] = [dict(r) for r in rows]
    except Exception:
        pass

    try:
        with db.begin_nested():
            rows = db.execute(text("""
                SELECT id, version_no, name, description, status, created_at, source_snapshot_sha256
                FROM rule_versions
                ORDER BY id DESC
            """)).mappings().all()
            result["rule_versions"] = [dict(r) for r in rows]
    except Exception:
        pass

    try:
        with db.begin_nested():
            rows = db.execute(text("""
                SELECT id, change_version, target_table, target_id, change_type, field_changed,
                       old_value, new_value, change_reason, changed_at, evidence_ref
                FROM rule_change_log
                ORDER BY id DESC
            """)).mappings().all()
            result["rule_change_log"] = [dict(r) for r in rows]
    except Exception:
        pass

    return result


@router.get("/rulebase-tree")
def read_rulebase_tree(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("iteration.manage")),
):
    """返回面向展示的结构化规则知识图谱。

    图谱保留数据库中两条真实规则链：
    1. 材料体系 -> 标准系列 -> 产品标准 -> 产品型号 -> 试验类别 -> 适用性规则；
    2. 试验类别 -> 试验项目 -> 试验方法 -> 参数规则。
    """
    matrix_rows = db.execute(text("""
        SELECT ss.material_family, ss.series_code, ss.series_name,
               ps.id AS standard_id, ps.standard_no, ps.standard_name,
               pm.id AS model_id, pm.model_code, pm.model_name, pm.product_unit,
               ti.id AS item_id, ti.item_name, COALESCE(ti.category, '其他') AS category,
               mtm.id AS rule_id, mtm.applicability_status, mtm.applicable_conditions,
               mtm.not_applicable_reason, mtm.table_item_no, mtm.evidence_text,
               mtm.verification_status
        FROM model_test_matrix mtm
        JOIN product_models pm ON pm.id = mtm.model_id
        JOIN product_standards ps ON ps.id = mtm.product_standard_id
        LEFT JOIN standard_series ss ON ss.id = ps.series_id
        JOIN test_items ti ON ti.id = mtm.test_item_id
        WHERE mtm.enabled_for_review = 'true'
        ORDER BY ss.material_family, ss.series_code, ps.standard_no, pm.model_code, ti.category, ti.item_name
    """)).mappings().all()
    parameter_rows = db.execute(text("""
        SELECT COALESCE(ti.category, '其他') AS category, ti.id AS item_id, ti.item_name,
               tm.id AS method_id, tm.method_name, tm.clause_no,
               ps.standard_no AS method_standard,
               pr.id AS rule_id, pr.parameter_name, pr.operator, pr.parameter_value, pr.unit,
               pr.applicable_conditions, pr.evidence_text, pr.verification_status
        FROM parameter_rules pr
        JOIN test_methods tm ON tm.id = pr.test_method_id
        JOIN test_items ti ON ti.id = tm.test_item_id
        LEFT JOIN product_standards ps ON ps.id = tm.method_standard_id
        WHERE pr.enabled_for_review = 'true'
        ORDER BY ti.category, ti.item_name, tm.method_name, pr.priority DESC, pr.id
    """)).mappings().all()

    family_names = {
        "PVC": "聚氯乙烯绝缘电缆",
        "rubber": "橡皮绝缘电缆",
        "other": "通用方法与管理规则",
    }

    def node(name: str, node_type: str, **meta: Any) -> dict[str, Any]:
        return {"name": name, "type": node_type, "meta": meta, "children": []}

    def child(parent: dict[str, Any], key: str, name: str, node_type: str, **meta: Any) -> dict[str, Any]:
        index = parent.setdefault("_index", {})
        if key not in index:
            created = node(name, node_type, **meta)
            parent["children"].append(created)
            index[key] = created
        return index[key]

    matrix_root = node("型号—项目适用矩阵", "rule_layer", description="回答某一型号应做、条件适用或不适用哪些试验")
    for row in matrix_rows:
        family_code = str(row["material_family"] or "other")
        family = child(matrix_root, family_code, family_names.get(family_code, family_code), "material_family", code=family_code)
        series_code = str(row["series_code"] or "未归类标准")
        series = child(family, series_code, series_code, "standard_series", description=row["series_name"] or "")
        standard = child(series, str(row["standard_id"]), str(row["standard_no"]), "product_standard", description=row["standard_name"] or "")
        model = child(
            standard, str(row["model_id"]), str(row["model_code"]), "product_model",
            model_name=row["model_name"] or "", product_unit=row["product_unit"] or "",
        )
        category = child(model, str(row["category"]), str(row["category"]), "test_category")
        status_text = {
            "required": "必做", "conditional": "条件适用",
            "not_applicable": "不适用", "unresolved": "待核实",
        }.get(str(row["applicability_status"]), str(row["applicability_status"]))
        display_item_name = str(row["item_name"])
        if status_text == "不适用":
            display_item_name = f"{display_item_name}（不适用）"
        category["children"].append(node(
            display_item_name, "applicability_rule",
            rule_id=row["rule_id"], status=status_text,
            condition=row["applicable_conditions"] or row["not_applicable_reason"] or "",
            table_item=row["table_item_no"] or "", evidence=row["evidence_text"] or "",
            verification=row["verification_status"], standard=row["standard_no"], model=row["model_code"],
        ))

    parameter_root = node("试验参数与判定规则", "rule_layer", description="回答试验条件、标准限值、计算公式和判定口径")
    for row in parameter_rows:
        category = child(parameter_root, str(row["category"]), str(row["category"]), "test_category")
        item = child(category, str(row["item_id"]), str(row["item_name"]), "test_item")
        method_label = str(row["method_name"])
        if row["method_standard"]:
            method_label = f"{method_label} · {row['method_standard']}"
        method = child(
            item, str(row["method_id"]), method_label, "test_method",
            clause=row["clause_no"] or "", standard=row["method_standard"] or "",
        )
        unit = f" {row['unit']}" if row["unit"] else ""
        value_text = f"{row['operator']}{row['parameter_value']}{unit}"
        method["children"].append(node(
            f"{row['parameter_name']} {value_text}", "parameter_rule",
            rule_id=row["rule_id"], parameter=row["parameter_name"], value=value_text,
            condition=row["applicable_conditions"] or "", evidence=row["evidence_text"] or "",
            verification=row["verification_status"],
        ))

    def finalize(current: dict[str, Any]) -> int:
        current.pop("_index", None)
        children = current.get("children") or []
        if not children:
            current["count"] = 1
            return 1
        count = sum(finalize(item) for item in children)
        current["count"] = count
        return count

    root = node("CCC电线电缆审核规则库", "root", description="可追溯的型号适用性与试验参数规则体系")
    root["children"] = [matrix_root, parameter_root]
    finalize(root)

    counts = db.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM standard_series) AS series,
          (SELECT COUNT(*) FROM product_standards) AS standards,
          (SELECT COUNT(*) FROM product_models) AS models,
          (SELECT COUNT(*) FROM test_items) AS test_items
    """)).mappings().one()
    latest_version = db.execute(text("""
        SELECT version_no, name FROM rule_versions ORDER BY id DESC LIMIT 1
    """)).mappings().first()
    return {
        "summary": {
            **dict(counts),
            "matrix_rules": len(matrix_rows),
            "parameter_rules": len(parameter_rows),
            "total_rules": len(matrix_rows) + len(parameter_rows),
            "version": latest_version["version_no"] if latest_version else "",
            "version_name": latest_version["name"] if latest_version else "",
        },
        "tree": root,
    }

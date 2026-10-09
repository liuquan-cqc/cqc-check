"""企业相关API。"""
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from backend.app import models, schemas
from backend.app.auth import get_current_user, require_permission
from backend.app.database import get_db
from backend.app.audit import add_audit

router = APIRouter(prefix="/api/companies", tags=["companies"])

OPERATING_STATUSES = {"unknown", "active", "cancelled", "revoked", "moved", "other"}
VERIFICATION_TASK_STATUSES = {"queued", "processing", "done", "failed", "cancelled"}


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _validate_status(status: str, source: str | None) -> None:
    if status not in OPERATING_STATUSES:
        raise HTTPException(status_code=400, detail="不支持的企业存续状态")
    if status != "unknown" and not source:
        raise HTTPException(status_code=400, detail="标记企业状态时必须填写核验来源")


def _apply_registry_result(company: models.Company, result: dict[str, str]) -> None:
    company.operating_status = result["operating_status"]
    company.status_source = f"天眼查企业数据接口（原始状态：{result['raw_status']}）"
    company.status_checked_at = models._utc_now()
    if result.get("unified_social_credit_code"):
        company.unified_social_credit_code = result["unified_social_credit_code"]
    if not company.address and result.get("address"):
        company.address = result["address"]


def _queue_browser_verification(db: Session, company: models.Company, user_id: int) -> models.CompanyVerificationTask:
    existing = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.company_id == company.id,
        models.CompanyVerificationTask.status.in_(["queued", "processing"]),
    ).order_by(models.CompanyVerificationTask.id.desc()).first()
    if existing:
        return existing
    task = models.CompanyVerificationTask(
        company_id=company.id,
        status="queued",
        requested_by=user_id,
    )
    db.add(task)
    db.flush()
    return task


def _verification_task_payload(task: models.CompanyVerificationTask) -> dict:
    return {
        "id": task.id,
        "company_id": task.company_id,
        "company_name": task.company.name,
        "status": task.status,
        "device_name": task.device.name if task.device else None,
        "requested_by": task.requester.username if task.requester else None,
        "raw_status": task.raw_status,
        "source_url": task.source_url,
        "error_msg": task.error_msg,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


@router.get("", response_model=list[schemas.CompanyOut])
def list_companies(
    q: str = "",
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """企业列表，支持按名称搜索；附带报告数和最近成功审核完成时间。"""
    query = db.query(
        models.Company,
        func.count(func.distinct(models.Report.id)).label("report_count"),
        func.max(models.Task.finished_at).label("last_review_at"),
    ).select_from(models.Company).outerjoin(
        models.Report,
        models.Report.company_id == models.Company.id,
    ).outerjoin(
        models.Task,
        (models.Task.report_id == models.Report.id) & (models.Task.status == "done"),
    )
    if q:
        query = query.filter(models.Company.name.ilike(f"%{q}%"))
    rows = query.group_by(models.Company.id).order_by(func.lower(models.Company.name)).all()
    return [
        {
            "id": company.id,
            "name": company.name,
            "address": company.address,
            "unified_social_credit_code": company.unified_social_credit_code,
            "operating_status": company.operating_status or "unknown",
            "status_source": company.status_source,
            "status_checked_at": company.status_checked_at,
            "created_at": company.created_at,
            "report_count": report_count,
            "last_review_at": last_review_at,
        }
        for company, report_count, last_review_at in rows
    ]


@router.post("", response_model=schemas.CompanyOut)
def create_company(
    body: schemas.CompanyIn,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    """新增企业（仅admin）。"""
    if not body.name:
        raise HTTPException(status_code=400, detail="企业名称不能为空")
    company_name = body.name.strip()
    if not company_name:
        raise HTTPException(status_code=400, detail="企业名称不能为空")
    status = body.operating_status or "unknown"
    source = _normalized(body.status_source)
    _validate_status(status, source)
    exists = db.query(models.Company).filter(models.Company.name == company_name).first()
    if exists:
        raise HTTPException(status_code=409, detail="企业已存在")
    company = models.Company(
        name=company_name,
        address=_normalized(body.address),
        unified_social_credit_code=_normalized(body.unified_social_credit_code),
        operating_status=status,
        status_source=source,
        status_checked_at=models._utc_now() if status != "unknown" else None,
    )
    db.add(company)
    add_audit(db, current, "企业管理", "新增企业", f"企业：{company_name}", request)
    db.commit()
    db.refresh(company)
    return company


@router.post("/verify-status-batch")
def verify_company_status_batch(
    request: Request,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    """逐家精确匹配核验；单家失败不影响其他企业。"""
    from backend.app.company_registry import TianyanchaClient
    only_unverified = bool(body.get("only_unverified", True))
    query = db.query(models.Company).filter(models.Company.name != "示例电缆有限公司")
    if only_unverified:
        query = query.filter(models.Company.operating_status == "unknown")
    companies = query.order_by(models.Company.id).limit(100).all()
    client = TianyanchaClient(db)
    results: list[dict] = []
    succeeded = 0
    for company in companies:
        try:
            registry_result = client.search_company(company.name)
            _apply_registry_result(company, registry_result)
            results.append({"id": company.id, "name": company.name, "ok": True, "status": registry_result["raw_status"]})
            succeeded += 1
        except Exception as exc:
            results.append({"id": company.id, "name": company.name, "ok": False, "error": str(exc)})
    add_audit(
        db, current, "企业核验", "批量核验企业状态",
        f"总数：{len(companies)}；成功：{succeeded}；失败：{len(companies) - succeeded}", request,
        success=(succeeded == len(companies)),
    )
    db.commit()
    return {"total": len(companies), "succeeded": succeeded, "failed": len(companies) - succeeded, "items": results}


@router.post("/browser-verification-batch")
def queue_browser_verification_batch(
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    """生成今日 Edge 网页核验队列，不访问天眼查、不读取 Cookie。"""
    from backend.app.settings_store import get_section
    config = get_section("company_registry", db)
    if not config.get("enabled") or config.get("mode") != "browser":
        raise HTTPException(status_code=400, detail="请先在系统设置中启用 Edge 网页辅助核验")
    limit = int(config.get("web_daily_limit", 10))
    active_company_ids = {
        row[0] for row in db.query(models.CompanyVerificationTask.company_id).filter(
            models.CompanyVerificationTask.status.in_(["queued", "processing"])
        ).all()
    }
    query = db.query(models.Company).filter(
        models.Company.name != "示例电缆有限公司",
        models.Company.operating_status == "unknown",
    )
    if active_company_ids:
        query = query.filter(~models.Company.id.in_(active_company_ids))
    companies = query.order_by(models.Company.id).limit(limit).all()
    tasks = [_queue_browser_verification(db, company, current.id) for company in companies]
    add_audit(db, current, "企业核验", "生成 Edge 核验批次", f"已入队：{len(tasks)} 家；每日上限：{limit}", request)
    db.commit()
    return {"queued": len(tasks), "daily_limit": limit, "task_ids": [task.id for task in tasks]}


@router.get("/browser-verification-summary")
def browser_verification_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("companies.manage")),
):
    counts = {
        status: db.query(models.CompanyVerificationTask).filter(
            models.CompanyVerificationTask.status == status
        ).count()
        for status in ("queued", "processing", "done", "failed", "cancelled")
    }
    return counts


@router.get("/browser-verification-tasks")
def list_browser_verification_tasks(
    status: str = "active",
    limit: int = 100,
    db: Session = Depends(get_db),
    _: models.User = Depends(require_permission("companies.manage")),
):
    """管理员查看企业核验队列和历史；默认只显示仍会阻塞队列的任务。"""
    if status not in VERIFICATION_TASK_STATUSES | {"active", "all"}:
        raise HTTPException(status_code=400, detail="不支持的核验任务状态")
    query = db.query(models.CompanyVerificationTask)
    if status == "active":
        query = query.filter(models.CompanyVerificationTask.status.in_(["queued", "processing"]))
    elif status != "all":
        query = query.filter(models.CompanyVerificationTask.status == status)
    tasks = query.order_by(models.CompanyVerificationTask.id.desc()).limit(max(1, min(int(limit), 200))).all()
    summary = {
        item_status: db.query(models.CompanyVerificationTask).filter(
            models.CompanyVerificationTask.status == item_status
        ).count()
        for item_status in ("queued", "processing", "done", "failed", "cancelled")
    }
    return {"summary": summary, "items": [_verification_task_payload(task) for task in tasks]}


@router.post("/browser-verification-tasks/{task_id}/remove")
def remove_browser_verification_task(
    task_id: int,
    request: Request,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    """将卡住或不需要核验的企业移出队列；保留记录和原因，不删除企业资料。"""
    task = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.id == task_id
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="企业核验任务不存在")
    if task.status not in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="该任务已经结束，无需移出队列")
    reason = str(body.get("reason") or "管理员手动移出核验队列").strip()[:800]
    company_name = task.company.name
    previous_status = task.status
    task.status = "cancelled"
    task.device_id = None
    task.error_msg = reason
    task.finished_at = models._utc_now()
    add_audit(
        db, current, "企业核验", "移出企业核验队列",
        f"任务：{task.id}；企业：{company_name}；原状态：{previous_status}；原因：{reason}", request,
    )
    db.commit()
    db.refresh(task)
    return _verification_task_payload(task)


@router.post("/{company_id}/browser-verification")
def queue_company_browser_verification(
    company_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    from backend.app.settings_store import get_section
    config = get_section("company_registry", db)
    if not config.get("enabled") or config.get("mode") != "browser":
        raise HTTPException(status_code=400, detail="请先在系统设置中启用 Edge 网页辅助核验")
    company = db.query(models.Company).filter(models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    task = _queue_browser_verification(db, company, current.id)
    add_audit(db, current, "企业核验", "企业加入 Edge 核验队列", f"企业：{company.name}；任务：{task.id}", request)
    db.commit()
    return {"queued": True, "task_id": task.id, "company_id": company.id, "company_name": company.name}


@router.post("/{company_id}/verify-status", response_model=schemas.CompanyOut)
def verify_company_status(
    company_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    company = db.query(models.Company).filter(models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    try:
        from backend.app.company_registry import TianyanchaClient
        result = TianyanchaClient(db).search_company(company.name)
    except Exception as exc:
        add_audit(db, current, "企业核验", "核验企业状态", f"企业：{company.name}；{exc}", request, success=False)
        db.commit()
        raise HTTPException(status_code=502, detail=f"核验失败：{exc}")
    _apply_registry_result(company, result)
    add_audit(db, current, "企业核验", "核验企业状态", f"企业：{company.name}；状态：{result['raw_status']}", request)
    db.commit()
    db.refresh(company)
    return company


@router.put("/{company_id}", response_model=schemas.CompanyOut)
def update_company(
    company_id: int,
    body: schemas.CompanyIn,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    """修改企业名称/地址（仅admin）。"""
    company = db.query(models.Company).filter(models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    next_status = body.operating_status if body.operating_status is not None else (company.operating_status or "unknown")
    next_source = _normalized(body.status_source) if body.status_source is not None else company.status_source
    if next_status == "unknown":
        next_source = None
    _validate_status(next_status, next_source)

    if body.name is not None:
        company_name = body.name.strip()
        if not company_name:
            raise HTTPException(status_code=400, detail="企业名称不能为空")
        duplicate = db.query(models.Company).filter(
            models.Company.name == company_name,
            models.Company.id != company_id,
        ).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="企业名称已存在")
        company.name = company_name
    if body.address is not None:
        company.address = _normalized(body.address)
    if body.unified_social_credit_code is not None:
        company.unified_social_credit_code = _normalized(body.unified_social_credit_code)
    status_changed = next_status != (company.operating_status or "unknown") or next_source != company.status_source
    company.operating_status = next_status
    company.status_source = next_source
    if next_status == "unknown":
        company.status_checked_at = None
    elif status_changed:
        company.status_checked_at = models._utc_now()
    add_audit(
        db, current, "企业管理", "更新企业",
        f"企业 ID：{company_id}；工商状态：{next_status}；来源：{next_source or '待核验'}", request,
    )
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}")
def delete_company(
    company_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_permission("companies.manage")),
):
    """删除企业（仅admin）；有关联报告时返回409拒绝。"""
    company = db.query(models.Company).filter(models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    report_count = db.query(models.Report).filter(models.Report.company_id == company_id).count()
    if report_count > 0:
        raise HTTPException(status_code=409, detail="该企业存在关联报告，不能删除")
    company_name = company.name
    verification_task_count = db.query(models.CompanyVerificationTask).filter(
        models.CompanyVerificationTask.company_id == company_id
    ).count()
    if verification_task_count:
        db.query(models.CompanyVerificationTask).filter(
            models.CompanyVerificationTask.company_id == company_id
        ).delete(synchronize_session=False)
    db.delete(company)
    add_audit(
        db, current, "企业管理", "删除企业",
        f"企业：{company_name}；同步清理核验任务：{verification_task_count} 条", request,
    )
    db.commit()
    return {"ok": True}

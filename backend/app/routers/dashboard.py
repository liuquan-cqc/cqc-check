"""仪表盘统计API：任意登录用户可访问。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app import models
from backend.app.auth import require_permission
from backend.app.database import get_db

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# 审核项问题分类规则：按 item_name 关键词匹配，先命中先生效，都不命中归入"其他问题"
_ERROR_CATEGORY_KEYWORDS = [
    ("标识问题", ["标识", "标志", "标记", "印字", "字迹"]),
    ("结构尺寸问题", ["结构", "尺寸", "外径", "厚度", "护套", "节距", "绞合"]),
    ("电气性能问题", ["电阻", "电压", "耐压", "绝缘电阻", "导体电阻", "电气", "击穿"]),
    ("机械性能问题", ["拉伸", "断裂", "伸长率", "抗张", "老化", "机械"]),
    ("燃烧问题", ["燃烧", "阻燃", "耐火", "烟密度", "卤酸"]),
]


def _classify_item(item_name: str) -> str:
    """按关键词把审核项名称归类。"""
    name = item_name or ""
    for category, keywords in _ERROR_CATEGORY_KEYWORDS:
        if any(kw in name for kw in keywords):
            return category
    return "其他问题"


@router.get("/stats")
def stats(db: Session = Depends(get_db), _: models.User = Depends(require_permission("dashboard.view"))):
    """今日新增、处理中、总数、需修改数。"""
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = db.query(models.Report).filter(models.Report.created_at >= today_start).count()
    pending_count = db.query(models.Report).filter(
        models.Report.status.in_(["pending", "extracting", "reviewing"])
    ).count()
    total_count = db.query(models.Report).count()
    issue_count = db.query(models.Report).filter(
        or_(
            models.Report.conclusion.ilike("%需修改%"),
            models.Report.conclusion.ilike("%待人工复核%"),
        )
    ).count()
    return {
        "today_count": today_count,
        "pending_count": pending_count,
        "total_count": total_count,
        "issue_count": issue_count,
    }


@router.get("/trend")
def trend(db: Session = Depends(get_db), _: models.User = Depends(require_permission("dashboard.view"))):
    """近7天每天创建的报告数（含0的日期），date格式MM-DD。"""
    today = datetime.now().date()
    start = today - timedelta(days=6)
    rows = (
        db.query(func.date(models.Report.created_at), func.count(models.Report.id))
        .filter(models.Report.created_at >= datetime.combine(start, datetime.min.time()))
        .group_by(func.date(models.Report.created_at))
        .all()
    )
    count_by_date = {str(day): cnt for day, cnt in rows}
    result = []
    for i in range(7):
        day = start + timedelta(days=i)
        result.append({
            "date": day.strftime("%m-%d"),
            "count": count_by_date.get(str(day), 0),
        })
    return result


@router.get("/error-distribution")
def error_distribution(db: Session = Depends(get_db), _: models.User = Depends(require_permission("dashboard.view"))):
    """审核不通过项按问题类别统计，返回[{name, value}]。"""
    names = (
        db.query(models.ReviewItem.item_name)
        .filter(models.ReviewItem.status != "ok")
        .all()
    )
    counter = {}
    for (item_name,) in names:
        category = _classify_item(item_name)
        counter[category] = counter.get(category, 0) + 1
    return [{"name": name, "value": value} for name, value in counter.items()]

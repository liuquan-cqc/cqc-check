"""统一写入操作审计日志。调用方负责提交数据库事务。"""
from __future__ import annotations

from backend.app import models


def add_audit(db, user, module: str, action: str, detail: str = "", request=None, success: bool = True):
    ip = request.client.host if request and request.client else ""
    db.add(models.AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "system",
        module=module,
        action=action,
        detail=detail[:2000],
        ip_address=ip,
        success=success,
    ))

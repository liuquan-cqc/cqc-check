"""认证API：登录、当前用户信息。"""
from __future__ import annotations
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app import models, schemas
from backend.app.auth import get_current_user, verify_password, create_token
from backend.app.database import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])
_login_failures: dict[str, tuple[int, datetime | None]] = {}


@router.post("/login", response_model=schemas.LoginOut)
def login(body: schemas.LoginIn, request: Request, db: Session = Depends(get_db)):
    """用户名密码登录，返回JWT。密码错误或用户禁用均返回401。"""
    from backend.app.settings_store import get_section
    security = get_section("security", db)
    failures, locked_until = _login_failures.get(body.username, (0, None))
    if locked_until and locked_until > datetime.utcnow():
        remaining = max(1, int((locked_until - datetime.utcnow()).total_seconds() / 60) + 1)
        raise HTTPException(status_code=429, detail=f"登录失败次数过多，请 {remaining} 分钟后重试")

    user = db.query(models.User).filter(models.User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        failures += 1
        max_failures = int(security.get("login_max_failures", 5))
        if failures >= max_failures:
            locked_until = datetime.utcnow() + timedelta(minutes=int(security.get("lock_minutes", 15)))
            failures = 0
        _login_failures[body.username] = (failures, locked_until)
        db.add(models.AuditLog(
            user_id=user.id if user else None,
            username=body.username,
            module="安全",
            action="登录失败",
            detail="用户名或密码错误",
            ip_address=request.client.host if request.client else "",
            success=False,
        ))
        db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != "active":
        raise HTTPException(status_code=401, detail="用户已禁用")
    _login_failures.pop(body.username, None)
    db.add(models.AuditLog(
        user_id=user.id,
        username=user.username,
        module="安全",
        action="登录成功",
        ip_address=request.client.host if request.client else "",
        success=True,
    ))
    db.commit()
    return {"token": create_token(user), "user": user}


@router.get("/me", response_model=schemas.UserOut)
def me(user: models.User = Depends(get_current_user)):
    """返回当前登录用户信息。"""
    return user

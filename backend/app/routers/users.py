"""用户管理 API：管理员可为普通账号设置角色或用户专属权限。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app import models, schemas
from backend.app.auth import hash_password, require_role
from backend.app.database import get_db
from backend.app.audit import add_audit

router = APIRouter(prefix="/api/users", tags=["users"])

VALID_ROLES = ("admin", "reviewer", "viewer")
VALID_STATUS = ("active", "disabled")
ADMIN_ONLY_PERMISSIONS = {"users.manage", "settings.manage"}


def _validated_permissions(role: str, permissions: list[str] | None) -> list[str] | None:
    """管理员固定全权限；其他账号的自定义权限必须来自系统权限目录。"""
    if role == "admin" or permissions is None:
        return None
    from backend.app.settings_store import DEFAULT_SETTINGS
    valid = set(DEFAULT_SETTINGS["roles"]["admin"])
    unknown = set(permissions) - valid
    if unknown:
        raise HTTPException(status_code=400, detail=f"包含未知权限：{', '.join(sorted(unknown))}")
    forbidden = set(permissions) & ADMIN_ONLY_PERMISSIONS
    if forbidden:
        raise HTTPException(status_code=400, detail="用户管理和系统设置为管理员专属权限")
    report_actions = {"reports.upload", "reports.review", "reports.download", "reports.delete"}
    if set(permissions) & report_actions and "reports.view" not in permissions:
        raise HTTPException(status_code=400, detail="上传、审核、下载或删除报告时必须同时开放查看报告权限")
    return list(dict.fromkeys(permissions))


@router.get("", response_model=list[schemas.UserOut])
def list_users(
    db: Session = Depends(get_db),
    _: models.User = Depends(require_role("admin")),
):
    """用户列表。"""
    return db.query(models.User).order_by(models.User.id).all()


@router.post("", response_model=schemas.UserOut)
def create_user(
    body: schemas.UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_role("admin")),
):
    """创建用户，用户名重复返回409。"""
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="角色不合法")
    from backend.app.settings_store import get_section
    min_length = int(get_section("security", db).get("password_min_length", 6))
    if len(body.password) < min_length:
        raise HTTPException(status_code=400, detail=f"密码至少需要 {min_length} 位")
    exists = db.query(models.User).filter(models.User.username == body.username).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = models.User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        permissions_override=_validated_permissions(body.role, body.permissions_override),
        status="active",
    )
    db.add(user)
    permission_text = "继承角色" if user.permissions_override is None else f"自定义{len(user.permissions_override)}项"
    add_audit(db, current, "用户管理", "新增用户", f"用户：{body.username}，角色：{body.role}，权限：{permission_text}", request)
    db.commit()
    db.refresh(user)
    return user


@router.put("/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: int,
    body: schemas.UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: models.User = Depends(require_role("admin")),
):
    """修改用户密码/角色/状态；禁止admin禁用自己。"""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail="角色不合法")
        if user.id == current.id and user.role == "admin" and body.role != "admin":
            raise HTTPException(status_code=400, detail="不能修改当前登录管理员自己的角色")
        user.role = body.role
    if "permissions_override" in body.model_fields_set:
        user.permissions_override = _validated_permissions(user.role, body.permissions_override)
    elif user.role == "admin":
        user.permissions_override = None
    if body.status is not None:
        if body.status not in VALID_STATUS:
            raise HTTPException(status_code=400, detail="状态不合法")
        # 禁止admin禁用自己，避免把系统锁死
        if user.id == current.id and body.status == "disabled":
            raise HTTPException(status_code=400, detail="不能禁用当前登录的管理员账号")
        user.status = body.status
    if body.password:
        from backend.app.settings_store import get_section
        min_length = int(get_section("security", db).get("password_min_length", 6))
        if len(body.password) < min_length:
            raise HTTPException(status_code=400, detail=f"密码至少需要 {min_length} 位")
        user.password_hash = hash_password(body.password)
    changes = []
    if body.role is not None:
        changes.append(f"角色={body.role}")
    if "permissions_override" in body.model_fields_set:
        changes.append("权限=继承角色" if user.permissions_override is None else f"权限=自定义{len(user.permissions_override)}项")
    if body.status is not None:
        changes.append(f"状态={body.status}")
    if body.password:
        changes.append("重置密码")
    add_audit(db, current, "用户管理", "更新用户", f"用户：{user.username}，{'，'.join(changes)}", request)
    db.commit()
    db.refresh(user)
    return user

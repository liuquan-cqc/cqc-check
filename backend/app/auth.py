"""认证与权限：密码哈希、JWT签发/校验、角色依赖、启动播种。"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.app import models
from backend.app.config import get_settings
from backend.app.database import get_db

# pbkdf2参数：算法/迭代次数
_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 100_000

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """用标准库 pbkdf2_hmac + 随机salt生成哈希，格式：pbkdf2$iterations$salt_hex$hash_hex。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储的哈希是否一致。"""
    try:
        _scheme, iterations, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGO, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def create_token(user: models.User) -> str:
    """签发JWT（HS256，过期时间由配置控制）。"""
    settings = get_settings()
    from backend.app.settings_store import get_section
    security = get_section("security")
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "exp": datetime.utcnow() + timedelta(hours=int(security.get("jwt_expire_hours", settings.jwt_expire_hours))),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> models.User:
    """解析Bearer JWT，返回当前用户；失败一律401。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="未授权")
    try:
        payload = jwt.decode(credentials.credentials, get_settings().jwt_secret, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="未授权或登录已过期")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or user.status != "active":
        raise HTTPException(status_code=401, detail="未授权或用户已禁用")
    return user


def require_role(*roles: str):
    """角色校验依赖：角色不符返回403。"""
    def _dep(user: models.User = Depends(get_current_user)) -> models.User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="无权限")
        return user
    return _dep


def require_permission(permission: str):
    """按用户实际权限校验。用户专属权限优先于角色默认值。"""
    def _dep(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        from backend.app.settings_store import has_permission
        if not has_permission(user, permission, db):
            raise HTTPException(status_code=403, detail="无权限")
        return user
    return _dep


def seed_admin(db: Session):
    """users表为空时创建默认admin账号（密码取配置admin_password）。"""
    if db.query(models.User).count() == 0:
        admin = models.User(
            username="admin",
            password_hash=hash_password(get_settings().admin_password),
            role="admin",
            status="active",
        )
        db.add(admin)
        db.commit()

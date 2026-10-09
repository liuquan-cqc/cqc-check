"""FastAPI 入口：注册路由、托管前端静态文件、启动建表。"""
import os
from pathlib import Path
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from backend.app.database import init_db, SessionLocal
from backend.app.routers import reports, companies, auth as auth_router, users, dashboard, settings, iteration, browser_sync, sampling
from backend.app.auth import seed_admin
from backend.app.config import get_settings
from backend.app.settings_store import get_section


app = FastAPI(
    title="CCC电线电缆检测报告智能审核系统",
    description="PDF上传 → 自动审核 → 生成审核意见书",
    version="1.0.0",
)

# 开发期联调：允许所有来源跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_GATEWAY_HOST = os.getenv(
    "PUBLIC_GATEWAY_HOST", "public-gateway.cqc-check.internal"
).strip().lower()


@app.middleware("http")
async def public_access_gate(request: Request, call_next):
    """只控制 Cloudflare 公网入口，内网 IP 入口始终保留。"""
    request_host = request.headers.get("host", "").split(":", 1)[0].lower()
    if request_host != PUBLIC_GATEWAY_HOST:
        return await call_next(request)

    db = SessionLocal()
    try:
        enabled = bool(get_section("security", db).get("public_access_enabled", True))
    except Exception:
        # 公网门禁读取异常时安全失败；内网仍可用于排障和重新开启。
        enabled = False
    finally:
        db.close()

    if enabled:
        return await call_next(request)

    headers = {"Cache-Control": "no-store, max-age=0"}
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=403,
            content={"detail": "管理员已关闭公网访问，请使用单位内网入口"},
            headers=headers,
        )
    return HTMLResponse(
        status_code=403,
        headers=headers,
        content="""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>公网访问已关闭</title>
    <style>
      :root { color-scheme: light; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { min-height: 100vh; margin: 0; display: grid; place-items: center; background: #f3f6fa; color: #172033; }
      main { width: min(520px, calc(100% - 40px)); padding: 40px; box-sizing: border-box; background: #fff; border: 1px solid #dfe5ee; border-radius: 14px; box-shadow: 0 18px 45px rgba(31, 45, 61, .08); }
      .mark { width: 44px; height: 44px; display: grid; place-items: center; border-radius: 12px; background: #eef3f8; color: #44546a; font-weight: 700; }
      h1 { margin: 24px 0 10px; font-size: 26px; line-height: 1.3; }
      p { margin: 0; color: #5e6b7f; line-height: 1.75; }
      small { display: block; margin-top: 24px; color: #8a96a8; }
    </style>
  </head>
  <body>
    <main>
      <div class="mark">CQC</div>
      <h1>公网访问已关闭</h1>
      <p>管理员已暂停报告审核网站的公网入口。请通过单位内网访问，或联系系统管理员重新开启。</p>
      <small>电线电缆检测报告审核系统</small>
    </main>
  </body>
</html>""",
    )

app.include_router(auth_router.router)
app.include_router(users.router)
app.include_router(dashboard.router)
app.include_router(reports.router)
app.include_router(companies.router)
app.include_router(settings.router)
app.include_router(iteration.router)
app.include_router(browser_sync.router)
app.include_router(sampling.router)


@app.on_event("startup")
def on_startup():
    """启动时初始化数据库表，并在users表为空时播种默认admin。"""
    init_db()
    db = SessionLocal()
    try:
        seed_admin(db)
    finally:
        db.close()


@app.post("/api/login")
def login(password: str = Form(...)):
    """旧版简单管理员登录校验（保留兼容，新代码请用 /api/auth/login）。"""
    if password != get_settings().admin_password:
        raise HTTPException(status_code=401, detail="密码错误")
    return {"ok": True}


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """全局异常兜底。"""
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# 静态文件：前端构建产物（SPA），所有非 /api 的GET路径回退到 index.html
dist_dir = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if dist_dir.exists():
    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        """SPA history fallback：/api 以外的GET都返回 index.html。"""
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="接口不存在")
        # 根目录下的静态文件（如 favicon.ico）存在则直接返回
        candidate = dist_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(dist_dir / "index.html"))

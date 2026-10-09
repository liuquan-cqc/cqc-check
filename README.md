# 缆审智核（CQC Check）

缆审智核是一套面向检测报告的解析、智能审核与人工复核管理平台。系统将 PDF 内容提取、表格结构恢复、规则匹配、大模型复核和审核记录集中在同一工作流中，帮助审核人员更快地定位可疑项并保留完整的处理轨迹。

> 本仓库是已脱敏的源码版。内部规则库、标准摘录、审核案例、真实报告及生产环境信息均不在仓库中。

## 主要功能

- PDF 文字层优先解析，对扫描页补充 OCR 识别。
- 结合页面坐标恢复表格行列关系，保留原文证据与页码定位。
- 按报告类型分流，支持型式试验报告及专项报告的独立处理。
- 结构化规则与 AI 复核协同，输出审核结论、修改项和来源依据。
- 支持更正版本链，对原报告与更正报告进行追踪。
- 提供审核迭代中心，用于登记、确认和追溯纠错。
- 提供用户、角色和功能权限管理。
- 通过 Edge 扩展选择报告入口和 PDF 文件，同步到审核平台。
- 支持企业状态辅助核验、报告筛选、CSV 导出与处理状态查看。

## 技术架构

- 后端：FastAPI + Python
- 前端：Vue 3 + TypeScript + Vite + Element Plus
- 数据库：PostgreSQL
- 异步处理：独立 Worker
- 部署：Docker Compose
- 浏览器助手：Microsoft Edge Manifest V3 扩展

## 目录结构

```text
backend/                              API、报告解析、审核逻辑与 Worker
frontend/                             Web 管理端
browser-extension/edge-report-sync/  Edge 报告同步与企业核验助手
data/knowledge/                       授权知识包挂载目录
data/uploads/                         报告上传数据（不进入 Git）
data/ocr/                             文档解析产物（不进入 Git）
deploy/                               Docker Compose 部署配置
scripts/build.sh                      可选的本地镜像构建脚本
```

## 快速启动

### 1. 环境要求

- Docker Engine 24+ 或 Docker Desktop
- Docker Compose v2
- 可供系统调用的大模型服务
- 如需 OCR，准备对应的 OCR 或文档解析服务

### 2. 创建配置

```bash
cp .env.example .env
```

打开 `.env`，至少替换以下项：

- `ADMIN_PASSWORD`：首个管理员账号 `admin` 的密码。
- `DB_PASSWORD`：PostgreSQL 密码。
- `JWT_SECRET`：用于签发登录令牌的长随机字符串。
- `LLM_BASE_URL`、`LLM_MODEL`和 `SILICONFLOW_API_KEY`：模型服务配置。

不要把真实密钥写入 `.env.example` 或提交到 Git。

### 3. 准备知识包

将你有权使用的规则、标准摘录和结构化数据放入 `data/knowledge/`。仓库中的占位文件不包含可用于正式审核的内容。

### 4. 启动服务

```bash
docker compose --env-file .env -f deploy/docker-compose.source.yml up -d --build
```

启动后访问 [http://localhost:8080](http://localhost:8080)，使用用户名 `admin` 和你在 `.env` 中设置的密码登录。

查看运行状态：

```bash
docker compose --env-file .env -f deploy/docker-compose.source.yml ps
docker compose --env-file .env -f deploy/docker-compose.source.yml logs -f api worker
```

停止服务：

```bash
docker compose --env-file .env -f deploy/docker-compose.source.yml stop
```

## 系统配置

管理员登录后，可在“系统设置”中管理以下项目：

- AI 审核供应商、接口地址、模型和思考模式。
- OCR 引擎、PaddleOCR 及 MinerU 文档解析。
- 表格视觉修复和可选的内部模型。
- 报告保留周期、企业核验和浏览器同步。
- 公网访问状态与管理员确认流程。

建议先使用测试文档验证模型、OCR、知识包和结论格式，再导入实际工作数据。

## Edge 报告同步助手

1. 在 Edge 地址栏输入 `edge://extensions/`。
2. 开启“开发人员模式”，选择“加载解压缩的扩展”。
3. 选择 `browser-extension/edge-report-sync/` 目录。
4. 在系统的“系统设置 → 浏览器同步”中生成一次性配对码。
5. 在扩展中填写审核系统地址并完成配对。

扩展不请求 Cookie 权限，不读取 USB 密钥、证书私钥或登录 Cookie。使用方法详见 `browser-extension/edge-report-sync/README.md`。

## 数据与安全

- 建议仅在受控内网或通过身份认证保护的私有环境部署。
- `.env`、数据库、上传报告、OCR 产物、日志和备份不应进入版本库。
- 上线前应更换所有默认密码，限制数据库与服务端口的暴露范围。
- 报告可能含有企业、产品和个人信息，请在满足组织数据管理要求的前提下配置外部 API。
- 更多说明见 `SECURITY.md`。

## 使用边界

本平台用于辅助发现问题，不替代审核人员对原报告、适用标准和检验数据的最终判断。未配置经授权的知识包时，不应将系统结果用于正式审核。

# 在镜像构建阶段编译 Vue 前端，确保部署内容始终与源码一致。
FROM node:22-alpine AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim-bookworm

# 使用国内镜像源加速 apt（bookworm用deb822格式，兼容旧格式）
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g; s|security.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g; s|security.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list; \
    fi

# 安装系统依赖：pymupdf需要部分lib，rapidocr需要onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender-dev \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖清单，利用缓存层（使用清华PyPI镜像加速）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目代码
COPY . .

# 前端 dist 在 .dockerignore 中排除，由上面的构建阶段提供。
COPY --from=frontend-builder /frontend/dist /app/frontend/dist

# 创建上传与ocr目录
RUN mkdir -p /app/data/uploads /app/data/ocr

EXPOSE 8080

# 默认启动FastAPI服务；worker在docker-compose中通过command覆盖
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8080"]

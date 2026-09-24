# GCP Manager Web · 容器镜像
# 单阶段即可：纯 Python + 静态文件，无构建步骤（Vue 用全局构建本地托管）
FROM python:3.12-slim

# OCI 标准标签：镜像元数据里也带上版本与仓库，
# 便于 `docker inspect` 直接看出这是哪个版本。
# 版本号与 core/version.py 保持一致（升级时一并改）。
LABEL org.opencontainers.image.title="GCP Manager Web" \
      org.opencontainers.image.version="1.1.1" \
      org.opencontainers.image.source="https://github.com/2016xyz/GCP-Manager-Web" \
      org.opencontainers.image.url="https://github.com/2016xyz/GCP-Manager-Web" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GCPWEB_DATA_DIR=/app/data \
    PORT=8000

WORKDIR /app

# 依赖单独一层，改代码不必重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY core/ ./core/
COPY static/ ./static/
COPY run.sh install.sh ./

# 数据目录（服务账号、密码库、SSH 密钥）—— 生产环境用 volume 挂出来
RUN mkdir -p /app/data/keys && chmod 700 /app/data

# 用非 root 运行；数据目录交给该用户，否则挂载卷会权限不足
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/login',timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]

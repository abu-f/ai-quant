# 一条命令跑起本地工作台（可选，非必需）
#   docker compose up -d --build   →  http://127.0.0.1:8770
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AQUANT_HOME=/data \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，改动源码时可复用这一层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir .

EXPOSE 8770
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/api/stats', timeout=3).status == 200 else 1)"

# 只读行情 + 本地计算，监听容器内所有网卡；对外暴露仍由 compose 的端口映射控制
CMD ["python", "-m", "aquant", "web", "--host", "0.0.0.0", "--port", "8770"]

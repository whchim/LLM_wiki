# 后端镜像：FastAPI + 数据层 + 共享业务模块。
# 不含 LLM 引擎（编译/审核由宿主机 Claude Code 消费 vault/ 触发文件）；
# 不含前端（Vue 工作台由 Dockerfile.web 构建，见 docker-compose 的 web 服务）。

FROM python:3.11-slim

WORKDIR /app

# pip 源：默认走清华镜像（国内构建避免超时与下载损坏）；需要时用 --build-arg PIP_INDEX_URL=... 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

# 依赖单独分层，改代码不触发重装；挂载 pip 缓存，失败重试无需从零下载
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# 运行期需要的全部代码与规范文件
COPY api/ ./api/
COPY core/ ./core/
COPY schema.sql ./
COPY pytest.ini ./
COPY tests/ ./tests/
COPY tools/ ./tools/
COPY prompts/ ./prompts/
COPY workflows/ ./workflows/

# PYTHONPATH 含 core：api/ 内 `import db/ops/customer_state` 共享数据层与领域逻辑
ENV PYTHONPATH=/app:/app/core \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=20s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY streamlit_app/ ./streamlit_app/
COPY api/ ./api/
COPY schema.sql .
# PYTHONPATH 含 streamlit_app：api/ 内 `import db/ops` 共享数据层与业务逻辑
ENV PYTHONPATH=/app:/app/streamlit_app
EXPOSE 8501 8000
# 默认启动 Streamlit（docker-compose 中 api 服务会覆盖 CMD 为 uvicorn）
CMD ["streamlit", "run", "streamlit_app/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
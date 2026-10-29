FROM python:3.10-slim

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple fastapi uvicorn redis python-multipart prometheus-client python-json-logger

WORKDIR /app
COPY api/main.py .
COPY shared/ /app/shared/

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

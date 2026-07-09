FROM python:3.10-slim

RUN pip install --no-cache-dir requests

WORKDIR /app
COPY generator.py .

# 不自动运行 — 由 Dashboard 通过 docker exec 触发
CMD ["tail", "-f", "/dev/null"]

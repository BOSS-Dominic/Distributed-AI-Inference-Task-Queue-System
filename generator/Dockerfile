FROM python:3.10-slim

RUN pip install --no-cache-dir requests

WORKDIR /app
COPY generator/generator.py .
COPY shared/ /app/shared/

# idle by default — started by Dashboard via docker exec
CMD ["tail", "-f", "/dev/null"]

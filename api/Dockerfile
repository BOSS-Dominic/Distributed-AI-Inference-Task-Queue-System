FROM python:3.10-slim

RUN pip install --no-cache-dir fastapi uvicorn redis python-multipart

WORKDIR /app
COPY main.py .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

# the app does not need root
RUN useradd --create-home appuser
USER appuser

# Cloud Run tells the app which port to use through $PORT
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-server-header

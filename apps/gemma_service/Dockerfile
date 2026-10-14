FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PORT=8080

WORKDIR /app
COPY requirements-cloud.txt requirements-gpu.txt ./
RUN python -m pip install --no-cache-dir -r requirements-gpu.txt

COPY apps ./apps
COPY packages ./packages
COPY model_artifacts /models

USER 65532:65532
CMD ["sh", "-c", "uvicorn apps.gemma_service.app:app --host 0.0.0.0 --port ${PORT}"]

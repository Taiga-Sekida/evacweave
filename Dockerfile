FROM python:3.12-slim

# One image serves both Cloud Run services: the public one runs the default APP_MODULE,
# and scripts/deploy_cloud_run_orchestrator.sh sets
# APP_MODULE=apps.orchestrator_service.main:app for the private one.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080 \
    APP_MODULE=apps.api.main:app

WORKDIR /app
COPY requirements-api.txt ./
RUN python -m pip install --no-cache-dir -r requirements-api.txt

COPY apps ./apps
COPY packages ./packages
COPY data ./data

USER 65532:65532
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn \"${APP_MODULE}\" --host 0.0.0.0 --port \"${PORT}\" --no-access-log"]

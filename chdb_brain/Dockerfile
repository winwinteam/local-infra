FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

RUN pip install --no-cache-dir \
    "chdb>=3.0.0" \
    "mcp>=1.9.0,<2.0.0" \
    "starlette>=0.46.0" \
    "uvicorn>=0.34.0"

COPY app/ /app/app/

RUN mkdir -p /data

CMD ["python", "-m", "app.server"]

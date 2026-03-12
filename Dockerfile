FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    psycopg2-binary \
    hvac \
    httpx

COPY dashboard_api.py db.py models.py vault_env_loader.py dashboard.html ./

CMD ["python", "-m", "uvicorn", "dashboard_api:app", "--host", "0.0.0.0", "--port", "8080"]

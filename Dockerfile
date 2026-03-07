FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[all]"

COPY . .

CMD ["python", "-m", "cuga", "run", "--workflows-dir", "workflows/", \
    "--schedule"]

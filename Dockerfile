FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    cron \
    postgresql-client \
    default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backup.py lib.py restore.py pick.py ./

# lib.py raises ConfigError if .env is missing — an empty file satisfies the
# check while Docker-injected env vars (already in os.environ) take precedence
RUN touch /app/.env

RUN mkdir -p /app/logs /app/tmp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

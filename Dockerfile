FROM python:3.12-alpine

RUN apk add --no-cache bash curl sqlite

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh /app/run_daily_scraper.sh /app/scripts/*.sh

ENV DB_PATH=/data/jobs.db
ENV ENABLE_SCRAPER_CRON=1
ENV SCRAPER_CRON_SCHEDULE="15 4,12,20 * * *"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
  CMD wget -qO- "http://localhost:${PORT:-8000}/health" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-8000} -w ${WEB_CONCURRENCY:-2} --threads ${GUNICORN_THREADS:-8} --access-logfile - --error-logfile - --log-level ${LOG_LEVEL:-info} app:app"]

FROM python:3.12-alpine

RUN apk add --no-cache bash curl sqlite

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh /app/run_daily_scraper.sh

ENV DB_PATH=/data/jobs.db
ENV ENABLE_SCRAPER_CRON=1
ENV SCRAPER_CRON_SCHEDULE="15 6 * * *"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
  CMD wget -qO- http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "--threads", "8", \
     "--access-logfile", "-", "app:app"]

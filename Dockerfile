FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DB_PATH=/data/jobs.db
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
  CMD wget -qO- http://localhost:8000/health || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "--threads", "8", \
     "--access-logfile", "-", "app:app"]

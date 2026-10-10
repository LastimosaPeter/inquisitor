FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends docker.io ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /srv/inquisitor/data /srv/inquisitor/data/judge-work
EXPOSE 8000
CMD ["gunicorn","--bind","0.0.0.0:8000","--workers","1","--threads","8","--timeout","60","--access-logfile","-","--error-logfile","-","wsgi:app"]

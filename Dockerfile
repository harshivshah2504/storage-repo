FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8765

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8765

CMD ["sh", "-c", "gunicorn 'github_drive.webapp:create_app()' --bind 0.0.0.0:${PORT:-8765} --workers 1 --threads 4 --timeout 600"]

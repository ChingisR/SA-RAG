# 1. Use Python 3.11 for compatibility
FROM python:3.11-slim

# 2. Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /app

# 3. Install system dependencies (netcat needed for wait scripts)
RUN apt-get update && apt-get install -y \
    build-essential \
    libicu-dev \
    curl \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/*

# 4. Copy requirements first for caching
COPY requirements.txt .

# 5. Install Python libraries
RUN pip install --upgrade pip && \
    pip install --default-timeout=3000 --retries 20 -r requirements.txt

# 6. Copy the rest of the application code
COPY . .

# 7. Make wait scripts executable
RUN chmod +x /app/wait-for-opensearch.sh
RUN chmod +x /app/wait-for-postgres.sh

# 8. Expose the FastAPI port
EXPOSE 8000

# 9. Start with Gunicorn + UvicornWorker (4 workers)
# Gunicorn manages 4 OS-level Uvicorn processes, ensuring concurrent requests
# are not blocked by synchronous LlamaIndex / psycopg2 calls on any single worker.
CMD sh /app/wait-for-opensearch.sh && sh /app/wait-for-postgres.sh && python init_db.py && \
    gunicorn main:app \
      --workers 4 \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind 0.0.0.0:8000 \
      --timeout 900 \
      --graceful-timeout 30 \
      --preload \
      --log-level info
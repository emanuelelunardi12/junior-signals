FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATABASE_PATH=/data/signals.db
# One worker: the in-process scheduler must not run twice.
CMD ["sh", "-c", "uvicorn app.web:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

# Single stage: nothing is compiled (psycopg[binary] is a prebuilt wheel with libpq).
FROM python:3.12-slim

# Unbuffered output, so logs reach `docker logs` immediately; no .pyc files in the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first: this layer is reused as long as requirements.txt is unchanged.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

RUN useradd --system --no-create-home server
USER server

# 8000: REST API, 9000: TCP ingest.
EXPOSE 8000 9000

# Exec form, so uvicorn is PID 1 and handles the SIGTERM from `docker stop`.
# 0.0.0.0: listen on the container's network interface, not only its loopback.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

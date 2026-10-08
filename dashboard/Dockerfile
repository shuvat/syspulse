# Single stage, like the server: nothing is compiled.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first: this layer is reused as long as requirements.txt is unchanged.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py data.py ./

RUN useradd --system --create-home dashboard
USER dashboard

EXPOSE 8501

# Exec form, so streamlit is PID 1 and receives the SIGTERM from `docker stop`.
# 0.0.0.0: listen on the container's network interface; headless: do not try to open a
# browser; gatherUsageStats=false: no usage telemetry sent to Streamlit.
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]

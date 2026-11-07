FROM python:3.12.16-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY trafficgen/ trafficgen/

# Config, prompts and the JSONL log live in /data; mount a host directory there.
# The UI writes config.yaml back to this directory.
WORKDIR /data
EXPOSE 8080

ENTRYPOINT ["python", "-m", "trafficgen"]
# Default: the web UI. It must listen on 0.0.0.0 inside the container; limit
# who can reach it with the host side of the port mapping instead.
CMD ["--ui", "--host", "0.0.0.0", "--port", "8080", "--config", "/data/config.yaml"]

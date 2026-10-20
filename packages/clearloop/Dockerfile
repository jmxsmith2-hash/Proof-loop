FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"
VOLUME ["/config", "/data"]
EXPOSE 8484
CMD ["clearloop", "serve", "--config", "/config/config.yaml"]

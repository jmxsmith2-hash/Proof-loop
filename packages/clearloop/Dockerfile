FROM python:3.12-slim
# ffmpeg reads the camera on X1, X2D, H2 and P2S printers (RTSPS).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
VOLUME ["/data"]
EXPOSE 8484
CMD ["clearloop", "--data", "/data", "app", "--host", "0.0.0.0", "--no-browser"]

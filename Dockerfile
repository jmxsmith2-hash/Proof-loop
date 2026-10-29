# Loop files & Printers on an always-on machine (a NAS, a mini PC): the printer monitor, plate checks,
# filament and costs, and making loop files, at http://<host>:8484.
FROM python:3.12-slim
# ffmpeg reads the camera on X1, X2D, H2 and P2S printers (RTSPS).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV PROOFLOOP_HOME=/data
VOLUME ["/data"]
EXPOSE 8484
CMD ["proofloop", "printers", "serve", "--host", "0.0.0.0", "--port", "8484"]

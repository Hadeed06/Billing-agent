# Use Debian 11 (bullseye) — libssl1.1 is in apt repos here, which Azure
# Cognitive Services Speech SDK requires. Bookworm (Debian 12) doesn't have
# libssl1.1 natively and side-loading it is fragile.
FROM --platform=linux/amd64 python:3.11-slim-bullseye

# ── System dependencies for Azure Cognitive Services Speech SDK ──────────────
# The Python package `azure-cognitiveservices-speech` is a thin wrapper around
# a native C++ library. Without these system libs, STT/TTS start then stop
# silently.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl1.1 \
        libasound2 \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

EXPOSE 5000

# Railway injects $PORT at runtime; fall back to 5000 for local/Azure.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-5000}"]

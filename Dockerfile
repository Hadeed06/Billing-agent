# Debian 12 (bookworm) — ships libssl3 (OpenSSL 3). The Speech SDK 1.51.x
# links against OpenSSL 3, so bookworm is the supported base (bullseye's
# libssl1.1 security packages are EOL and no longer resolve on the mirror).
FROM python:3.11-slim-bookworm

# ── System dependencies for Azure Cognitive Services Speech SDK ──────────────
# The Python package `azure-cognitiveservices-speech` is a thin wrapper around
# a native C++ library. Without these system libs, STT/TTS start then stop
# silently.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 \
        libasound2 \
        ca-certificates \
        build-essential \
    && update-ca-certificates \
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

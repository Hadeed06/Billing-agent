# Debian 12 (bookworm) with OpenSSL 3 / libssl3. Moved off Debian 11 + the
# end-of-life OpenSSL 1.1.1 (libssl1.1), which could no longer complete the TLS
# handshake to Azure Speech's updated endpoint (WS_OPEN_ERROR_UNDERLYING_IO_OPEN_FAILED).
# The Azure Speech SDK (bumped to a current version in requirements.txt) supports OpenSSL 3.
FROM --platform=linux/amd64 python:3.11-slim-bookworm

# ── System dependencies for Azure Cognitive Services Speech SDK ──────────────
# The Python package `azure-cognitiveservices-speech` wraps a native C++ library
# that links against OpenSSL. update-ca-certificates refreshes the trust store so
# the WSS handshake to *.stt.speech.microsoft.com succeeds.
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

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]

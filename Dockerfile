FROM python:3.12-slim

# Chromium renders pages that stay empty without JavaScript.
# Debian's package works on amd64 and arm64 (Raspberry Pi).
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY earniq ./earniq
COPY web ./web
COPY config ./config

RUN useradd --create-home --uid 1000 earniq && mkdir -p /data && chown earniq /data
USER earniq

ENV EARNIQ_DATA_DIR=/data \
    EARNIQ_CHROMIUM=/usr/bin/chromium \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8106
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8106/api/health', timeout=4)"
CMD ["uvicorn", "earniq.server:app", "--host", "0.0.0.0", "--port", "8106"]

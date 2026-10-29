# Agent Ultra 2 — container image.
# Base matches the reference build: python:3.12-slim, bash, Playwright
# + Chromium. No weights, no secrets, no profile data are baked into
# this image (see .gitignore).
#
# ENTRYPOINT NOTE: the entrypoint module is server/main.py, which
# lands in a LATER wave (this wave owns only server/supervisor.py
# and server/health.py). Until that wave lands, this image builds
# but its entrypoint module is absent — do not claim it runs until
# server/main.py exists. When it lands, it must read $PORT itself
# (Render sets $PORT; the default below is only a local fallback).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# bash + Chromium system deps (Playwright needs these).
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl ca-certificates \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
  && python -m playwright install --with-deps chromium

# Repo code only. Secrets, .env files, profiles, chats, workdirs and
# storage state are excluded by .gitignore and must NEVER be baked
# into an image.
COPY . ./

ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health')"

# server/main.py arrives in a later wave; it serves on $PORT.
CMD ["sh", "-c", "exec python3 -m server.main"]

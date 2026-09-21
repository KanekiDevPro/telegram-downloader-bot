# Deno is yt-dlp's preferred JavaScript runtime: without one, YouTube extraction
# is degraded ("some formats may be missing") and PO tokens cannot be solved.
# Copying the official binary avoids curl-pipe-bash in the build.
FROM denoland/deno:bin AS deno

# Bot runtime image: Python + ffmpeg, no build tooling left behind.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg is required for MP3 conversion and for merging video+audio streams.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno
# yt-dlp drives the runtime itself; unset DENO_DIR would only matter for caching.
ENV DENO_NO_UPDATE_CHECK=1

WORKDIR /app

# Dependencies first so code edits don't invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user; downloads/ is the only directory the bot writes to.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/downloads \
    && chown -R bot:bot /app
USER bot

# BOT_TOKEN, DATABASE_URL and REDIS_URL come from the environment (see compose).
CMD ["python", "main.py"]

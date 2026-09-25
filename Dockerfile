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
# aria2c is the external downloader yt-dlp drives for plain files: eight
# connections per download instead of one, and `-c` resumes a partial file —
# the difference between minutes and tens of minutes on a 600 MB fetch. Its
# absence is never fatal: yt-dlp measures the binary first (ExternalFD.available)
# and quietly keeps its own downloader where aria2c is not installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg aria2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno
# yt-dlp drives the runtime itself; unset DENO_DIR would only matter for caching.
ENV DENO_NO_UPDATE_CHECK=1

WORKDIR /app

# Dependencies first so code edits don't invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. Three directories are written to: downloads/ (job
# files), cobalt/ (the cookies.json generated for the fallback engine from the
# bot's own jar), and cache/yt-dlp (yt-dlp's persistent cache — client ids,
# signatures and, under an OAuth plugin, the device-flow token a /oauth login
# wrote; the compose file mounts the yt-cache volume here so a token survives
# recreation).
#
# cobalt/ is world-writable on purpose. docker-compose mounts a *host* directory
# over it, and a bind mount keeps the host's ownership, so the mode set here only
# covers the case where nothing is mounted — but that case is real (running the
# image without compose) and a 0755 root-owned directory is exactly the
# PermissionError this line exists to prevent. The bot also checks it at boot and
# says what to fix when the mounted directory is not writable by uid 10001.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/downloads /app/cobalt /app/cache/yt-dlp \
    && chown -R bot:bot /app \
    && chmod 0777 /app/cobalt
USER bot

# BOT_TOKEN, DATABASE_URL and REDIS_URL come from the environment (see compose).
CMD ["python", "main.py"]

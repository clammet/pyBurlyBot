# syntax=docker/dockerfile:1
FROM python:3.14-slim

# git: the bot self-updates its own checkout at runtime (updaterelaunch module).
# tini: PID 1 reaper; logindexsearch forks multiprocessing children.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/*

# Dependencies live in a venv the runtime user can write to, so self-update
# can install changed requirements without an image rebuild.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 burlybot

# .git is deliberately included: runtime self-update needs a real checkout.
COPY --chown=burlybot:burlybot . .
# /app itself was created root-owned by WORKDIR; git refuses to operate on a
# repo whose top level isn't owned by the current user ("dubious ownership").
RUN chown burlybot:burlybot /app \
    && git config --system --add safe.directory /app \
    && chown -R burlybot:burlybot /opt/venv \
    && mkdir -p /app/state /app/shared/pastes \
    && chown -R burlybot:burlybot /app/state /app/shared

USER burlybot

# All private persistent state (config, sqlite DBs, log search index) lives here.
VOLUME /app/state
# /app/shared/<name>: data another container reads (e.g. pastes served by the
# reverse proxy). One volume per dataset, mounted here rw and elsewhere ro.
# Pre-created + chowned above so a fresh named volume inherits UID 1000.

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

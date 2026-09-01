# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# Pinned by digest; Renovate PRs tag/digest bumps and CI proves them. Rebuilds
# stay deliberate (the bot self-updates in-process), so a merged bump reaches a
# host only when someone chooses to rebuild and redeploy the image.
FROM python:3.14-slim@sha256:656d12e70054d5fda18a045e2494c96701e9792dd1445f95b3d038df954f57e9

# git: the bot self-updates its own checkout at runtime (updaterelaunch module).
# tini: PID 1 reaper so no module that forks children can leave zombies.
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

# Dedicated fixed identity, deliberately far above the distro interactive-user
# range (uid 1000+ is where hosts create real people; 10001 is passwordreset's)
# so the account owning the bind-mounted state on a host can never coincide
# with a human login there. Deployments pre-chown their mounts to this UID/GID.
ARG RUN_UID=10002
ARG RUN_GID=10002
RUN groupadd --gid "${RUN_GID}" burlybot \
    && useradd --create-home --uid "${RUN_UID}" --gid burlybot burlybot

# .git is deliberately included: runtime self-update needs a real checkout.
COPY --chown=burlybot:burlybot . .
# /app itself was created root-owned by WORKDIR; git refuses to operate on a
# repo whose top level isn't owned by the current user ("dubious ownership").
RUN chown burlybot:burlybot /app \
    && git config --system --add safe.directory /app \
    && chown -R burlybot:burlybot /opt/venv \
    && mkdir -p /app/state /app/shared/pastes \
    && chown -R burlybot:burlybot /app/state /app/shared

# Seed new paste volumes with the static assets referenced by HTML pastes.
# The entrypoint also refreshes these files so existing volumes receive image
# updates (Docker only copies image contents into a volume when it is empty).
COPY --chown=burlybot:burlybot docker/paste-assets/ /app/shared/pastes/

USER burlybot

# All private persistent state (config, sqlite DBs) lives here.
VOLUME /app/state
# /app/shared/<name>: data another container reads (e.g. pastes served by the
# reverse proxy). One volume per dataset, mounted here rw and elsewhere ro.
# Pre-created + chowned above so a fresh named volume inherits UID 10002.

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

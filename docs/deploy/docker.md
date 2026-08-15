# Running pyBurlyBot in Docker

The image is designed around one goal: **maximum stable uptime**. The
container is deployed once and then stays up; code updates are applied
*in-process* by the bot itself, not by recreating the container.

## Update model (why the container almost never restarts)

There are three tiers of update, from least to most disruptive:

| Change | Mechanism | IRC impact |
|---|---|---|
| `pyburlybot_modules/*.py` | git merge + module hot-reload (`util/moduleloader.py`) | **None** — no disconnect |
| core `*.py` / `requirements.txt` | git merge (+ `pip install`) + in-place `execv` restart (`Settings.shutdown(relaunch=True)`) | One clean QUIT/rejoin; the *container* does not restart (PID preserved, Docker sees nothing) |
| base image (Python, Debian) | rebuild image, recreate container | One QUIT/rejoin; rare and operator-initiated |

The image bakes in a full git checkout (`.git` included) plus the `git`
binary, so the existing `updaterelaunch` module works unchanged inside the
container:

- `!update` (admin command) — fetch/merge `origin/<git_branch>`, hot-reload
  or restart as needed.
- **Automatic checks** — set the `updaterelaunch` module option
  `update_interval` (seconds) in the config to have the bot poll on its own.
  Module-only changes hot-reload silently; core changes restart in place
  (set `auto_restart` to `false` to instead merge and wait for an operator).
- **On container start** — the entrypoint fast-forwards the checkout before
  launching the bot, so even a months-old image comes up on current code.
  Dependencies are installed into a runtime-writable venv (`/opt/venv`), and
  both the entrypoint and the updater re-run `pip install` when
  `requirements.txt` changes.

The one thing self-update cannot refresh is the base image itself. Because
the entrypoint syncs code at start, a stale base image is *not* an
operational problem — rebuilds are only needed for Python/Debian security
updates, on whatever cadence you like.

**Corollary: whatever branch the updater tracks (default `main`) must always
be deployable.** A broken merge to main will be picked up automatically.

## Image contents

- Base `python:3.14-slim`, plus `git` and `tini` (PID 1; the log indexer
  forks `multiprocessing` children that need reaping).
- Runs as non-root user `burlybot` (UID 1000), which owns `/app` and
  `/opt/venv`.
- `WORKDIR /app` is the checkout; all bot paths are CWD-relative.
- Built and pushed by `.github/workflows/docker-image.yml` on every push to
  `main`: `ghcr.io/clammet/pyburlybot:latest` (+ a per-commit SHA tag).

## Persistent state: `/app/state`

Everything that must survive a container recreate lives in one directory,
`/app/state`, mounted as a volume:

```
state/
├── BurlyBot.json    # config — the bot REWRITES this (!config), see below
├── data/            # sqlite DBs (WAL — mount the directory, never one file)
├── logindex/        # Whoosh chat-log search index
├── pastes/          # selfpaste output (if enabled)
└── .heartbeat       # touched every 30s for the healthcheck
```

For this layout the config must point its relative paths into `state/`:

```json
{
    "datadir": "state/data",
    "moduleopts": {
        "logindexsearch": {"indexdir": "state/logindex"},
        "selfpaste": {"wwwroot": "state/pastes/"},
        "updaterelaunch": {"update_interval": 3600}
    }
}
```

Two hard rules:

1. **The bot owns its config at runtime.** `!config <opt> <value>` rewrites
   `BurlyBot.json` atomically. Deployment tooling must seed the file only
   when absent, never converge/overwrite it.
2. **Mount a directory, not the single config file.** The atomic save uses
   `mkstemp` + `os.replace` in the config's parent directory; a single-file
   bind mount breaks it.

If no config exists on first start, the entrypoint writes a starter config
to `state/BurlyBot.json` and exits 1 so you can edit it.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PYBB_CONFIG` | `state/BurlyBot.json` | Config path (relative to `/app`) |
| `PYBB_SELF_UPDATE_ON_START` | `1` | Fast-forward the checkout before launch (`0` to disable) |
| `PYBB_GIT_BRANCH` | `main` | Branch the *entrypoint* syncs (keep in step with the `git_branch` module option) |
| `PYBB_HEARTBEAT_FILE` | unset | When set, the reactor touches this file every 30s; enables the healthcheck |
| `PYBB_HEARTBEAT_MAX_AGE` | `120` | Seconds of staleness before the healthcheck fails |

## Signals and shutdown

`docker stop` sends SIGTERM, which now triggers a *graceful* shutdown: IRC
QUIT to all servers, DB flush, then reactor stop, staged over ~3 seconds.
Give it room with `stop_grace_period: 30s`. SIGHUP (legacy `screen`
behaviour) still does a bare reactor stop.

## Healthcheck semantics

The healthcheck (`docker/healthcheck.py`) only verifies the reactor is
alive (heartbeat file freshness). It deliberately does **not** check IRC
connectivity: the reconnecting factory (max 45s backoff) self-heals from
netsplits, and having Docker restart the container on disconnect would
destroy uptime, not improve it.

## Local build & run

```sh
docker compose up --build -d       # first run writes a starter config and exits
docker compose run --rm pyburlybot sh -c 'cat state/BurlyBot.json'   # inspect
# edit the config inside the volume, then:
docker compose up -d
docker compose logs -f
```

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
- **Push-triggered updates** — an authorized `update` webhook (see "Webhooks"
  below) runs the same check unattended: point a GitHub push webhook at
  `<path_prefix>update` and every merge to `git_branch` is applied within
  `update_debounce` seconds (default 30; a burst of pushes coalesces into one
  check). Pushes to other branches, tags and GitHub `ping` deliveries are
  ignored. Module-only changes hot-reload silently; core changes restart in
  place (set `auto_restart` to `false` to instead merge and wait for an
  operator). There is no polling timer.
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
- Runs as non-root user `burlybot` (UID/GID 10002 — a dedicated identity far
  above the range where hosts create interactive users, so the owner of
  bind-mounted state can never coincide with a human login), which owns
  `/app` and `/opt/venv`.
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
└── .heartbeat       # touched every 30s for the healthcheck
```

`/app/state` is private to the bot (it holds API keys and DBs). Anything
another container must read lives in a **separate mount under
`/app/shared/`** — see the next section.

For this layout the config must point its relative paths into `state/` (and
shared datasets into `shared/`):

```json
{
    "datadir": "state/data",
    "moduleopts": {
        "selfpaste": {"wwwroot": "shared/pastes/"},
        "updaterelaunch": {"update_debounce": 30}
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

## Shared data: `/app/shared/<name>`

Some modules produce files that a *different* container serves or consumes.
`selfpaste` is the first: it only writes static `<token>.txt`/`.html` files
(mode 0644) into `wwwroot` and returns `url_prefix + filename`; serving them
is the reverse proxy's job.

The pattern, one volume per dataset:

```
/app/shared/
└── pastes/          # selfpaste wwwroot — mounted ro by the reverse proxy
```

- The image pre-creates `/app/shared/<name>` owned by UID 10002, so a fresh
  named volume mounted there inherits that ownership and the bot can write
  into it. **Add a `mkdir`/`chown` for any new dataset in the Dockerfile.**
- The bot mounts it read-write; every consumer mounts the *same* volume
  read-only. Never hand out `/app/state` instead — it is `0700` and contains
  secrets.
- Bind-mount deployments use a host directory per dataset,
  pre-created `10002:10002` mode `0755`, e.g.
  `{{ pyburlybot_host_dir }}/shared/pastes`.

Bot side (`docker-compose.yml`):

```yaml
    volumes:
      - pyburlybot_data:/app/state
      - pyburlybot_pastes:/app/shared/pastes
volumes:
  pyburlybot_pastes:
    name: pyburlybot_pastes
```

Reverse-proxy side (its own compose file, same host):

```yaml
    volumes:
      - pyburlybot_pastes:/srv/pyburlybot/pastes:ro
volumes:
  pyburlybot_pastes:
    external: true
```

with an nginx `location` matching `url_prefix`:

```nginx
location /paste/ {
    alias /srv/pyburlybot/pastes/;
    autoindex off;
    add_header X-Content-Type-Options nosniff;
}
```

and `"selfpaste": {"url_prefix": "https://example.test/paste/"}` in the
config. Bring the bot up first so the volume exists before the proxy
references it as `external`.

To share a new dataset later: add `mkdir -p /app/shared/<name>` to the
Dockerfile, a `pyburlybot_<name>` volume in both compose files, and point the
module's option at `shared/<name>/`.

## Webhooks: reload and update

The `webhook` module exposes `<path_prefix><name>` (default `/hooks/<name>`)
and `/health`. Two hook names are wired to actions out of the box:

| Hook | Trigger | Effect (only when the request is authorized) |
|---|---|---|
| `reload` | orchestrator edited `state/BurlyBot.json` | re-read config, hot-reload modules (as `!reload`) |
| `update` | GitHub push webhook / CI / manual | fetch+merge `origin/<git_branch>`, hot-reload or restart (as `!update`), debounced |

Secrets are configured **per hook** (`secrets` option), so the GitHub secret
authorizes `update` only and the orchestrator's secret authorizes `reload`
only. Requests without the right secret are accepted (`202`,
`"authorized": false`) but both handlers ignore them, so nobody can trigger a
fetch or reload by guessing the URL.

### GitHub push → update

The endpoint must be reachable by GitHub, i.e. behind your TLS reverse
proxy. Publish the port to the host loopback (below) and add e.g. an nginx
location — `path_prefix` lets you namespace it:

```nginx
location /pyburlybot/hooks/ {
    proxy_pass http://127.0.0.1:8642/pyburlybot/hooks/;
    proxy_set_header X-Forwarded-For $remote_addr;
    client_max_body_size 1m;
}
```

Bot config: `"webhook": {"listen_host": "0.0.0.0", "listen_port": 8642,
"secrets": {"update": "<random>"}, "path_prefix": "/pyburlybot/hooks"}`.
GitHub repo → Settings → Webhooks → Add: Payload URL
`https://host/pyburlybot/hooks/update`, content type `application/json`,
Secret = the same `<random>`, event "Just the push event". GitHub signs the
body (`X-Hub-Signature-256`), which is what marks the delivery authorized.
GitHub cannot filter by branch; the bot does (`git_branch`).

Test without GitHub:

```sh
BODY='{"ref":"refs/heads/main"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')
curl -X POST -H 'Content-Type: application/json' -H 'X-GitHub-Event: push' \
     -H "X-Hub-Signature-256: sha256=$SIG" --data-binary "$BODY" \
     https://host/pyburlybot/hooks/update
```

A plain `curl -X POST -H "Authorization: Bearer $SECRET" .../update` (no
GitHub headers) also works and is not branch-filtered.

### Applying external config changes: the reload webhook

The bot owns `state/BurlyBot.json`, but sometimes tooling outside the
container legitimately edits it (secret rotation, adding a channel). Instead
of restarting the container, tell the bot to re-read the file through the
`webhook` module:

1. Enable the module (`"webhook"` in `modules`) and give the `reload` hook a
   secret. Inside
   a container it must bind to all interfaces of the container's own network
   namespace, and the port should be published **to localhost only**:

   ```json
   "moduleopts": {
       "webhook": {
           "listen_host": "0.0.0.0",
           "listen_port": 8642,
           "secrets": {"reload": "<long random string>"}
       }
   }
   ```

   ```yaml
   ports:
     - "127.0.0.1:8642:8642"
   ```

2. After editing the config on the host:

   ```sh
   curl -fsS -X POST -H "Authorization: Bearer $SECRET" http://127.0.0.1:8642/hooks/reload
   ```

   The request returns `202` immediately; the bot then reloads the config
   and hot-reloads all modules exactly as `!reload` does (no IRC disconnect
   unless a server entry changed). Requests without the correct secret are
   still accepted (`202`, `"authorized": false`) but the `reload` module
   ignores them — only requests marked authorized trigger a reload. Look for
   `RELOAD: reloading configuration (event from ...)` in the log.

The `/hooks/` prefix is the module's `path_prefix` option (e.g. set it to
`/pyburlybot/hooks` when a reverse proxy routes by path).
`GET /health` on the same port answers `{"ok": true}` and can be used as a
liveness probe for the listener itself. See the README section
"Module-posted events and webhooks" for the module-side API. Each hook has
its own secret: GitHub's `update` secret cannot trigger `reload`, and vice
versa. `!webhook` lists which hooks currently have one configured.

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

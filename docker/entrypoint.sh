#!/bin/sh
# Container entrypoint. The image bakes in a git checkout; on start we
# fast-forward it so a recreated container comes up on current code even if
# the image itself is stale (the image is only a bootstrap - day-to-day
# updates happen in-process via the updaterelaunch module).
set -eu
cd /app

CONFIG="${PYBB_CONFIG:-state/BurlyBot.json}"
BRANCH="${PYBB_GIT_BRANCH:-main}"

if [ "${PYBB_SELF_UPDATE_ON_START:-1}" = "1" ] && [ -d .git ]; then
    echo "entrypoint: checking for updates on origin/${BRANCH}..."
    before="$(git rev-parse HEAD)"
    if git fetch origin "$BRANCH" && git merge --ff-only "origin/$BRANCH"; then
        after="$(git rev-parse HEAD)"
        if [ "$before" != "$after" ]; then
            echo "entrypoint: updated ${before} -> ${after}"
            if ! git diff --quiet "$before" "$after" -- requirements.txt; then
                echo "entrypoint: requirements.txt changed, installing dependencies"
                pip install --no-cache-dir -r requirements.txt
            fi
        else
            echo "entrypoint: already up to date"
        fi
    else
        echo "entrypoint: warning: self-update failed (offline?), starting with baked-in code" >&2
    fi
fi

# Named and bind-mounted volumes hide the directory baked into the image.
# Keep the versioned static paste assets present in both new and existing
# volumes, including assets updated by the runtime git fast-forward above.
cp -R docker/paste-assets/. shared/pastes/

if [ ! -f "$CONFIG" ]; then
    echo "entrypoint: no config at ${CONFIG}, writing a starter config"
    python pyBurlyBot.py -c "$CONFIG"
    echo "entrypoint: edit ${CONFIG} (servers, admins, datadir) and restart the container" >&2
    exit 1
fi

exec python pyBurlyBot.py "$CONFIG"

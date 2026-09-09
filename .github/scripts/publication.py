"""Only publish the current default-branch revision after its CI succeeds."""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


def api(path: str) -> Any:
    return json.loads(subprocess.check_output(["gh", "api", path]))  # noqa: S603, S607


def ready() -> bool:
    if os.environ["GITHUB_EVENT_NAME"] == "pull_request":
        return True  # Existing build steps still prohibit publication on PRs.
    repo = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    source = event.get("workflow_run", {}).get("head_sha", os.environ["GITHUB_SHA"])
    branch = api(f"repos/{repo}")["default_branch"]
    config = json.loads(Path(".github/maintenance.json").read_text())
    for _ in range(240):
        # Recheck during polling: a delayed build must not publish over newer code.
        if api(f"repos/{repo}/commits/{branch}")["sha"] != source:
            print("Publication skipped: a newer default-branch revision exists.")
            return False
        ci = config.get("ci_workflow")
        if not ci:
            return True  # This publisher has its own required test job.
        runs = api(
            f"repos/{repo}/actions/workflows/{ci}/runs?head_sha={source}&event=push&per_page=10"
        )["workflow_runs"]
        if runs and runs[0]["status"] == "completed":
            if runs[0]["conclusion"] == "success":
                return True
            print("Publication skipped: CI did not pass for this revision.")
            return False
        time.sleep(10)
    # Missing/unfinished CI is a closed gate, never permission to publish.
    print(
        "Publication skipped: CI has not completed; a later successful CI run or maintenance rebuild can retry."
    )
    return False


if __name__ == "__main__":
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"ready={str(ready()).lower()}\n")

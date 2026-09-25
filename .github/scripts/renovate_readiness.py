"""Publish a native-automerge gate using Renovate's statuses on the PR's exact SHA.

Run only from the default branch; never import or execute pull-request code.
"""

import json
import os
import subprocess

CONTEXT = "Dependency update safety"
ARTIFACTS = "renovate/artifacts"


def evaluate(pr, statuses, repository):
    head = pr["head"]
    if (head.get("repo") or {}).get("full_name") != repository or not head[
        "ref"
    ].startswith("renovate/"):
        return "success", "Not a Renovate branch; required CI still applies"
    if pr["user"]["type"] != "Bot":
        return "failure", "Renovate branch was not opened by a bot"
    # Status IDs increase; evaluate the latest result for each context, including
    # later failures. Never accept a different actor spoofing a bot's success.
    latest = {}
    for status in sorted(statuses, key=lambda item: item["id"], reverse=True):
        if status["context"].startswith("renovate/"):
            latest.setdefault(status["context"], status)
    if ARTIFACTS not in latest:
        return "pending", "Waiting for Renovate to verify generated lockfiles/artifacts"
    for context, status in latest.items():
        if status["creator"]["id"] != pr["user"]["id"]:
            return "failure", "Renovate status was written by an unexpected actor"
        if status["state"] in ("failure", "error"):
            return "failure", f"Blocked by {context}"
    if any(status["state"] != "success" for status in latest.values()):
        return "pending", "Waiting for Renovate release-age or other internal checks"
    # Age-exempt updates (including lockfile maintenance) have no stability
    # status. The explicit successful artifacts check is still mandatory.
    return "success", "Renovate release-age and artifact checks are satisfied"


def api(path, payload=None, paginate=False):
    command = ["gh", "api", path]
    if paginate:
        command += ["--paginate", "--slurp"]
    if payload is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload else None,
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    return [item for page in data for item in page] if paginate else data


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    # Reconcile every open PR each time. Actions coalesces pending runs in a
    # concurrency group, so checking only the event's PR could lose an update.
    prs = api(f"repos/{repository}/pulls?state=open&per_page=100", paginate=True)
    for pr in prs:
        sha = pr["head"]["sha"]
        statuses = api(
            f"repos/{repository}/commits/{sha}/statuses?per_page=100", paginate=True
        )
        state, description = evaluate(pr, statuses, repository)
        previous = next((s for s in statuses if s["context"] == CONTEXT), None)
        if (
            previous
            and previous.get("creator", {}).get("login") == "github-actions[bot]"
            and previous["state"] == state
            and previous["description"] == description
        ):
            continue
        api(
            f"repos/{repository}/statuses/{sha}",
            {
                "context": CONTEXT,
                "state": state,
                "description": description,
                "target_url": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
            },
        )
        print(f"PR #{pr['number']}: {state}: {description}")


if __name__ == "__main__":
    main()

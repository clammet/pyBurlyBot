"""Fleet maintenance: findings are data; scanner/API failures are errors.

This file is distributed by scripts/sync-maintenance.py. It uses only the
standard library, gh, and Trivy installed by the workflow.
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Collection, Sequence


def command(args: list[str]) -> bytes:
    # Fixed CLI argument lists from this controller; shell execution is never used.
    result = subprocess.run(args, check=False, capture_output=True)  # noqa: S603
    if result.returncode:
        sys.stderr.write(result.stderr.decode(errors="replace"))
        result.check_returncode()
    return result.stdout


def api(path: str, method: str = "GET", data: dict[str, Any] | None = None) -> Any:
    args = ["gh", "api", path, "--method", method]
    if data is None:
        raw = command(args)
    else:
        raw = subprocess.run(  # noqa: S603
            [*args, "--input", "-"],
            input=json.dumps(data).encode(),
            check=True,
            capture_output=True,
        ).stdout
    return json.loads(raw) if raw else None


def pages(path: str) -> list[dict[str, Any]]:
    result = []
    for page in range(1, 101):
        separator = "&" if "?" in path else "?"
        data = api(f"{path}{separator}per_page=100&page={page}")
        if not isinstance(data, list):
            raise ValueError("Expected a paginated list")
        result.extend(data)
        if len(data) < 100:
            return result
    raise ValueError("Pagination limit exceeded")


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def findings(report: dict[str, Any], scope: str) -> dict[str, dict[str, Any]]:
    """Reject incomplete output instead of treating a scanner error as clean."""
    if report.get("SchemaVersion") != 2 or not report.get("ArtifactName"):
        raise ValueError("Invalid Trivy report")
    results = report.get("Results", [])
    if not isinstance(results, list):
        raise ValueError("Invalid Trivy results")
    found = {}
    for result in results:
        for vuln in result.get("Vulnerabilities", []) or []:
            if vuln["Severity"] not in ("HIGH", "CRITICAL"):
                continue
            key_parts = [scope, vuln["PkgName"], vuln["VulnerabilityID"]]
            key = hashlib.sha256(json.dumps(key_parts).encode()).hexdigest()[:24]
            found[key] = dict(
                scope=scope,
                package=vuln["PkgName"],
                advisory=vuln["VulnerabilityID"],
                severity=vuln["Severity"],
                installed=vuln["InstalledVersion"],
                fixed=vuln.get("FixedVersion", ""),
                target=result["Target"],
            )
        for secret in result.get("Secrets", []) or []:
            key_parts = [
                scope,
                result["Target"],
                secret["RuleID"],
                secret.get("StartLine"),
            ]
            key = hashlib.sha256(json.dumps(key_parts).encode()).hexdigest()[:24]
            found[key] = dict(
                scope=scope,
                package="exposed credential",
                advisory=secret["RuleID"],
                severity="CRITICAL",
                installed="detected",
                fixed="",
                target=result["Target"],
            )
    return found


def redact_secrets(report: dict[str, Any]) -> dict[str, Any]:
    for result in report.get("Results", []):
        for secret in result.get("Secrets", []) or []:
            secret["Match"] = "[redacted]"
            secret["Code"] = {"Lines": []}
    return report


def plan(
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    now: float,
    can_rebuild: bool,
    pending_packages: Collection[str] = (),
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], bool]:
    """Attempt one fresh build per advisory, then allow a bounded repair window.

    A changing installed version does not restart the clock. Only absence in a
    completed scan resolves an advisory. Low severity findings remain in SARIF.
    """
    state, actionable = {}, {}
    rebuild = False
    for key, finding in current.items():
        old = previous.get(key, {})
        record = {
            **finding,
            "first_seen": old.get("first_seen", now),
            "rebuild_requested": old.get("rebuild_requested", False),
            "escalated": old.get("escalated", False),
        }
        if finding["fixed"] and can_rebuild and not record["rebuild_requested"]:
            rebuild = True
            record["rebuild_requested"] = True
        # Renovate runs six-hourly. Give it three days to refresh pins and
        # publish, but never let a permanently failing PR defer an issue forever.
        # A matching update that is still green/pending gets the full major
        # release soak. An unrelated PR cannot suppress an advisory.
        grace = 8 * 86400 if finding["package"] in pending_packages else 72 * 3600
        if (
            record["escalated"]
            or finding["package"] == "exposed credential"
            or now - record["first_seen"] >= grace
        ):
            record["escalated"] = True
            actionable[key] = record
        state[key] = record
    return state, actionable, rebuild


def restore_state(
    repo: str, workflow: str, branch: str, artifact_name: str
) -> dict[str, dict[str, Any]]:
    # Select only artifacts from this trusted workflow on the default branch.
    # Never download PR artifacts or execute any content from an artifact.
    runs = api(
        f"repos/{repo}/actions/workflows/{workflow}/runs?branch={branch}&per_page=30"
    )["workflow_runs"]
    for run in runs:
        if str(run["id"]) == os.environ["GITHUB_RUN_ID"]:
            continue
        if run["event"] not in ("schedule", "workflow_dispatch", "workflow_run"):
            continue
        if run["status"] != "completed" or run["conclusion"] != "success":
            continue
        artifacts = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts")["artifacts"]
        for artifact in artifacts:
            if artifact["name"] != artifact_name or artifact["expired"]:
                continue
            raw = command(
                ["gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"]
            )
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                info = archive.getinfo("state.json")
                if info.file_size > 2_000_000:
                    raise ValueError("Maintenance state is too large")
                state = json.loads(archive.read(info))
                if not isinstance(state, dict):
                    raise ValueError("Invalid maintenance state")
                return state
    return {}


def reconcile_issue(repo: str, key: str, title: str, body: str | None) -> None:
    marker = f"<!-- fleet-maintenance:{key} -->"
    issues = [
        x
        for x in pages(f"repos/{repo}/issues?state=open")
        if "pull_request" not in x
        and (
            marker in (x.get("body") or "")
            or (
                key == "host"
                and any(
                    label["name"] == "audit-failure" for label in x.get("labels", [])
                )
            )
        )
        and x["user"]["type"] == "Bot"
    ]
    if body is None:
        for issue in issues:
            api(
                f"repos/{repo}/issues/{issue['number']}",
                "PATCH",
                {"state": "closed", "state_reason": "completed"},
            )
        return
    body = marker + "\n\n" + body
    if issues:
        issue = issues[0]
        if issue["body"] != body or issue["title"] != title:
            api(
                f"repos/{repo}/issues/{issue['number']}",
                "PATCH",
                {"title": title, "body": body},
            )
        # No repeated comments, mentions, labels, or assignees.
    else:
        api(f"repos/{repo}/issues", "POST", {"title": title, "body": body})


def issue_body(actionable: dict[str, dict[str, Any]]) -> str | None:
    if not actionable:
        return None
    lines = [
        "Automatic maintenance needs attention: persistent HIGH/CRITICAL findings or exposed credentials.",
        "A fresh image build was requested where this repository owns the image and a fix exists.",
        "Renovate continues to update dependencies. This issue closes when a complete scan confirms recovery.",
        "",
        "| Scope | Package | Advisory | Installed | Fix |",
        "| --- | --- | --- | --- | --- |",
    ]
    for finding in sorted(
        actionable.values(), key=lambda x: (x["scope"], x["package"], x["advisory"])
    ):
        values = [
            finding["scope"],
            finding["package"],
            finding["advisory"],
            finding["installed"],
            finding["fixed"] or "No upstream fix",
        ]
        lines.append(
            "| "
            + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values)
            + " |"
        )
    return "\n".join(lines)[:60000]


def collect_images(
    paths: Sequence[Path], owned: Collection[str], defaults: dict[str, str]
) -> list[str]:
    values = {}
    for path in paths:
        for key, value in re.findall(
            r"^([a-z0-9_]+):\s*([^\n]+)", path.read_text(), re.M
        ):
            values[key] = value.split(" #", 1)[0].strip().strip('"').strip("'")
    values.update(defaults)

    def resolve(value: str, seen: set[str]) -> str:
        def substitute(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            variable = expression.split("|", 1)[0].strip()
            if variable in seen:
                raise ValueError("Cyclic image variable: " + variable)
            if variable in values:
                return resolve(values[variable], seen | {variable})
            fallback = re.fullmatch(
                r"[a-z0-9_]+\s*\|\s*default\(['\"]([^'\"]*)['\"]\)", expression
            )
            if fallback:
                return fallback.group(1)
            raise ValueError("Unresolved image variable: " + variable)

        return re.sub(r"{{(.*?)}}", substitute, value)

    images = set()
    for key, value in values.items():
        if not key.endswith("_image"):
            continue
        image = resolve(value, {key})
        if not image:
            continue
        if any(character.isspace() for character in image) or image in (">", ">-", "|"):
            raise ValueError("Invalid image variable: " + key)
        # Drop tag/digest for ownership matching, retaining registry port syntax.
        repository = image.split("@", 1)[0]
        if ":" in repository.rsplit("/", 1)[-1]:
            repository = repository.rsplit(":", 1)[0]
        if repository not in owned:
            images.add(image)
    return sorted(images)


def pending_updates(repo: str, packages: set[str]) -> set[str]:
    pending = set()
    for pr in pages(f"repos/{repo}/pulls?state=open"):
        if pr["user"]["type"] != "Bot" or not pr["head"]["ref"].startswith("renovate/"):
            continue
        text = pr["title"] + "\n" + (pr.get("body") or "")
        matched = {
            package
            for package in packages
            if re.search(r"(?<![\w@/.-])" + re.escape(package) + r"(?![\w/.-])", text)
        }
        if not matched:
            continue
        status = api(f"repos/{repo}/commits/{pr['head']['sha']}/status")["state"]
        checks = api(
            f"repos/{repo}/commits/{pr['head']['sha']}/check-runs?per_page=100"
        )["check_runs"]
        if status not in ("error", "failure") and all(
            c["conclusion"] in (None, "success", "neutral", "skipped") for c in checks
        ):
            pending.update(matched)
    return pending


def scan(config: dict[str, Any]) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    metadata = api(f"repos/{repo}")
    branch = metadata["default_branch"]
    if os.environ["GITHUB_REF"] != "refs/heads/" + branch:
        raise ValueError("Maintenance must run on the default branch")
    output = Path("maintenance-results")
    output.mkdir(exist_ok=True)
    previous = restore_state(repo, config["workflow"], branch, "maintenance-state")
    current = {}
    targets = list(config.get("images", []))
    if config.get("collect_images"):
        paths = sorted(Path("playbooks/services").glob("*/vars/*.yml"))
        paths += sorted(Path("inventory/group_vars/all").glob("*.yml"))
        for image in collect_images(
            paths, config["owned_images"], config.get("image_variable_defaults", {})
        ):
            targets.append({"ref": image, "scope": image, "sarif": False})
    for index, target in enumerate(targets):
        json_path = output / f"scan-{index}.json"
        args = [
            "trivy",
            "image",
            "--scanners",
            "vuln,secret",
            "--format",
            "json",
            "--list-all-pkgs",
            "--exit-code",
            "0",
            "--timeout",
            "15m",
            "--output",
            str(json_path),
        ]
        if config.get("ignorefile"):
            args += ["--ignorefile", config["ignorefile"]]
        command([*args, target["ref"]])
        report = redact_secrets(json.loads(json_path.read_text()))
        current.update(findings(report, target["scope"]))
        json_path.write_text(json.dumps(report))
        if target.get("sarif", True):
            sarif_path = output / f"scan-{index}.sarif"
            command(
                [
                    "trivy",
                    "convert",
                    "--format",
                    "sarif",
                    "--output",
                    str(sarif_path),
                    str(json_path),
                ]
            )
            sarif = json.loads(sarif_path.read_text())
            for run in sarif["runs"]:
                # Preserve historical categories. Changing category strands old alerts.
                run["automationDetails"] = {"id": target["category"] + "/"}
            sarif_path.write_text(json.dumps(sarif))
    if config.get("filesystem"):
        json_path = output / "dependencies.json"
        fs_args = [
            "trivy",
            "fs",
            "--scanners",
            "vuln,secret",
            "--format",
            "json",
            "--exit-code",
            "0",
            "--skip-dirs",
            "maintenance-results",
            "--skip-dirs",
            ".cache",
            "--skip-dirs",
            ".venv",
            "--skip-dirs",
            "node_modules",
            "--output",
            str(json_path),
        ]
        if config.get("ignorefile"):
            fs_args += ["--ignorefile", config["ignorefile"]]
        command([*fs_args, "."])
        report = redact_secrets(json.loads(json_path.read_text()))
        current.update(findings(report, "locked dependencies"))
        json_path.write_text(json.dumps(report))
    if not targets and not config.get("filesystem"):
        raise ValueError("No scan targets configured")
    pending = (
        pending_updates(repo, {f["package"] for f in current.values()})
        if current
        else set()
    )
    state, actionable, rebuild = plan(
        current, previous, time.time(), bool(config.get("build_workflow")), pending
    )
    (output / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    reconcile_issue(
        repo, "security", "Security maintenance needs attention", issue_body(actionable)
    )
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(
            f"\nScan complete: {len(current)} HIGH/CRITICAL findings; "
            f"{len(actionable)} require attention. Full reports are attached.\n"
        )
        if rebuild:
            summary.write(
                "A fresh image rebuild will be requested after saving this scan.\n"
            )
    with open(os.environ["GITHUB_OUTPUT"], "a") as outputs:
        outputs.write(f"rebuild={str(rebuild).lower()}\n")


def recover_workflows(repo: str, config: dict[str, Any]) -> None:
    # Only these repository-reviewed workflows may be replayed. Bootstrap,
    # database restores, and arbitrary manual operations are never inferred safe.
    branch = api(f"repos/{repo}")["default_branch"]
    commit = api(f"repos/{repo}/commits/{branch}")
    now = time.time()
    failures = []
    uncertain = False
    for workflow in config.get("retry_workflows", []):
        runs = api(
            f"repos/{repo}/actions/workflows/{workflow}/runs?branch={branch}&per_page=20"
        )["workflow_runs"]
        run = next(
            (
                r
                for r in runs
                if r["event"]
                in ("push", "schedule", "workflow_dispatch", "workflow_run")
            ),
            None,
        )
        if run and run["status"] != "completed":
            uncertain = True
        if not run or run["conclusion"] not in ("failure", "timed_out"):
            continue
        if run["head_sha"] != commit["sha"]:
            # A recent fix gets time to reach its next scheduled run.
            if now - epoch(commit["commit"]["committer"]["date"]) < 48 * 3600:
                uncertain = True
                continue
        elif (
            run.get("run_attempt", 1) < 2
            and now - epoch(run["created_at"]) < 30 * 86400
        ):
            api(f"repos/{repo}/actions/runs/{run['id']}/rerun-failed-jobs", "POST")
            uncertain = True
            continue
        failures.append(f"- [{run['name']}]({run['html_url']}): {run['conclusion']}")
    body = None
    if failures:
        body = (
            "The latest runs below have failed after a retry, or refer to an older "
            "revision that is unsafe to redeploy automatically. This issue closes "
            "after successful replacement runs.\n\n" + "\n".join(sorted(failures))
        )
    if failures or not uncertain:
        reconcile_issue(
            repo, "workflows", "Maintenance machinery needs attention", body
        )


def stalled_updates(config: dict[str, Any]) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    blocked = []
    for pr in pages(f"repos/{repo}/pulls?state=open"):
        if pr["user"]["type"] != "Bot" or not pr["head"]["ref"].startswith("renovate/"):
            continue
        # An update that has been waiting more than a week has exhausted the
        # normal release soak and repeated six-hourly Renovate attempts.
        if time.time() - epoch(pr["created_at"]) < 8 * 86400:
            continue
        blocked.append(f"- [{pr['title']}]({pr['html_url']})")
    body = None
    if blocked:
        body = (
            "These automated updates have remained open for more than eight days. "
            "The normal update/CI/merge loop has not completed; inspect their checks, "
            "merge requirements, and Dependency Dashboard. This issue closes automatically "
            "when none remain.\n\n" + "\n".join(sorted(blocked))
        )
    reconcile_issue(repo, "updates", "Automatic updates need attention", body)
    recover_workflows(repo, config)


def host_issue(path: str) -> None:
    report = json.loads(Path(path).read_text())
    checks = report["checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError("Missing host audit checks")
    problems = [f"- {c['name']}: {p}" for c in checks for p in c["problems"]]
    body = None
    if problems:
        body = (
            "The host audit found persistent problems after its existing retry/staleness "
            "thresholds. This issue closes automatically when the host is healthy.\n\n"
            + "\n".join(sorted(problems))
        )
    reconcile_issue(
        os.environ["GITHUB_REPOSITORY"],
        "host",
        "Host maintenance needs attention",
        body,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["scan", "updates", "host"])
    parser.add_argument("config", nargs="?", default=".github/maintenance.json")
    args = parser.parse_args()
    if args.mode == "scan":
        scan(json.loads(Path(args.config).read_text()))
    elif args.mode == "host":
        host_issue(args.config)
    else:
        stalled_updates(json.loads(Path(args.config).read_text()))

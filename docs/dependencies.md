# Dependency pinning and automation

Everything this repo consumes is pinned exactly; a self-hosted Renovate run
turns upstream releases into PRs, CI proves them, and green non-major PRs
merge themselves.

## What is pinned where

| What | Pinned by | File |
|---|---|---|
| Python packages (direct) | `==` version | `requirements.in`, `requirements-dev.in` |
| Python packages (all, incl. transitive) | version + sha256 hashes | `requirements.txt`, `requirements-dev.txt` (compiled lockfiles) |
| Docker base image | tag + digest | `Dockerfile` |
| GitHub Actions | commit SHA (+ version comment) | `.github/workflows/*.yml` |
| Renovate itself | exact `RENOVATE_VERSION` env pin, bumped weekly | `.github/workflows/renovate.yml` |
| Trivy engine | exact `TRIVY_VERSION` env pin | `.github/workflows/image-scan.yml` |

The deploy side pins the *published* image by digest in the yuzuyu repo, not
here; `docker-compose.yml` in this repo is local/dev and builds from source.

## Editing dependencies by hand

Edit the `.in` file (never the compiled `.txt`), then recompile:

```sh
docker run --rm -v "$PWD":/w -w /w python:3.14-slim sh -c '
  pip install -q uv &&
  uv pip compile --universal --generate-hashes --python-version=3.14 --output-file=requirements.txt requirements.in &&
  uv pip compile --universal --generate-hashes --python-version=3.14 --constraint=requirements.txt --output-file=requirements-dev.txt requirements-dev.in'
```

Keep the equals-form flags. Renovate's pip-compile manager re-runs the
command it finds in each lockfile's header and rejects space-separated
option arguments, so `--python-version 3.14` breaks Python updates entirely.

`--universal` makes one lockfile serve both the macOS dev venv and the Linux
container; `--generate-hashes` puts pip into hash-verifying mode everywhere the
file is installed (image build, container entrypoint, updaterelaunch's
in-process `pip install`, CI). Renovate re-runs the exact command recorded in
each lockfile's header.

Dev deps compile with `-c requirements.txt` so transitives shared with runtime
resolve to the same versions (both files install into one venv in CI).

## Renovate (self-hosted)

`.github/workflows/renovate.yml` runs Renovate every six hours under a repo-owned
GitHub App; repo behavior lives in `.github/renovate.json5`.
The cron requests a run at 00:27, 06:27, 12:27, and 18:27 UTC. GitHub can
delay or skip scheduled runs, so these are not delivery guarantees.

- App permissions: Contents RW, Pull requests RW, Workflows RW (action digest
  bumps edit workflow files), Checks R, Commit statuses R, Dependabot alerts R,
  Metadata R, Issues RW (dependency dashboard).
- Actions variable `RENOVATE_APP_CLIENT_ID` (the app's Client ID; public) and
  secret `RENOVATE_APP_PRIVATE_KEY`.
- The dependency dashboard issue lists everything pending, including majors
  waiting on a human.

## Merge policy

- CI (`.github/workflows/ci.yml`: `test` = ruff/mypy/unittest against the
  lockfiles, `docker-build` = full image build) runs on every PR. Both jobs are
  required status checks in the `main` ruleset.
- Renovate sets GitHub auto-merge on minor/patch/pin/digest/lockfile PRs; they
  merge only after the required checks pass. A red check leaves the PR open
  for a human — that is the designed failure path, not a fault.
- Majors never automerge. Neither do Python minors of the base image: a Python
  minor is a runtime upgrade, bump ruff `target-version` (pyproject.toml) and
  CI `python-version` in the same PR.
- Repo settings: auto-merge allowed, head branches deleted on merge.
- The ruleset requires PRs on `main` with 0 approvals (solo repo; an approval
  requirement would deadlock automerge), blocks force-pushes and deletion, and
  gives repository admins bypass so direct pushes to `main` (the bot
  self-updates from it) keep working.

## Dependabot's role

Alerts only (`.github/dependabot.yml`). Alerts and the dependency graph are
enabled; Dependabot version-update and security-update PRs are disabled.
Renovate reads the alerts through its app token and raises the fix PRs
(labeled `security`), so there is one PR pipeline, not two.

## CVE scanning

`.github/workflows/image-scan.yml` runs Trivy daily against the published
`ghcr.io/clammet/pyburlybot:latest` — OS packages and the baked venv — for
CVEs disclosed between merges. The image build also calls it after publishing,
using the exact image digest so a later build cannot change the scan target.
The scan is a post-publication notification, not a deployment or PR merge gate.

### Ignoring a reviewed finding

Edit `.trivyignore.yaml` in the repository root. The scan workflow checks out
this file and applies it before generating the JSON report, SARIF upload, and
HIGH/CRITICAL failure check. Ignored findings therefore do not fail the job.
GitHub's **Dismiss alert** button only changes the portal's alert state; it
does not tell Trivy to ignore a finding.

For a table row such as `setuptools | CVE-2025-47273`, also read the
**Installed Version** column. For version `70.3.0`, add an entry like this
under the existing `vulnerabilities:` list (this example is already present):

```yaml
  - id: CVE-2025-47273
    purls:
      - pkg:pypi/setuptools@70.3.0
    statement: >-
      pip vendors only pkg_resources from setuptools. The vulnerable
      setuptools.package_index.PackageIndex code is absent from the image.
```

- `id`: copy the exact CVE or GHSA identifier from the finding.
- `purls`: identify the package and installed version. For Python packages,
  use `pkg:pypi/NAME@VERSION`. Other ecosystems have different formats; copy
  `PkgIdentifier.PURL` from a Trivy JSON report rather than guessing.
- `statement`: record why this particular vulnerability does not apply, or
  why its risk is accepted. The statement is documentation, not a filter.
- Optional `expired_at: YYYY-MM-DD`: stop ignoring the finding on that date
  so it is assessed again. Without this field the entry does not expire.

Keep all entries under one `vulnerabilities:` key. An entry matches both the
advisory and the package/version; it does not ignore future advisories for
the whole package. Use the installed version, not the advisory's fixed
version. See [Trivy's ignore-file reference](https://trivy.dev/docs/v0.74/guide/configuration/filtering/#trivyignoreyaml)
for additional filters, including paths.

Commit and merge the file change into `main`. The post-publication scan or
the next daily scan will pick it up. To scan immediately after merging, use
**Actions → Image CVE scan → Run workflow**, selecting `main`. Use a new run,
not a rerun of an old run, which uses its original commit. To undo an
exception, remove its list entry and merge that change.

The initial three exceptions cover pip's bundled setuptools 70.3.0 and
msgpack 1.1.2, investigated on September 4, 2026. The affected setuptools
code and msgpack C extension were absent. Trivy reports these through an
embedded inventory without package paths, so the rules also match a
standalone installation of the same package/version. Reassess these entries
if either package becomes an application dependency; they are not blanket
claims that those packages are safe.

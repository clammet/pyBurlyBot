# Repository maintenance

Shared controllers, workflow steps, Renovate rules and their tests live in
[repo-automation](https://github.com/yuzuyuzuyu/repo-automation).
See its [maintenance policy](https://github.com/yuzuyuzuyu/repo-automation/blob/main/docs/maintenance.md)
for age gates, lockfiles, remediation windows, bounded retries and issue behavior.

This repository keeps scan targets, stable SARIF categories, CI/publisher names
and the retry allowlist in [`maintenance.json`](maintenance.json). Workflow
wrappers preserve local schedules, permissions and check names. Build and deploy
logic and project-specific Renovate exceptions stay here.

Renovate runs hourly with protected GitHub-native automerge. Required checks
include `Dependency update safety`, which verifies artifact generation and
Renovate age/internal checks on the exact PR revision.

Actions use a full release commit SHA and presets use its version tag. Renovate
proposes updates; edit shared behavior in repo-automation instead of copying
scripts into this repository. Keep local scan category and workflow names stable.

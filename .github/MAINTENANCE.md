# Automatic maintenance

Renovate is the only dependency PR writer. Dependabot provides alerts, with its
security-update PRs disabled in repository settings. Renovate runs hourly and
queues eligible PRs for GitHub-native automerge. The main-branch ruleset requires
up-to-date branches, the existing CI/build checks and `Dependency update safety`.
The safety workflow reads only trusted default-branch code and PR-head statuses;
it requires Renovate's successful artifact-generation check and blocks pending
release-age checks or failed Renovate checks. Status events refresh the gate
without waiting for another Renovate run after CI passes.

Routine releases soak for three days; majors soak for a week. Lockfile maintenance
is age-exempt because it has no release timestamp, but still needs successful
artifact generation and CI. Stale branches need a rebase and fresh CI before
merging. GitHub Actions digest changes and configured runtime/data migrations
still require review. Private repositories retain bot-managed merges because
the current GitHub plan does not provide their required branch protections.
The daily watchdog raises one issue for updates stalled
longer than eight days, with 48 hours of grace after a new bot commit, and
closes it when they recover.

Security scans run daily and after image builds. All severities remain in the
reports; high/critical findings start a 72-hour remediation window. A matching green or
pending Renovate update extends that window to eight days; exposed credentials
need immediate attention and are redacted from saved reports. For images
owned here, a fixable finding requests one fresh uncached rebuild. Renovate and
the publication/deployment machinery get time to land dependency fixes. Findings
still present after the window produce one issue, updated without repeated
comments. A complete clean scan closes the issue. Scan, registry, authentication,
and upload errors still fail Actions; they are never interpreted as a clean scan.

Code scanning receives each completed image scan, including an empty report when
findings disappear. Historical SARIF categories are preserved, so GitHub can
mark absent findings fixed. Reports retain all severities rather than dismissing
real vulnerabilities just to reduce the count. Private repositories without code
scanning use the same issue lifecycle and downloadable JSON reports.

Retry state is a 30-day artifact from successful default-branch scans of this
workflow only. State excludes secrets; PR artifacts are never consumed. If state
expires, the next scan starts a new bounded remediation window. A failed scanner
cannot close an issue or erase the last successful state. The workflow's
concurrency group serializes scans, including scans triggered by rebuilds.

The fleet implementation is maintained in the infra repository under
`maintenance/maintenance.py`; `scripts/sync-maintenance.py` distributes it into
`.github/scripts/maintenance.py`. Target and category differences live in
`.github/maintenance.json`. Keep those category values stable. New dependencies
and infrastructure migrations still need meaningful tests before unattended
updates can be considered safe.

The watchdog retries current-revision workflow failures once, then reports
persistent failures in one automatically resolved issue. Publication verifies
that CI passed and the revision is still current before using registry or
production credentials. Build/test failures remain blocking checks.

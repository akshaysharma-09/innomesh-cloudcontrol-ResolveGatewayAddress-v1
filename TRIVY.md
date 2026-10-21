<!--
#### THIS FILE IS MANAGED BY AN AUTOMATED WORKFLOW ####
#### UPDATES MUST BE DONE IN THE CI-WORKFLOWS REPO - OR YOUR CHANGES WILL BE LOST ####
#### https://github.com/next-mesh/ci-workflows ####
-->

# Trivy Security Scanning

[Trivy](https://trivy.dev) is an open-source vulnerability scanner from Aqua Security. It scans source-code dependencies, container images, IaC configs, and committed secrets for known CVEs and misconfigurations.

## What gets scanned

| Scan type | Triggered when | What it finds |
|---|---|---|
| **Filesystem — vulns** (`trivy fs --scanners vuln`) | Dependency manifest present (`go.mod`, `requirements.txt`, `package-lock.json`, etc.) | CVEs in Go, Python, Node, Java, Ruby, Rust packages |
| **Filesystem — secrets** (`trivy fs --scanners secret`) | Always | Committed credentials, API keys, private keys |
| **Config / IaC** (`trivy config`) | Terraform (`.tf`), Kubernetes (`k8s/`), or CloudFormation files present | IaC misconfigurations (open SGs, missing encryption, etc.) |

Scan results land at **<https://trivy.innomesh.au>** (Google Workspace login, `@innomate.com.au` accounts). On pull requests, a summary comment is also posted directly to the PR by the `nextmesh-ci[bot]` App.

## How it runs in your repo

This repo has a synced workflow at `.github/workflows/trivy-scan.yml` that triggers:

- on every push to the default branch (`main` or `master`)
- on every pull request (opened, synchronize, reopened, ready_for_review)
- on manual `workflow_dispatch`

The shim is a thin wrapper around the reusable workflow in `next-mesh/ci-workflows/.github/workflows/trivy-scan.yml`. All scan orchestration logic lives in the composite action `next-mesh/ci-workflows/.github/actions/trivy-scan`:

1. Auto-detect which scans apply to this repo
2. Run [`aquasecurity/trivy-action`](https://github.com/aquasecurity/trivy-action) for each applicable scan (binary install + DB cache handled by the action)
3. POST each result JSON to `trivy.innomesh.au/scan`
4. On PRs: post a summary comment to the PR via the GitHub API
5. Upload all result files as workflow artefacts

To change scan behaviour, edit the action in ci-workflows — no per-repo changes needed.

## Required organisation configuration

These are set ONCE at the org level (`next-mesh` → Settings → Secrets and variables → Actions):

| Where | Name | What |
|---|---|---|
| Variables | `TRIVY_SCAN_ENDPOINT` | `https://trivy.innomesh.au/scan` |
| Secrets | `TRIVY_SCAN_TOKEN` | Shared secret from SSM `/trivy/scan-token` |
| Variables | `NEXTMESH_CI_CLIENT_ID` | The `nextmesh-ci` App's Client ID, alphanumeric starts with `Iv` (already used elsewhere) |
| Secrets | `NEXTMESH_CI_APP_PRIVATE_KEY` | The App's PEM private key (already used elsewhere) |

Optional org / repo variables:

| Name | Purpose | Default |
|---|---|---|
| `TRIVY_SCANS_DISABLED` | Kill-switch — set to `true` to skip all scans. Settable at org level (kills globally) or repo level (per-repo opt-out). | unset (scans run) |
| `TRIVY_PROJECT_NAME_<KEY>` | Friendly display name for a project key in the dashboard. Project key is derived from the repo's first two slug components, upper-cased with `_`: e.g. `innomesh-platformapi-*` → `INNOMESH_PLATFORMAPI`. Set `TRIVY_PROJECT_NAME_INNOMESH_PLATFORMAPI=Platform API` once at org level; applies to every repo in that group. | raw key |

## Running Trivy locally

### Install

```bash
# macOS
brew install aquasecurity/trivy/trivy

# Linux — pinned to match CI
VERSION=0.74.0
curl -sfL "https://github.com/aquasecurity/trivy/releases/download/v${VERSION}/trivy_${VERSION}_Linux-64bit.tar.gz" \
  | tar -xzf - trivy && sudo mv trivy /usr/local/bin/trivy

trivy --version
```

### Scan your repo

```bash
# Filesystem (deps)
trivy fs --severity CRITICAL,HIGH,MEDIUM,LOW .

# Secrets
trivy fs --scanners secret .

# Config / IaC
trivy config .

# JSON output (same format the pipeline ships)
trivy fs --format json --output trivy-fs-results.json .
```

### Useful flags

| Flag | Purpose |
|---|---|
| `--severity CRITICAL,HIGH` | Only show critical and high findings |
| `--ignore-unfixed` | Skip CVEs with no fix available |
| `--exit-code 1` | Fail with non-zero exit if findings (useful in pre-commit hooks) |
| `--skip-db-update` | Use cached DB; omit on first run |
| `--format table` / `--format json` | Human or machine output |

### Update the vulnerability database

```bash
trivy image --download-db-only
```

## Suppressing known CVEs

If a CVE is a false positive, only exploitable in a configuration you don't use, or affects a transitive dependency you can't yet upgrade, suppress it by adding a `.trivyignore.yaml` file to the **root of your repo**:

```yaml
# .trivyignore.yaml
ignores:
  - cve_id: CVE-2025-1234
    reason: "Transitive dep via requests — no direct fix, accepted risk"
    ignored_until: "2026-06-30"
  - cve_id: CVE-2026-5678
    reason: "Only exploitable in server mode, we use client-only"
```

| Field | Required | Description |
|---|---|---|
| `cve_id` | Yes | CVE ID to suppress (e.g. `CVE-2025-1234`) |
| `reason` | No | Human-readable justification — shown as a tooltip in the dashboard |
| `ignored_until` | No | Expiry date in `YYYY-MM-DD` format. Omit for indefinite suppression. |

How it works:

1. The pipeline reads `.trivyignore.yaml` at scan time and includes the ignore list in the metadata sent to the dashboard.
2. The dashboard stores each matching CVE record with `ignored: true`, `ignore_reason`, and `ignored_until`.
3. Ignored CVEs are hidden by default — a **"Show ignored"** toggle reveals them with a grey *Ignored* badge (or amber *Ignored (expired)* if past the expiry date).
4. Ignored CVEs are excluded from the **Remediation** page entirely.
5. Expired ignores re-surface so you get a visible nudge to either fix or re-acknowledge.

To add or remove an ignore: edit `.trivyignore.yaml`, commit, push. The next scan re-applies the list.

**Note:** This file suppresses CVEs in the *dashboard* only. It does NOT change Trivy's own exit code. For the workflow's `fail_on:` behaviour to ignore a CVE, you also need to handle that explicitly (or use Trivy's native `.trivyignore` plain-text format alongside).

## Triggering a manual scan

### Via GitHub UI

Actions tab → **trivy-scan** → "Run workflow" → pick the branch.

### Via gh CLI

```bash
gh workflow run trivy-scan.yml --repo next-mesh/<repo> --ref <branch>
```

### Across multiple repos

```bash
for REPO in innomesh-platformapi-getAlerts-v1 innomesh-platformapi-getRooms-v1; do
  gh workflow run trivy-scan.yml --repo "next-mesh/${REPO}" --ref master
  echo "Triggered: ${REPO}"
done
```

## Dashboard

Scan results: **<https://trivy.innomesh.au>** (Google Workspace login, `@innomate.com.au` accounts).

## Troubleshooting

**No PR comment** — confirm the workflow has `permissions: pull-requests: write` and that the consumer repo's shim has `secrets: inherit` (the `nextmesh-ci` App credentials need to flow into the reusable).

**`HTTP 403` from the dashboard** — `TRIVY_SCAN_TOKEN` doesn't match what `trivy.innomesh.au` expects. Reconcile the SSM-backed secret.

**Scan runs but no findings posted, no comment** — check the workflow logs for the "Detect scan types" step. If `HAS_MANIFESTS=false` and `HAS_CONFIGS=false`, only the secrets scan ran (which doesn't necessarily produce findings). Make sure your manifests are within depth 5 of the repo root.

**Workflow itself fails** — `aquasecurity/trivy-action` exits non-zero on certain errors. The composite passes `exit-code: "0"` to each scan step so a finding doesn't fail the run by itself — failures here usually mean Trivy itself couldn't run (network, disk, malformed config). Check workflow logs.

**`workflow_call` URL not found** — `next-mesh/ci-workflows` must have "Accessible from repositories in the organization" enabled (Settings → Actions → General → Access). One-time org-level toggle.

**Want to disable scans on one repo temporarily** — set `TRIVY_SCANS_DISABLED=true` as a **repo variable** (Settings → Secrets and variables → Actions → Variables). The reusable's job-level `if:` skips the run. Remove the variable to re-enable.

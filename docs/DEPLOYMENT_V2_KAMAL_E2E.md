# Deployment v2 Kamal E2E (Slice 1)

This runbook validates the full `deployment_v2` flow on a real Ubuntu VPS before any legacy SSH cleanup.

## Scope

- Strategy: `kamal`
- App stack: `Next.js` + `npm` only
- Registry: `GHCR`
- Trigger mode: `workflow_dispatch` (default)
- Artifact push mode: `pr` (default)

## Prerequisites

1. Ubuntu 22.04+ VPS (public IP, SSH access, port `22` open).
2. Domain with `A` record pointing to the VPS public IP.
3. GitHub repo connected to an Automatron project.
4. GHCR token with package push access.
5. API server running locally (`http://localhost:8000`).

## 1) Configure deploy target (config + write-only secrets)

```bash
curl -sS -X PUT "http://localhost:8000/api/projects/<PROJECT_ID>/deploy-target" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "strategy": "kamal",
      "host": "<VPS_IP>",
      "ssh_user": "root",
      "ssh_port": 22,
      "domain": "<DEPLOY_DOMAIN>",
      "container_port": 3000,
      "health_path": "/api/health",
      "registry": "ghcr.io",
      "registry_username": "<GITHUB_USERNAME_OR_ORG>",
      "image": "ghcr.io/<OWNER>/<REPO>",
      "clear_env": { "NODE_ENV": "production" },
      "secret_env_names": ["NEXTAUTH_SECRET"],
      "auto_deploy_on_main": false,
      "artifacts_push_mode": "pr"
    },
    "secrets": {
      "ssh_private_key": "<MULTILINE_PRIVATE_KEY>",
      "registry_password": "<GHCR_PAT>",
      "secret_env_values": {
        "NEXTAUTH_SECRET": "<VALUE>"
      }
    }
  }'
```

Pass criteria:

1. Response contains `status=configured`.
2. Response includes `secret_names` but never returns secret values.
3. Project stage becomes `deploy_target_configured`.

## 2) Generate deployment artifacts

```bash
curl -sS -X POST "http://localhost:8000/api/projects/<PROJECT_ID>/generate-deploy-artifacts"
```

Pass criteria:

1. Response contains `fingerprint` with `commit_sha`, `branch`, and `template_version`.
2. A PR is created (default `artifacts_push_mode=pr`) with generated files:
- `Dockerfile`
- `.dockerignore`
- `config/deploy.yml`
- `.kamal/secrets.example`
- `.github/workflows/ci.yml`
- `.github/workflows/deploy.yml`
- `DEPLOYMENT.md`

## 3) Run preflight checks

```bash
curl -sS -X POST "http://localhost:8000/api/projects/<PROJECT_ID>/deploy-preflight" \
  -H "Content-Type: application/json" \
  -d '{"phase":"generate_artifacts"}'

curl -sS -X POST "http://localhost:8000/api/projects/<PROJECT_ID>/deploy-preflight" \
  -H "Content-Type: application/json" \
  -d '{"phase":"deploy"}'
```

Pass criteria:

1. `generate_artifacts` phase can return DNS/SSH warnings (non-blocking).
2. `deploy` phase requires blocking checks to pass (DNS/SSH/registry shape/runtime health).

## 4) First rollout (`kamal setup`)

```bash
curl -sS -X POST "http://localhost:8000/api/projects/<PROJECT_ID>/setup"
```

Then poll status:

```bash
curl -sS "http://localhost:8000/api/projects/<PROJECT_ID>/deploy-status"
```

Pass criteria:

1. Dispatch result contains `automatron_run_id`.
2. Correlated GitHub workflow run is found and linked.
3. Final stage transitions to `deployed`.
4. App is reachable: `curl -i https://<DEPLOY_DOMAIN>/api/health` returns `200`.

## 5) Regular deploy (`kamal deploy`)

```bash
curl -sS -X POST "http://localhost:8000/api/projects/<PROJECT_ID>/deploy"
curl -sS "http://localhost:8000/api/projects/<PROJECT_ID>/deploy-status"
```

Pass criteria:

1. Status moves through `deploying` to `deployed`.
2. Latest run metadata (`run_id`, URL, SHA) is persisted.

## 6) Rollback guard + rollback

Guard check (fresh project with no successful deploy):

```bash
curl -i -X POST "http://localhost:8000/api/projects/<PROJECT_ID>/rollback" \
  -H "Content-Type: application/json" \
  -d '{"rollback_to":""}'
```

Expected: HTTP `409` with `rollback_no_previous_deploy`.

Rollback after at least one successful deploy:

```bash
curl -sS -X POST "http://localhost:8000/api/projects/<PROJECT_ID>/rollback" \
  -H "Content-Type: application/json" \
  -d '{"rollback_to":"ghcr.io/<OWNER>/<REPO>:<PREVIOUS_TAG>"}'
```

Pass criteria:

1. Stage enters `rolling_back`.
2. Final stage becomes `rolled_back` (or `deploy_failed` on failure).

## 7) Log redaction verification

```bash
curl -sS -o deploy-logs.zip "http://localhost:8000/api/projects/<PROJECT_ID>/deploy-logs"
```

Pass criteria:

1. ZIP is downloadable.
2. Secret assignments are redacted (`NAME=***`) for:
- `KAMAL_REGISTRY_PASSWORD`
- `KAMAL_SSH_PRIVATE_KEY`
- configured `secret_env_names`

## 8) Secret persistence regression check

Optional direct DB check to confirm secret values are not persisted:

```bash
sqlite3 orchestrator/data/automatron.db \
  "select deploy_target_json, deployment_profile_json, deployment_secret_names_json from projects where id='<PROJECT_ID>';"
```

Pass criteria:

1. Secret values are absent.
2. Only secret names are present in `deployment_secret_names_json`.

## Exit Criteria Before Slice 1.5 Cleanup

1. `setup` + `deploy` + `rollback` all succeed on a real VPS.
2. Health endpoint is `200` over HTTPS.
3. Correlation tracking and logs retrieval work.
4. Secret redaction and non-persistence checks pass.
5. No fallback to legacy SSH deploy path is needed during this run.

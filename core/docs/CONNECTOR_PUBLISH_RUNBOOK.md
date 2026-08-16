# Connector publish runbook

How a connector's source becomes an installable wheel that running gateways use.

```
R2  (source of truth — connector source, keyed per tenant/service/session)
 │   connector_session_r2_prefix(tenant_id, service_slug, session_id)   [r2_service.py]
 │
 │   ── promotion / mirror to a flat {name}_connector/ layout ──         ← the gap (see §3)
 ▼
build_artifact.py  --src <flat dir>  --publish
 │   one versioned wheel per connector: shielva-connector-{type}-{ver}-py3-none-any.whl
 │   each declares a `shielva.connectors` entry-point
 ▼
Nexus PyPI  (repo `shielva-pypi` @ nexus.shielva.ai, bytes in R2 blobstore)
 │
 ▼
gateway  ── pip install + importlib.metadata.entry_points at runtime ──►  connector runs in-process
```

---

## 1. Registry facts (verified 2026-08-07)

- Repo: **`shielva-pypi`** (Nexus PyPI *hosted*), `https://nexus.shielva.ai/repository/shielva-pypi/`.
  In-cluster index: `http://nexus.shielva.svc:8081/repository/shielva-pypi/simple`.
- **`writePolicy = ALLOW`** (redeploy *permitted*). Re-uploading an existing version
  **overwrites** — it does **not** error like the old JFrog repo did.
- Creds (env-driven, never hard-coded):
  - `PYPI_USER=shielva-ci`, `PYPI_TOKEN=<nexus-ci-password>`, `PYPI_PUBLISH_URL`,
    `PYPI_INDEX_URL` — live in the **`shielva-config`** k8s secret; source of truth in
    **vault KV** (`nexus-ci-*`). See `creds_nexus_prod`.

## 2. ⚠️ The version-bump rule (must-read)

**Always bump `CONNECTOR_VERSION` for every fix — even though Nexus allows overwrite.**

Overwriting the *same* version is unreliable in practice:
- `pip`'s cache and the gateway's **pinned installs won't re-pull an unchanged version
  string**, so a same-version "fix" silently won't reach running pods.
- Immutability is good hygiene (reproducible builds, no "which build of 1.2.3 is this?").

If you want the registry to *enforce* this, flip the repo to **`ALLOW_ONCE`** (Disable
redeploy) via the Nexus UI/API — then a duplicate version is rejected outright.

## 3. Current process (MANUAL — the bus-factor to remove)

Today `build_artifact.py` reads a **local** directory (`--src`, default
`~/Documents/client_dir`) — it has **no R2 pull** and **nothing in CI invokes it**. So a
person must (a) mirror the approved connector source from R2 into a flat
`{name}_connector/` layout on their laptop, then (b) run:

```bash
export PYPI_PUBLISH_URL=https://nexus.shielva.ai/repository/shielva-pypi/
export PYPI_USER=shielva-ci
export PYPI_TOKEN=<nexus-ci-password>        # from vault / shielva-config
python core/build_artifact.py --src ~/Documents/client_dir --publish
# single connector, fast iteration:
python core/build_artifact.py --src ~/Documents/client_dir --only activecampaign --publish
```

💥 **This is a bus-factor problem for a customer-facing path** — publishing depends on one
person's laptop and their hand-assembled `client_dir`. §4 removes it.

**Verify a publish:**
```bash
# the wheel is in the index
curl -s -u shielva-ci:$PYPI_TOKEN https://nexus.shielva.ai/repository/shielva-pypi/simple/shielva-connector-<type>/ | grep <version>
# the gateway picks it up (on-demand install re-discovers entry_points; else restart)
```

## 4. Automating it (CI/CD) — the git-backed design (chosen 2026-08-07)

**Source of truth = git** (R2 dropped as the publish source). Connector source (the flat
`{name}_connector/` packages, as under `generated_connectors/`) lives in **two repos**,
split by tenant-gating:

| Repo (org `Shielva-Sense`) | Holds | Tenant-gated? | Nexus target |
|----------------------------|-------|---------------|--------------|
| **`public-connectors`** | shared/standard connectors (confluence, gdrive, slack, …) | **no** — all tenants | `public-pypi` (or `shielva-pypi`) |
| **`private-connectors`** | tenant-scoped generated connectors (`Tenant-<id>/…`) | **yes** — owning tenant only | `private-pypi` |

```
git push (master) ──► devops-hooks.shielva.ai (HMAC) ──► devops flow-pipeline
   └─ publish stage: python build_artifact.py --src generated_connectors/ --publish
                      (Nexus creds from pipeline secrets; ALWAYS bump CONNECTOR_VERSION — §2)
       ├─ public-connectors  ─► Nexus public wheels
       └─ private-connectors ─► Nexus private wheels
                                     │
   gateway (connector-runtime) pip-installs at runtime via entry_points ◄──┘
       tenant-gating ENFORCED AT THE GATEWAY: a tenant loads public connectors + only its
       own private ones (private wheels carry a tenant_id in metadata).
```

**Why git-backed:** fits the existing fleet CI/CD (`Shielva-Sense/*` push → devops-hooks →
flow-pipeline), gives PR review + history + rollback, and removes the laptop entirely.

**Requirements / build checklist:**
- [ ] Create `Shielva-Sense/public-connectors` + `Shielva-Sense/private-connectors` (or one
      repo, two top-level dirs — decide).
- [ ] Move connector source in: shared → public, `generated_connectors/Tenant-*/` → private.
- [ ] Vendor/reference `build_artifact.py` so the pipeline can run it (it stays canonical in
      shielva-connectors; the connector repos invoke it).
- [ ] Add the per-repo CI config (`.shielva-ci.yml` + a **publish stage** — verify the
      flow-pipeline supports a run-script/twine step, not only container builds; add one if not).
- [ ] Pipeline secrets: `PYPI_PUBLISH_URL/USER/TOKEN` (Nexus). Decide **one Nexus PyPI repo**
      (gateway gates tenants) **vs two** (`public-pypi`/`private-pypi`, hard access separation).
- [ ] Version: bump `CONNECTOR_VERSION` per publish (§2) — pipeline-computed (e.g. build number).
- [ ] Gateway: confirm tenant-gating (load public for all, private only for the owning tenant);
      after publish, on-demand install re-discovers, or `rollout restart` the connector-runtime.

**Path to true CD:** git push → pipeline → Nexus → gateway install. (An optional later step:
have the generation/approve flow open a PR to the connector repo automatically, closing the
loop from "connector generated" to "connector published" with review in between.)

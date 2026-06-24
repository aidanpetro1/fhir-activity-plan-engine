# T21 CDS: SMART on FHIR app (Epic sandbox PoC)

A thin web wrapper that puts the existing T21 clinical-decision-support engine behind Epic in two ways: a SMART on FHIR app that shows a read-only supervision dashboard in the patient's chart, and a CDS Hooks service that surfaces due and overdue screenings as orderable suggestions Epic can place. Both pull the patient's data from Epic's FHIR R4 API and run the same engine.

This is a proof of concept scoped to Epic's public sandbox. No real PHI. The dashboard is read-only; ordering is handled through the CDS Hooks service (see *CDS Hooks service* and *Ordering*, below).

---

## What is reused vs. new

The engine, knowledge artifacts, and dashboard UI are unchanged. This app is a wrapper around them.

| Reused (repo root, untouched) | Purpose |
|---|---|
| `logic.py` | the engine: `load_fixtures`, `apply_plan` |
| `fixtures/` | the FHIR knowledge artifacts (Plan/ActivityDefinitions), the app's ruleset |
| `trisomy21_dashboard_v18.html` | the dashboard template (data injected at the sentinels) |
| `build_supervision_dashboard.py` | reused for `to_date` / `fmt_result` / `latest_matching_obs` |

| New (this folder, `smart-on-fhir/`) | Purpose |
|---|---|
| `app.py` | Flask app and SMART routes (`/`, `/launch`, `/callback`, `/app`, `/order`, `/health`); registers the CDS Hooks blueprint |
| `smart.py` | SMART OAuth2: discovery, PKCE, authorize URL, token exchange |
| `fhir_source.py` | data sources: `EpicFhirSource` (R4 fetch to minimal R4B models) and `LocalFixtureSource` |
| `engine_adapter.py` | runs the engine and renders the read-only dashboard from loaded data |
| `cds_hooks.py` | the CDS Hooks service: discovery, `patient-view`, card building, JWT verification |
| `config.py` | env-based config (no secrets in code) |
| `requirements.txt`, `.env.example` | deps and config template |
| `test_cds_hooks.py`, `cds-request.sample.json` | local test + sample request for the CDS service |

---

## How it works

Launch and auth (SMART App Launch v2, EHR launch):

```
Epic (user opens the app on a patient)
   └─> GET /launch?iss=<FHIR base>&launch=<context>
         • discover {iss}/.well-known/smart-configuration
         • build PKCE + state, redirect to Epic's /authorize
   └─> Epic authenticates the user, asks consent, redirects back
   └─> GET /callback?code=...&state=...
         • verify state, exchange code for access_token (+ patient context)
   └─> redirect to /app
```

Data and render (every page load, stateless):

```
/app
  └─ EpicFhirSource: GET Patient / Observation / Procedure / Encounter (R4)
       └─ build minimal R4B models (truncate dates) for the engine
  └─ engine_adapter.build_items: logic.apply_plan(...) -> due/overdue/complete
  └─ engine_adapter.render_dashboard: inject ITEMS + DOB into the template
```

The engine runs stateless. Nothing is persisted locally. Epic owns the chart, so we recompute from scratch on each load. This matches the project's existing design.

---

## CDS Hooks service

This is how orders actually reach Epic. When a clinician opens the patient's chart, Epic calls the service, which runs the same engine and returns one card per due or overdue screening. Each card carries a suggestion whose action creates a `ServiceRequest`; the clinician clicks accept and Epic places the order through its own signed ordering. Epic stays the system of record and the app never needs a write scope.

Endpoints (registered by `cds_hooks.py`):

- `GET /cds-services` discovery, which lists the `t21-screening-supervision` service (hook `patient-view`).
- `POST /cds-services/t21-screening-supervision` invocation, which returns the cards.

It reuses the engine unchanged (`engine_adapter.load_engine` + `logic.apply_plan`) and the same data sources: live Epic via the token in the hook request, or the repo fixtures in local mode. The suggested `ServiceRequest` is set to `status: draft` so Epic assigns the real order on accept.

Test it locally, no Epic needed:

```bash
python test_cds_hooks.py            # discovery + patient-view against the fixtures
python test_cds_hooks.py patient-2  # try another patient
```

Security: Epic signs a JWT on every CDS call. `verify_request_jwt` checks the signature against Epic's JWKS (the `jku` header) plus an `iss` and `aud` allowlist. It is off for local dev; set `CDS_REQUIRE_JWT=true` with `CDS_TRUSTED_ISS`, `CDS_TRUSTED_JKU`, and `CDS_SERVICE_BASE_URL` in any deployment Epic can reach (`PyJWT` does the verification).

Registering with Epic: the CDS service is registered separately from the SMART app, with the same R4 read scopes. Epic supports `patient-view`, `order-select`, and `order-sign`; this service uses `patient-view`. Confirm with the site whether accepting a `patient-view` suggestion places the order, or whether they prefer the `order-sign` hook (the suggestion payload is the same; adding `order-sign` is a small extension).

---

## Quickstart

### A. Local mode (no Epic, runs in 30 seconds)

Proves the whole pipeline using the repo's `patients/` fixtures.

```bash
cd smart-on-fhir
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env          # defaults to DATA_SOURCE=local
python app.py
# open http://localhost:5000  -> pick patient-1 / 2 / 3
```

### B. Epic sandbox mode

1. Sign up at **fhir.epic.com** (free) and register a new app. When you register, set:
   - Audience: clinicians or administrative users. This is a provider-facing, EHR-launched app, not a patient/MyChart app.
   - FHIR version: R4.
   - Incoming APIs, R4 Read only: `Patient.Read`, `Observation.Read`, `Procedure.Read`, `Encounter.Read`. Request nothing more, since Epic enforces data minimization.
   - Redirect URI: `http://localhost:5000/callback` for local testing. It must match `SMART_REDIRECT_URI` exactly.
   - Client type: confidential (Epic issues a secret, set it as `SMART_CLIENT_SECRET`) or public (PKCE only, no secret).

   Epic then issues a non-production **client ID**.
2. In `.env` set:
   ```
   DATA_SOURCE=epic
   SMART_CLIENT_ID=<your non-prod client id>
   SMART_REDIRECT_URI=http://localhost:5000/callback
   FLASK_SECRET_KEY=<random>
   ```
   (`SMART_DEFAULT_ISS` already points at Epic's public R4 sandbox.)
3. `python app.py`, then start a launch (from Epic's sandbox launcher, or hit `/launch` for a standalone sandbox test).

---

## FHIR version: R4B, aligned with Epic

The engine, knowledge artifacts, and patient fixtures are all FHIR R4B, the same version Epic serves, so there is no version translation. `fhir_source.py` builds *minimal* R4B models from Epic's search results (Epic resources carry many extra fields and extensions; minimal models keep the engine input clean and predictable) and truncates dates to `YYYY-MM-DD` to match the engine's date-only window comparisons. CodeableConcepts (`code`, `type`) and `value[x]` pass through verbatim.

This was migrated from an earlier R5 build. The local and Epic paths were checked against the pre-migration engine output and match across all three example patients.

## Scopes (least-privilege)

Requested at authorization (configurable via `SMART_SCOPES`):

```
launch openid fhirUser offline_access
patient/Patient.read patient/Observation.read
patient/Procedure.read patient/Encounter.read
```

No write scope is requested. Epic is strict about data minimization, and write access needs separate review (see *Ordering*).

**Epic quirk:** Observation search usually requires a `category`. We fetch `laboratory, vital-signs, social-history, exam` and merge; adjust `SMART_OBSERVATION_CATEGORIES` to match the codes your artifacts depend on.

---

## Ordering

Ordering goes through the CDS Hooks service, not the dashboard. The dashboard is read-only: it shows what is due, overdue, and complete, with no order controls. Orders are placed when the clinician accepts a CDS Hooks suggestion inside Epic's workflow (see *CDS Hooks service* above), which keeps Epic as the system of record and avoids needing a FHIR write scope. The old `POST /order` endpoint remains only as a `501` stub and is unused.

---

## Hosting requirements (what you need to stand this up)

- **HTTPS** with a stable public hostname (Epic requires TLS for redirect URIs; `http://localhost` is allowed for dev). The registered redirect URI must match exactly.
- **Python 3.10 or newer**, run under a real WSGI server (gunicorn or uwsgi) behind your proxy. `app.run()` is dev-only.
- **Secrets via env** (`FLASK_SECRET_KEY`, and `SMART_CLIENT_SECRET` if confidential). Never commit `.env`.
- **Session store:** this PoC keeps tokens in an in-process dict keyed by an opaque cookie id. For multi-worker or production use, use Redis or a DB-backed session and a short token TTL. Tokens must never land in the browser cookie or logs.

---

## Production roadmap (beyond this PoC)

1. **Per-customer client IDs.** Each Epic site issues its own client ID and approves scopes and launch contexts; the sandbox client ID is not reusable in production.
2. **Ordering.** The CDS Hooks service is built. What remains is per-site enablement, JWT trust configuration, and confirming the accept or `order-sign` behavior with the site.
3. **Server-side sessions and audit logging.** Log access without logging PHI.
4. **Artifact coverage.** Only the 9 engine-backed orderables are live, and counseling items remain static. Grow `fixtures/` incrementally.
5. **Compliance.** BAA, security review, and Epic's connection and go-live process (maintenance window, smoke tests, hypercare).

---

## Security notes

- No PHI is stored; the engine is stateless and recomputes per request.
- Keep `VERIFY_TLS=true`. PKCE and `state` are enforced on every launch.
- The dashboard is the existing self-contained HTML; it makes no external calls.
- CDS Hooks requests are verified against Epic's signed JWT when `CDS_REQUIRE_JWT` is enabled.

"""Configuration for the T21 SMART on FHIR app.

All secrets and environment-specific values come from environment variables
(optionally loaded from a local .env file). Nothing sensitive is hard-coded.

Copy .env.example to .env and fill in the values from your Epic on FHIR app
registration before running against Epic. For a no-Epic local demo you can leave
the Epic values blank and set DATA_SOURCE=local.
"""
import os
import sys
from pathlib import Path

# --- Load .env if python-dotenv is installed (optional convenience) ---------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass  # python-dotenv is optional; plain environment variables work too.

# --- Make the engine (logic.py) + generator importable from the repo root ---
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list(name, default):
    raw = os.environ.get(name, "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else list(default)


class Config:
    # Repo paths. Derived fresh from __file__ (this app lives in
    # <repo>/smart-on-fhir/ and reuses the engine + artifacts + dashboard
    # template that live in the repo root one level up).
    APP_DIR = Path(__file__).resolve().parent
    REPO_ROOT = APP_DIR.parent
    FIXTURES_DIR = REPO_ROOT / "fixtures"
    DASHBOARD_TEMPLATE = REPO_ROOT / "trisomy21_dashboard_v18.html"

    # Flask session signing key. MUST be set to a random value in any real
    # deployment. A throwaway dev default is used if unset.
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

    # "local"  -> read patient data from the repo's patients/ fixtures (no Epic).
    # "epic"   -> read patient data live from an Epic FHIR R4 server via SMART.
    DATA_SOURCE = os.environ.get("DATA_SOURCE", "local").strip().lower()

    # ---- Epic / SMART on FHIR settings (only needed when DATA_SOURCE=epic) ----
    # Client ID issued when you register the app at https://fhir.epic.com.
    CLIENT_ID = os.environ.get("SMART_CLIENT_ID", "")
    # Only for confidential clients that Epic issued a secret to. Public clients
    # (PKCE only) leave this blank.
    CLIENT_SECRET = os.environ.get("SMART_CLIENT_SECRET", "")

    # Where Epic sends the browser back after authorization. Must EXACTLY match
    # a redirect URI registered on the Epic app.
    REDIRECT_URI = os.environ.get("SMART_REDIRECT_URI", "http://localhost:5000/callback")

    # Default FHIR base ("iss"). In a real EHR launch Epic passes `iss` on the
    # query string and we use that instead; this is only a fallback / for
    # standalone testing against Epic's public sandbox.
    DEFAULT_ISS = os.environ.get(
        "SMART_DEFAULT_ISS",
        "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
    )

    # Scopes requested at authorization. Granular + least-privilege (Epic is
    # strict about data minimization). ServiceRequest.write is intentionally
    # NOT requested yet -- ordering is deferred to a later phase (see README).
    SCOPES = os.environ.get(
        "SMART_SCOPES",
        "launch openid fhirUser offline_access "
        "patient/Patient.read patient/Observation.read "
        "patient/Procedure.read patient/Encounter.read",
    )

    # Epic's Observation search usually requires a `category`. We fetch these
    # categories and merge the results. Adjust to match the observations your
    # artifacts depend on.
    OBSERVATION_CATEGORIES = _list(
        "SMART_OBSERVATION_CATEGORIES",
        ["laboratory", "vital-signs", "social-history", "exam"],
    )

    # Verify TLS on FHIR/token calls. Keep True. Only disable for a local mock.
    VERIFY_TLS = _bool("SMART_VERIFY_TLS", True)

    # ---- CDS Hooks service settings ----
    # Require + verify the EHR-signed JWT on every CDS Hooks call. Keep False for
    # local dev; set True in any deployment Epic can reach.
    CDS_REQUIRE_JWT = _bool("CDS_REQUIRE_JWT", False)
    # Allowlists for the JWT `iss` claim and the `jku` header (the EHR's JWKS URL),
    # comma-separated. Leave empty in dev; populate with Epic's values in prod.
    CDS_TRUSTED_ISS = _list("CDS_TRUSTED_ISS", [])
    CDS_TRUSTED_JKU = _list("CDS_TRUSTED_JKU", [])
    # This service's own public base URL, used as the expected JWT `aud`.
    CDS_SERVICE_BASE_URL = os.environ.get("CDS_SERVICE_BASE_URL", "").strip()

    # ---- Local demo settings (only used when DATA_SOURCE=local) ----
    DEFAULT_LOCAL_PATIENT = os.environ.get("LOCAL_PATIENT", "patient-1")

    HOST = os.environ.get("APP_HOST", "127.0.0.1")
    PORT = int(os.environ.get("APP_PORT", "5000"))


config = Config()

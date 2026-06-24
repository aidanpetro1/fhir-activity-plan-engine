"""T21 Clinical Decision Support — SMART on FHIR app.

A thin web wrapper around the existing T21 engine. It implements the SMART App
Launch (EHR launch) flow, pulls the patient's data from the EHR's FHIR R4 API,
runs the engine, and serves the existing supervision dashboard.

Routes
  GET  /            landing page (local demo links, or Epic launch info)
  GET  /launch      SMART EHR-launch entry point (Epic redirects the browser here)
  GET  /callback    OAuth2 redirect URI; exchanges the code for an access token
  GET  /app         renders the dashboard for the launched/selected patient
  POST /order       placeholder — ordering is deferred to Epic (read-only PoC)
  GET  /health      liveness check
  GET  /cds-services         CDS Hooks discovery (see cds_hooks.py)
  POST /cds-services/<id>    CDS Hooks patient-view -> screening order suggestions

Two data sources, chosen by DATA_SOURCE (see config.py / .env):
  local  -> repo patients/ fixtures, no Epic needed (great for dev + demo)
  epic   -> live Epic R4 via the SMART access token obtained at /callback
"""
import secrets

from flask import (Flask, request, redirect, session, Response, jsonify,
                   url_for)
from markupsafe import escape  # Flask 3 no longer re-exports escape

import smart
from config import config
from fhir_source import LocalFixtureSource, EpicFhirSource
from engine_adapter import build_items, render_dashboard

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# CDS Hooks service (discovery + patient-view) lives in its own blueprint.
from cds_hooks import cds as cds_blueprint
app.register_blueprint(cds_blueprint)

# --- Session state -----------------------------------------------------------
# Stored in Flask's signed cookie session so the OAuth flow (state, PKCE
# verifier, tokens) survives the redirect back from Epic AND any dev-server
# restart. Fine for a sandbox PoC with synthetic data; for production, move
# tokens to a server-side store (Redis/DB) and keep only a session id here.
def sess():
    return session


def _known_patients():
    base = config.REPO_ROOT / "patients"
    return {p.name for p in base.iterdir() if p.is_dir()} if base.exists() else set()


def _error(msg, code=400):
    return Response(f"<h3>Launch error</h3><p>{escape(str(msg))}</p>",
                    status=code, mimetype="text/html")


# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    if config.DATA_SOURCE == "local":
        links = "".join(
            f'<li><a href="{url_for("app_view")}?patient={escape(p)}">{escape(p)}</a></li>'
            for p in sorted(_known_patients())
        )
        body = f"""
        <h2>T21 CDS — local demo mode</h2>
        <p>Running the engine against the repo's <code>patients/</code> fixtures
        (no Epic connection). Pick a patient:</p>
        <ul>{links}</ul>
        <p>Set <code>DATA_SOURCE=epic</code> in <code>.env</code> to run against
        an Epic FHIR R4 server via SMART instead.</p>"""
    else:
        body = f"""
        <h2>T21 CDS — Epic / SMART mode</h2>
        <p>This app is launched from within Epic (EHR launch). Epic will call
        <code>/launch</code> with <code>iss</code> and <code>launch</code>.</p>
        <p>For standalone sandbox testing you can start a launch against the
        default sandbox: <a href="{url_for("launch")}">/launch</a>.</p>"""
    return Response(f"<!doctype html><meta charset=utf-8><title>T21 CDS</title>"
                    f"<body style='font-family:system-ui;max-width:42rem;margin:3rem auto'>"
                    f"{body}</body>", mimetype="text/html")


@app.route("/launch")
def launch():
    """SMART EHR launch. Epic passes `iss` (FHIR base) and `launch` (context)."""
    if not config.CLIENT_ID:
        return _error("SMART_CLIENT_ID is not configured. Register the app at "
                      "fhir.epic.com and set it in .env.", 500)
    iss = request.args.get("iss", config.DEFAULT_ISS)
    launch_ctx = request.args.get("launch")  # present on EHR launch
    try:
        conf = smart.discover(iss, verify_tls=config.VERIFY_TLS)
    except Exception as e:
        return _error(f"Could not read SMART configuration from {iss}: {e}", 502)

    verifier, challenge = smart.make_pkce()
    state = smart.new_state()
    s = sess()
    # Keyed by `state` so several in-flight launches can coexist. (The SMART
    # launcher pre-pings the launch URL while you configure it, and a user can
    # double-launch; a single state slot would get clobbered -> CSRF error.)
    flows = s.get("flows") or {}
    flows[state] = {"iss": iss, "token_endpoint": conf["token_endpoint"],
                    "verifier": verifier}
    for old in list(flows)[:-5]:   # keep the cookie small: last 5 launches
        flows.pop(old)
    s["flows"] = flows

    # EHR launch provides a `launch` token and uses the `launch` scope. A
    # standalone launch (no token) must instead request patient context via
    # `launch/patient`, or Epic rejects the authorize request.
    scope = config.SCOPES
    if not launch_ctx:
        parts = [p for p in scope.split() if p != "launch"]
        if "launch/patient" not in parts:
            parts.insert(0, "launch/patient")
        scope = " ".join(parts)

    url = smart.authorize_url(
        conf, client_id=config.CLIENT_ID, redirect_uri=config.REDIRECT_URI,
        scope=scope, state=state, aud=iss,
        launch=launch_ctx, code_challenge=challenge,
    )
    return redirect(url)


@app.route("/callback")
def callback():
    s = sess()
    if request.args.get("error"):
        return _error(f"{request.args.get('error')}: "
                      f"{request.args.get('error_description', '')}", 400)
    state = request.args.get("state")
    flows = s.get("flows") or {}
    flow = flows.pop(state, None) if state else None
    s["flows"] = flows         # the matched flow is single-use
    if flow is None:
        return _error("State mismatch (possible CSRF) — restart the launch.", 400)
    code = request.args.get("code")
    if not code:
        return _error("No authorization code returned.", 400)

    try:
        tok = smart.exchange_code(
            {"token_endpoint": flow["token_endpoint"]},
            code=code, redirect_uri=config.REDIRECT_URI,
            client_id=config.CLIENT_ID, code_verifier=flow.get("verifier"),
            client_secret=config.CLIENT_SECRET or None,
            verify_tls=config.VERIFY_TLS,
        )
    except Exception as e:
        return _error(f"Token exchange failed: {e}", 502)

    s["iss"] = flow["iss"]     # /app reads the FHIR base from here
    s["access_token"] = tok.get("access_token")
    s["patient"] = tok.get("patient")
    if not s.get("patient"):
        return _error("No patient context returned by the EHR. The launch needs "
                      "a patient-scoped context (launch/patient).", 400)
    return redirect(url_for("app_view"))


@app.route("/app")
def app_view():
    if config.DATA_SOURCE == "local":
        pid = request.args.get("patient", config.DEFAULT_LOCAL_PATIENT)
        if pid not in _known_patients():
            return _error(f"Unknown patient '{pid}'.", 404)
        src = LocalFixtureSource(config.REPO_ROOT / "patients" / pid)
        patient, obs, procs, encs = src.load()
    else:
        s = sess()
        if not s.get("access_token"):
            return redirect(url_for("launch"))
        src = EpicFhirSource(
            s["iss"], s["access_token"], s["patient"],
            observation_categories=config.OBSERVATION_CATEGORIES,
            verify_tls=config.VERIFY_TLS,
        )
        try:
            patient, obs, procs, encs = src.load()
        except Exception as e:
            return _error(f"Failed to load patient data from Epic: {e}", 502)

    items = build_items(patient, obs, procs, encs)
    return Response(render_dashboard(patient, items), mimetype="text/html")


@app.route("/order", methods=["POST"])
def order():
    """Placeholder. Ordering is intentionally deferred to Epic.

    The dashboard's "Order all" button is disabled; this endpoint exists only to
    document the integration point. The real implementation would request the
    `patient/ServiceRequest.write` scope (subject to Epic site approval) and
    either POST ServiceRequest resources or surface them via CDS Hooks. See
    README -> "Ordering (deferred)".
    """
    return jsonify({
        "status": "not_implemented",
        "message": "This PoC is read-only. Ordering will be wired through Epic "
                   "(ServiceRequest.write or CDS Hooks) in a later phase.",
    }), 501


@app.route("/health")
def health():
    return jsonify({"status": "ok", "data_source": config.DATA_SOURCE})


if __name__ == "__main__":
    # use_reloader=False: the auto-reloader would restart the process
    # mid-OAuth-flow and drop the session. Keep it off for SMART testing.
    app.run(host=config.HOST, port=config.PORT, debug=True, use_reloader=False)

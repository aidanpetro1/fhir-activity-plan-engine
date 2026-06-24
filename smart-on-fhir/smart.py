"""SMART on FHIR OAuth2 helpers (framework-agnostic).

Implements the pieces of the SMART App Launch v2 EHR-launch flow we need:

  1. discover()        - read the server's SMART configuration metadata
  2. make_pkce()       - generate a PKCE verifier/challenge pair
  3. authorize_url()   - build the /authorize redirect URL
  4. exchange_code()   - swap the authorization code for an access token

Reference: HL7 SMART App Launch v2 (https://hl7.org/fhir/smart-app-launch/).
Epic implements this spec; nothing here is Epic-specific, which is deliberate.
"""
import base64
import hashlib
import os
import secrets
import urllib.parse

import requests

REQUEST_TIMEOUT = 30


def make_pkce():
    """Return (code_verifier, code_challenge) for PKCE S256.

    PKCE protects the authorization code in transit and is required for public
    clients; Epic also accepts it for confidential clients.
    """
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def new_state():
    """Opaque anti-CSRF value echoed back on the callback."""
    return secrets.token_urlsafe(24)


def discover(iss, verify_tls=True):
    """Fetch the SMART configuration for a FHIR base URL (the `iss`).

    Tries /.well-known/smart-configuration first (SMART v2). Returns a dict with
    at least `authorization_endpoint` and `token_endpoint`.
    """
    base = iss.rstrip("/")
    url = base + "/.well-known/smart-configuration"
    r = requests.get(
        url,
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
        verify=verify_tls,
    )
    r.raise_for_status()
    conf = r.json()
    if "authorization_endpoint" not in conf or "token_endpoint" not in conf:
        raise ValueError(
            f"SMART configuration at {url} is missing authorization/token endpoints"
        )
    return conf


def authorize_url(conf, *, client_id, redirect_uri, scope, state, aud,
                  launch=None, code_challenge=None):
    """Build the authorization redirect URL.

    `aud` MUST be the FHIR base URL (iss) — Epic rejects requests whose audience
    doesn't match the resource server. `launch` is the opaque context handle the
    EHR passed us; include it for an EHR launch, omit it for a standalone launch.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "aud": aud,
    }
    if launch:
        params["launch"] = launch
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return conf["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


def exchange_code(conf, *, code, redirect_uri, client_id, code_verifier=None,
                  client_secret=None, verify_tls=True):
    """Exchange an authorization code for a token response.

    Returns the full token JSON: access_token, token_type, expires_in, scope,
    and SMART launch context such as `patient`, plus id_token / refresh_token
    when granted.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    # Confidential clients authenticate with HTTP Basic; public clients send
    # only client_id in the body (above).
    auth = None
    if client_secret:
        auth = (client_id, client_secret)

    r = requests.post(
        conf["token_endpoint"],
        data=data,
        headers=headers,
        auth=auth,
        timeout=REQUEST_TIMEOUT,
        verify=verify_tls,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Token exchange failed ({r.status_code}): {r.text}")
    return r.json()

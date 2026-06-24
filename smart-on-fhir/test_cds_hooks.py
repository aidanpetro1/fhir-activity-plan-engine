"""Local smoke test for the CDS Hooks service (no Epic needed).

Drives the Flask app's test client against the discovery endpoint and the
patient-view invocation using cds-request.sample.json, then prints the cards and
the ServiceRequest each suggestion would create. Runs entirely on the repo's
patients/ fixtures (DATA_SOURCE=local), so no network or Epic connection.

    cd smart-on-fhir
    python test_cds_hooks.py [patient-id]
"""
import json
import sys
from pathlib import Path

from app import app

HERE = Path(__file__).resolve().parent


def main():
    client = app.test_client()

    # 1) Discovery
    r = client.get("/cds-services")
    assert r.status_code == 200, f"discovery -> {r.status_code}"
    services = r.get_json()["services"]
    assert services, "discovery returned no services"
    sid = services[0]["id"]
    print(f"Discovery OK: {[s['id'] for s in services]} (hook={services[0]['hook']})")

    # 2) Invoke patient-view with the sample request (optionally override patient)
    req = json.loads((HERE / "cds-request.sample.json").read_text(encoding="utf-8"))
    if len(sys.argv) > 1:
        req["context"]["patientId"] = sys.argv[1]
    pid = req["context"]["patientId"]

    r = client.post(f"/cds-services/{sid}", json=req)
    assert r.status_code == 200, f"invoke -> {r.status_code}: {r.get_data(as_text=True)}"
    cards = r.get_json()["cards"]

    print(f"\n{len(cards)} card(s) for context.patientId={pid!r}:\n")
    for c in cards:
        assert c.get("summary") and c.get("indicator") and c.get("source", {}).get("label"), \
            "card missing a required field (summary/indicator/source.label)"
        print(f"  [{c['indicator']:7}] {c['summary']}")
        for s in c.get("suggestions", []):
            assert "selectionBehavior" in c, "suggestions present but no selectionBehavior"
            for a in s["actions"]:
                res = a["resource"]
                coding = (res.get("code", {}).get("coding") or [{}])[0]
                print(f"            -> {a['type']} {res['resourceType']} "
                      f"[{coding.get('code', '?')} {coding.get('display', '')}], "
                      f"status={res.get('status')}, intent={res.get('intent')}")

    print("\nSmoke test passed.")


if __name__ == "__main__":
    main()

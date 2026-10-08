from __future__ import annotations

import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
RISK = json.loads((ROOT / "data" / "vendor_risk.json").read_text(encoding="utf-8"))


def _http_error(status: int, detail: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({"detail": detail}).encode()
    return requests.HTTPError(response=response)


def fake_vendor_risk(vendor_name: str, overrides: dict | None = None) -> dict:
    """Same behaviour as the mock API, without needing it running."""
    record = (overrides or {}).get(vendor_name, RISK.get(vendor_name))
    if record is None:
        raise _http_error(404, f"No vendor-risk record for '{vendor_name}'")
    if record.get("force_error"):
        raise _http_error(503, record.get("error_message", "unavailable"))
    return {"vendor_name": vendor_name, **record}


def base_request(**changes) -> dict:
    request = {
        "request_id": "REQ-TEST",
        "requester_id": "E004",
        "product_name": "SignFlow Add-on",
        "vendor_name": "SignFlow",
        "category": "E-signature",
        "annual_cost_usd": 800,
        "user_count": 3,
        "business_justification": "Finance needs three additional signing identities for quarter-end agreements.",
        "data_access_level": "internal_documents",
        "requested_integrations": [],
        "urgency": "normal",
    }
    request.update(changes)
    return request

"""Focused validation for fixed-secret authentication without external API calls."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.testclient import TestClient
from fastapi.security import HTTPAuthorizationCredentials

import api_funda_agent_exp as api


secret = os.environ["HERBALIFE_EXTERNAL_ACCESS_SECRET"]
api.require_external_access(
    HTTPAuthorizationCredentials(scheme="Bearer", credentials=secret)
)

for credentials in (
    None,
    HTTPAuthorizationCredentials(scheme="Basic", credentials=secret),
    HTTPAuthorizationCredentials(scheme="Bearer", credentials="incorrect-secret-value"),
):
    try:
        api.require_external_access(credentials)
    except HTTPException as exc:
        assert exc.status_code == 401
        assert exc.headers == {"WWW-Authenticate": "Bearer"}
    else:
        raise AssertionError("invalid credentials were accepted")

client = TestClient(api.app)
request_body = {"prompt": "auth check", "survey_id": "00000000-0000-0000-0000-000000000000"}
assert client.post("/ask", json=request_body).status_code == 401
assert client.post(
    "/ask",
    json=request_body,
    headers={"Authorization": "Bearer wrong-secret"},
).status_code == 401
assert client.get("/health").status_code == 200

openapi = api.app.openapi()
assert openapi["paths"]["/ask"]["post"]["security"]
assert openapi["paths"]["/threads/{thread_id}"]["delete"]["security"]
assert "security" not in openapi["paths"]["/health"]["get"]
print("Herbalife Bearer authentication OK")

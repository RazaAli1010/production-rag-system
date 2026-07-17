"""T12 — OpenAPI completeness (AC-19): F14 must be buildable from the contract alone."""


async def test_openapi_documents_all_f11_paths(client):
    spec = (await client.get("/openapi.json")).json()
    paths = spec["paths"]
    for p in ("/api/ask", "/api/health", "/api/documents", "/api/history"):
        assert p in paths, f"missing {p}"
    # ask has request schema + a summary
    ask = paths["/api/ask"]["post"]
    assert ask["summary"]
    assert ask["requestBody"]["content"]["application/json"]["schema"]


async def test_openapi_has_bearer_scheme(client):
    spec = (await client.get("/openapi.json")).json()
    schemes = spec["components"]["securitySchemes"]
    # F10's OAuth2 password flow — the bearer auth /docs authorizes with.
    assert any(v.get("type") in ("oauth2", "http") for v in schemes.values())

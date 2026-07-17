"""AC-39: the Authorize button in /docs must drive a real OAuth2 password flow, which means the
schemes have to be declared in the OpenAPI document rather than the headers being read by hand.
"""


async def test_openapi_declares_the_password_flow(client):
    schemes = (await client.get("/openapi.json")).json()["components"]["securitySchemes"]

    oauth = schemes["OAuth2PasswordBearer"]
    assert oauth["type"] == "oauth2"
    assert oauth["flows"]["password"]["tokenUrl"] == "api/auth/token"


async def test_openapi_declares_the_api_key_header(client):
    schemes = (await client.get("/openapi.json")).json()["components"]["securitySchemes"]

    assert schemes["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}


async def test_token_url_actually_resolves(client):
    """A tokenUrl that 404s makes the Authorize button fail with no useful error."""
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/api/auth/token" in paths
    assert "post" in paths["/api/auth/token"]


async def test_internal_ping_is_documented_as_protected(client):
    spec = (await client.get("/openapi.json")).json()

    assert "/internal/ping" in spec["paths"]

import os

from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 30)

from api.main import app  # noqa: E402


def test_security_headers_are_added_to_public_response():
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_oversized_request_is_rejected_before_route():
    client = TestClient(app)
    response = client.post("/healthz", content=b"x", headers={"content-length": str(60 * 1024 * 1024 + 1)})
    assert response.status_code == 413


def test_pagination_limits_are_enforced():
    client = TestClient(app)
    assert client.get("/search/missed", params={"limit": 501}).status_code == 401
    assert client.get("/entries", params={"limit": 501}).status_code == 401

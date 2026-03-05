"""Test health endpoint."""


def test_health(client):
    """GET /health returns 200 with status healthy."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

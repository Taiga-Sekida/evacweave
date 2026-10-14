"""The 2D laboratory stays static and never enables the public compute API."""

from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_lab_html_and_modules_are_served():
    page = client.get("/lab")
    assert page.status_code == 200
    assert "EvacWeave" in page.text
    assert 'id="map"' in page.text
    assert "evac-lab-app.mjs" in page.text
    for path in (
        "/assets/evac-lab-model.mjs",
        "/assets/evac-lab-app.mjs",
        "/assets/evac-lab.css",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.content


def test_lab_is_not_a_public_ai_or_simulate_service(monkeypatch):
    monkeypatch.delenv("PUBLIC_SIMULATION_ENABLED", raising=False)
    assert client.post("/v1/simulate", json={}).status_code == 403

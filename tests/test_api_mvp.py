from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root_is_browser_demo():
    response = client.get("/")
    assert response.status_code == 200
    assert "EvacWeave" in response.text
    assert "現実の安全や避難判断を保証しません" in response.text


def test_blocked_demo_fails():
    response = client.get("/v1/demo/blocked")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "FAIL"
    assert payload["simulation_only"] is True


def test_repaired_demo_passes():
    response = client.get("/v1/demo/repaired")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "PASS"
    assert payload["simulation_only"] is True


def test_unknown_demo_kind_is_rejected():
    response = client.get("/v1/demo/not-a-kind")
    assert response.status_code == 422

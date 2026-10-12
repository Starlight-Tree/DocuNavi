def test_health_reports_mock_mode(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "mode": "mock"}

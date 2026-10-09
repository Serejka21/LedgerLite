from fastapi.testclient import TestClient

from app.main import app, settings

client = TestClient(app)
AUTH = {"Authorization": f"Bearer {settings.provider_api_key}"}


def _create(amount: str = "10.00") -> str:
    r = client.post(
        "/invoices",
        json={"amount": amount, "currency": "USD", "external_id": "dep_1",
              "callback_url": "http://127.0.0.1:9/never"},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    return r.json()["invoice_id"]


def test_requires_auth() -> None:
    assert client.get("/invoices/inv_x").status_code == 401


def test_final_status_is_immutable() -> None:
    inv = _create()
    r = client.post(f"/invoices/{inv}/simulate",
                    json={"status": "partially_paid", "paid_amount": "7.50",
                          "deliver_webhook": False}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["invoice"]["paid_amount"] == "7.50"

    r = client.post(f"/invoices/{inv}/simulate",
                    json={"status": "paid", "deliver_webhook": False}, headers=AUTH)
    assert r.status_code == 409
    assert client.get(f"/invoices/{inv}", headers=AUTH).json()["status"] == "partially_paid"


def test_unreachable_callback_is_reported_not_crashing() -> None:
    inv = _create()
    r = client.post(f"/invoices/{inv}/simulate",
                    json={"status": "paid", "duplicate": True}, headers=AUTH)
    assert r.status_code == 200
    deliveries = r.json()["webhook_deliveries"]
    assert len(deliveries) == 2
    assert deliveries[0]["event_id"] == deliveries[1]["event_id"]
    assert all("error" in d for d in deliveries)

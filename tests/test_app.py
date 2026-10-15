import hashlib
import hmac
import json

import pytest

from app import create_app, init_db, now_local


@pytest.fixture()
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE_PATH": str(tmp_path / "test.db"),
        "SEED_DEMO_DATA": True,
        "WEBHOOK_VERIFY_TOKEN": "test-verify",
        "META_APP_SECRET": "test-app-secret",
        "ADMIN_PASSWORD": "",
        "ENABLE_DEMO_TOOLS": True,
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def demo_request(client, message="Bhaiya, aaj mere tiffin mat lana.", phone="919000000001"):
    response = client.post("/api/demo/message", json={"phone": phone, "message": message})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_home_page_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"DukaanFlow" in response.data
    assert b"Try a demo message" in response.data


def test_confirm_skip_keeps_subscription_active(client, app):
    request_data = demo_request(client)
    assert request_data["status"] == "pending"
    response = client.post(f"/api/requests/{request_data['request_id']}/confirm", json={})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        status = db.execute("SELECT subscription_status FROM customers WHERE phone='919000000001'").fetchone()[0]
        today_count = db.execute("SELECT COUNT(*) FROM delivery_skips WHERE delivery_date=?", (now_local().date().isoformat(),)).fetchone()[0]
    assert status == "active"
    assert today_count == 1


def test_duplicate_skip_for_same_customer_date_is_ignored_at_ingestion(client, app):
    first = demo_request(client)
    assert first["status"] == "pending"
    assert client.post(f"/api/requests/{first['request_id']}/confirm", json={}).status_code == 200

    second = demo_request(client, "Aaj tiffin mat bhejna please.")
    assert second["duplicate"] is True
    assert second["status"] == "confirmed"
    assert "already confirmed" in second["reason"].lower()
    assert second["request_id"] == first["request_id"]

    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        count = db.execute("SELECT COUNT(*) FROM delivery_skips WHERE customer_id=1 AND delivery_date=?", (now_local().date().isoformat(),)).fetchone()[0]
        request_count = db.execute("SELECT COUNT(*) FROM skip_requests WHERE customer_id=1 AND requested_date=?", (now_local().date().isoformat(),)).fetchone()[0]
        status = db.execute("SELECT subscription_status FROM customers WHERE id=1").fetchone()[0]
    assert count == 1
    assert request_count == 1
    assert status == "active"


def test_duplicate_pending_skip_does_not_create_a_second_request(client, app):
    first = demo_request(client)
    second = demo_request(client, "Aaj tiffin mat bhejna please.")
    assert first["status"] == "pending"
    assert second["duplicate"] is True
    assert second["status"] == "pending"
    assert second["request_id"] == first["request_id"]
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        pending_count = db.execute("SELECT COUNT(*) FROM skip_requests WHERE customer_id=1 AND requested_date=? AND intent='skip_today' AND status='pending'", (now_local().date().isoformat(),)).fetchone()[0]
    assert pending_count == 1


def test_startup_resolves_old_pending_request_if_skip_already_exists(client, app):
    first = demo_request(client)
    assert client.post(f"/api/requests/{first['request_id']}/confirm", json={}).status_code == 200

    # Simulate a stale pending duplicate made by a previous version of the app.
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        db.execute(
            """INSERT INTO skip_requests(customer_id,sender_phone,sender_name,message,intent,requested_date,
                       status,source,wamid,ai_reason,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (1, "919000000001", "Ravi Sharma", "Aaj tiffin mat bhejna please.", "skip_today",
             now_local().date().isoformat(), "pending", "demo", None, "Old reason", now_local().isoformat()),
        )
        db.commit()

    init_db(app.config["DATABASE_PATH"], seed=False)
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        rows = db.execute("SELECT status, ai_reason FROM skip_requests ORDER BY id").fetchall()
        skip_count = db.execute("SELECT COUNT(*) FROM delivery_skips").fetchone()[0]
    assert rows[1][0] == "confirmed"
    assert "already skipped" in rows[1][1].lower()
    assert skip_count == 1


def test_subscription_cancel_goes_to_review(client, app):
    result = demo_request(client, "Bhaiya meri tiffin subscription ab se band kar do.")
    assert result["status"] == "review"
    response = client.post(f"/api/requests/{result['request_id']}/confirm", json={})
    assert response.status_code == 409
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        status = db.execute("SELECT subscription_status FROM customers WHERE phone='919000000001'").fetchone()[0]
    assert status == "active"


def test_unknown_customer_cannot_auto_confirm(client):
    result = demo_request(client, "Aaj mere tiffin mat lana", "919811112222")
    assert result["status"] == "review"
    response = client.post(f"/api/requests/{result['request_id']}/confirm", json={})
    assert response.status_code == 409


def test_tomorrow_request_goes_to_review(client):
    result = demo_request(client, "Kal mere tiffin mat bhejna.")
    assert result["status"] == "review"
    assert result["requested_date"] != now_local().date().isoformat()


def test_webhook_verification(client):
    good = client.get("/webhook?hub.mode=subscribe&hub.verify_token=test-verify&hub.challenge=abc123")
    bad = client.get("/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc123")
    assert good.status_code == 200 and good.get_data(as_text=True) == "abc123"
    assert bad.status_code == 403


def test_webhook_signature_and_message_ingestion(client, app):
    payload = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "919000000001", "profile": {"name": "Ravi Sharma"}}],
              "messages": [{"from": "919000000001", "id": "wamid.TEST.001", "type": "text", "text": {"body": "Aaj mere tiffin mat lana"}}]}}]}]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"test-app-secret", raw, hashlib.sha256).hexdigest()
    response = client.post("/webhook", data=raw, content_type="application/json", headers={"X-Hub-Signature-256": signature})
    assert response.status_code == 200
    assert response.get_json()["processed"] == 1
    duplicate = client.post("/webhook", data=raw, content_type="application/json", headers={"X-Hub-Signature-256": signature})
    assert duplicate.status_code == 200
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        assert db.execute("SELECT COUNT(*) FROM skip_requests WHERE wamid='wamid.TEST.001'").fetchone()[0] == 1


def test_bad_webhook_signature_rejected(client):
    response = client.post("/webhook", data=b'{"entry":[]}', content_type="application/json", headers={"X-Hub-Signature-256": "sha256=bad"})
    assert response.status_code == 401


def test_unknown_customer_can_be_mapped_and_rechecked(client, app):
    result = demo_request(client, "Aaj mere tiffin mat lana", "919811112222")
    assert result["status"] == "review"
    saved = client.post("/api/customers", json={"name": "Nisha Customer", "phone": "919811112222", "plan": "Veg tiffin · lunch"})
    assert saved.status_code == 200
    rechecked = client.post(f"/api/requests/{result['request_id']}/recheck", json={})
    assert rechecked.status_code == 200
    assert rechecked.get_json()["ok"] is True
    confirmed = client.post(f"/api/requests/{result['request_id']}/confirm", json={})
    assert confirmed.status_code == 200
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        status = db.execute("SELECT subscription_status FROM customers WHERE phone='919811112222'").fetchone()[0]
    assert status == "active"


def test_dismiss_does_not_create_delivery_skip(client, app):
    result = demo_request(client, "Good morning bhaiya")
    assert result["status"] == "review"
    response = client.post(f"/api/requests/{result['request_id']}/dismiss", json={})
    assert response.status_code == 200
    with __import__("sqlite3").connect(app.config["DATABASE_PATH"]) as db:
        status = db.execute("SELECT status FROM skip_requests WHERE id=?", (result["request_id"],)).fetchone()[0]
        skips = db.execute("SELECT COUNT(*) FROM delivery_skips").fetchone()[0]
    assert status == "rejected"
    assert skips == 0

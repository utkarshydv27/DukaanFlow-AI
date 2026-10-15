"""DukaanFlow AI: local-first WhatsApp order operations MVP."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from functools import wraps
from typing import Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from services import classify_message

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Windows Python installations may not ship an IANA timezone database. Prefer
# the canonical Asia/Kolkata zone when available, but fall back to India's
# fixed UTC+05:30 offset (India does not observe daylight-saving time).
try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_local() -> datetime:
    return datetime.now(IST)


def local_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def normalize_phone(value: str | None) -> str:
    """Normalize a WhatsApp wa_id/phone to digits for matching."""
    return re.sub(r"\D", "", value or "")


@contextmanager
def get_db(db_path: str) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db(db_path: str, seed: bool = True) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_db(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                subscription_status TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (subscription_status IN ('active','paused','cancelled','unknown')),
                plan TEXT NOT NULL DEFAULT 'Not set',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS skip_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
                sender_phone TEXT NOT NULL,
                sender_name TEXT,
                message TEXT NOT NULL,
                intent TEXT NOT NULL,
                requested_date TEXT,
                status TEXT NOT NULL DEFAULT 'review'
                    CHECK (status IN ('pending','review','confirmed','rejected')),
                source TEXT NOT NULL DEFAULT 'demo',
                wamid TEXT UNIQUE,
                ai_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS delivery_skips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                delivery_date TEXT NOT NULL,
                request_id INTEGER NOT NULL REFERENCES skip_requests(id) ON DELETE CASCADE,
                reason TEXT NOT NULL DEFAULT 'Customer requested',
                created_at TEXT NOT NULL,
                UNIQUE(customer_id, delivery_date)
            );
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_requests_status ON skip_requests(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_requests_date ON skip_requests(requested_date);
            """
        )
        if seed and db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
            demo_customers = [
                ("Ravi Sharma", "919000000001", "active", "Standard tiffin · lunch"),
                ("Neha Verma", "919000000002", "active", "Veg tiffin · lunch"),
                ("Aman Singh", "919000000003", "active", "Full meal · lunch + dinner"),
                ("Pooja Kumari", "919000000004", "paused", "Tiffin plan paused"),
            ]
            db.executemany(
                "INSERT INTO customers(name, phone, subscription_status, plan, created_at) VALUES(?,?,?,?,?)",
                [(n, p, s, pl, local_iso()) for n, p, s, pl in demo_customers],
            )
            db.execute(
                "INSERT INTO activity_log(event, details, created_at) VALUES(?,?,?)",
                ("demo_seeded", "Demo customers created for local testing.", local_iso()),
            )

        # Repair duplicate pending/review rows created by earlier MVP versions.
        # If a delivery skip for the same customer/date already exists, that
        # request has already been satisfied and must not remain actionable.
        stale_duplicates = db.execute(
            """SELECT r.id, r.customer_id, r.requested_date
               FROM skip_requests r
               WHERE r.status IN ('pending','review')
                 AND r.intent='skip_today'
                 AND r.requested_date IS NOT NULL
                 AND EXISTS (
                     SELECT 1 FROM delivery_skips d
                     WHERE d.customer_id=r.customer_id
                       AND d.delivery_date=r.requested_date
                 )"""
        ).fetchall()
        for duplicate in stale_duplicates:
            note = "Duplicate request resolved: this customer's delivery for this date was already skipped."
            db.execute(
                "UPDATE skip_requests SET status='confirmed', reviewed_at=COALESCE(reviewed_at, ?), ai_reason=? WHERE id=?",
                (local_iso(), note, duplicate["id"]),
            )
            log_event(
                db,
                "duplicate_skip_request_reconciled",
                f"Legacy request #{duplicate['id']} resolved because a delivery skip already exists for customer #{duplicate['customer_id']} on {duplicate['requested_date']}.",
            )


def log_event(db: sqlite3.Connection, event: str, details: str) -> None:
    db.execute(
        "INSERT INTO activity_log(event, details, created_at) VALUES(?,?,?)",
        (event, details, local_iso()),
    )


def verify_meta_signature(raw_body: bytes, signature: str | None, app_secret: str) -> bool:
    if not app_secret:
        # Never accept unsigned Meta webhook traffic. Demo ingestion uses its own local endpoint.
        return False
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_incoming_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return inbound text messages from standard WhatsApp Cloud webhook payloads."""
    found: list[dict[str, str]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            contacts = value.get("contacts", []) or []
            names = {}
            for c in contacts:
                wa_id = normalize_phone(c.get("wa_id", ""))
                profile = c.get("profile", {}) or {}
                if wa_id:
                    names[wa_id] = profile.get("name", "") or ""
            for msg in value.get("messages", []) or []:
                if msg.get("type") != "text":
                    continue
                sender = normalize_phone(msg.get("from", ""))
                text = (msg.get("text", {}) or {}).get("body", "").strip()
                if sender and text:
                    found.append({
                        "phone": sender,
                        "name": names.get(sender, ""),
                        "message": text,
                        "wamid": str(msg.get("id", "")),
                    })
    return found


def send_whatsapp_text(to_phone: str, body: str) -> tuple[bool, str]:
    """Send a free-form response only when explicitly enabled and configured."""
    if os.getenv("WHATSAPP_SEND_CONFIRMATIONS", "false").lower() != "true":
        return False, "Outgoing WhatsApp confirmations are disabled."
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    version = os.getenv("META_GRAPH_VERSION", "v26.0").strip()
    if not token or not phone_number_id:
        return False, "WhatsApp API credentials are not configured."
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_phone,
               "type": "text", "text": {"preview_url": False, "body": body}}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.ok:
            return True, "WhatsApp confirmation sent."
        return False, f"WhatsApp API returned HTTP {response.status_code}."
    except requests.RequestException:
        return False, "WhatsApp API request failed; delivery status was not changed."


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "local-demo-key-change-before-deploy"),
        DATABASE_PATH=os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "dukaanflow.db")),
        SEED_DEMO_DATA=os.getenv("SEED_DEMO_DATA", "true").lower() == "true",
        WEBHOOK_VERIFY_TOKEN=os.getenv("WHATSAPP_VERIFY_TOKEN", "change-me-local-verify-token"),
        META_APP_SECRET=os.getenv("META_APP_SECRET", "").strip(),
        ADMIN_PASSWORD=os.getenv("DASHBOARD_PASSWORD", "").strip(),
        ENABLE_DEMO_TOOLS=os.getenv("ENABLE_DEMO_TOOLS", "true").lower() == "true",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)
    init_db(app.config["DATABASE_PATH"], app.config["SEED_DEMO_DATA"])

    def dashboard_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            password = app.config.get("ADMIN_PASSWORD", "")
            if password and not session.get("authenticated"):
                if request.endpoint == "login":
                    return view(*args, **kwargs)
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    app.dashboard_auth = dashboard_auth  # type: ignore[attr-defined]

    @app.get("/login")
    def login():
        if not app.config.get("ADMIN_PASSWORD"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", error=None)

    @app.post("/login")
    def login_post():
        if not app.config.get("ADMIN_PASSWORD"):
            return redirect(url_for("dashboard"))
        submitted = request.form.get("password", "")
        if hmac.compare_digest(submitted, app.config["ADMIN_PASSWORD"]):
            session["authenticated"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("login.html", error="Password did not match."), 401

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @dashboard_auth
    def dashboard():
        today = now_local().date().isoformat()
        with get_db(app.config["DATABASE_PATH"]) as db:
            stats = {
                "active_customers": db.execute("SELECT COUNT(*) FROM customers WHERE subscription_status='active'").fetchone()[0],
                "pending_requests": db.execute("SELECT COUNT(*) FROM skip_requests WHERE status='pending'").fetchone()[0],
                "today_skips": db.execute("SELECT COUNT(*) FROM delivery_skips WHERE delivery_date=?", (today,)).fetchone()[0],
                "needs_review": db.execute("SELECT COUNT(*) FROM skip_requests WHERE status='review'").fetchone()[0],
            }
            requests_rows = db.execute(
                """SELECT r.*, c.name AS customer_name, c.subscription_status, c.plan
                   FROM skip_requests r LEFT JOIN customers c ON c.id=r.customer_id
                   ORDER BY CASE r.status WHEN 'pending' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                            r.created_at DESC LIMIT 40"""
            ).fetchall()
            customers = db.execute("SELECT * FROM customers ORDER BY name").fetchall()
            skip_rows = db.execute(
                """SELECT d.delivery_date, d.created_at, c.name, c.phone, r.message
                   FROM delivery_skips d JOIN customers c ON c.id=d.customer_id
                   JOIN skip_requests r ON r.id=d.request_id
                   ORDER BY d.delivery_date DESC, d.created_at DESC LIMIT 8"""
            ).fetchall()
            activity = db.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 6").fetchall()
        return render_template("dashboard.html", stats=stats, requests=requests_rows,
                               customers=customers, skips=skip_rows, activity=activity,
                               today=today, demo_tools=app.config["ENABLE_DEMO_TOOLS"],
                               admin_password_set=bool(app.config["ADMIN_PASSWORD"]))

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "DukaanFlow AI"})

    @app.post("/api/customers")
    @dashboard_auth
    def create_customer():
        data = request.get_json(silent=True) or request.form
        name = str(data.get("name", "")).strip()[:120]
        phone = normalize_phone(str(data.get("phone", "")))
        plan = str(data.get("plan", "Tiffin · lunch")).strip()[:160] or "Tiffin · lunch"
        if not name:
            return jsonify({"ok": False, "error": "Customer name is required."}), 400
        if not (8 <= len(phone) <= 15):
            return jsonify({"ok": False, "error": "Enter a phone number with country code (8–15 digits)."}), 400
        with get_db(app.config["DATABASE_PATH"]) as db:
            existing = db.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
            if existing and existing["subscription_status"] != "unknown":
                return jsonify({"ok": False, "error": "This number is already registered."}), 409
            if existing:
                db.execute("UPDATE customers SET name=?, subscription_status='active', plan=? WHERE id=?",
                           (name, plan, existing["id"]))
                log_event(db, "customer_mapped", f"Unmatched WhatsApp number ending {phone[-4:]} mapped to {name}; subscription marked active by seller.")
                result = "Unknown sender mapped and marked active. Review their existing message requests."
            else:
                db.execute("INSERT INTO customers(name,phone,subscription_status,plan,created_at) VALUES(?,?,?,?,?)",
                           (name, phone, "active", plan, local_iso()))
                log_event(db, "customer_created", f"Customer {name} added with number ending {phone[-4:]}.")
                result = "Customer added to the directory."
        return jsonify({"ok": True, "message": result})

    @app.post("/api/demo/message")
    @dashboard_auth
    def demo_message():
        if not app.config["ENABLE_DEMO_TOOLS"]:
            abort(404)
        data = request.get_json(silent=True) or request.form
        phone = normalize_phone(data.get("phone", "919000000001"))
        message = str(data.get("message", "")).strip()[:2000]
        name = str(data.get("name", "")).strip()[:120]
        if not message:
            return jsonify({"ok": False, "error": "Message is required."}), 400
        item = ingest_message(app, phone=phone, name=name, message=message, source="demo", wamid="")
        return jsonify({"ok": True, **item})

    @app.post("/api/requests/<int:request_id>/confirm")
    @dashboard_auth
    def confirm_request(request_id: int):
        today = now_local().date().isoformat()
        db_path = app.config["DATABASE_PATH"]
        with get_db(db_path) as db:
            row = db.execute(
                """SELECT r.*, c.subscription_status, c.name AS customer_name
                   FROM skip_requests r LEFT JOIN customers c ON c.id=r.customer_id WHERE r.id=?""",
                (request_id,),
            ).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "Request not found."}), 404
            if row["status"] == "confirmed":
                return jsonify({"ok": True, "message": "This request is already confirmed."})
            if row["status"] != "pending":
                return jsonify({"ok": False, "error": "Only a clear pending request can be confirmed. Review it first."}), 409
            if row["intent"] != "skip_today" or row["requested_date"] != today:
                return jsonify({"ok": False, "error": "Only a clearly identified request for today can be confirmed here."}), 409
            if not row["customer_id"] or row["subscription_status"] != "active":
                return jsonify({"ok": False, "error": "Customer is not matched to an active subscription."}), 409
            duplicate = db.execute(
                "SELECT id FROM delivery_skips WHERE customer_id=? AND delivery_date=?",
                (row["customer_id"], today),
            ).fetchone()
            if duplicate:
                db.execute("UPDATE skip_requests SET status='confirmed', reviewed_at=? WHERE id=?", (local_iso(), request_id))
                log_event(db, "duplicate_skip_request", f"Request #{request_id} matched an existing skip; no second skip created.")
                return jsonify({"ok": True, "message": "Today was already skipped. No duplicate skip was created."})
            inserted = db.execute(
                "INSERT OR IGNORE INTO delivery_skips(customer_id, delivery_date, request_id, created_at) VALUES(?,?,?,?)",
                (row["customer_id"], today, request_id, local_iso()),
            )
            if inserted.rowcount == 0:
                db.execute("UPDATE skip_requests SET status='confirmed', reviewed_at=? WHERE id=?", (local_iso(), request_id))
                log_event(db, "duplicate_skip_request", f"Request #{request_id} raced with an existing skip; no second skip created.")
                return jsonify({"ok": True, "message": "Today was already skipped. No duplicate skip was created."})
            db.execute("UPDATE skip_requests SET status='confirmed', reviewed_at=? WHERE id=?", (local_iso(), request_id))
            # Intentionally do not modify customers.subscription_status.
            log_event(db, "skip_confirmed", f"{row['customer_name']} — delivery skipped for {today}; subscription remains active.")
            sender_phone = row["sender_phone"]
            source = row["source"]
            message_created_at = row["created_at"]
        customer_message = "Theek hai, aaj ka tiffin skip kar diya hai. Aapki regular subscription active hai; agla tiffin normal schedule par aayega."
        sent = False
        outbound_note = "No external message sent."
        if source == "whatsapp":
            try:
                received_at = datetime.fromisoformat(message_created_at)
                conversation_is_open = now_local() - received_at <= timedelta(hours=24)
            except (TypeError, ValueError):
                conversation_is_open = False
            if conversation_is_open:
                sent, outbound_note = send_whatsapp_text(sender_phone, customer_message)
            else:
                outbound_note = "Message not sent: the 24-hour customer-service window may have expired. Configure an approved message template for later replies."
        return jsonify({"ok": True, "message": "Today's delivery skipped. Subscription remains active.",
                        "customer_message": customer_message, "whatsapp_sent": sent, "outbound_note": outbound_note})

    @app.post("/api/requests/<int:request_id>/review")
    @dashboard_auth
    def review_request(request_id: int):
        with get_db(app.config["DATABASE_PATH"]) as db:
            row = db.execute("SELECT id,status FROM skip_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "Request not found."}), 404
            if row["status"] in ("confirmed", "rejected"):
                return jsonify({"ok": False, "error": "Completed requests cannot be moved back to review."}), 409
            db.execute("UPDATE skip_requests SET status='review', reviewed_at=? WHERE id=?", (local_iso(), request_id))
            log_event(db, "request_reviewed", f"Request #{request_id} sent for manual review.")
        return jsonify({"ok": True, "message": "Request moved to review."})

    @app.post("/api/requests/<int:request_id>/recheck")
    @dashboard_auth
    def recheck_request(request_id: int):
        today = now_local().date().isoformat()
        with get_db(app.config["DATABASE_PATH"]) as db:
            row = db.execute(
                """SELECT r.*, c.subscription_status FROM skip_requests r
                   LEFT JOIN customers c ON c.id=r.customer_id WHERE r.id=?""", (request_id,)
            ).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "Request not found."}), 404
            if row["status"] != "review":
                return jsonify({"ok": False, "error": "Only requests in review can be rechecked."}), 409
            if row["intent"] == "skip_today" and row["requested_date"] == today and row["subscription_status"] == "active":
                db.execute("UPDATE skip_requests SET status='pending', reviewed_at=NULL WHERE id=?", (request_id,))
                log_event(db, "request_rechecked", f"Request #{request_id} matched to an active customer; awaiting seller confirmation.")
                return jsonify({"ok": True, "message": "Request is eligible. It is back in the confirmation queue; no delivery was changed."})
            return jsonify({"ok": False, "error": "Still needs manual handling: verify date, intent, and active subscription."}), 409

    @app.post("/api/requests/<int:request_id>/dismiss")
    @dashboard_auth
    def dismiss_request(request_id: int):
        with get_db(app.config["DATABASE_PATH"]) as db:
            row = db.execute("SELECT id,status FROM skip_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "Request not found."}), 404
            if row["status"] == "confirmed":
                return jsonify({"ok": False, "error": "A confirmed delivery skip cannot be dismissed."}), 409
            db.execute("UPDATE skip_requests SET status='rejected', reviewed_at=? WHERE id=?", (local_iso(), request_id))
            log_event(db, "request_dismissed", f"Request #{request_id} dismissed by seller; no delivery change made.")
        return jsonify({"ok": True, "message": "Request dismissed. No delivery was changed."})

    @app.get("/webhook")
    def webhook_verify():
        mode = request.args.get("hub.mode", "")
        token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and hmac.compare_digest(token, app.config["WEBHOOK_VERIFY_TOKEN"]):
            return challenge, 200
        return "Webhook verification failed", 403

    @app.post("/webhook")
    def webhook_receive():
        raw = request.get_data(cache=True)
        if not verify_meta_signature(raw, request.headers.get("X-Hub-Signature-256"), app.config["META_APP_SECRET"]):
            return jsonify({"ok": False, "error": "Invalid webhook signature."}), 401
        payload = request.get_json(silent=True) or {}
        messages = extract_incoming_messages(payload)
        for msg in messages:
            ingest_message(app, phone=msg["phone"], name=msg["name"], message=msg["message"],
                           source="whatsapp", wamid=msg["wamid"])
        return jsonify({"ok": True, "processed": len(messages)})

    return app


def ingest_message(app: Flask, phone: str, name: str, message: str, source: str, wamid: str = "") -> dict[str, Any]:
    db_path = app.config["DATABASE_PATH"]
    phone = normalize_phone(phone)
    parsed = classify_message(message)
    today = now_local().date()
    target_date: str | None = None
    if parsed["date"] == "today":
        target_date = today.isoformat()
    elif parsed["date"] == "tomorrow":
        target_date = (today + timedelta(days=1)).isoformat()

    with get_db(db_path) as db:
        # Serialize message ingestion so two near-simultaneous messages cannot both
        # pass the semantic duplicate checks and create separate pending requests.
        db.execute("BEGIN IMMEDIATE")
        if wamid:
            existing = db.execute("SELECT id,status FROM skip_requests WHERE wamid=?", (wamid,)).fetchone()
            if existing:
                return {"request_id": existing["id"], "status": existing["status"], "duplicate": True,
                        "intent": parsed["intent"], "reason": "Duplicate WhatsApp message ignored."}
        customer = db.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        if not customer and phone:
            # Unknown senders are recorded for identification but never auto-confirmed.
            cursor = db.execute(
                "INSERT INTO customers(name,phone,subscription_status,plan,created_at) VALUES(?,?,?,?,?)",
                (name or f"Unknown contact · {phone[-4:]}", phone, "unknown", "Needs customer mapping", local_iso()),
            )
            customer = db.execute("SELECT * FROM customers WHERE id=?", (cursor.lastrowid,)).fetchone()
            log_event(db, "unknown_customer", f"Unmatched number ending {phone[-4:]} needs mapping.")
        elif customer and name and customer["name"].startswith("Unknown contact"):
            db.execute("UPDATE customers SET name=? WHERE id=?", (name, customer["id"]))
            customer = db.execute("SELECT * FROM customers WHERE id=?", (customer["id"],)).fetchone()

        clear_skip = parsed["intent"] == "skip_today" and parsed["confidence"] >= 0.75 and target_date == today.isoformat()
        eligible = bool(customer and customer["subscription_status"] == "active")

        # Idempotency for semantic repeats, including demo messages that have no
        # WhatsApp message ID (wamid). Once today's skip is confirmed, ignore any
        # later request for the same customer/date without creating another row.
        if clear_skip and eligible and customer:
            confirmed_skip = db.execute(
                "SELECT r.id FROM delivery_skips d "
                "JOIN skip_requests r ON r.id=d.request_id "
                "WHERE d.customer_id=? AND d.delivery_date=? LIMIT 1",
                (customer["id"], target_date),
            ).fetchone()
            if confirmed_skip:
                reason = "Today's delivery skip is already confirmed; duplicate request ignored."
                log_event(db, "duplicate_skip_request", f"Repeated skip request from {customer['name']} ignored; today's skip already exists.")
                return {"request_id": confirmed_skip["id"], "status": "confirmed", "duplicate": True,
                        "intent": parsed["intent"], "reason": reason, "requested_date": target_date,
                        "customer_name": customer["name"]}

            pending_skip = db.execute(
                "SELECT id,status FROM skip_requests "
                "WHERE customer_id=? AND requested_date=? AND intent='skip_today' AND status='pending' "
                "ORDER BY id LIMIT 1",
                (customer["id"], target_date),
            ).fetchone()
            if pending_skip:
                reason = "A skip request for this customer and date is already pending; duplicate request ignored."
                log_event(db, "duplicate_skip_request", f"Repeated skip request from {customer['name']} ignored; request #{pending_skip['id']} is already pending.")
                return {"request_id": pending_skip["id"], "status": pending_skip["status"], "duplicate": True,
                        "intent": parsed["intent"], "reason": reason, "requested_date": target_date,
                        "customer_name": customer["name"]}

        status = "pending" if clear_skip and eligible else "review"
        reason = parsed["reason"]
        if not customer or customer["subscription_status"] != "active":
            reason += " Customer is not matched to an active subscription."
        elif parsed["intent"] == "skip_today" and target_date != today.isoformat():
            reason += " Only same-day skip is supported in this MVP; review date before acting."
        if parsed["intent"] in ("cancel_subscription", "unknown"):
            reason += " This is not a safe one-day skip; manual review required."
        try:
            cursor = db.execute(
                """INSERT INTO skip_requests(customer_id,sender_phone,sender_name,message,intent,requested_date,
                    status,source,wamid,ai_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (customer["id"] if customer else None, phone, name, message, parsed["intent"], target_date,
                 status, source, wamid or None, reason, local_iso()),
            )
        except sqlite3.IntegrityError:
            existing = db.execute("SELECT id,status FROM skip_requests WHERE wamid=?", (wamid,)).fetchone() if wamid else None
            if existing:
                return {"request_id": existing["id"], "status": existing["status"], "duplicate": True,
                        "intent": parsed["intent"], "reason": "Duplicate WhatsApp message ignored."}
            raise
        request_id = cursor.lastrowid
        log_event(db, "message_received", f"Request #{request_id} received from {source}; intent={parsed['intent']}; status={status}.")
        return {"request_id": request_id, "status": status, "duplicate": False, "intent": parsed["intent"],
                "reason": reason, "requested_date": target_date, "customer_name": customer["name"] if customer else "Unknown"}


app = create_app()

if __name__ == "__main__":
    # Local development only. Use a WSGI server behind HTTPS for real webhooks.
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False)

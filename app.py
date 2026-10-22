"""
Flask app for IP Monitor.
Ping loop ticks every 60s and calls check_due_hosts() for per-host intervals.
Ping All button and Uptime/SLA routes removed.
"""

import os
import re
import csv
import io
import logging
import threading
import time
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from database import (
    init_db, add_host, remove_host, get_all_hosts, get_host, update_host,
    get_setting, get_all_settings, update_setting,
    get_incidents, get_latency_history, get_customer_latency,
    log_audit, get_audit_log,
)
from pinger import check_due_hosts, check_single_host

PING_INTERVAL = int(os.getenv("PING_INTERVAL", "300"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ip_monitor")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# IP address validation
# ---------------------------------------------------------------------------
def normalize_ip(raw):
    """Check that raw is a usable IPv4 address (four numbers 0-255, like
    192.168.1.10). Returns (clean_ip, None) if valid, or (None, message)."""
    ip = (raw or "").strip()
    if not ip:
        return None, "Enter an IP address."
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() and len(p) <= 3 for p in parts):
        return None, f"{ip} isn't a valid IP address. Use four numbers separated by dots, like 192.168.1.10."
    nums = [int(p) for p in parts]
    if any(n > 255 for n in nums):
        return None, f"{ip} isn't a valid IP address. Each number must be between 0 and 255."
    clean = ".".join(str(n) for n in nums)  # drops leading zeros, e.g. 010 -> 10
    if clean in ("0.0.0.0", "255.255.255.255"):
        return None, f"{clean} can't be monitored. Enter the address of a specific device."
    return clean, None


EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


def normalize_emails(raw):
    """Turn 'a@x.com; b@y.com, A@x.com' into 'a@x.com,b@y.com'.
    Accepts commas, semicolons, spaces or new lines between addresses and
    removes duplicates. Returns (clean_string, None) or (None, message).
    An empty value is allowed (it means 'use the default list')."""
    parts = [p.strip() for p in re.split(r"[,;\s]+", raw or "") if p.strip()]
    clean, seen = [], set()
    for p in parts:
        if not EMAIL_RE.match(p):
            return None, f"{p} isn't a valid email address."
        if p.lower() not in seen:
            seen.add(p.lower())
            clean.append(p)
    return ",".join(clean), None


# ---------------------------------------------------------------------------
# Background ping loop — 60s tick, per-host intervals via get_due_hosts()
# ---------------------------------------------------------------------------
def ping_loop():
    while True:
        try:
            check_due_hosts()
        except Exception as e:
            logger.error(f"Error in ping loop: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    hosts    = get_all_hosts()
    settings = get_all_settings()
    return render_template("index.html", hosts=hosts,
                           ping_interval=int(settings.get("ping_interval", PING_INTERVAL)),
                           settings=settings)


@app.route("/api/hosts", methods=["GET"])
def api_get_hosts():
    return jsonify(get_all_hosts())


@app.route("/api/hosts", methods=["POST"])
def api_add_host():
    data     = request.form if request.form else request.get_json(force=True)
    ip       = data.get("ip_address", "").strip()
    name     = data.get("customer_name", "").strip()
    location = data.get("location", "").strip()
    dtype    = data.get("device_type", "").strip()
    notes    = data.get("notes", "").strip()
    interval = max(int(data.get("ping_interval") or 300), 60)
    alert_to = data.get("alert_to", "").strip()

    if not ip or not name:
        if request.content_type and "json" in request.content_type:
            return jsonify({"error": "ip_address and customer_name are required"}), 400
        return redirect(url_for("index"))

    ip, ip_error = normalize_ip(ip)
    if ip_error:
        if request.content_type and "json" in request.content_type:
            return jsonify({"error": ip_error}), 400
        return redirect(url_for("index"))
    alert_to, email_error = normalize_emails(alert_to)
    if email_error:
        if request.content_type and "json" in request.content_type:
            return jsonify({"error": email_error}), 400
        return redirect(url_for("index"))

    result = add_host(ip, name, location, dtype, notes, interval, alert_to)
    if result is None:
        if request.content_type and "json" in request.content_type:
            return jsonify({"error": "IP address already exists"}), 409
        return redirect(url_for("index"))

    log_audit("add_host", ip, f"Customer: {name}, Interval: {interval}s, AlertTo: {alert_to or 'global'}")
    if request.content_type and "json" in request.content_type:
        return jsonify(result), 201
    return redirect(url_for("index"))


@app.route("/api/hosts/<int:host_id>", methods=["PUT"])
def api_update_host(host_id):
    data     = request.get_json(force=True)
    interval = data.get("ping_interval")
    if interval is not None:
        interval = max(int(interval), 60)
    if not get_host(host_id):
        return jsonify({"error": "This device no longer exists. Refresh the page."}), 404
    new_ip = data.get("ip_address")
    if new_ip is not None:
        new_ip, ip_error = normalize_ip(new_ip)
        if ip_error:
            return jsonify({"error": ip_error}), 400
    new_alert = data.get("alert_to")
    if new_alert is not None:
        new_alert, email_error = normalize_emails(new_alert)
        if email_error:
            return jsonify({"error": email_error}), 400
    result = update_host(
        host_id,
        ip_address    = new_ip,
        customer_name = data.get("customer_name"),
        location      = data.get("location"),
        device_type   = data.get("device_type"),
        notes         = data.get("notes"),
        ping_interval = interval,
        alert_to      = new_alert,
    )
    if result is None:
        return jsonify({"error": f"{new_ip} is already used by another device."}), 409
    log_audit("update_host", result["ip_address"], f"Updated host #{host_id}")
    return jsonify(result)


@app.route("/api/hosts/<int:host_id>", methods=["DELETE"])
def api_delete_host(host_id):
    host = get_host(host_id)
    remove_host(host_id)
    if host:
        log_audit("remove_host", host["ip_address"], f"Customer: {host['customer_name']}")
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(get_all_settings())


@app.route("/api/settings", methods=["PUT"])
def api_update_settings():
    data = request.get_json(force=True)
    if "alert_to" in data:
        clean, email_error = normalize_emails(data["alert_to"])
        if email_error:
            return jsonify({"error": email_error}), 400
        update_setting("alert_to", clean)
    log_audit("update_settings", "settings", f"Updated: {', '.join(data.keys())}")
    return jsonify(get_all_settings())


@app.route("/api/hosts/<int:host_id>/ping", methods=["POST"])
def api_ping_host(host_id):
    host = get_host(host_id)
    if not host:
        return jsonify({"error": "Host not found"}), 404
    check_single_host(host)
    return jsonify(get_host(host_id))


@app.route("/api/incidents")
def api_incidents():
    host_id = request.args.get("host_id", type=int)
    limit   = request.args.get("limit", 100, type=int)
    return jsonify(get_incidents(host_id, limit))


@app.route("/api/hosts/<int:host_id>/latency")
def api_host_latency(host_id):
    hours = request.args.get("hours", 24, type=int)
    return jsonify(get_latency_history(host_id, hours))


@app.route("/api/latency/customer")
def api_customer_latency():
    customer = request.args.get("customer", "").strip()
    if not customer:
        return jsonify({"error": "customer parameter required"}), 400
    hours = request.args.get("hours", 24, type=int)
    return jsonify(get_customer_latency(customer, hours))


@app.route("/api/report/csv")
def api_report_csv():
    incidents = get_incidents(limit=5000)
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["Customer", "IP Address", "Location", "Started",
                "Ended", "Duration (min)", "Resolved"])
    for inc in incidents:
        dur = round(inc["duration_seconds"] / 60, 1) if inc["duration_seconds"] else "ongoing"
        w.writerow([inc["customer_name"], inc["ip_address"], inc.get("location", ""),
                    inc["started_at"], inc["ended_at"] or "—", dur,
                    "Yes" if inc["resolved"] else "No"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=incidents_report.csv"})


# Columns the import understands, in the order they appear in the template
TEMPLATE_COLUMNS = ["ip_address", "customer_name", "location", "device_type",
                    "notes", "ping_interval", "alert_to"]


@app.route("/api/hosts/template")
def api_hosts_template():
    """Download a ready-to-fill CSV template for bulk import.
    The example row starts with '#', so the import skips it even if
    nobody deletes it."""
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(TEMPLATE_COLUMNS)
    w.writerow(["#192.0.2.10", "Example Customer", "Main Office", "Router",
                "Example row - lines starting with # are ignored", "300",
                "alerts@example.com;noc@example.com"])
    # The leading BOM helps Excel open the file with the right encoding
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=ip_monitor_import_template.csv"})


@app.route("/api/hosts/import", methods=["POST"])
def api_import_hosts():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        text = f.read().decode("utf-8-sig")
    except Exception:
        return jsonify({"error": "Could not read file as UTF-8"}), 400

    reader  = csv.DictReader(io.StringIO(text))
    added = skipped = 0
    errors = []
    for i, row in enumerate(reader, start=2):
        ip    = (row.get("ip_address") or row.get("ip") or "").strip()
        name  = (row.get("customer_name") or row.get("customer") or "").strip()
        loc   = (row.get("location") or "").strip()
        dtype = (row.get("device_type") or row.get("type") or "").strip()
        notes = (row.get("notes") or "").strip()
        alert = (row.get("alert_to") or "").strip()
        try:
            interval = max(int(float((row.get("ping_interval") or "").strip() or 300)), 60)
        except ValueError:
            interval = 300  # unreadable value -> default 5 minutes
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        if ip.startswith("#"):
            continue  # example or comment row
        if not ip or not name:
            errors.append(f"Row {i}: missing ip_address or customer_name")
            continue
        ip, ip_error = normalize_ip(ip)
        if ip_error:
            errors.append(f"Row {i}: {ip_error}")
            continue
        alert, email_error = normalize_emails(alert)
        if email_error:
            errors.append(f"Row {i}: {email_error}")
            continue
        result = add_host(ip, name, loc, dtype, notes, interval, alert)
        if result is None:
            skipped += 1
        else:
            added += 1
    if added:
        log_audit("bulk_import", f"{added} hosts", f"Skipped {skipped}, Errors {len(errors)}")
    return jsonify({"added": added, "skipped": skipped, "errors": errors})


@app.route("/api/audit-log")
def api_audit_log():
    limit = request.args.get("limit", 200, type=int)
    return jsonify(get_audit_log(limit))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    threading.Thread(target=ping_loop, daemon=True).start()
    logger.info("Background pinger started (60s tick, per-host intervals)")
    app.run(host="0.0.0.0", port=5000, debug=False)
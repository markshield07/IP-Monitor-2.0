"""
Notification module for IP Monitor.
Sends alerts via Workato webhook.
Falls back to SMTP if Workato is not configured.
"""

import smtplib
import os
import logging
import json
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

WORKATO_URL = os.getenv("WORKATO_WEBHOOK_URL", "")


def _send_workato(event, host):
    """Send alert to Workato webhook."""
    if not WORKATO_URL:
        logger.warning("WORKATO_WEBHOOK_URL not set — skipping webhook")
        return False

    # Get alert_to: per-host override → global setting → default
    alert_to = host.get("alert_to", "")
    if not alert_to:
        try:
            from database import get_setting
            alert_to = get_setting("alert_to", "")
        except Exception:
            pass

    payload = {
        "event": event,
        "ip_address": host.get("ip_address", ""),
        "customer_name": host.get("customer_name", ""),
        "location": host.get("location", "N/A"),
        "device_type": host.get("device_type", "N/A"),
        "status": "down" if event == "host_down" else "up",
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "alert_to": alert_to,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            WORKATO_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Workato webhook sent: {event} for {host['ip_address']} (HTTP {resp.status})")
            return True
    except Exception as e:
        logger.error(f"Workato webhook failed: {e}")
        return False


def _get_smtp_config(alert_to_override=None):
    """Read SMTP settings from environment, with alert_to overridable from DB."""
    db_alert_to = ""
    try:
        from database import get_setting
        db_alert_to = get_setting("alert_to", "")
    except Exception:
        pass

    to_addr = alert_to_override or db_alert_to or os.getenv("ALERT_TO", "")

    return {
        "host": os.getenv("SMTP_HOST", "smtp.office365.com"),
        "port": int(os.getenv("SMTP_PORT", "587") or "587"),
        "user": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from_addr": os.getenv("ALERT_FROM", ""),
        "to_addr": to_addr,
    }


def _send_email(subject, body_html, alert_to_override=None):
    """Send an email using SMTP. Fails silently with a log if not configured."""
    config = _get_smtp_config(alert_to_override)

    if not config["host"] or not config["user"] or not config["to_addr"]:
        logger.warning("SMTP not configured — skipping email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["from_addr"]
    msg["To"] = config["to_addr"]
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(config["host"], config["port"]) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(config["user"], config["password"])
            server.sendmail(
                config["from_addr"],
                config["to_addr"].split(","),
                msg.as_string()
            )
        logger.info(f"Alert email sent → {config['to_addr']}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def send_down_alert(host, alert_to_override=None):
    """Send alert that a host is unreachable."""
    # Try Workato first
    _send_workato("host_down", host)

    # Also try SMTP as fallback
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"🔴 DOWN: {host['ip_address']} — {host['customer_name']}"
    body = f"""
    <html><body style="font-family:Arial,sans-serif;">
    <h2 style="color:#dc3545;">Host Unreachable</h2>
    <table style="border-collapse:collapse;">
        <tr><td style="padding:6px 12px;font-weight:bold;">IP Address:</td>
            <td style="padding:6px 12px;">{host['ip_address']}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Customer:</td>
            <td style="padding:6px 12px;">{host['customer_name']}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Location:</td>
            <td style="padding:6px 12px;">{host.get('location','N/A')}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Device Type:</td>
            <td style="padding:6px 12px;">{host.get('device_type','N/A')}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Detected At:</td>
            <td style="padding:6px 12px;">{now}</td></tr>
    </table>
    <p style="color:#666;margin-top:20px;">
        Host failed to respond to ICMP ping. Please investigate.
    </p>
    </body></html>"""
    _send_email(subject, body, alert_to_override)


def send_recovery_alert(host, alert_to_override=None):
    """Send alert that a host has recovered."""
    # Try Workato first
    _send_workato("host_recovered", host)

    # Also try SMTP as fallback
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"🟢 RECOVERED: {host['ip_address']} — {host['customer_name']}"
    body = f"""
    <html><body style="font-family:Arial,sans-serif;">
    <h2 style="color:#28a745;">Host Recovered</h2>
    <table style="border-collapse:collapse;">
        <tr><td style="padding:6px 12px;font-weight:bold;">IP Address:</td>
            <td style="padding:6px 12px;">{host['ip_address']}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Customer:</td>
            <td style="padding:6px 12px;">{host['customer_name']}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Location:</td>
            <td style="padding:6px 12px;">{host.get('location','N/A')}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Device Type:</td>
            <td style="padding:6px 12px;">{host.get('device_type','N/A')}</td></tr>
        <tr><td style="padding:6px 12px;font-weight:bold;">Recovered At:</td>
            <td style="padding:6px 12px;">{now}</td></tr>
    </table>
    <p style="color:#666;margin-top:20px;">
        Host is now responding to ICMP ping again.
    </p>
    </body></html>"""
    _send_email(subject, body, alert_to_override)
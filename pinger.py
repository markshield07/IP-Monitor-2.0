"""
Pinger module for IP Monitor.
check_due_hosts() — only pings hosts whose interval has elapsed (60s tick).
check_all_hosts() — force-pings everything (manual trigger only).
"""

import re
import subprocess
import platform
import logging
from database import (
    get_all_hosts, get_due_hosts, update_host_status,
    record_ping, open_incident, close_incident,
)
from notifier import send_down_alert, send_recovery_alert

logger = logging.getLogger(__name__)

PING_FAIL_PATTERNS = [
    "destination host unreachable", "request timed out",
    "general failure", "transmit failed", "100% packet loss", "unreachable",
]
_LATENCY_RE = re.compile(r"time[=<](\d+\.?\d*)\s*ms", re.IGNORECASE)
_LATENCY_SUMMARY_RE = re.compile(r"min/avg/max/[a-z]+ = [\d.]+/([\d.]+)/", re.IGNORECASE)


def ping_host(ip_address, timeout=2):
    system = platform.system().lower()
    cmd = (["ping", "-n", "1", "-w", str(timeout * 1000), ip_address]
           if system == "windows"
           else ["ping", "-c", "1", "-W", str(timeout), ip_address])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        full_output = result.stdout + result.stderr
        output_lower = full_output.lower()
        has_failure = any(p in output_lower for p in PING_FAIL_PATTERNS)
        reachable = result.returncode == 0 and not has_failure
        latency_ms = None
        if reachable:
            m = _LATENCY_RE.search(full_output)
            if m:
                latency_ms = float(m.group(1))
            else:
                m2 = _LATENCY_SUMMARY_RE.search(full_output)
                if m2:
                    latency_ms = float(m2.group(1))
        logger.info(f"PING {ip_address}: reachable={reachable} latency={latency_ms}ms")
        return reachable, latency_ms
    except subprocess.TimeoutExpired:
        logger.warning(f"PING {ip_address}: timeout")
        return False, None
    except Exception as e:
        logger.error(f"Ping error for {ip_address}: {e}")
        return False, None


def _handle_transition(host, old_status, new_status):
    if old_status is None:
        return
    per_host_alert = host.get("alert_to") or None
    if old_status != "down" and new_status == "down":
        logger.warning(f"HOST DOWN: {host['ip_address']} ({host['customer_name']})")
        open_incident(host["id"])
        send_down_alert(host, alert_to_override=per_host_alert)
    elif old_status == "down" and new_status == "up":
        logger.info(f"HOST RECOVERED: {host['ip_address']} ({host['customer_name']})")
        close_incident(host["id"])
        send_recovery_alert(host, alert_to_override=per_host_alert)


def check_single_host(host):
    reachable, latency_ms = ping_host(host["ip_address"])
    new_status = "up" if reachable else "down"
    old_status = update_host_status(host["id"], new_status)
    record_ping(host["id"], new_status, latency_ms)
    _handle_transition(host, old_status, new_status)
    return new_status


def check_due_hosts():
    """Called every 60s — only pings hosts whose interval has elapsed."""
    hosts = get_due_hosts()
    if not hosts:
        logger.info("No hosts due for pinging.")
        return
    logger.info(f"Pinging {len(hosts)} due host(s)...")
    for host in hosts:
        reachable, latency_ms = ping_host(host["ip_address"])
        new_status = "up" if reachable else "down"
        old_status = update_host_status(host["id"], new_status)
        record_ping(host["id"], new_status, latency_ms)
        _handle_transition(host, old_status, new_status)
    logger.info("Ping cycle complete.")


def check_all_hosts():
    """Force-ping all hosts regardless of interval (manual triggers only)."""
    hosts = get_all_hosts()
    if not hosts:
        return
    logger.info(f"Force-pinging {len(hosts)} host(s)...")
    for host in hosts:
        reachable, latency_ms = ping_host(host["ip_address"])
        new_status = "up" if reachable else "down"
        old_status = update_host_status(host["id"], new_status)
        record_ping(host["id"], new_status, latency_ms)
        _handle_transition(host, old_status, new_status)
    logger.info("Force ping complete.")
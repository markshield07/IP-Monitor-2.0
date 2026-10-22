"""
Database layer for IP Monitor.
Now supports per-host ping_interval and alert_to.
"""

import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "ip_monitor.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL UNIQUE,
            customer_name TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            device_type TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            ping_interval INTEGER NOT NULL DEFAULT 300,
            alert_to TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unknown',
            last_checked TEXT,
            last_state_change TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ping_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            latency_ms REAL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_seconds INTEGER,
            resolved INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            detail TEXT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ping_history_host_ts ON ping_history(host_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_host ON incidents(host_id)")

    # Safe migrations for existing DBs
    for col, default in [("notes", "''"), ("device_type", "''"),
                         ("ping_interval", "300"), ("alert_to", "''")]:
        try:
            conn.execute(f"ALTER TABLE hosts ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass

    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ping_interval', '300')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('alert_to', '')")
    conn.commit()
    conn.close()


def get_setting(key, default=""):
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def get_all_settings():
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def update_setting(key, value):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def add_host(ip_address, customer_name, location="", device_type="",
             notes="", ping_interval=300, alert_to=""):
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO hosts
               (ip_address, customer_name, location, device_type, notes, ping_interval, alert_to)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ip_address.strip(), customer_name.strip(), location.strip(),
             device_type.strip(), notes.strip(), max(int(ping_interval), 60), alert_to.strip()),
        )
        conn.commit()
        host = conn.execute("SELECT * FROM hosts WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(host)
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def remove_host(host_id):
    conn = get_connection()
    for table in ("status_log", "ping_history", "incidents"):
        conn.execute(f"DELETE FROM {table} WHERE host_id = ?", (host_id,))
    conn.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
    conn.commit()
    conn.close()


def get_all_hosts():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM hosts ORDER BY customer_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_host(host_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_due_hosts():
    """Return only hosts whose ping_interval has elapsed since last_checked."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM hosts ORDER BY customer_name").fetchall()
    conn.close()
    due = []
    for r in rows:
        h = dict(r)
        interval = max(int(h.get("ping_interval") or 300), 60)
        last = h.get("last_checked")
        if not last:
            due.append(h)
        else:
            try:
                elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
                if elapsed >= interval:
                    due.append(h)
            except Exception:
                due.append(h)
    return due


def update_host(host_id, ip_address=None, customer_name=None, location=None,
                device_type=None, notes=None, ping_interval=None, alert_to=None):
    conn = get_connection()
    row = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
    if not row:
        conn.close()
        return None

    new_ip       = ip_address.strip()    if ip_address    is not None else row["ip_address"]
    new_name     = customer_name.strip() if customer_name is not None else row["customer_name"]
    new_loc      = location.strip()      if location      is not None else row["location"]
    new_dtype    = device_type.strip()   if device_type   is not None else row["device_type"]
    new_notes    = notes.strip()         if notes         is not None else row["notes"]
    new_interval = max(int(ping_interval), 60) if ping_interval is not None else row["ping_interval"]
    new_alert    = alert_to.strip()      if alert_to      is not None else row["alert_to"]

    try:
        conn.execute(
            """UPDATE hosts SET ip_address=?, customer_name=?, location=?,
               device_type=?, notes=?, ping_interval=?, alert_to=? WHERE id=?""",
            (new_ip, new_name, new_loc, new_dtype, new_notes, new_interval, new_alert, host_id),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone())
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def update_host_status(host_id, new_status):
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    row = conn.execute("SELECT status FROM hosts WHERE id = ?", (host_id,)).fetchone()
    if not row:
        conn.close()
        return None
    old_status = row["status"]
    conn.execute(
        """UPDATE hosts SET status=?, last_checked=?,
           last_state_change=COALESCE(?, last_state_change) WHERE id=?""",
        (new_status, now, now if old_status != new_status else None, host_id),
    )
    conn.execute("INSERT INTO status_log (host_id, status) VALUES (?, ?)", (host_id, new_status))
    conn.commit()
    conn.close()
    return old_status


def record_ping(host_id, status, latency_ms=None):
    conn = get_connection()
    conn.execute(
        "INSERT INTO ping_history (host_id, status, latency_ms) VALUES (?, ?, ?)",
        (host_id, status, latency_ms),
    )
    conn.commit()
    conn.close()


def get_latency_history(host_id, hours=24):
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = get_connection()
    rows = conn.execute(
        """SELECT status, latency_ms, timestamp FROM ping_history
           WHERE host_id=? AND timestamp>=? ORDER BY timestamp""",
        (host_id, since),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customer_latency(customer_name, hours=24):
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = get_connection()
    hosts = conn.execute(
        """SELECT id, ip_address, location, device_type, ping_interval
           FROM hosts WHERE customer_name=? ORDER BY ip_address""",
        (customer_name,),
    ).fetchall()
    result = []
    for h in hosts:
        pings = conn.execute(
            """SELECT status, latency_ms, timestamp FROM ping_history
               WHERE host_id=? AND timestamp>=? ORDER BY timestamp""",
            (h["id"], since),
        ).fetchall()
        result.append({
            "host_id":       h["id"],
            "ip_address":    h["ip_address"],
            "location":      h["location"],
            "device_type":   h["device_type"],
            "ping_interval": h["ping_interval"],
            "pings":         [dict(r) for r in pings],
        })
    conn.close()
    return result


def open_incident(host_id):
    conn = get_connection()
    conn.execute(
        "INSERT INTO incidents (host_id, started_at) VALUES (?, ?)",
        (host_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def close_incident(host_id):
    now = datetime.utcnow()
    conn = get_connection()
    row = conn.execute(
        """SELECT id, started_at FROM incidents
           WHERE host_id=? AND resolved=0 ORDER BY started_at DESC LIMIT 1""",
        (host_id,),
    ).fetchone()
    if row:
        duration = int((now - datetime.fromisoformat(row["started_at"])).total_seconds())
        conn.execute(
            "UPDATE incidents SET ended_at=?, duration_seconds=?, resolved=1 WHERE id=?",
            (now.isoformat(), duration, row["id"]),
        )
        conn.commit()
    conn.close()


def get_incidents(host_id=None, limit=100):
    conn = get_connection()
    if host_id:
        rows = conn.execute(
            """SELECT i.*, h.ip_address, h.customer_name, h.location
               FROM incidents i JOIN hosts h ON i.host_id=h.id
               WHERE i.host_id=? ORDER BY i.started_at DESC LIMIT ?""",
            (host_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT i.*, h.ip_address, h.customer_name, h.location
               FROM incidents i JOIN hosts h ON i.host_id=h.id
               ORDER BY i.started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_audit(action, target, detail=None):
    conn = get_connection()
    conn.execute(
        "INSERT INTO audit_log (action, target, detail) VALUES (?, ?, ?)",
        (action, target, detail),
    )
    conn.commit()
    conn.close()


def get_audit_log(limit=200):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
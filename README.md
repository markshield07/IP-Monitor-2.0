# IP Monitor 2.0

A web dashboard that pings customer devices on a schedule and sends an email alert (through a Workato webhook) when a device goes offline and again when it comes back online.

## What it does

- Pings each device on its own schedule (every 1 minute to every hour, 5 minutes by default).
- Alerts only when a device's status **changes**: one email when it goes offline, one when it recovers. No repeated emails while it stays down.
- Groups devices by customer, with customers that have outages listed first.
- Keeps an outage history (Incidents), response-time charts (Latency), and a record of every change made in the tool (Audit log).
- Bulk-adds devices from a CSV file. A ready-made template can be downloaded from the dashboard.
- Rejects invalid IP addresses and email addresses.

## Setup

Requires Python 3.9 or newer.

1. Download or clone this repository.
2. Copy `.env.example`, rename the copy to `.env`, and fill in `WORKATO_WEBHOOK_URL`. Never upload `.env` to GitHub.
3. In a terminal, from the project folder:

   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python app.py
   ```

   On Windows, activate with `.venv\Scripts\activate` instead.

4. Open http://localhost:5000 in a browser.
5. Click **Alert settings** in the sidebar and add the emails that should receive alerts.

The database file (`ip_monitor.db`) is created automatically on first start.

## Adding devices

- **One at a time:** use the "Add a device" form on the dashboard.
- **Many at once:** click **Download CSV template**, fill it in (one device per row), and drag the file onto the import box. Rows starting with `#` are ignored. Put multiple alert emails in one cell, separated by semicolons.

## How alerts work

When a device changes status, the app sends this JSON to the Workato webhook:

| Field | Example |
|---|---|
| `event` | `host_down` or `host_recovered` |
| `ip_address` | `144.91.180.139` |
| `customer_name` | `Lodi USD` |
| `location` | `Liberty` |
| `device_type` | `Router` |
| `status` | `down` or `up` |
| `timestamp` | `2026-10-22 20:04:37 UTC` |
| `alert_to` | `a@goto.com,b@goto.com` |

`alert_to` is the device's own alert list if it has one, otherwise the list from Alert settings. The Workato recipe should check `event` to choose the down or recovery email, and use `alert_to` as the email's To field.

## Files

| File | Purpose |
|---|---|
| `app.py` | Starts the web dashboard and the background pinger |
| `pinger.py` | Pings devices and detects status changes |
| `notifier.py` | Sends alerts to Workato (and optionally by email) |
| `database.py` | Stores devices, ping history, incidents and settings |
| `templates/index.html` | The dashboard page |
| `.env.example` | Template for the private settings file |

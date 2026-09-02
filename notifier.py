"""
E-Report generic webhook notification daemon.

Runs continuously in the background (24/7) and checks every form that has
Webhook reminders enabled in form_config.json. For each such form:

  - Starting from `start_hour` (default 07:00) every day, checks whether a
    new record has been created today (CreatedAt >= today's start_hour).
    - If no new record yet, sends a webhook reminder, then keeps sending
    the reminder every `interval_hours` until a new record for today
    appears.
  - Once a record for today exists, reminders stop until the next day's
    start_hour cycle begins again.

State (last check time / last reminder time per table) is persisted to
notify_state.json so restarts don't cause a flood of duplicate messages.

Run this alongside the Streamlit app, e.g. via run_notifier.bat, on a
machine that stays on 24/7.
"""
import json
import time as time_mod
import requests
from datetime import datetime, timedelta, timezone

FORM_CONFIG_FILE = "form_config.json"
NOTIFY_STATE_FILE = "notify_state.json"
CHECK_INTERVAL_SECONDS = 300  # check every 5 minutes
LOCAL_TZ = timezone(timedelta(hours=7))  # UTC+7 (same as app.py)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[notifier] failed to save {path}: {e}")


def get_secrets():
    """Read .streamlit/secrets.toml without extra dependencies (no external toml lib assumed)."""
    import re
    path = ".streamlit/secrets.toml"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"\[nocodb\](.*?)(\n\[|\Z)", text, re.S)
    body = m.group(1)
    out = {}
    for line in body.strip().splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


_secrets = get_secrets()
NOCO_BASE = _secrets["base_url"]
NOCO_HEADERS = {"xc-token": _secrets["token"]}


def noco_get_latest_record(table_id):
    """Fetch the most recently created record of a table (sorted by CreatedAt desc)."""
    url = f"{NOCO_BASE}/api/v2/tables/{table_id}/records?limit=1&sort=-CreatedAt"
    try:
        resp = requests.get(url, headers=NOCO_HEADERS, timeout=15)
        if resp.status_code == 200:
            lst = resp.json().get("list", [])
            return (lst[0] if lst else None), None
        return None, f"HTTP {resp.status_code}"
    except Exception as e:
        return None, str(e)


def parse_created_at(rec):
    """Parse a NocoDB record's CreatedAt into a timezone-aware datetime (UTC+7)."""
    import re
    cat = str(rec.get("CreatedAt", "") or "")
    if not cat:
        return None
    try:
        dstr = cat.strip().replace("Z", "+00:00")
        dstr = re.sub(r"(\.\d{6})\d+", r"\1", dstr)
        dt = datetime.fromisoformat(dstr)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ)
    except Exception:
        return None


def send_webhook_message(webhook_url, text):
    """Send a notification to a generic webhook endpoint as JSON."""
    if not webhook_url:
        return False, "no webhook url"
    payload = {"message": text, "text": text}
    try:
        current_url = webhook_url
        for _ in range(5):
            resp = requests.post(current_url, json=payload, timeout=10, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308) and "Location" in resp.headers:
                current_url = resp.headers["Location"]
                continue
            break
        if 200 <= resp.status_code < 300:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def today_start_time(now, start_hour, start_minute=0):
    """Return today's reminder start datetime (in LOCAL_TZ)."""
    return now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)


def check_and_notify(table_id, cfg, state):
    """Check one table's notify config, send reminder if needed. Mutates `state` in place."""
    notify = cfg.get("notify", {})
    if not notify.get("enabled"):
        return

    webhook_url = notify.get("webhook_url", "")
    start_hour = int(notify.get("start_hour", 7))
    start_minute = int(notify.get("start_minute", 0))
    interval_hours = float(notify.get("interval_hours", 2))
    report_name = notify.get("report_name", table_id)
    message_template = notify.get("message_template") or (
        "⚠️ แจ้งเตือนการกรอกข้อมูล\n"
        "รายงาน: {report_name}\n"
        "ยังไม่มีการกรอกข้อมูลใหม่วันนี้ (เริ่มเช็คตั้งแต่ {start_hour})\n"
        "เวลาแจ้งเตือน: {now}"
    )
    notify_on_success = notify.get("notify_on_success", False)
    success_template = notify.get("success_message_template") or (
        "✅ มีการกรอกข้อมูลแล้ว\n"
        "รายงาน: {report_name}\n"
        "เวลาที่ตรวจพบ: {now}"
    )

    if not webhook_url:
        return

    now = datetime.now(LOCAL_TZ)
    cycle_start = today_start_time(now, start_hour, start_minute)
    if now < cycle_start:
        # Not yet reached today's start time — nothing to do.
        return

    rec, err = noco_get_latest_record(table_id)
    if err:
        print(f"[notifier] {report_name}: failed to fetch latest record: {err}")
        return

    latest_created = parse_created_at(rec) if rec else None
    has_new_today = latest_created is not None and latest_created >= cycle_start

    tstate = state.setdefault(table_id, {})

    if has_new_today:
        # Data already entered today — reset reminder state, wait for next cycle.
        already_notified_success = tstate.get("success_notified_cycle") == cycle_start.isoformat()
        if tstate.get("last_reminder_cycle") == cycle_start.isoformat():
            print(f"[notifier] {report_name}: new record found, stopping reminders for today")
        tstate["last_reminder_cycle"] = None
        tstate["last_reminder_at"] = None

        if notify_on_success and not already_notified_success:
            try:
                success_text = success_template.format(
                    report_name=report_name,
                    start_hour=f"{start_hour:02d}:{start_minute:02d}",
                    now=now.strftime("%Y-%m-%d %H:%M"),
                )
            except Exception as e:
                print(f"[notifier] {report_name}: invalid success_message_template ({e}), falling back to default")
                success_text = (
                    f"✅ มีการกรอกข้อมูลแล้ว\n"
                    f"รายงาน: {report_name}\n"
                    f"เวลาที่ตรวจพบ: {now.strftime('%Y-%m-%d %H:%M')}"
                )
            ok, send_err = send_webhook_message(webhook_url, success_text)
            if ok:
                print(f"[notifier] {report_name}: success notification sent")
                tstate["success_notified_cycle"] = cycle_start.isoformat()
            else:
                print(f"[notifier] {report_name}: failed to send success notification: {send_err}")
        return

    # No new record yet today — figure out if it's time to (re)send.
    last_sent_str = tstate.get("last_reminder_at")
    should_send = True
    if last_sent_str:
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            should_send = (now - last_sent) >= timedelta(hours=interval_hours)
        except Exception:
            should_send = True

    if should_send:
        try:
            text = message_template.format(
                report_name=report_name,
                start_hour=f"{start_hour:02d}:{start_minute:02d}",
                now=now.strftime("%Y-%m-%d %H:%M"),
            )
        except Exception as e:
            print(f"[notifier] {report_name}: invalid message_template ({e}), falling back to default")
            text = (
                f"⚠️ แจ้งเตือนการกรอกข้อมูล\n"
                f"รายงาน: {report_name}\n"
                f"ยังไม่มีการกรอกข้อมูลใหม่วันนี้ (เริ่มเช็คตั้งแต่ {start_hour:02d}:{start_minute:02d})\n"
                f"เวลาแจ้งเตือน: {now.strftime('%Y-%m-%d %H:%M')}"
            )
        ok, send_err = send_webhook_message(webhook_url, text)
        if ok:
            print(f"[notifier] {report_name}: reminder sent")
            tstate["last_reminder_at"] = now.isoformat()
            tstate["last_reminder_cycle"] = cycle_start.isoformat()
        else:
            print(f"[notifier] {report_name}: failed to send reminder: {send_err}")


def run_once():
    form_config = load_json(FORM_CONFIG_FILE, {})
    state = load_json(NOTIFY_STATE_FILE, {})

    for table_id, cfg in form_config.items():
        try:
            check_and_notify(table_id, cfg, state)
        except Exception as e:
            print(f"[notifier] error processing {table_id}: {e}")

    save_json(NOTIFY_STATE_FILE, state)


def main():
    print("[notifier] E-Report notification daemon started.")
    print(f"[notifier] Checking every {CHECK_INTERVAL_SECONDS} seconds.")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[notifier] fatal error in run_once: {e}")
        time_mod.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

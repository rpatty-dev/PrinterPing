"""
PrinterPing - Automated Network Printer Supply & Health Monitor
Zero-dependency, pure Python SNMP monitoring engine with automated replenishment alerts.
Supports Email (SMTP), Mobile Push (ntfy), or both.
"""

import os
import sys
import json
import time
import socket
import smtplib
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.toml")
CONFIG_EXAMPLE = os.path.join(BASE_DIR, "config.example.toml")
STATE_FILE = os.path.join(BASE_DIR, "printerping_state.json")
LOG_FILE = os.path.join(BASE_DIR, "printerping.log")

def parse_simple_toml(path):
    """Simple built-in TOML parser for zero external dependencies."""
    config = {}
    current_section = config
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                sec_name = line[1:-1].strip()
                config[sec_name] = {}
                current_section = config[sec_name]
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip().strip('"').strip("'")
                v = v.split("#")[0].strip()
                if v.lower() == "true":
                    val = True
                elif v.lower() == "false":
                    val = False
                elif v.isdigit():
                    val = int(v)
                else:
                    try:
                        val = float(v)
                    except ValueError:
                        val = v.strip('"').strip("'")
                current_section[k] = val
    return config

def load_config():
    target = CONFIG_FILE if os.path.exists(CONFIG_FILE) else CONFIG_EXAMPLE
    if not os.path.exists(target):
        return {
            "printer": {"ip": "192.168.1.200", "name": "Network Printer", "snmp_community": "public", "snmp_timeout": 3.0},
            "thresholds": {"warning_threshold": 15, "critical_threshold": 5, "min_drop_realert": 2, "remind_interval_hours": 24},
            "notifications": {"email_enabled": True, "ntfy_enabled": True},
            "ntfy": {"topic": "ryanmamoupdates132009", "server": "https://ntfy.sh", "priority": "high"},
            "smtp": {"host": "smtp.example.com", "port": 587, "use_tls": True, "username": "", "password": "", "from_email": "alerts@example.com", "to_email": "admin@example.com"},
            "spares_initial": {"Black": False, "Cyan": False, "Magenta": True, "Yellow": False, "Black Drum": False}
        }
    return parse_simple_toml(target)

CONFIG = load_config()
PRINTER_IP = os.environ.get("PRINTERPING_IP", CONFIG.get("printer", {}).get("ip", "192.168.1.200"))
PRINTER_NAME = CONFIG.get("printer", {}).get("name", "Network Printer")
SNMP_COMMUNITY = os.environ.get("PRINTERPING_COMMUNITY", CONFIG.get("printer", {}).get("snmp_community", "public"))
SNMP_TIMEOUT = float(CONFIG.get("printer", {}).get("snmp_timeout", 3.0))

THRESHOLDS = CONFIG.get("thresholds", {})
THRESHOLD_WARNING = int(THRESHOLDS.get("warning_threshold", 15))
THRESHOLD_CRITICAL = int(THRESHOLDS.get("critical_threshold", 5))
MIN_DROP_TO_REALERT = int(THRESHOLDS.get("min_drop_realert", 2))
REMIND_INTERVAL_HOURS = int(THRESHOLDS.get("remind_interval_hours", 24))

DEFAULT_SPARES = CONFIG.get("spares_initial", {
    "Black": False,
    "Cyan": False,
    "Magenta": True,
    "Yellow": False,
    "Black Drum": False
})

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

def encode_oid(oid_str):
    parts = [int(p) for p in oid_str.split(".")]
    res = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        buf = []
        buf.append(p & 0x7F)
        p >>= 7
        while p > 0:
            buf.append(0x80 | (p & 0x7F))
            p >>= 7
        res.extend(reversed(buf))
    return bytes([0x06, len(res)]) + bytes(res)

def make_snmp_getnext(oid_str, community="public", req_id=1):
    encoded_oid = encode_oid(oid_str)
    varbind = bytes([0x30, len(encoded_oid) + 2]) + encoded_oid + bytes([0x05, 0x00])
    varbind_list = bytes([0x30, len(varbind)]) + varbind
    pdu_type = 0xA1
    pdu_body = bytes([0x02, 0x01, req_id % 255, 0x02, 0x01, 0x00, 0x02, 0x01, 0x00]) + varbind_list
    pdu = bytes([pdu_type, len(pdu_body)]) + pdu_body
    comm_bytes = community.encode()
    msg_body = bytes([0x02, 0x01, 0x00, 0x04, len(comm_bytes)]) + comm_bytes + pdu
    return bytes([0x30, len(msg_body)]) + msg_body

def parse_snmp_response(data):
    pdu_idx = data.find(b"\xa2")
    if pdu_idx == -1:
        return None, None
    try:
        p = pdu_idx + 2
        p += 2 + data[p+1]  # req_id
        p += 2 + data[p+1]  # err_stat
        p += 2 + data[p+1]  # err_idx
        p += 2              # VarBindList
        p += 2              # VarBind
        if data[p] != 0x06:
            return None, None
        oid_len = data[p+1]
        oid_bytes = data[p+2:p+2+oid_len]
        parts = [oid_bytes[0] // 40, oid_bytes[0] % 40]
        val = 0
        for b in oid_bytes[1:]:
            val = (val << 7) | (b & 0x7F)
            if not (b & 0x80):
                parts.append(val)
                val = 0
        oid_str = ".".join(str(x) for x in parts)
        val_idx = p + 2 + oid_len
        val_type = data[val_idx]
        val_len = data[val_idx+1]
        val_bytes = data[val_idx+2:val_idx+2+val_len]

        if val_type == 0x02:
            v = int.from_bytes(val_bytes, byteorder="big", signed=True)
        elif val_type == 0x04:
            v = val_bytes.decode("utf-8", errors="ignore")
        else:
            v = val_bytes
        return oid_str, v
    except Exception:
        return None, None

def query_printer_all():
    """Queries standard RFC 3805 / RFC 1514 printer MIBs over pure UDP socket."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(SNMP_TIMEOUT)

    # 1. Supply descriptions
    desc_map = {}
    cur = "1.3.6.1.2.1.43.11.1.1.6"
    for i in range(25):
        try:
            pkt = make_snmp_getnext(cur, SNMP_COMMUNITY, i+1)
            s.sendto(pkt, (PRINTER_IP, 161))
            data, _ = s.recvfrom(4096)
            oid, val = parse_snmp_response(data)
            if not oid or not oid.startswith("1.3.6.1.2.1.43.11.1.1.6."):
                break
            idx = oid.split("1.3.6.1.2.1.43.11.1.1.6.")[1]
            desc_map[idx] = val
            cur = oid
        except Exception:
            break

    # 2. Supply current levels
    level_map = {}
    cur = "1.3.6.1.2.1.43.11.1.1.9"
    for i in range(25):
        try:
            pkt = make_snmp_getnext(cur, SNMP_COMMUNITY, i+50)
            s.sendto(pkt, (PRINTER_IP, 161))
            data, _ = s.recvfrom(4096)
            oid, val = parse_snmp_response(data)
            if not oid or not oid.startswith("1.3.6.1.2.1.43.11.1.1.9."):
                break
            idx = oid.split("1.3.6.1.2.1.43.11.1.1.9.")[1]
            level_map[idx] = val
            cur = oid
        except Exception:
            break

    # 3. Hardware Detected Errors
    detected_errors = []
    try:
        pkt = make_snmp_getnext("1.3.6.1.2.1.25.3.5.1.2", SNMP_COMMUNITY, 100)
        s.sendto(pkt, (PRINTER_IP, 161))
        data, _ = s.recvfrom(4096)
        oid, val = parse_snmp_response(data)
        if oid and oid.startswith("1.3.6.1.2.1.25.3.5.1.2.") and isinstance(val, (bytes, bytearray)):
            if len(val) >= 1:
                b0 = val[0]
                if (b0 & 0x04): detected_errors.append("Paper Jam")
                if (b0 & 0x02): detected_errors.append("Door Open")
                if (b0 & 0x01): detected_errors.append("Offline")
            if len(val) >= 2:
                b1 = val[1]
                if (b1 & 0x80): detected_errors.append("Input Tray Empty")
                if (b1 & 0x40): detected_errors.append("Output Tray Full")
    except Exception:
        pass

    s.close()

    supplies = {}
    for idx, desc in desc_map.items():
        supplies[desc] = level_map.get(idx, None)
    return supplies, detected_errors

def load_state():
    state = {
        "alerts": {},
        "spares": DEFAULT_SPARES.copy(),
        "hardware_errors": [],
        "last_seen_levels": {}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if "alerts" in saved: state["alerts"] = saved["alerts"]
                if "spares" in saved: state["spares"] = saved["spares"]
                if "hardware_errors" in saved: state["hardware_errors"] = saved["hardware_errors"]
                if "last_seen_levels" in saved: state["last_seen_levels"] = saved["last_seen_levels"]
        except Exception as e:
            log(f"Error loading state: {e}")
    return state

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Failed to save state: {e}")

def send_ntfy_alert(title, message, priority="high", tags="printer,warning"):
    """Dispatches instant push notification via ntfy (zero dependencies)."""
    ntfy_cfg = CONFIG.get("ntfy", {})
    topic = os.environ.get("PRINTERPING_NTFY_TOPIC", ntfy_cfg.get("topic", "ryanmamoupdates132009"))
    server = os.environ.get("PRINTERPING_NTFY_SERVER", ntfy_cfg.get("server", "https://ntfy.sh")).rstrip("/")

    if not topic:
        log("ntfy topic unconfigured. Skipping push alert.")
        return False

    url = f"{server}/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags
    }

    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log(f"Push notification sent successfully via ntfy ({topic}).")
                return True
            else:
                log(f"ntfy server returned HTTP {resp.status}")
                return False
    except Exception as e:
        log(f"ntfy notification failed: {e}")
        return False

def send_email_alert(subject, text_body, html_body):
    smtp_cfg = CONFIG.get("smtp", {})
    host = smtp_cfg.get("host", "smtp.example.com")
    port = int(smtp_cfg.get("port", 587))
    user = os.environ.get("PRINTERPING_SMTP_USER", smtp_cfg.get("username", ""))
    pwd = os.environ.get("PRINTERPING_SMTP_PASS", smtp_cfg.get("password", ""))
    sender = smtp_cfg.get("from_email", "PrinterPing Alerts <alerts@example.com>")
    recipient = smtp_cfg.get("to_email", "admin@example.com")

    if not user or not pwd or pwd == "YOUR_SMTP_PASSWORD":
        log("SMTP credentials unconfigured. Skipping email dispatch.")
        return False

    msg = MIMEMultipart("alternative")
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        server = smtplib.SMTP(host, port, timeout=20)
        if smtp_cfg.get("use_tls", True):
            server.starttls()
        server.login(user, pwd)
        server.sendmail(user, [recipient], msg.as_string())
        server.quit()
        log(f"Alert email sent to {recipient} via {host}.")
        return True
    except Exception as e:
        log(f"SMTP send failed: {e}")
        return False

def dispatch_notification(title, message, html_body=None, priority="high", tags="printer,warning"):
    """
    Unified router dispatching to Email, ntfy Phone Push, or Both
    based on config [notifications] email_enabled / ntfy_enabled.
    """
    notif_cfg = CONFIG.get("notifications", {})
    email_enabled = notif_cfg.get("email_enabled", True)
    ntfy_enabled = notif_cfg.get("ntfy_enabled", True)

    results = []

    if ntfy_enabled:
        ntfy_ok = send_ntfy_alert(title, message, priority=priority, tags=tags)
        results.append(ntfy_ok)
    else:
        log("ntfy push alerts disabled in configuration.")

    if email_enabled:
        html = html_body if html_body else f"<h3>{title}</h3><pre>{message}</pre>"
        email_ok = send_email_alert(title, message, html)
        results.append(email_ok)
    else:
        log("Email alerts disabled in configuration.")

    return any(results) if results else False

def check_auto_replacement(current_levels, state):
    last_seen = state.get("last_seen_levels", {})
    spares = state.get("spares", {})

    for item, current_val in current_levels.items():
        if current_val is None or not isinstance(current_val, int):
            continue
        prev_val = last_seen.get(item)
        if prev_val is not None and isinstance(prev_val, int):
            if prev_val <= 15 and current_val >= 85:
                had_spare = spares.get(item, False)
                log(f"Auto-replacement detected: {item} rose from {prev_val}% to {current_val}%.")
                if had_spare:
                    spares[item] = False
                    log(f"Office spare for {item} marked consumed. Dispatching reorder notice.")
                    send_spare_consumed_alert(item, current_val)
                if item in state.get("alerts", {}):
                    del state["alerts"][item]

        last_seen[item] = current_val

    state["last_seen_levels"] = last_seen
    state["spares"] = spares

def send_spare_consumed_alert(item_name, new_level):
    title = f"Spare {item_name} Installed - Reorder Backup"
    text_body = f"Spare {item_name} cartridge was installed into {PRINTER_NAME} (level: {new_level}%).\nOffice storeroom is now out of backup spares. Reorder now!"
    html_body = f"""
    <!DOCTYPE html><html><body style="font-family: sans-serif; padding: 20px; background: #f8fafc;">
    <div style="max-width: 500px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
        <h3 style="color: #2563eb; margin-top: 0;">Spare Installed &bull; Reorder Backup</h3>
        <p>The office spare for <b>{item_name}</b> was installed in <b>{PRINTER_NAME}</b> ({PRINTER_IP}).</p>
        <p>Current level: <b>{new_level}%</b>.</p>
        <p style="color: #dc2626; font-weight: bold;">Office storeroom has 0 remaining backup spares. Please order a new backup cartridge.</p>
    </div></body></html>
    """
    dispatch_notification(title, text_body, html_body, priority="default", tags="package,printer,white_check_mark")

def check_hardware_health(printer_reachable, detected_errors, state):
    prev_errors = set(state.get("hardware_errors", []))
    current_errors = set(detected_errors)
    if not printer_reachable:
        current_errors.add("Printer Offline / Network Unreachable")

    new_errors = current_errors - prev_errors
    resolved_errors = prev_errors - current_errors

    if new_errors:
        log(f"Hardware error occurred: {', '.join(new_errors)}")
        title = f"Printer Alert: {', '.join(new_errors)}"
        text = f"{PRINTER_NAME} ({PRINTER_IP}) requires attention:\n - " + "\n - ".join(new_errors)
        html = f"<h3>Printer Hardware Alert</h3><p>Device: {PRINTER_NAME} ({PRINTER_IP})</p><p>Issues: <b>{', '.join(new_errors)}</b></p>"
        dispatch_notification(title, text, html, priority="urgent", tags="warning,printer,rotating_light")

    if resolved_errors:
        log(f"Hardware error resolved: {', '.join(resolved_errors)}")
        title = f"Printer Restored: {', '.join(resolved_errors)} Cleared"
        text = f"Issues resolved on {PRINTER_NAME} ({PRINTER_IP}):\n - " + "\n - ".join(resolved_errors)
        html = f"<h3>Printer Alert Cleared</h3><p>Device: {PRINTER_NAME} ({PRINTER_IP})</p><p>Resolved: <b>{', '.join(resolved_errors)}</b></p>"
        dispatch_notification(title, text, html, priority="default", tags="white_check_mark,printer")

    state["hardware_errors"] = list(current_errors)

def should_alert_for_item(item_name, level, state, is_waste=False):
    spares = state.get("spares", {})
    has_spare = spares.get(item_name, False)
    threshold = THRESHOLD_CRITICAL if has_spare else THRESHOLD_WARNING

    if is_waste:
        if level is not None and level != -3 and level != 100:
            return True, "Waste Toner Box is full or near capacity."
        return False, None

    if level is None or level > threshold:
        return False, None

    now = time.time()
    item_state = state.get("alerts", {}).get(item_name)

    if not item_state:
        reason = f"Critical low ({level}% <= {THRESHOLD_CRITICAL}% - replace with spare)" if has_spare else f"Low supply ({level}% <= {THRESHOLD_WARNING}% - reorder)"
        return True, reason

    last_alert_time = item_state.get("timestamp", 0)
    last_alert_level = item_state.get("level", 100)

    if level <= THRESHOLD_CRITICAL and last_alert_level > THRESHOLD_CRITICAL:
        return True, f"Reached critical cutoff level ({level}%)"

    if (last_alert_level - level) >= MIN_DROP_TO_REALERT:
        return True, f"Supply decreased from {last_alert_level}% to {level}%"

    if (now - last_alert_time) / 3600.0 >= REMIND_INTERVAL_HOURS:
        return True, f"Daily reminder: {item_name} is at {level}%"

    return False, None

def run_monitor():
    log(f"Starting PrinterPing scan for {PRINTER_IP}...")
    state = load_state()

    supplies, detected_errors = query_printer_all()
    printer_reachable = bool(supplies)

    check_hardware_health(printer_reachable, detected_errors, state)

    if not printer_reachable:
        log("ERROR: Printer unreachable via SNMP.")
        save_state(state)
        return

    full_levels = {}
    for color in ["Black", "Cyan", "Magenta", "Yellow"]:
        for key, val in supplies.items():
            if color.lower() in key.lower() and "toner" in key.lower():
                full_levels[color] = val
                break

    drum_level = None
    for key, val in supplies.items():
        if "drum" in key.lower() or "imaging" in key.lower():
            drum_level = val
            break

    waste_level = None
    for key, val in supplies.items():
        if "waste" in key.lower():
            waste_level = val
            break

    log(f"Supplies detected: Toners={full_levels}, Drum={drum_level}%, WasteRaw={waste_level}")

    check_dict = {**full_levels}
    if drum_level is not None:
        check_dict["Black Drum"] = drum_level
    check_auto_replacement(check_dict, state)

    spares = state.get("spares", DEFAULT_SPARES)
    triggered = []

    for c, lvl in full_levels.items():
        alert, reason = should_alert_for_item(c, lvl, state)
        if alert:
            log(f"Alert triggered for {c}: {reason}")
            triggered.append(c)

    if drum_level is not None:
        alert, reason = should_alert_for_item("Black Drum", drum_level, state)
        if alert:
            log(f"Alert triggered for Drum: {reason}")
            triggered.append("Black Drum")

    alert_w, reason_w = should_alert_for_item("Waste Box", waste_level, state, is_waste=True)
    if alert_w:
        log(f"Alert triggered for Waste Box: {reason_w}")
        triggered.append("Waste Box")

    if triggered:
        log(f"Dispatching notification for: {', '.join(triggered)}")
        title = f"PrinterPing: {', '.join(triggered)} Alert"
        text_lines = [f"{PRINTER_NAME} ({PRINTER_IP}) supply alert:"]
        for item in triggered:
            lvl = full_levels.get(item, drum_level if item == "Black Drum" else "N/A")
            sp = " [SPARE IN STOCK - REPLACE]" if spares.get(item, False) else " [ORDER NEEDED]"
            text_lines.append(f" - {item}: {lvl}%{sp}")
        text_body = "\n".join(text_lines)
        html_body = f"<h3>PrinterPing Supply Alert</h3><p>Device: {PRINTER_NAME} ({PRINTER_IP})</p><pre>{text_body}</pre>"
        
        if dispatch_notification(title, text_body, html_body, priority="high", tags="printer,warning"):
            now = time.time()
            if "alerts" not in state: state["alerts"] = {}
            for item in triggered:
                state["alerts"][item] = {
                    "level": full_levels.get(item, drum_level),
                    "timestamp": now,
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
    else:
        log("All supplies within nominal thresholds.")

    save_state(state)

if __name__ == "__main__":
    run_monitor()

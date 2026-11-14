import ipaddress
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
from flask import Flask, Response, abort, jsonify, render_template, request

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mqtt-dashboard")

# --------------------------------------------------------------------------
# Configuration (all from environment variables)
# --------------------------------------------------------------------------

MQTT_HOST = os.environ.get("MQTT_HOST", "192.168.1.160")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASSWORD") or None
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "weather")
MQTT_QOS = int(os.environ.get("MQTT_QOS", "0"))
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "mqtt-dashboard")
MQTT_TLS = os.environ.get("MQTT_TLS", "false").lower() in ("1", "true", "yes")
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))

# JSON field names inside the payload published on MQTT_TOPIC,
# e.g. {"temp": 16.21, "hum": 40.75}
TEMP_FIELD = os.environ.get("MQTT_TEMP_FIELD", "temp")
HUMIDITY_FIELD = os.environ.get("MQTT_HUMIDITY_FIELD", "hum")

# Temperature thresholds (Celsius) used to color the reading on the page.
TEMP_COLD_BELOW = float(os.environ.get("TEMP_COLD_BELOW", "7"))
TEMP_HOT_ABOVE = float(os.environ.get("TEMP_HOT_ABOVE", "27"))

HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
HISTORY_HOURS = float(os.environ.get("HISTORY_HOURS", "12"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))

DATA_DIR = os.environ.get("DATA_DIR", "data")
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(DATA_DIR, "history.xml"))

TRUST_PROXY = os.environ.get("TRUST_PROXY", "false").lower() in ("1", "true", "yes")

DEFAULT_ALLOWED_NETWORKS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
]

_raw_networks = os.environ.get("ALLOWED_NETWORKS")
if _raw_networks:
    ALLOWED_NETWORKS = [n.strip() for n in _raw_networks.split(",") if n.strip()]
else:
    ALLOWED_NETWORKS = DEFAULT_ALLOWED_NETWORKS

ALLOWED_NETWORK_OBJS = [ipaddress.ip_network(n, strict=False) for n in ALLOWED_NETWORKS]

# --------------------------------------------------------------------------
# In-memory data store
# --------------------------------------------------------------------------

_lock = threading.Lock()
_latest = None  # {"temp": float, "hum": int, "timestamp": iso str, "raw": str}
_history = deque()  # deque of {"timestamp": iso str, "temp": float, "hum": int}
_status = {
    "connected": False,
    "last_connect_attempt": None,
    "last_error": None,
    "last_message_time": None,
}


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _parse_iso(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# XML persistence for history (timestamp + rounded temp/hum, last 12h)
# --------------------------------------------------------------------------


def _prune_history_locked():
    cutoff = _now() - timedelta(hours=HISTORY_HOURS)
    while _history:
        ts = _parse_iso(_history[0]["timestamp"])
        if ts is None or ts < cutoff:
            _history.popleft()
        else:
            break


def _load_history():
    if not os.path.exists(DATA_FILE):
        return
    try:
        tree = ET.parse(DATA_FILE)
        root = tree.getroot()
        loaded = deque()
        for reading in root.findall("reading"):
            ts = reading.get("timestamp")
            temp = reading.get("temp")
            hum = reading.get("hum")
            if ts is None or temp is None or hum is None:
                continue
            loaded.append({"timestamp": ts, "temp": float(temp), "hum": int(float(hum))})
        with _lock:
            _history.clear()
            _history.extend(loaded)
            _prune_history_locked()
        log.info("Loaded %d history entries from %s", len(loaded), DATA_FILE)
    except (ET.ParseError, OSError, ValueError) as exc:
        log.error("Failed to load history file %s: %s", DATA_FILE, exc)


def _save_history_locked():
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    root = ET.Element("history")
    for entry in _history:
        ET.SubElement(
            root,
            "reading",
            {
                "timestamp": entry["timestamp"],
                "temp": f"{entry['temp']:.1f}",
                "hum": str(entry["hum"]),
            },
        )
    tree = ET.ElementTree(root)
    tmp_path = DATA_FILE + ".tmp"
    tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
    os.replace(tmp_path, DATA_FILE)


def _store_reading(raw_payload):
    try:
        value = json.loads(raw_payload)
    except (ValueError, TypeError) as exc:
        log.warning("Ignoring non-JSON payload on %s: %s (%s)", MQTT_TOPIC, raw_payload, exc)
        return

    if not isinstance(value, dict) or TEMP_FIELD not in value or HUMIDITY_FIELD not in value:
        log.warning(
            "Ignoring payload missing '%s'/'%s' fields on %s: %s",
            TEMP_FIELD, HUMIDITY_FIELD, MQTT_TOPIC, raw_payload,
        )
        return

    try:
        temp = round(float(value[TEMP_FIELD]), 1)
        hum = round(float(value[HUMIDITY_FIELD]))
    except (TypeError, ValueError) as exc:
        log.warning("Ignoring payload with non-numeric values on %s: %s (%s)", MQTT_TOPIC, raw_payload, exc)
        return

    ts = _now_iso()
    entry = {"timestamp": ts, "temp": temp, "hum": hum}

    with _lock:
        _latest_entry = {"temp": temp, "hum": hum, "timestamp": ts, "raw": raw_payload}
        globals()["_latest"] = _latest_entry
        _status["last_message_time"] = ts
        _history.append(entry)
        _prune_history_locked()
        try:
            _save_history_locked()
        except OSError as exc:
            log.error("Failed to persist history to %s: %s", DATA_FILE, exc)


def snapshot():
    with _lock:
        return {
            "status": dict(_status),
            "latest": dict(_latest) if _latest else None,
            "history": list(_history),
            "config": {
                "temp_cold_below": TEMP_COLD_BELOW,
                "temp_hot_above": TEMP_HOT_ABOVE,
                "refresh_seconds": REFRESH_SECONDS,
                "history_hours": HISTORY_HOURS,
            },
        }


def service_snapshot():
    with _lock:
        return {
            "status": dict(_status),
            "latest": dict(_latest) if _latest else None,
            "history_count": len(_history),
            "oldest_history_timestamp": _history[0]["timestamp"] if _history else None,
            "broker": {
                "host": MQTT_HOST,
                "port": MQTT_PORT,
                "tls": MQTT_TLS,
                "topic": MQTT_TOPIC,
                "client_id": MQTT_CLIENT_ID,
            },
            "fields": {"temp": TEMP_FIELD, "hum": HUMIDITY_FIELD},
            "data_file": os.path.abspath(DATA_FILE),
            "history_hours": HISTORY_HOURS,
        }


# --------------------------------------------------------------------------
# MQTT client
# --------------------------------------------------------------------------


def on_connect(client, userdata, flags, rc):
    with _lock:
        _status["connected"] = rc == 0
        _status["last_error"] = None if rc == 0 else f"connect rc={rc}"

    if rc == 0:
        log.info("Connected to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
        client.subscribe(MQTT_TOPIC, qos=MQTT_QOS)
        log.info("Subscribed to topic: %s", MQTT_TOPIC)
    else:
        log.error("Failed to connect to MQTT broker, rc=%s", rc)


def on_disconnect(client, userdata, rc):
    with _lock:
        _status["connected"] = False
    log.warning("Disconnected from MQTT broker, rc=%s", rc)


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8", errors="replace")
    except Exception:
        payload = repr(msg.payload)
    log.debug("Message on %s: %s", msg.topic, payload)
    _store_reading(payload)


def mqtt_worker():
    client = mqtt.Client(client_id=MQTT_CLIENT_ID, clean_session=True)

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)

    if MQTT_TLS:
        client.tls_set()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    while True:
        with _lock:
            _status["last_connect_attempt"] = _now_iso()
        try:
            log.info("Connecting to MQTT broker %s:%s ...", MQTT_HOST, MQTT_PORT)
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _status["connected"] = False
                _status["last_error"] = str(exc)
            log.error("MQTT connection error: %s", exc)
        log.info("Reconnecting to MQTT broker in 5 seconds...")
        time.sleep(5)


# --------------------------------------------------------------------------
# Flask app / local-network-only access control
# --------------------------------------------------------------------------

app = Flask(__name__)


def _client_ip():
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr


def _is_allowed(ip_str):
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOWED_NETWORK_OBJS)


@app.before_request
def restrict_to_local_network():
    if request.path == "/healthz":
        return None  # health checks run from inside the host/docker network
    client_ip = _client_ip()
    if not _is_allowed(client_ip):
        log.warning("Blocked request from disallowed IP: %s", client_ip)
        abort(403, description="Access restricted to the local network.")


@app.route("/")
def index():
    return render_template(
        "index.html",
        refresh_seconds=REFRESH_SECONDS,
        temp_cold_below=TEMP_COLD_BELOW,
        temp_hot_above=TEMP_HOT_ABOVE,
        history_hours=HISTORY_HOURS,
    )


@app.route("/service")
def service():
    return render_template("service.html", refresh_seconds=REFRESH_SECONDS)


@app.route("/api/data")
def api_data():
    return jsonify(snapshot())


@app.route("/api/service")
def api_service():
    return jsonify(service_snapshot())


@app.route("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")


_load_history()

if __name__ == "__main__":
    thread = threading.Thread(target=mqtt_worker, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=HTTP_PORT)
else:
    # When served via gunicorn, start the MQTT background thread on import.
    _thread = threading.Thread(target=mqtt_worker, daemon=True)
    _thread.start()

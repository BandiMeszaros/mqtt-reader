import ipaddress
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

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
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "#")
MQTT_QOS = int(os.environ.get("MQTT_QOS", "0"))
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "mqtt-dashboard")
MQTT_TLS = os.environ.get("MQTT_TLS", "false").lower() in ("1", "true", "yes")
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))

HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "50"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3"))

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

TOPICS = [t.strip() for t in MQTT_TOPIC.split(",") if t.strip()]

# --------------------------------------------------------------------------
# In-memory data store
# --------------------------------------------------------------------------

_lock = threading.Lock()
_latest = {}  # topic -> {"value": ..., "raw": str, "timestamp": iso str}
_history = {}  # topic -> deque of the above dicts
_status = {"connected": False, "last_connect_attempt": None, "last_error": None}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _store_message(topic, raw_payload):
    try:
        value = json.loads(raw_payload)
    except (ValueError, TypeError):
        value = raw_payload

    entry = {"value": value, "raw": raw_payload, "timestamp": _now_iso()}

    with _lock:
        _latest[topic] = entry
        if topic not in _history:
            _history[topic] = deque(maxlen=HISTORY_SIZE)
        _history[topic].append(entry)


def snapshot():
    with _lock:
        return {
            "status": dict(_status),
            "topics": {
                topic: {
                    "latest": _latest.get(topic),
                    "history": list(_history.get(topic, [])),
                }
                for topic in _latest.keys()
            },
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
        for topic in TOPICS:
            client.subscribe(topic, qos=MQTT_QOS)
            log.info("Subscribed to topic: %s", topic)
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
    _store_message(msg.topic, payload)


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
        topics=TOPICS,
        refresh_seconds=REFRESH_SECONDS,
    )


@app.route("/api/data")
def api_data():
    return jsonify(snapshot())


@app.route("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    thread = threading.Thread(target=mqtt_worker, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=HTTP_PORT)
else:
    # When served via gunicorn, start the MQTT background thread on import.
    _thread = threading.Thread(target=mqtt_worker, daemon=True)
    _thread.start()

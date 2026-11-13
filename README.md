# MQTT Dashboard

A tiny dockerized app that:

- Connects to an MQTT broker using credentials/host from environment variables
- Subscribes to one or more topics (wildcards `+`/`#` supported)
- Keeps the latest value (and a short history) per topic in memory
- Serves the data as JSON (`/api/data`) and as a simple auto-refreshing HTML page (`/`)
- Restricts access to private/local-network IP addresses only

## Files

- `main.py` – Flask app + MQTT client (background thread)
- `templates/index.html` – the dashboard page
- `requirements.txt`, `Dockerfile`, `docker-compose.yml`
- `.env.example` – copy to `.env` and fill in your values

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `192.168.1.160` | Broker hostname/IP (the mosquitto broker from `devops/homelab/mqtt.yaml`) |
| `MQTT_PORT` | `1883` | Broker port (use `8883` + `MQTT_TLS=true` for TLS) |
| `MQTT_USER` | – | MQTT username (matches `MQTT_USER` in `mqtt.yaml`) |
| `MQTT_PASSWORD` | – | MQTT password (matches `MQTT_PASSWORD` in `mqtt.yaml`) |
| `MQTT_TOPIC` | `#` | Topic, or comma-separated list of topics |
| `MQTT_QOS` | `0` | QoS level for subscriptions |
| `MQTT_TLS` | `false` | Enable TLS |
| `HTTP_PORT` | `8080` | Port the web UI listens on inside the container |
| `HOST_PORT` | `8080` | Port published on the host (change if `8080` is already taken on the Coolify server) |
| `HISTORY_SIZE` | `50` | Messages kept per topic |
| `REFRESH_SECONDS` | `3` | Page auto-refresh interval |
| `ALLOWED_NETWORKS` | RFC1918 + loopback + link-local | Comma-separated CIDRs allowed to load the page |
| `TRUST_PROXY` | `false` | Set `true` only if a *trusted* reverse proxy in front of this app sets `X-Forwarded-For` |

## Running locally

```bash
cp .env.example .env
# edit .env with your broker details
docker compose up --build
```

Visit `http://<server-ip>:8080/`.

## Keeping it local-network only on Coolify

This app enforces an IP allowlist itself (default: private ranges only,
`403 Forbidden` for everything else), so even if it's reachable it will
reject public traffic. On top of that, on Coolify:

1. **Don't** attach a public domain/FQDN to this service in Coolify's
   "Domains" settings — that's what makes Traefik expose it to the internet.
2. Leave the service reachable only via the server's internal/LAN IP and the
   mapped port (the `ports: ["8080:8080"]` line in `docker-compose.yml`), or
   switch to `expose` only and reach it through a VPN/Tailscale/same-LAN
   connection to the host.
3. If your Coolify host is reachable from the internet and you want extra
   safety, also block the port at the host firewall for anything outside
   your LAN/VPN CIDR.
4. If you do put a reverse proxy you trust in front of it, set
   `TRUST_PROXY=true` so the allowlist checks the real client IP from
   `X-Forwarded-For` instead of the proxy's IP.

## Endpoints

- `GET /` – HTML dashboard
- `GET /api/data` – JSON snapshot of all topics (latest value + history + connection status)
- `GET /healthz` – plain-text health check (not IP-restricted, used for container healthchecks)
# mqtt-reader

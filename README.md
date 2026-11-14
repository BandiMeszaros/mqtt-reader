# Weather Dashboard

A tiny dockerized app that:

- Connects to an MQTT broker using credentials/host from environment variables
- Subscribes to a single topic publishing JSON like `{"temp": 16.21, "hum": 40.75}`
- Keeps the last 12 hours of readings (rounded: temp to 1 decimal, humidity to a whole number)
  in memory and persists them to a simple XML file (no database)
- Serves a minimal, auto-refreshing dashboard (`/`) with the current temperature/humidity
  and graphs of the last 12 hours, plus a `/service` page with connection diagnostics
- Restricts access to private/local-network IP addresses only

## Files

- `main.py` – Flask app + MQTT client (background thread) + XML history persistence
- `templates/index.html` – the dashboard page (current values + graphs)
- `templates/service.html` – connection/service diagnostics page
- `requirements.txt`, `Dockerfile`, `docker-compose.yml`
- `.env.example` – copy to `.env` and fill in your values

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `192.168.1.160` | Broker hostname/IP |
| `MQTT_PORT` | `1883` | Broker port (use `8883` + `MQTT_TLS=true` for TLS) |
| `MQTT_USER` | – | MQTT username |
| `MQTT_PASSWORD` | – | MQTT password |
| `MQTT_TOPIC` | `weather` | Topic publishing JSON with both readings |
| `MQTT_QOS` | `0` | QoS level for the subscription |
| `MQTT_TLS` | `false` | Enable TLS |
| `MQTT_TEMP_FIELD` | `temp` | JSON key for the temperature value |
| `MQTT_HUMIDITY_FIELD` | `hum` | JSON key for the humidity value |
| `TEMP_COLD_BELOW` | `7` | Below this (°C) the temperature is shown in blue |
| `TEMP_HOT_ABOVE` | `27` | Above this (°C) the temperature is shown in red (green in between) |
| `HTTP_PORT` | `8080` | Port the web UI listens on inside the container |
| `HOST_PORT` | `8080` | Port published on the host |
| `HISTORY_HOURS` | `12` | Hours of history kept/graphed |
| `REFRESH_SECONDS` | `300` | Page auto-refresh interval (also used on the service page) |
| `DATA_DIR` | `data` | Directory for the `history.xml` persistence file |
| `DATA_FILE` | `<DATA_DIR>/history.xml` | Full path override for the history file |
| `ALLOWED_NETWORKS` | RFC1918 + loopback + link-local | Comma-separated CIDRs allowed to load the page |
| `TRUST_PROXY` | `false` | Set `true` only if a *trusted* reverse proxy in front of this app sets `X-Forwarded-For` |

## Running locally

```bash
cp .env.example .env
# edit .env with your broker details
docker compose up --build
```

Visit `http://<server-ip>:8080/`.

The `docker-compose.yml` mounts a named volume at `/app/data` so the history
XML file survives container restarts/redeploys.

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

- `GET /` – dashboard: current temperature/humidity + 12h graphs
- `GET /service` – connection status, last message time, last raw payload, etc.
- `GET /api/data` – JSON: latest reading, history, status, thresholds
- `GET /api/service` – JSON: broker/connection diagnostics
- `GET /healthz` – plain-text health check (not IP-restricted, used for container healthchecks)

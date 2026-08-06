# docker-health-watch

A tiny standalone monitor for Docker container health. It polls the local
Docker engine, watches every container that declares a HEALTHCHECK, and POSTs
a JSON alert the moment any container flips to (or recovers from) unhealthy.

No Docker SDK, no compose stack, no Prometheus. Just one Python script that
uses the standard library and talks to `/var/run/docker.sock` directly.

## Why

Docker restarts containers when they exit, and `restart: always` keeps them
alive, but a process can hang in place while its HEALTHCHECK fails. Most
homelab setups would like to know immediately rather than discover it on
Tuesday morning. This script gives you that single missing signal without
standing up a full metrics pipeline.

## Requirements

- Python 3.8 or newer (only the standard library is used)
- Read access to the Docker socket (`/var/run/docker.sock`)
- Containers that define a HEALTHCHECK in their image or compose file

## Quick start

Run it directly on a host with Docker:

```bash
python3 docker_health_watch.py \
    --webhook https://hooks.example.com/health \
    --interval 30
```

That prints status lines to stdout and sends one JSON POST per state change.
Anything that POSTs JSON works: a home-rolled endpoint, ntfy.sh,
Healthchecks.io, a small relay, or your own chat relay.

## Options

All flags also read from environment variables, which keeps containers tidy.

| Flag        | Env var        | Default                | Purpose                                |
|-------------|----------------|------------------------|----------------------------------------|
| --webhook   | DWH_WEBHOOK    | (none)                 | URL to POST JSON alerts to             |
| --interval  | DWH_INTERVAL   | 15                     | Seconds between polls                  |
| --socket    | DWH_SOCKET     | /var/run/docker.sock   | Path to the Docker socket              |
| --hostname  | DWH_HOSTNAME   | system hostname        | Pretty label in alerts                 |
| --timeout   | DWH_TIMEOUT    | 10                     | Per-request socket timeout, seconds    |
| --once      |                | (off)                  | Poll a single time then exit           |

## Alert payload

Every alert is a small JSON document, easy to read in any receiver:

```json
{
  "source": "docker-health-watch",
  "host": "lab-1",
  "timestamp": "2026-08-06T02:14:03+00:00",
  "container": {
    "id": "9f3c...e1",
    "name": "nextcloud",
    "image": "nextcloud:29",
    "state": "unhealthy"
  },
  "message": "Container nextcloud is now unhealthy"
}
```

State is `unhealthy` when something broke and `healthy` when it recovered,
so you can subscribe to both directions or just the bad news.

## Run once (cron mode)

For hosts where you would rather have a cron job than a long-running process:

```bash
*/2 * * * * /usr/bin/python3 /opt/docker-health-watch/docker_health_watch.py --once --webhook https://hooks.example.com/health
```

State is kept in memory and resets each run, so each `--once` invocation will
report any container that is unhealthy right now rather than only fresh
transitions. That is intentional for a pull-style monitor.

## Run as a Docker container

Mount the socket in read-only and pass the webhook in:

```bash
docker run -d --name docker-health-watch \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -e DWH_WEBHOOK=https://hooks.example.com/health \
  -e DWH_INTERVAL=30 \
  python:3.12-slim \
  python3 /app/docker_health_watch.py
```

For a self-contained image, copy the script in a tiny Dockerfile:

```dockerfile
FROM python:3.12-slim
COPY docker_health_watch.py /app/docker_health_watch.py
ENTRYPOINT ["python3", "/app/docker_health_watch.py"]
```

## Adding a HEALTHCHECK to your containers

This monitor only sees containers that already declare a health check. In a
compose file it looks like this:

```yaml
services:
  app:
    image: my-app:latest
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

Once Docker itself reports the status, this script will react to it.

## Exit codes

- 0: ran once successfully in `--once` mode
- 1: fatal startup error (socket not found, bad arguments)
- 2: at least one webhook delivery failed during a poll (the loop continues)

## Limitations

- Watches only containers on the local Docker engine it can read via socket.
- Health status comes from Docker's own HEALTHCHECK, so a container without
  one will never generate an alert here.
- Webhook delivery is best-effort. A flaky endpoint will log a warning and
  keep polling; nothing is queued for retry.

## License

MIT. See [LICENSE](LICENSE).

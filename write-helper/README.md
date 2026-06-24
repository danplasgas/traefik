# Traefik Admin Write Helper

Local-only helper for controlled edits to the `sunny` Traefik route fragments.

It is deliberately narrow. It manages allowlisted route groups, files, service
status/actions, and service logs. It is not a general shell or arbitrary TOML
editor.

## Run

```sh
python3 write-helper/traefik_admin_helper.py
```

The helper refuses to bind anywhere except `127.0.0.1`.

On the headless `sunny` host, the helper is reached by the admin UI through
Traefik at `http://admin.sunny/api/`. Keep that route restricted to
local/Tailscale or admin-auth contexts.

## Endpoints

- `GET /health`
- `GET /config`
- `GET /routes`
- `POST /routes/toggle`
- `GET /files`
- `GET /files/{id}`
- `POST /files/{id}/apply`
- `GET /system/services`
- `GET /system/logs?unit=<unit>`
- `POST /system/services/action`
- `POST /config/propose`
- `POST /config/validate`
- `POST /config/apply`

Write endpoints require `{"confirmed": true}` where they mutate files or routes.

## Route layout

The live static config is `/home/dan/work/traefik/conf/traefik.toml`.

Enabled route groups live in:

```text
/home/dan/work/traefik/conf/routes/*.toml
```

Disabled route groups live in:

```text
/home/dan/work/traefik/conf/routes.disabled/*.toml
```

Protected route groups include `shared`, `traefik`, and `traefik-admin`.

## Apply safety

The apply flow:

1. Acquires a file lock.
2. Builds the controlled middleware-chain candidate.
3. Parses TOML and runs an isolated Traefik startup validation.
4. Creates a timestamped backup of the live config.
5. Writes the candidate to a temporary file and atomically replaces the live config.
6. Checks the live Traefik API.
7. Rolls back from backup if the live check fails.
8. Appends apply metadata to `/home/dan/work/traefik/logs/admin-applies.jsonl`.

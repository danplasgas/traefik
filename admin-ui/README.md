# Traefik Admin UI

Standalone local operator UI for the `sunny` Traefik setup.

This is intentionally not part of Traefik's bundled `webui/`. It reads Traefik's
existing dashboard API and talks to the local write helper for controlled config
changes.

## Features

- Route groups displayed by host, status, and upstream app.
- Protected core/admin route groups are visible but cannot be disabled.
- Non-protected route groups can be enabled or disabled by moving their fragment
  between `conf/routes/` and `conf/routes.disabled/`.
- Allowlisted system services show status, recent logs, and permitted actions.
- Allowlisted files can be viewed; safe route fragments can be edited with
  validation and rollback.

## Local development

Serve this directory with any static file server:

```sh
python3 admin-ui/serve.py
```

For local development, `serve.py` proxies `/traefik-api/*` to the live Traefik
route using `Host: traefik.sunny`, and proxies `/api/*` to the helper.

The UI expects the helper at `/api` by default. Override it by setting
`window.TRAEFIK_ADMIN_HELPER_BASE` before loading `app.js`.

## Traefik routing

The intended live route is `http://admin.sunny/`, served separately from the
built-in Traefik dashboard. Helper endpoints are routed at
`http://admin.sunny/api/` for local/Tailscale/admin-auth use only; do not expose
them on a public router.

Example dynamic config shape:

```toml
[http.services.traefik-admin-ui.loadBalancer]
  [[http.services.traefik-admin-ui.loadBalancer.servers]]
    url = "http://127.0.0.1:8091"

[http.services.traefik-admin-helper.loadBalancer]
  [[http.services.traefik-admin-helper.loadBalancer.servers]]
    url = "http://127.0.0.1:8092"

[http.middlewares.admin-api-strip.stripPrefix]
  prefixes = ["/api"]

[http.routers.admin]
  rule = "Host(`admin.sunny`) && PathPrefix(`/`)"
  entryPoints = ["web"]
  service = "traefik-admin-ui"
  middlewares = ["traefik-auth", "error-pages"]

[http.routers.admin-api]
  rule = "Host(`admin.sunny`) && PathPrefix(`/api/`)"
  entryPoints = ["web"]
  service = "traefik-admin-helper"
  middlewares = ["traefik-auth", "admin-api-strip"]
```

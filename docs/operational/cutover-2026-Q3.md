# Traefik Runtime Cutover Runbook — 2026 Q3

**Phase:** P-Comp-13 (tmweb completion programme)
**Status:** Ready to execute. Service restart: user-only.
**Date written:** 2026-06-29

## What this does

Moves Traefik's runtime directories (conf, logs, run) from the old path
`/home/dan/work/traefik/` to the new canonical path `/srv/tmweb/traefik/`.

Source code and Python scripts remain at `/home/dan/work/tmweb/infra/traefik/`.
Only the runtime state (configuration, logs, socket files) moves.

## Preconditions

- [ ] ADR-006 (secrets policy): `/etc/tmweb/traefik.env` exists if Traefik needs secrets
- [ ] ADR-007 (runtime baseline): `/health` or `/ping` endpoint confirmed working
- [ ] ADR-009 (deployment pipeline): `foreman deploy` available or manual rsync fallback ready
- [ ] `systemctl status traefik-admin-helper traefik-admin-ui` — both green
- [ ] Backup: `cp -a /home/dan/work/traefik/ /home/dan/work/traefik.backup-$(date +%Y%m%d)/`

## Step 1 — Create runtime directory tree

```bash
sudo mkdir -p /srv/tmweb/traefik/{conf,logs,run}
sudo chown -R dan:dan /srv/tmweb/traefik/
```

## Step 2 — Copy configuration

```bash
rsync -av /home/dan/work/traefik/conf/ /srv/tmweb/traefik/conf/
```

Verify: `diff -r /home/dan/work/traefik/conf/ /srv/tmweb/traefik/conf/` — expect no output.

## Step 3 — Copy logs and run dirs (optional, for continuity)

```bash
cp -a /home/dan/work/traefik/logs/. /srv/tmweb/traefik/logs/ 2>/dev/null || true
cp -a /home/dan/work/traefik/run/. /srv/tmweb/traefik/run/ 2>/dev/null || true
```

## Step 4 — Install updated unit files

The unit files in `systemd/` now reference `/srv/tmweb/traefik/`. Install them:

```bash
sudo cp /home/dan/work/tmweb/infra/traefik/systemd/traefik-admin-helper.service \
        /etc/systemd/system/traefik-admin-helper.service
sudo cp /home/dan/work/tmweb/infra/traefik/systemd/traefik-admin-ui.service \
        /etc/systemd/system/traefik-admin-ui.service
sudo systemctl daemon-reload
```

## Step 5 — Restart services (operator only)

```bash
sudo systemctl restart traefik-admin-helper traefik-admin-ui
```

## Step 6 — Validate

```bash
systemctl status traefik-admin-helper traefik-admin-ui
curl -sf http://localhost:8080/ping && echo "OK"
curl -sf https://foreman.tmweb.uk/ -o /dev/null -w "%{http_code}\n"
curl -sf https://workpacker.taylormadecontrols.uk/ -o /dev/null -w "%{http_code}\n"
```

Expected: both systemctl statuses green, ping returns `OK`, HTTP 200 from both public routes.

## Rollback

If validation fails at any step:

```bash
sudo cp /etc/systemd/system/traefik-admin-helper.service{.bak,} 2>/dev/null || \
  sudo tee /etc/systemd/system/traefik-admin-helper.service < /home/dan/work/traefik/systemd/traefik-admin-helper.service
sudo systemctl daemon-reload
sudo systemctl restart traefik-admin-helper traefik-admin-ui
```

Or, if unit backups are not present, restore from the original path:

```bash
# The original unit files used /home/dan/work/traefik/ paths.
# Restore them and restart; the /srv/tmweb/traefik/ copy is safe to leave or remove.
```

## Post-cutover

- Update `runtime-inventory.toml`: change traefik `runtime_path` from `/home/dan/work/traefik` to `/srv/tmweb/traefik`
- Old `/home/dan/work/traefik/` can be archived after one week of stable operation

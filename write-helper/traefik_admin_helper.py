#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


DEFAULT_CONFIG_PATH = Path("/home/dan/work/traefik/conf/traefik.toml")
DEFAULT_ROUTES_DIR = Path("/home/dan/work/traefik/conf/routes")
DEFAULT_DISABLED_ROUTES_DIR = Path("/home/dan/work/traefik/conf/routes.disabled")
DEFAULT_BACKUP_DIR = Path("/home/dan/work/traefik/conf/backups")
DEFAULT_TRAEFIK_BIN = Path("/home/dan/work/tools/traefik")
DEFAULT_METADATA_PATH = Path("/home/dan/work/traefik/logs/admin-applies.jsonl")
DEFAULT_LOCK_PATH = Path("/home/dan/work/traefik/run/admin-helper.lock")
DEFAULT_POST_APPLY_URL = "http://127.0.0.1/api/rawdata"
ALLOWED_ORIGINS = {"http://admin.sunny", "http://traefik.sunny", "http://127.0.0.1:8091", "http://localhost:8091"}

GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
PROTECTED_GROUPS = {"shared", "traefik", "traefik-admin"}
PROTECTED_ROUTER_PREFIXES = ("traefik", "traefik-admin")

CHAIN_BLOCK = """
    [http.middlewares.traefik-protected]
      [http.middlewares.traefik-protected.chain]
        middlewares = ["traefik-auth", "error-pages"]

    [http.middlewares.workpacker-protected]
      [http.middlewares.workpacker-protected.chain]
        middlewares = ["workpacker-auth", "error-pages"]

    [http.middlewares.workpacker-trusted]
      [http.middlewares.workpacker-trusted.chain]
        middlewares = ["workpacker-local-origin", "error-pages"]

    [http.middlewares.agentmemory-mounted]
      [http.middlewares.agentmemory-mounted.chain]
        middlewares = ["error-pages", "agentmemory-strip"]
"""

CONTROLLED_REPLACEMENTS = {
    'middlewares = ["traefik-auth", "error-pages"]': 'middlewares = ["traefik-protected"]',
    'middlewares = ["workpacker-auth", "error-pages"]': 'middlewares = ["workpacker-protected"]',
    'middlewares = ["workpacker-local-origin", "error-pages"]': 'middlewares = ["workpacker-trusted"]',
    'middlewares = ["error-pages", "agentmemory-strip"]': 'middlewares = ["agentmemory-mounted"]',
}

SYSTEM_SERVICES = {
    "traefik.service": {
        "label": "Traefik",
        "actions": ["restart"],
        "files": ["/etc/systemd/system/traefik.service", "/etc/systemd/system/traefik.service.d/10-harden.conf"],
    },
    "traefik-error-pages.service": {
        "label": "Traefik error pages",
        "actions": ["start", "restart"],
        "files": [
            "/etc/systemd/system/traefik-error-pages.service",
            "/etc/systemd/system/traefik-error-pages.service.d/10-harden.conf",
        ],
    },
    "workpacker.service": {
        "label": "Workpacker",
        "actions": ["start", "restart"],
        "files": ["/etc/systemd/system/workpacker.service", "/etc/systemd/system/workpacker.service.d/10-harden.conf"],
    },
    "agent-memory-web.service": {
        "label": "Agent Memory",
        "actions": ["start", "restart"],
        "files": [
            "/etc/systemd/system/agent-memory-web.service",
            "/etc/systemd/system/agent-memory-web.service.d/10-harden.conf",
        ],
    },
    "traefik-admin-ui.service": {
        "label": "Traefik Admin UI",
        "actions": ["start", "restart"],
        "files": ["/etc/systemd/system/traefik-admin-ui.service"],
    },
    "traefik-admin-helper.service": {
        "label": "Traefik Admin helper",
        "actions": ["start", "restart"],
        "files": ["/etc/systemd/system/traefik-admin-helper.service"],
    },
}


@dataclass
class Settings:
    config_path: Path = DEFAULT_CONFIG_PATH
    routes_dir: Path = DEFAULT_ROUTES_DIR
    disabled_routes_dir: Path = DEFAULT_DISABLED_ROUTES_DIR
    backup_dir: Path = DEFAULT_BACKUP_DIR
    traefik_bin: Path = DEFAULT_TRAEFIK_BIN
    metadata_path: Path = DEFAULT_METADATA_PATH
    lock_path: Path = DEFAULT_LOCK_PATH
    post_apply_url: str = DEFAULT_POST_APPLY_URL
    post_apply_host: str = "traefik.sunny"
    bind_host: str = "127.0.0.1"
    port: int = 8092


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_toml(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


def build_chain_candidate(text: str) -> str:
    parse_toml(text)
    for old, new in CONTROLLED_REPLACEMENTS.items():
        text = text.replace(old, new)

    if "http.middlewares.traefik-protected" not in text:
        marker = "    [http.middlewares.workpacker-auth]\n"
        if marker not in text:
            return text
        text = text.replace(marker, f"{CHAIN_BLOCK}\n{marker}", 1)

    parse_toml(text)
    return text


def unified_diff(old: str, new: str, old_name: str = "current", new_name: str = "candidate") -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=old_name,
            tofile=new_name,
        )
    )


def validate_group_name(name: str) -> str:
    if not GROUP_NAME_RE.match(name):
        raise ValueError(f"Invalid route group name: {name}")
    return name


def isolated_static_config_text(static_config: str, provider_dir: Path) -> str:
    static_config = re.sub(
        r'directory\s*=\s*"[^"]+"',
        f'directory = "{provider_dir}"',
        static_config,
        count=1,
    )
    return static_config.replace('address = ":80"', 'address = "127.0.0.1:0"', 1)


class TraefikAdminHelper:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextlib.contextmanager
    def lock(self):
        self.settings.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.lock_path.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def backup_path(self, label: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-")
        self.settings.backup_dir.mkdir(parents=True, exist_ok=True)
        return self.settings.backup_dir / f"{safe_label}-{stamp}"

    def append_metadata(self, record: dict[str, Any]) -> None:
        self.settings.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.metadata_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def post_apply_check(self) -> None:
        request = Request(self.settings.post_apply_url, headers={"Host": self.settings.post_apply_host})
        with urlopen(request, timeout=5) as response:
            if response.status >= 400:
                raise RuntimeError(f"Post-apply check failed with HTTP {response.status}")

    def current_config(self) -> dict[str, Any]:
        text = read_text(self.settings.config_path)
        return {"path": str(self.settings.config_path), "checksum": sha256_text(text), "config": text}

    def route_files(self, enabled: bool = True) -> dict[str, Path]:
        root = self.settings.routes_dir if enabled else self.settings.disabled_routes_dir
        if not root.exists():
            return {}
        return {path.stem: path for path in sorted(root.glob("*.toml"))}

    def enabled_route_texts(self) -> dict[str, str]:
        return {name: read_text(path) for name, path in self.route_files(enabled=True).items()}

    def validate_config_set(self, route_texts: dict[str, str] | None = None, static_config: str | None = None) -> dict[str, Any]:
        static_config = static_config if static_config is not None else read_text(self.settings.config_path)
        route_texts = route_texts if route_texts is not None else self.enabled_route_texts()
        parse_toml(static_config)
        for text in route_texts.values():
            parse_toml(text)

        if not self.settings.traefik_bin.exists():
            raise FileNotFoundError(f"Traefik binary not found: {self.settings.traefik_bin}")

        with tempfile.TemporaryDirectory(prefix="traefik-admin-validate-") as tmp:
            tmp_path = Path(tmp)
            routes_dir = tmp_path / "routes"
            routes_dir.mkdir()
            for name, text in route_texts.items():
                (routes_dir / f"{name}.toml").write_text(text, encoding="utf-8")

            static_path = tmp_path / "traefik.toml"
            static_path.write_text(isolated_static_config_text(static_config, routes_dir), encoding="utf-8")
            command = ["timeout", "3s", str(self.settings.traefik_bin), f"--configFile={static_path}"]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            output = f"{result.stdout}\n{result.stderr}".strip()
            valid = result.returncode == 124
            if not valid:
                raise ValueError(output or f"Traefik validation failed with exit code {result.returncode}")
            return {"valid": True, "output": output}

    def propose(self) -> dict[str, Any]:
        current = read_text(self.settings.config_path)
        candidate = build_chain_candidate(current)
        return {
            "previous_checksum": sha256_text(current),
            "candidate_checksum": sha256_text(candidate),
            "diff": unified_diff(current, candidate, str(self.settings.config_path), "candidate"),
            "candidate": candidate,
        }

    def validate_candidate(self, candidate: str | None = None) -> dict[str, Any]:
        if candidate is None:
            candidate = self.propose()["candidate"]
        return self.validate_config_set(static_config=candidate)

    def group_from_file(self, name: str, path: Path, enabled: bool) -> dict[str, Any]:
        text = read_text(path)
        data = parse_toml(text)
        http = data.get("http", {})
        routers = http.get("routers", {})
        services = http.get("services", {})
        middlewares = http.get("middlewares", {})
        router_items = []
        hosts: set[str] = set()
        upstreams: set[str] = set()

        for router_name, router in routers.items():
            rule = router.get("rule", "")
            router_hosts = re.findall(r"Host\(`([^`]+)`\)", rule)
            hosts.update(router_hosts)
            service = router.get("service", "")
            if service and service not in {"noop@internal", "api@internal"}:
                upstreams.add(service.removesuffix("@file"))
            router_items.append(
                {
                    "name": router_name,
                    "rule": rule,
                    "service": service,
                    "middlewares": router.get("middlewares", []),
                    "priority": router.get("priority"),
                    "hosts": router_hosts or ["path-only"],
                }
            )

        if not hosts:
            hosts.add("path-only")
        upstreams.update(name for name in services if name not in {"error-pages"})
        protected = name in PROTECTED_GROUPS or any(
            router["name"].startswith(PROTECTED_ROUTER_PREFIXES) for router in router_items
        )
        return {
            "name": name,
            "file": str(path),
            "enabled": enabled,
            "protected": protected,
            "hosts": sorted(hosts),
            "upstream_apps": sorted(upstreams) or ["internal"],
            "routers": sorted(router_items, key=lambda item: item["name"]),
            "services": sorted(services.keys()),
            "middlewares": sorted(middlewares.keys()),
        }

    def list_route_groups(self) -> dict[str, Any]:
        groups = []
        for name, path in self.route_files(enabled=True).items():
            groups.append(self.group_from_file(name, path, enabled=True))
        for name, path in self.route_files(enabled=False).items():
            groups.append(self.group_from_file(name, path, enabled=False))
        groups.sort(key=lambda item: (not item["protected"], item["hosts"], item["name"]))
        return {"routes_dir": str(self.settings.routes_dir), "disabled_routes_dir": str(self.settings.disabled_routes_dir), "groups": groups}

    def toggle_route_group(self, name: str, enable: bool, operator: str) -> dict[str, Any]:
        name = validate_group_name(name)
        with self.lock():
            enabled_path = self.settings.routes_dir / f"{name}.toml"
            disabled_path = self.settings.disabled_routes_dir / f"{name}.toml"
            if enable:
                src, dst = disabled_path, enabled_path
            else:
                src, dst = enabled_path, disabled_path
                if name in PROTECTED_GROUPS:
                    raise ValueError(f"Route group {name} is protected and cannot be disabled")
                if src.exists():
                    group = self.group_from_file(name, src, enabled=True)
                    if group["protected"]:
                        raise ValueError(f"Route group {name} contains protected routers and cannot be disabled")

            if not src.exists():
                raise FileNotFoundError(f"Route group source not found: {src}")
            if dst.exists():
                raise FileExistsError(f"Route group destination already exists: {dst}")

            candidate = self.enabled_route_texts()
            if enable:
                candidate[name] = read_text(src)
            else:
                candidate.pop(name, None)
            self.validate_config_set(route_texts=candidate)

            backup = self.backup_path(f"route-{name}")
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, backup / src.name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            try:
                self.post_apply_check()
            except Exception:
                os.replace(dst, src)
                with contextlib.suppress(Exception):
                    self.post_apply_check()
                raise

            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "operator": operator,
                "source": "traefik-admin-helper",
                "action": "enable-route-group" if enable else "disable-route-group",
                "group": name,
                "backup_path": str(backup),
            }
            self.append_metadata(record)
            return {"changed": True, "group": name, "enabled": enable, "backup_path": str(backup)}

    def apply(self, operator: str = "unknown") -> dict[str, Any]:
        with self.lock():
            current = read_text(self.settings.config_path)
            proposal = self.propose()
            candidate = proposal["candidate"]
            if candidate == current:
                return {
                    "changed": False,
                    "previous_checksum": sha256_text(current),
                    "new_checksum": sha256_text(current),
                    "backup_path": "",
                }

            self.validate_config_set(static_config=candidate)
            backup = self.backup_path("traefik.toml")
            previous_checksum = sha256_text(current)
            new_checksum = sha256_text(candidate)
            shutil.copy2(self.settings.config_path, backup)
            tmp_path = self.settings.config_path.with_name(f".{self.settings.config_path.name}.admin-tmp")
            tmp_path.write_text(candidate, encoding="utf-8")
            os.replace(tmp_path, self.settings.config_path)

            try:
                self.post_apply_check()
            except Exception:
                shutil.copy2(backup, self.settings.config_path)
                with contextlib.suppress(Exception):
                    self.post_apply_check()
                raise

            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "operator": operator,
                "source": "traefik-admin-helper",
                "action": "apply-static-config",
                "previous_checksum": previous_checksum,
                "new_checksum": new_checksum,
                "backup_path": str(backup),
            }
            self.append_metadata(record)
            return {
                "changed": True,
                "previous_checksum": previous_checksum,
                "new_checksum": new_checksum,
                "backup_path": str(backup),
            }

    def file_entries(self) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {
            "traefik-static": {
                "label": "Traefik static config",
                "path": self.settings.config_path,
                "editable": False,
                "kind": "config",
            }
        }
        for name, path in self.route_files(enabled=True).items():
            entries[f"route:{name}"] = {
                "label": f"Enabled route group: {name}",
                "path": path,
                "editable": name not in PROTECTED_GROUPS,
                "kind": "route",
            }
        for name, path in self.route_files(enabled=False).items():
            entries[f"disabled-route:{name}"] = {
                "label": f"Disabled route group: {name}",
                "path": path,
                "editable": False,
                "kind": "route-disabled",
            }
        for unit, meta in SYSTEM_SERVICES.items():
            for file_path in meta.get("files", []):
                path = Path(file_path)
                if path.exists():
                    entries[f"unit:{unit}:{path.name}"] = {
                        "label": f"{meta['label']} unit: {path.name}",
                        "path": path,
                        "editable": False,
                        "kind": "systemd",
                    }
        for label, path in {
            "log:traefik": Path("/home/dan/work/traefik/logs/traefik.log"),
            "log:error-pages": Path("/home/dan/work/traefik/log/error-pages-server.log"),
        }.items():
            if path.exists():
                entries[label] = {"label": path.name, "path": path, "editable": False, "kind": "log"}
        return entries

    def list_files(self) -> dict[str, Any]:
        files = []
        for file_id, entry in sorted(self.file_entries().items(), key=lambda item: item[1]["label"]):
            path = entry["path"]
            files.append(
                {
                    "id": file_id,
                    "label": entry["label"],
                    "path": str(path),
                    "editable": entry["editable"],
                    "kind": entry["kind"],
                    "size": path.stat().st_size if path.exists() else 0,
                }
            )
        return {"files": files}

    def get_file(self, file_id: str) -> dict[str, Any]:
        entries = self.file_entries()
        if file_id not in entries:
            raise FileNotFoundError(f"File is not allowlisted: {file_id}")
        entry = entries[file_id]
        path = entry["path"]
        return {
            "id": file_id,
            "label": entry["label"],
            "path": str(path),
            "editable": entry["editable"],
            "kind": entry["kind"],
            "content": read_text(path),
            "checksum": sha256_text(read_text(path)),
        }

    def apply_file(self, file_id: str, content: str, operator: str) -> dict[str, Any]:
        entries = self.file_entries()
        if file_id not in entries:
            raise FileNotFoundError(f"File is not allowlisted: {file_id}")
        entry = entries[file_id]
        if not entry["editable"]:
            raise ValueError(f"File is read-only: {file_id}")
        path = entry["path"]
        current = read_text(path)
        parse_toml(content)

        with self.lock():
            if entry["kind"] == "route":
                route_texts = self.enabled_route_texts()
                route_name = file_id.split(":", 1)[1]
                route_texts[route_name] = content
                self.validate_config_set(route_texts=route_texts)
            else:
                self.validate_config_set(static_config=content)

            backup = self.backup_path(f"file-{path.name}")
            shutil.copy2(path, backup)
            tmp_path = path.with_name(f".{path.name}.admin-tmp")
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, path)
            try:
                self.post_apply_check()
            except Exception:
                shutil.copy2(backup, path)
                with contextlib.suppress(Exception):
                    self.post_apply_check()
                raise

            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "operator": operator,
                "source": "traefik-admin-helper",
                "action": "apply-file",
                "file_id": file_id,
                "path": str(path),
                "previous_checksum": sha256_text(current),
                "new_checksum": sha256_text(content),
                "backup_path": str(backup),
            }
            self.append_metadata(record)
            return {
                "changed": current != content,
                "path": str(path),
                "previous_checksum": sha256_text(current),
                "new_checksum": sha256_text(content),
                "backup_path": str(backup),
            }

    def service_status(self, unit: str) -> dict[str, Any]:
        meta = SYSTEM_SERVICES[unit]
        result = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "--no-pager",
                "--property=Id,Description,LoadState,ActiveState,SubState,FragmentPath,MainPID,ExecMainStatus,NRestarts",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        return {
            "unit": unit,
            "label": meta["label"],
            "actions": meta["actions"],
            "ok": result.returncode == 0,
            "error": result.stderr.strip(),
            "description": fields.get("Description", ""),
            "load_state": fields.get("LoadState", "not-found"),
            "active_state": fields.get("ActiveState", "unknown"),
            "sub_state": fields.get("SubState", ""),
            "main_pid": fields.get("MainPID", ""),
            "fragment_path": fields.get("FragmentPath", ""),
        }

    def list_services(self) -> dict[str, Any]:
        return {"services": [self.service_status(unit) for unit in SYSTEM_SERVICES]}

    def service_action(self, unit: str, action: str, operator: str) -> dict[str, Any]:
        if unit not in SYSTEM_SERVICES:
            raise ValueError(f"Service is not allowlisted: {unit}")
        if action not in SYSTEM_SERVICES[unit]["actions"]:
            raise ValueError(f"Action {action} is not allowed for {unit}")
        result = subprocess.run(["sudo", "-n", "systemctl", action, unit], capture_output=True, text=True, check=False)
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "operator": operator,
            "source": "traefik-admin-helper",
            "action": f"systemctl-{action}",
            "unit": unit,
            "returncode": result.returncode,
        }
        self.append_metadata(record)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"systemctl {action} failed")
        return {"unit": unit, "action": action, "status": "ok", "output": result.stdout.strip()}

    def service_logs(self, unit: str, lines: int = 120) -> dict[str, Any]:
        if unit not in SYSTEM_SERVICES:
            raise ValueError(f"Service is not allowlisted: {unit}")
        bounded_lines = max(20, min(lines, 500))
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(bounded_lines), "--no-pager", "--output=short-iso"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "journalctl failed")
        return {"unit": unit, "lines": bounded_lines, "logs": result.stdout}

    def cpu_snapshot(self) -> tuple[int, int]:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + values[4]
        total = sum(values)
        return idle, total

    def cpu_usage_percent(self) -> float:
        idle_a, total_a = self.cpu_snapshot()
        time.sleep(0.15)
        idle_b, total_b = self.cpu_snapshot()
        idle_delta = idle_b - idle_a
        total_delta = total_b - total_a
        if total_delta <= 0:
            return 0.0
        return round((1 - idle_delta / total_delta) * 100, 1)

    def memory_summary(self) -> dict[str, Any]:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", 1)
            values[key] = int(raw_value.strip().split()[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        used = max(total - available, 0)
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "used_percent": round((used / total) * 100, 1) if total else 0,
        }

    def disk_summary(self) -> list[dict[str, Any]]:
        disks = []
        for label, path in {"root": Path("/"), "work": Path("/home/dan/work")}.items():
            usage = shutil.disk_usage(path)
            disks.append(
                {
                    "label": label,
                    "path": str(path),
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
                }
            )
        return disks

    def uptime_seconds(self) -> int:
        return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))

    def failed_units(self) -> list[dict[str, str]]:
        result = subprocess.run(
            ["systemctl", "--failed", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in {0, 1}:
            return [{"unit": "systemctl --failed", "state": "error", "description": result.stderr.strip()}]
        failures = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= 5:
                failures.append({"unit": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3], "description": parts[4]})
        return failures

    def system_overview(self) -> dict[str, Any]:
        load1, load5, load15 = os.getloadavg()
        return {
            "hostname": socket.gethostname(),
            "kernel": " ".join(os.uname()),
            "uptime_seconds": self.uptime_seconds(),
            "load_average": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "cpu": {"count": os.cpu_count() or 1, "usage_percent": self.cpu_usage_percent()},
            "memory": self.memory_summary(),
            "disks": self.disk_summary(),
            "failed_units": self.failed_units(),
            "package_updates": {"implemented": False, "status": "not implemented in this stage"},
        }


class RequestHandler(http.server.BaseHTTPRequestHandler):
    helper: TraefikAdminHelper

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.respond(204, None)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/health":
                self.respond(200, {"status": "ok", "bind_host": self.helper.settings.bind_host})
            elif path == "/config":
                self.respond(200, self.helper.current_config())
            elif path == "/routes":
                self.respond(200, self.helper.list_route_groups())
            elif path == "/files":
                self.respond(200, self.helper.list_files())
            elif path.startswith("/files/"):
                self.respond(200, self.helper.get_file(unquote(path.removeprefix("/files/"))))
            elif path == "/system/services":
                self.respond(200, self.helper.list_services())
            elif path == "/system/overview":
                self.respond(200, self.helper.system_overview())
            elif path == "/system/logs":
                unit = query.get("unit", [""])[0]
                lines = int(query.get("lines", ["120"])[0])
                self.respond(200, self.helper.service_logs(unit, lines=lines))
            else:
                self.respond(404, {"error": "Not found"})
        except Exception as exc:
            self.respond(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/config/propose":
                proposal = self.helper.propose()
                proposal.pop("candidate", None)
                self.respond(200, proposal)
            elif path == "/config/validate":
                self.respond(200, self.helper.validate_candidate())
            elif path == "/config/apply":
                payload = self.read_json()
                if not payload.get("confirmed"):
                    self.respond(400, {"error": "Explicit confirmation is required"})
                    return
                operator = self.headers.get("X-Traefik-Admin-Operator", "unknown")
                self.respond(200, self.helper.apply(operator=operator))
            elif path == "/routes/toggle":
                payload = self.read_json()
                if not payload.get("confirmed"):
                    self.respond(400, {"error": "Explicit confirmation is required"})
                    return
                operator = self.headers.get("X-Traefik-Admin-Operator", "unknown")
                self.respond(200, self.helper.toggle_route_group(payload["group"], bool(payload["enabled"]), operator))
            elif path.startswith("/files/") and path.endswith("/apply"):
                payload = self.read_json()
                if not payload.get("confirmed"):
                    self.respond(400, {"error": "Explicit confirmation is required"})
                    return
                file_id = unquote(path.removeprefix("/files/").removesuffix("/apply"))
                operator = self.headers.get("X-Traefik-Admin-Operator", "unknown")
                self.respond(200, self.helper.apply_file(file_id, payload["content"], operator))
            elif path == "/system/services/action":
                payload = self.read_json()
                operator = self.headers.get("X-Traefik-Admin-Operator", "unknown")
                self.respond(200, self.helper.service_action(payload["unit"], payload["action"], operator))
            else:
                self.respond(404, {"error": "Not found"})
        except Exception as exc:
            self.respond(400, {"error": str(exc)})

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def respond(self, status: int, payload: dict[str, Any] | None) -> None:
        self.send_response(status)
        if payload is None:
            self.end_headers()
            return
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Traefik admin write helper")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--routes-dir", type=Path, default=DEFAULT_ROUTES_DIR)
    parser.add_argument("--disabled-routes-dir", type=Path, default=DEFAULT_DISABLED_ROUTES_DIR)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--traefik-bin", type=Path, default=DEFAULT_TRAEFIK_BIN)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host != "127.0.0.1":
        raise SystemExit("Refusing to bind write helper anywhere except 127.0.0.1")

    settings = Settings(
        config_path=args.config,
        routes_dir=args.routes_dir,
        disabled_routes_dir=args.disabled_routes_dir,
        backup_dir=args.backup_dir,
        traefik_bin=args.traefik_bin,
        metadata_path=args.metadata,
        lock_path=args.lock,
        bind_host=args.host,
        port=args.port,
    )
    RequestHandler.helper = TraefikAdminHelper(settings)
    server = http.server.ThreadingHTTPServer((settings.bind_host, settings.port), RequestHandler)
    print(f"traefik-admin-helper listening on http://{settings.bind_host}:{settings.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

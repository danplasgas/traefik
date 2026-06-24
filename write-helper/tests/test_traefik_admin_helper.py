import tempfile
import unittest
from pathlib import Path
from unittest import mock

from traefik_admin_helper import SYSTEM_SERVICES, Settings, TraefikAdminHelper, build_chain_candidate, sha256_text


STATIC_CONFIG = """# Traefik HTTP-only local setup (no HTTPS).

[entryPoints]
  [entryPoints.web]
    address = ":80"

[api]
  dashboard = true
  insecure = false

[providers]
  [providers.file]
    directory = "/tmp/routes"
    watch = true
"""

SAMPLE_MONOLITHIC_CONFIG = """[http]
  [http.middlewares]
    [http.middlewares.error-pages]
      [http.middlewares.error-pages.errors]
        status = ["404"]
        service = "error-pages"
        query = "/{status}.html"

    [http.middlewares.workpacker-auth]
      [http.middlewares.workpacker-auth.basicAuth]
        usersFile = "/tmp/workpacker.htpasswd"

    [http.middlewares.agentmemory-strip]
      [http.middlewares.agentmemory-strip.stripPrefix]
        prefixes = ["/agentmemory"]

    [http.middlewares.traefik-auth]
      [http.middlewares.traefik-auth.basicAuth]
        usersFile = "/tmp/traefik.htpasswd"

    [http.middlewares.workpacker-local-origin]
      [http.middlewares.workpacker-local-origin.headers.customRequestHeaders]
        Host = "127.0.0.1:8000"

  [http.routers]
    [http.routers.traefik]
      middlewares = ["traefik-auth", "error-pages"]

    [http.routers.workpacker]
      middlewares = ["workpacker-auth", "error-pages"]

    [http.routers.workpacker-tailscale]
      middlewares = ["workpacker-local-origin", "error-pages"]

    [http.routers.agentmemory]
      middlewares = ["error-pages", "agentmemory-strip"]
"""

WORKPACKER_ROUTE = """[http]
  [http.services.workpacker.loadBalancer]
    [[http.services.workpacker.loadBalancer.servers]]
      url = "http://127.0.0.1:8000"

  [http.routers.workpacker]
    rule = "Host(`workpacker.sunny`)"
    entryPoints = ["web"]
    service = "workpacker"
"""

TRAEFIK_ROUTE = """[http]
  [http.routers.traefik]
    rule = "Host(`traefik.sunny`) && PathPrefix(`/dashboard`)"
    entryPoints = ["web"]
    service = "api@internal"
"""


class HelperTests(unittest.TestCase):
    def make_helper(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        routes = root / "routes"
        disabled = root / "routes.disabled"
        routes.mkdir()
        disabled.mkdir()
        config = root / "traefik.toml"
        config.write_text(STATIC_CONFIG.replace("/tmp/routes", str(routes)), encoding="utf-8")
        (routes / "workpacker.toml").write_text(WORKPACKER_ROUTE, encoding="utf-8")
        (routes / "traefik.toml").write_text(TRAEFIK_ROUTE, encoding="utf-8")
        helper = TraefikAdminHelper(
            Settings(
                config_path=config,
                routes_dir=routes,
                disabled_routes_dir=disabled,
                backup_dir=root / "backups",
                traefik_bin=Path("/tmp/fake-traefik"),
                metadata_path=root / "metadata.jsonl",
                lock_path=root / "helper.lock",
            )
        )
        return tmp, root, config, routes, disabled, helper

    def test_build_chain_candidate_is_controlled(self):
        candidate = build_chain_candidate(SAMPLE_MONOLITHIC_CONFIG)

        self.assertIn("[http.middlewares.traefik-protected.chain]", candidate)
        self.assertIn('middlewares = ["traefik-protected"]', candidate)
        self.assertIn('middlewares = ["workpacker-protected"]', candidate)
        self.assertIn('middlewares = ["workpacker-trusted"]', candidate)
        self.assertIn('middlewares = ["agentmemory-mounted"]', candidate)

    def test_list_route_groups_marks_protected_groups(self):
        tmp, _, _, _, _, helper = self.make_helper()
        with tmp:
            groups = {group["name"]: group for group in helper.list_route_groups()["groups"]}

            self.assertFalse(groups["workpacker"]["protected"])
            self.assertTrue(groups["traefik"]["protected"])
            self.assertEqual(groups["workpacker"]["hosts"], ["workpacker.sunny"])

    def test_toggle_route_group_moves_file_and_records_metadata(self):
        tmp, root, _, routes, disabled, helper = self.make_helper()
        with tmp:
            with mock.patch.object(helper, "validate_config_set", return_value={"valid": True}), mock.patch.object(
                helper, "post_apply_check", return_value=None
            ):
                result = helper.toggle_route_group("workpacker", enable=False, operator="test")

            self.assertTrue(result["changed"])
            self.assertFalse((routes / "workpacker.toml").exists())
            self.assertTrue((disabled / "workpacker.toml").exists())
            self.assertIn('"group": "workpacker"', (root / "metadata.jsonl").read_text(encoding="utf-8"))

    def test_toggle_protected_route_group_is_rejected(self):
        tmp, _, _, routes, disabled, helper = self.make_helper()
        with tmp:
            with self.assertRaises(ValueError):
                helper.toggle_route_group("traefik", enable=False, operator="test")

            self.assertTrue((routes / "traefik.toml").exists())
            self.assertFalse((disabled / "traefik.toml").exists())

    def test_toggle_rolls_back_when_post_apply_check_fails(self):
        tmp, _, _, routes, disabled, helper = self.make_helper()
        with tmp:
            with mock.patch.object(helper, "validate_config_set", return_value={"valid": True}), mock.patch.object(
                helper, "post_apply_check", side_effect=RuntimeError("failed")
            ):
                with self.assertRaises(RuntimeError):
                    helper.toggle_route_group("workpacker", enable=False, operator="test")

            self.assertTrue((routes / "workpacker.toml").exists())
            self.assertFalse((disabled / "workpacker.toml").exists())

    def test_apply_file_writes_editable_route_with_backup(self):
        tmp, root, _, _, _, helper = self.make_helper()
        with tmp:
            changed = WORKPACKER_ROUTE.replace("workpacker.sunny", "workpacker.local")
            with mock.patch.object(helper, "validate_config_set", return_value={"valid": True}), mock.patch.object(
                helper, "post_apply_check", return_value=None
            ):
                result = helper.apply_file("route:workpacker", changed, operator="test")

            self.assertTrue(result["changed"])
            self.assertIn("workpacker.local", helper.get_file("route:workpacker")["content"])
            self.assertTrue((root / "backups").exists())

    def test_read_only_files_cannot_be_written(self):
        tmp, _, _, _, _, helper = self.make_helper()
        with tmp:
            with self.assertRaises(ValueError):
                helper.apply_file("traefik-static", STATIC_CONFIG, operator="test")

    def test_settings_default_bind_host_is_localhost(self):
        self.assertEqual(Settings().bind_host, "127.0.0.1")
        self.assertEqual(sha256_text("x"), sha256_text("x"))

    def test_v1_service_actions_do_not_expose_stop(self):
        for meta in SYSTEM_SERVICES.values():
            self.assertNotIn("stop", meta["actions"])


if __name__ == "__main__":
    unittest.main()

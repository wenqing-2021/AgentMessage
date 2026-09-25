from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_message.core.config import ConfigError, load_config

from tests.helpers import make_config


class ConfigTests(unittest.TestCase):
    def test_loads_absolute_whitelisted_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), ("alpha", "beta"))
            self.assertEqual(set(config.projects), {"alpha", "beta"})
            self.assertTrue(config.projects["alpha"].path.is_absolute())
            self.assertEqual(config.service.default_chat_project, "alpha")
            self.assertFalse(config.service.codex_tool_network)

    def test_global_sandbox_git_identity_and_ssh_shared_by_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), ("alpha", "beta", "plain"), sandbox_gpu_projects=("alpha", "beta"))
            original = config.config_path.read_text()
            settings = ('sandbox_git_user_name = "Example User"\n'
                'sandbox_git_user_email = "example@example.com"\n'
                'sandbox_ssh_agent_socket = "/run/user/1000/test.sock"\n'
                'sandbox_ssh_known_hosts = "/home/example/.ssh/known_hosts"\n')
            config.config_path.write_text(original.replace("[service]\n", "[service]\n" + settings))
            loaded = load_config(config.config_path)
            self.assertEqual(loaded.service.sandbox_git_user_name, "Example User")
            for alias in ("alpha", "beta"):
                sandbox = loaded.projects[alias].sandbox
                assert sandbox is not None
                self.assertTrue(sandbox.gpu)
                self.assertEqual(sandbox.git_user_name, "Example User")
                self.assertEqual(sandbox.git_user_email, "example@example.com")
                self.assertEqual(sandbox.ssh_agent_socket, Path("/run/user/1000/test.sock"))
                self.assertEqual(sandbox.ssh_known_hosts, loaded.service.sandbox_ssh_known_hosts)
                self.assertFalse(sandbox.network)
            self.assertIsNone(loaded.projects["plain"].sandbox)
            # Legacy gpu_* service keys keep working and still imply GPU passthrough.
            config.config_path.write_text(
                original.replace("[service]\n", "[service]\n" + settings.replace("sandbox_", "gpu_"))
            )
            legacy = load_config(config.config_path)
            self.assertEqual(legacy.service.sandbox_git_user_name, "Example User")
            self.assertEqual(legacy.projects["alpha"].sandbox.git_user_name, "Example User")
            self.assertTrue(legacy.projects["alpha"].sandbox.gpu)

    def test_global_gpu_git_config_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            original = config.config_path.read_text()
            for settings in (
                'sandbox_git_user_name = 42', 'sandbox_git_user_email = ""',
                'gpu_git_user_name = "bad\\nname"',
                'sandbox_ssh_agent_socket = "relative"',
                'sandbox_ssh_agent_socket = "/run/../tmp/test.sock"',
                'sandbox_ssh_agent_socket = "/tmp/test.sock"',
                'sandbox_ssh_known_hosts = "/tmp/hosts"',
                'gpu_ssh_agent_socket = "relative"',
            ):
                config.config_path.write_text(original.replace("[service]\n", "[service]\n" + settings + "\n"))
                with self.subTest(settings=settings), self.assertRaisesRegex(ConfigError, r"\[service\]"):
                    load_config(config.config_path)

    def test_service_readonly_paths_are_validated_and_shared(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(
                Path(temp), ("alpha", "beta", "plain"), sandbox_projects=("alpha", "beta")
            )
            original = config.config_path.read_text()
            settings = 'sandbox_readonly_paths = ["/opt/quarto", "/etc/fonts"]'
            config.config_path.write_text(
                original.replace("[service]\n", "[service]\n" + settings + "\n")
            )
            loaded = load_config(config.config_path)
            expected = (Path("/opt/quarto"), Path("/etc/fonts"))
            self.assertEqual(loaded.service.sandbox_readonly_paths, expected)
            for alias in ("alpha", "beta"):
                sandbox = loaded.projects[alias].sandbox
                assert sandbox is not None
                self.assertEqual(sandbox.readonly_paths, expected)
            self.assertIsNone(loaded.projects["plain"].sandbox)

            for bad in (
                'sandbox_readonly_paths = "/opt/quarto"',
                "sandbox_readonly_paths = [42]",
                'sandbox_readonly_paths = [""]',
                'sandbox_readonly_paths = ["relative/path"]',
                'sandbox_readonly_paths = ["/opt/../etc"]',
                'sandbox_readonly_paths = ["/"]',
                f'sandbox_readonly_paths = ["{Path.home().parent}"]',
                'sandbox_readonly_paths = ["/opt/quarto", "/opt/quarto"]',
            ):
                config.config_path.write_text(
                    original.replace("[service]\n", "[service]\n" + bad + "\n")
                )
                with self.subTest(settings=bad), self.assertRaisesRegex(ConfigError, r"\[service\]"):
                    load_config(config.config_path)

    def test_project_git_settings_report_global_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            original = config.config_path.read_text()
            for key, value in (("git_user_name", "Example User"), ("git_user_email", "example@example.com"),
                               ("ssh_agent_socket", "/tmp/test.sock"), ("ssh_known_hosts", "/tmp/hosts")):
                for settings in (f'gpu_{key} = "{value}"', f'[projects.alpha.gpu]\n{key} = "{value}"'):
                    config.config_path.write_text(original + "\n" + settings + "\n")
                    with self.subTest(settings=settings), self.assertRaisesRegex(ConfigError, r"global \[service\]"):
                        load_config(config.config_path)

    def test_rejects_relative_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "projects.toml"
            path.write_text(
                "[projects.bad]\npath = 'relative'\ndefault_agent='codex'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "absolute"):
                load_config(path)

    def test_rejects_unknown_default_chat_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "missing"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        'default_agent = "codex"',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "unknown"):
                load_config(path)

    def test_default_chat_project_can_be_qoder_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        'default_agent = "qoder"',
                        'allowed_agents = ["qoder"]',
                    ]
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.projects["alpha"].default_agent.value, "qoder")
            self.assertEqual(
                {agent.value for agent in config.projects["alpha"].allowed_agents},
                {"qoder"},
            )

    def test_rejects_non_boolean_codex_tool_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        'codex_tool_network = "true"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "true or false"):
                load_config(path)

    def test_loads_sandbox_project_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), sandbox_gpu_projects=("alpha",))
            sandbox = config.projects["alpha"].sandbox
            assert sandbox is not None
            self.assertTrue(sandbox.enabled)
            self.assertTrue(sandbox.gpu)
            self.assertFalse(sandbox.network)
            self.assertEqual(sandbox.timeout_seconds, 3600)

    def test_sandbox_without_gpu_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), sandbox_projects=("alpha",))
            sandbox = config.projects["alpha"].sandbox
            assert sandbox is not None
            self.assertTrue(sandbox.enabled)
            self.assertFalse(sandbox.gpu)
            self.assertEqual(sandbox.timeout_seconds, 3600)

    def test_gpu_network_defaults_to_enabled_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        "gpu_enabled = true",
                    ]
                ),
                encoding="utf-8",
            )
            sandbox = load_config(path).projects["alpha"].sandbox
            assert sandbox is not None
            # Legacy gpu_* keys still imply GPU passthrough and default to networking on.
            self.assertTrue(sandbox.gpu)
            self.assertTrue(sandbox.network)

    def test_qoder_only_gpu_project_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(
                Path(temp),
                sandbox_gpu_projects=("alpha",),
                qoder_only_projects=("alpha",),
            )
            project = config.projects["alpha"]
            self.assertEqual(project.default_agent.value, "qoder")
            self.assertEqual({agent.value for agent in project.allowed_agents}, {"qoder"})
            self.assertTrue(project.sandbox and project.sandbox.enabled)

    def test_rejects_invalid_gpu_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        "gpu_enabled = true",
                        "gpu_timeout_seconds = 10",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "between 60 and 604800"):
                load_config(path)

    def test_legacy_nested_gpu_table_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        "",
                        "[projects.alpha.gpu]",
                        "enabled = true",
                        "network = false",
                        "timeout_seconds = 3600",
                    ]
                ),
                encoding="utf-8",
            )
            sandbox = load_config(path).projects["alpha"].sandbox
            assert sandbox is not None
            self.assertTrue(sandbox.enabled)
            self.assertTrue(sandbox.gpu)

    def test_rejects_mixed_flat_and_nested_gpu_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        "gpu_enabled = true",
                        "",
                        "[projects.alpha.gpu]",
                        "enabled = true",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "cannot mix"):
                load_config(path)

    def test_rejects_mixing_sandbox_and_legacy_gpu_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        "sandbox_enabled = true",
                        "gpu_enabled = true",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "cannot mix"):
                load_config(path)

    def test_loads_container_project_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            path = root / "projects.toml"
            path.write_text(
                "\n".join(
                    [
                        "[service]",
                        'default_chat_project = "alpha"',
                        "",
                        "[projects.alpha]",
                        f'path = "{project}"',
                        'allowed_agents = ["codex"]',
                        'container_name = "alpha-dev"',
                    ]
                ),
                encoding="utf-8",
            )
            container = load_config(path).projects["alpha"].container
            assert container is not None
            self.assertEqual(container.name, "alpha-dev")
            self.assertTrue(container.auto_start)
            self.assertEqual(container.timeout_seconds, 86400)
            self.assertIsNone(container.project_path)

    def test_loads_and_validates_explicit_container_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            base = [
                "[service]",
                'default_chat_project = "alpha"',
                "",
                "[projects.alpha]",
                f'path = "{project}"',
                'allowed_agents = ["codex"]',
                'container_name = "alpha-dev"',
            ]
            path = root / "projects.toml"
            path.write_text(
                "\n".join([*base, 'container_path = "/workspace/alpha"']),
                encoding="utf-8",
            )
            container = load_config(path).projects["alpha"].container
            assert container is not None
            self.assertEqual(str(container.project_path), "/workspace/alpha")

            for index, value in enumerate(('"relative/path"', '"/workspace/../other"', "true")):
                with self.subTest(value=value):
                    invalid = root / f"invalid-container-path-{index}.toml"
                    invalid.write_text(
                        "\n".join([*base, f"container_path = {value}"]),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ConfigError, "container_path"):
                        load_config(invalid)

    def test_container_project_accepts_qoder_but_rejects_gpu_and_bad_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            base = [
                "[service]",
                'default_chat_project = "alpha"',
                "",
                "[projects.alpha]",
                f'path = "{project}"',
                'container_name = "alpha-dev"',
            ]
            qoder_path = root / "projects-qoder.toml"
            qoder_path.write_text(
                "\n".join(
                    [
                        *base,
                        'default_agent = "qoder"',
                        'allowed_agents = ["qoder"]',
                    ]
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(load_config(qoder_path).projects["alpha"].container)

            cases = (
                (
                    ['allowed_agents = ["codex"]', "gpu_enabled = true"],
                    "both container execution and the bubblewrap sandbox",
                ),
                (
                    [
                        'allowed_agents = ["codex"]',
                        "container_timeout_seconds = 10",
                    ],
                    "between 60 and 604800",
                ),
            )
            for index, (extra, error) in enumerate(cases):
                with self.subTest(error=error):
                    path = root / f"projects-{index}.toml"
                    path.write_text("\n".join([*base, *extra]), encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, error):
                        load_config(path)

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

    def test_loads_gpu_project_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), gpu_projects=("alpha",))
            gpu = config.projects["alpha"].gpu
            assert gpu is not None
            self.assertTrue(gpu.enabled)
            self.assertFalse(gpu.network)
            self.assertEqual(gpu.timeout_seconds, 3600)

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
            gpu = load_config(path).projects["alpha"].gpu
            assert gpu is not None
            self.assertTrue(gpu.network)

    def test_qoder_only_gpu_project_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(
                Path(temp),
                gpu_projects=("alpha",),
                qoder_only_projects=("alpha",),
            )
            project = config.projects["alpha"]
            self.assertEqual(project.default_agent.value, "qoder")
            self.assertEqual({agent.value for agent in project.allowed_agents}, {"qoder"})
            self.assertTrue(project.gpu and project.gpu.enabled)

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
            gpu = load_config(path).projects["alpha"].gpu
            assert gpu is not None
            self.assertTrue(gpu.enabled)

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
                    "both container execution and GPU",
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

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_message.config import ConfigError, load_config

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

    def test_default_chat_project_must_allow_codex(self) -> None:
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
            with self.assertRaisesRegex(ConfigError, "must allow codex"):
                load_config(path)

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

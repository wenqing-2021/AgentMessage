from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_message.agents.model_catalog import (
    CodexModel,
    configured_codex_model,
    find_codex_model,
    list_codex_models,
)


class ModelCatalogTests(unittest.TestCase):
    def _write_config(self, root: Path, catalog: Path, model: str = "gpt-5.6-sol") -> Path:
        config = root / "config.toml"
        config.write_text(
            f'''model_catalog_json = "{catalog}"
model = "{model}"
''',
            encoding="utf-8",
        )
        return config

    def test_lists_models_from_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = root / "catalog.json"
            catalog.write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"},
                            {
                                "slug": "deepseek/deepseek-v4-pro",
                                "display_name": "deepseek/deepseek-v4-pro",
                            },
                            {"slug": "skip-me", "display_name": ""},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config = self._write_config(root, catalog)
            models = list_codex_models(config)
            self.assertEqual(
                [model.slug for model in models],
                ["gpt-5.6-sol", "deepseek/deepseek-v4-pro", "skip-me"],
            )
            self.assertEqual(models[0].display_name, "GPT-5.6-Sol")
            self.assertEqual(models[2].display_name, "skip-me")
            self.assertEqual(configured_codex_model(config), "gpt-5.6-sol")
            found = find_codex_model("deepseek/deepseek-v4-pro", config)
            assert found is not None
            self.assertEqual(found.slug, "deepseek/deepseek-v4-pro")
            self.assertIsNone(find_codex_model("missing", config))

    def test_reasoning_metadata_handles_missing_empty_and_malformed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"models": [
                {"slug": "first", "supported_reasoning_levels": [
                    {"effort": "low"}, {"effort": "ultra"}, {"effort": "low"},
                    {"effort": []}, None, {"effort": "invalid"}], "default_reasoning_level": "low"},
                {"slug": "second", "supported_reasoning_levels": []},
                {"slug": "third"},
            ]}))
            models = list_codex_models(self._write_config(root, catalog))
            self.assertEqual(models[0].reasoning_levels, ("low", "ultra"))
            self.assertEqual(models[0].default_reasoning_level, "low")
            self.assertEqual(models[1].reasoning_levels, ())
            self.assertIsNone(models[2].reasoning_levels)

    def test_missing_config_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "nope.toml"
            self.assertEqual(list_codex_models(config), [])
            self.assertIsNone(configured_codex_model(config))
            self.assertIsNone(find_codex_model("anything", config))

    def test_missing_catalog_json_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text(
                f'''model = "gpt-5.6-sol"
''',
                encoding="utf-8",
            )
            self.assertEqual(list_codex_models(config), [])
            self.assertEqual(configured_codex_model(config), "gpt-5.6-sol")

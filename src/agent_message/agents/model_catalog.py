'''Discover the Codex model catalog injected by opencodex.

opencodex runs a local OpenAI-compatible proxy and points the official Codex
CLI at it by injecting model_catalog_json into ~/.codex/config.toml.
That catalog is the authoritative list of models Codex can select with -m.
Reading it here keeps model listing deterministic and proxy-independent, so
/model works even when the proxy is temporarily stopped.
'''

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10, which remains supported by this project.
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class CodexModel:
    '''A single selectable model: slug is what codex -m accepts.'''

    slug: str
    display_name: str


def codex_home() -> Path:
    return Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()


def codex_config_path() -> Path:
    return codex_home() / 'config.toml'


def load_codex_config(path: Path | None = None) -> dict[str, object]:
    path = path or codex_config_path()
    if not path.is_file():
        return {}
    try:
        with path.open('rb') as handle:
            return tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _catalog_path(raw: dict[str, object]) -> Path | None:
    candidate = raw.get('model_catalog_json')
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    return Path(candidate).expanduser()


def list_codex_models(config_path: Path | None = None) -> list[CodexModel]:
    '''Return the catalog models Codex can select, preserving catalog order.'''
    raw = load_codex_config(config_path)
    catalog_path = _catalog_path(raw)
    if catalog_path is None or not catalog_path.is_file():
        return []
    try:
        with catalog_path.open('rb') as handle:
            catalog = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return []
    models = catalog.get('models') if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        return []
    result: list[CodexModel] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        slug = item.get('slug')
        if not isinstance(slug, str) or not slug.strip():
            continue
        display = item.get('display_name')
        display_name = display.strip() if isinstance(display, str) and display.strip() else slug
        result.append(CodexModel(slug=slug.strip(), display_name=display_name))
    return result


def configured_codex_model(config_path: Path | None = None) -> str | None:
    '''Return the model field from Codex config (opencodex injected default).'''
    raw = load_codex_config(config_path)
    model = raw.get('model')
    return model.strip() if isinstance(model, str) and model.strip() else None


def find_codex_model(name: str, config_path: Path | None = None) -> CodexModel | None:
    for model in list_codex_models(config_path):
        if model.slug == name:
            return model
    return None

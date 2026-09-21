"""Bridge routes reference Codex providers, without requiring Codex profiles."""
from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import urlsplit


@dataclass
class Route:
    base_url: str
    token: str = field(repr=False)
    model: dict = field(repr=False)
    upstream_model: str
    headers: dict = field(default_factory=dict, repr=False)
    use_template: bool = False
    reasoning_passthrough: bool = True


@dataclass
class Settings:
    routes: list[Route]
    official_url: str = 'https://chatgpt.com/backend-api/codex'


def default_config_path() -> Path:
    return Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / 'codex-model-bridge/config.toml'


def provider_connection(provider):
    if provider.get('wire_api') != 'responses':
        raise ValueError('Selected provider must exist and use wire_api = responses')
    url = provider.get('base_url', '').rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provider requires a plain HTTP(S) base URL without embedded credentials')
    token = provider.get('experimental_bearer_token') or os.environ.get(provider.get('env_key', ''), '')
    if not token and provider.get('auth'):
        helper = provider['auth']
        command, args = helper.get('command'), helper.get('args', [])
        if not isinstance(command, str) or not command or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError('Invalid provider authentication command')
        try:
            result = subprocess.run([command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=10, check=True)
            token = result.stdout.decode().strip()
            if len(token) > 16384 or '\n' in token or '\r' in token:
                raise ValueError('Authentication command must output a single token')
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise ValueError('Provider authentication command failed') from None
    if not token:
        raise ValueError('Provider credential is unavailable')
    headers = dict(provider.get('http_headers', {}))
    for name, env in provider.get('env_http_headers', {}).items():
        if not os.environ.get(env):
            raise ValueError('Provider header environment variable is unavailable')
        headers[name] = os.environ[env]
    return url, token, headers


def make_route(entry, providers, directory):
    required = {'slug', 'provider', 'model', 'catalog'}
    allowed = required | {'catalog_model', 'display_name'}
    if not isinstance(entry, dict) or not required <= entry.keys() or entry.keys() - allowed:
        raise ValueError('Each route requires slug, provider, model and catalog; unknown fields are not accepted')
    if any(not isinstance(v, str) or not v.strip() for v in entry.values()):
        raise ValueError('Route fields must be nonempty strings')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}', entry['slug']):
        raise ValueError('Invalid route slug')
    provider = providers.get(entry['provider'], {})
    url, token, headers = provider_connection(provider)
    catalog_path = Path(entry['catalog']).expanduser()
    if not catalog_path.is_absolute():
        catalog_path = directory / catalog_path
    models = json.loads(catalog_path.read_text()).get('models')
    if not isinstance(models, list):
        raise ValueError('Catalog requires a models array')
    selected = entry.get('catalog_model', entry['model'])
    matches = [m for m in models if isinstance(m, dict) and m.get('slug') == selected]
    if len(matches) != 1:
        raise ValueError('Catalog must contain exactly one matching model')
    model = deepcopy(matches[0])
    model['slug'] = entry['slug']
    if 'display_name' in entry:
        model['display_name'] = entry['display_name']
    elif entry['slug'] != selected:
        model['display_name'] = f'{model.get("display_name", selected)} ({entry["slug"]})'
    return Route(url, token, model, entry['model'], headers)


def load_settings(codex_home: Path, profile: str | None = None, *, config_path: Path | None = None, cache_path: Path | None = None) -> Settings:
    """Default: route config + global providers. Explicit --profile remains compatible."""
    if profile is None and config_path is None:
        from .discovery import load_cached_settings
        return load_cached_settings(codex_home, cache_path)
    home = Path(codex_home).expanduser()
    base = tomllib.loads((home / 'config.toml').read_text())
    providers = deepcopy(base.get('model_providers', {}))
    if profile is not None:
        if config_path is not None or not re.fullmatch(r'[A-Za-z0-9_-]+', profile):
            raise ValueError('Use either a route config or a valid legacy profile')
        overlay = tomllib.loads((home / f'{profile}.config.toml').read_text())
        for name, values in overlay.get('model_providers', {}).items():
            providers.setdefault(name, {}).update(values)
        merged = {**base, **overlay}
        entries = [{'slug': merged['model'], 'model': merged['model'],
                    'provider': merged['model_provider'], 'catalog': merged['model_catalog_json']}]
        directory = home
    else:
        path = Path(config_path or default_config_path()).expanduser()
        config = tomllib.loads(path.read_text())
        if set(config) != {'models'} or not isinstance(config['models'], list) or not config['models']:
            raise ValueError('Bridge config requires a nonempty [[models]] list only')
        entries, directory = config['models'], path.parent
    routes = [make_route(entry, providers, directory) for entry in entries]
    if len({r.model['slug'] for r in routes}) != len(routes):
        raise ValueError('Duplicate model slug in bridge configuration')
    return Settings(routes)

"""Explicit provider discovery; generated cache contains metadata, never credentials."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

import aiohttp

from .config import Route, Settings, provider_connection

MODEL_FIELDS = set('''base_instructions model_messages display_name description
context_window max_context_window effective_context_window_percent auto_compact_token_limit
default_reasoning_level supported_reasoning_levels supports_reasoning_summaries
reasoning_summary_format default_reasoning_summary input_modalities supports_image_detail_original
tool_mode use_responses_lite shell_type apply_patch_tool_type web_search_tool_type
supports_search_tool supports_parallel_tool_calls experimental_supported_tools
multi_agent_version include_skills_usage_instructions support_verbosity default_verbosity
truncation_policy minimal_client_version priority'''.split())


def default_cache_path():
    return Path(os.environ.get('XDG_CACHE_HOME', '~/.cache')).expanduser() / 'codex-model-bridge/models.json'


def fingerprint(provider):
    value = [provider.get('base_url', '').rstrip('/'), provider.get('wire_api')]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def read_cache(path):
    if not path.exists():
        return {'version': 1, 'providers': {}}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('providers'), dict):
        raise ValueError('Invalid model cache; move it aside before refreshing')
    for provider in value['providers'].values():
        if not isinstance(provider, dict) or not isinstance(provider.get('models'), list):
            raise ValueError('Invalid model cache provider')
        for row in provider['models']:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not isinstance(row.get('metadata'), dict):
                raise ValueError('Invalid cached model')
    return value


def local_seeds(home):
    """Existing profile-linked catalogs are optional migration hints, never required."""
    seeds = {}
    for path in sorted(home.glob('*.config.toml')):
        try:
            config = tomllib.loads(path.read_text())
            provider, catalog = config.get('model_provider'), config.get('model_catalog_json')
            if not provider or not catalog:
                continue
            catalog = Path(catalog).expanduser()
            if not catalog.is_absolute():
                catalog = home / catalog
            for model in json.loads(catalog.read_text())['models']:
                key = (provider, model['slug'])
                metadata = {k: deepcopy(v) for k, v in model.items() if k in MODEL_FIELDS}
                if key not in seeds:
                    seeds[key] = metadata
                elif seeds[key] != metadata:
                    seeds[key] = None  # conflicting hints must not silently pick a winner
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return seeds


def normalize_model(row, seed):
    model_id = row.get('id', row.get('slug'))
    if not isinstance(model_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,191}', model_id):
        raise ValueError('Invalid upstream model ID')
    # Do not expose explicitly non-chat models as coding assistants.
    if row.get('type') in ('embedding', 'image', 'audio', 'rerank'):
        return None
    native = {k: deepcopy(v) for k, v in row.items() if k in MODEL_FIELDS} if 'slug' in row else {}
    known = seed or native
    metadata = deepcopy(known) if known else {
        'tool_mode': None, 'use_responses_lite': False, 'shell_type': 'shell_command',
        'apply_patch_tool_type': 'freeform', 'multi_agent_version': 'v1',
        'supports_search_tool': False, 'experimental_supported_tools': [],
        'supports_parallel_tool_calls': False, 'supports_image_detail_original': False,
        'input_modalities': ['text'], 'context_window': 32768, 'max_context_window': 32768,
        'effective_context_window_percent': 90, 'auto_compact_token_limit': None,
        'default_reasoning_level': 'high',
        'supported_reasoning_levels': [{'effort': 'high', 'description': 'Use upstream default; capabilities unverified'}],
        'supports_reasoning_summaries': False, 'default_reasoning_summary': 'none',
        'support_verbosity': False, 'default_verbosity': 'low',
    }
    context = row.get('context_window', row.get('context'))
    if isinstance(context, int) and not isinstance(context, bool) and context > 0:
        metadata.update(context_window=context, max_context_window=context)
        # Recompute compaction from the newly reported window, not a stale local threshold.
        metadata['auto_compact_token_limit'] = None
    if isinstance(row.get('supports_images'), bool):
        metadata['input_modalities'] = ['text', 'image'] if row['supports_images'] else ['text']
    return {'id': model_id, 'metadata': metadata,
            'reasoning_passthrough': bool(known and metadata.get('supported_reasoning_levels')),
            'source': 'local-catalog' if seed else ('upstream-catalog' if native else 'compatibility-template')}


async def refresh(codex_home, cache_path=None):
    home = Path(codex_home).expanduser()
    path = Path(cache_path or default_cache_path()).expanduser()
    providers = tomllib.loads((home / 'config.toml').read_text()).get('model_providers', {})
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another model refresh is already running') from None
        old = read_cache(path)
        hints = local_seeds(home)
        report, result = {}, {}
        semaphore = asyncio.Semaphore(4)
        async with aiohttp.ClientSession(trust_env=True, cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=20, sock_connect=5)) as session:
            async def discover(name, provider):
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
                    report[name] = {'status': 'skipped', 'reason': 'invalid_provider_name'}
                    return
                previous = old['providers'].get(name, {})
                same_endpoint = previous.get('fingerprint') == fingerprint(provider)
                reason = 'configuration_or_credentials_unavailable'
                try:
                    url, token, headers = await asyncio.to_thread(provider_connection, provider)
                    headers = {k: v for k, v in headers.items() if k.lower() not in ('authorization', 'cookie', 'chatgpt-account-id')}
                    headers.update(Authorization='Bearer ' + token, Accept='application/json')
                    async with semaphore:
                        reason = 'upstream_unavailable'
                        async with session.get(url + '/models', headers=headers, allow_redirects=False) as response:
                            if response.status != 200:
                                raise ValueError('Discovery rejected')
                            raw = bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                raw.extend(chunk)
                                if len(raw) > 8 * 1024 * 1024:
                                    raise ValueError('Model list too large')
                    reason = 'invalid_model_list'
                    value = json.loads(raw)
                    rows = value.get('data', value.get('models'))
                    if not isinstance(rows, list):
                        raise ValueError('Missing model list')
                    found, seen = [], set()
                    for row in rows:
                        if not isinstance(row, dict):
                            raise ValueError('Invalid model entry')
                        model_id = row.get('id', row.get('slug'))
                        seed = hints.get((name, model_id))
                        if seed is None and same_endpoint:
                            seed = next((m['metadata'] for m in previous.get('models', [])
                                         if m['id'] == model_id and m.get('source') == 'local-catalog'), None)
                        model = normalize_model(row, seed)
                        if model is None:
                            continue
                        if model['id'] in seen:
                            raise ValueError('Duplicate upstream model ID')
                        seen.add(model['id'])
                        found.append(model)
                    result[name] = {'fingerprint': fingerprint(provider), 'models': found,
                                    'fetched_at': datetime.now(timezone.utc).isoformat()}
                    report[name] = {'status': 'updated', 'models': len(found),
                                    'compatibility_templates': sum(m['source'] == 'compatibility-template' for m in found)}
                except (OSError, ValueError, TypeError, KeyError, AttributeError, aiohttp.ClientError, asyncio.TimeoutError):
                    if same_endpoint:
                        result[name] = previous
                    report[name] = {'status': 'retained' if same_endpoint else 'skipped', 'reason': reason}
            await asyncio.gather(*(discover(name, provider) for name, provider in providers.items()))
        data = {'version': 1, 'providers': {k: result[k] for k in sorted(result)}}
        fd, temporary = tempfile.mkstemp(prefix='.models-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return report
    finally:
        os.close(lock)


def load_cached_settings(codex_home, cache_path=None):
    home = Path(codex_home).expanduser()
    providers = tomllib.loads((home / 'config.toml').read_text()).get('model_providers', {})
    cache = read_cache(Path(cache_path or default_cache_path()).expanduser())
    routes = []
    for name, provider in providers.items():
        saved = cache['providers'].get(name)
        if not saved or saved.get('fingerprint') != fingerprint(provider):
            continue
        try:
            url, token, headers = provider_connection(provider)
        except (ValueError, KeyError, TypeError):
            continue
        for saved_model in saved['models']:
            model_id = saved_model['id']
            metadata = deepcopy(saved_model['metadata'])
            metadata.update(slug=f'{name}/{model_id}', display_name=f'{name} / {model_id}')
            routes.append(Route(url, token, metadata, model_id, headers, use_template=True,
                                reasoning_passthrough=saved_model.get('reasoning_passthrough', False)))
    return Settings(routes)

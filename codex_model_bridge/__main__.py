import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from . import create_app, load_settings
from .discovery import refresh, default_cache_path


def main():
    parser = argparse.ArgumentParser(description='Codex official + custom model bridge')
    parser.add_argument('command', nargs='?', choices=['serve', 'refresh'], default='serve')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser())
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--profile', help='Legacy: read one model from a Codex profile')
    source.add_argument('--config', type=Path, help='Legacy: explicit bridge route config')
    parser.add_argument('--cache', type=Path, default=default_cache_path(), help='Generated discovery cache')
    parser.add_argument('--port', type=int, default=17843)
    parser.add_argument('--check', action='store_true', help='Validate configuration without starting a service')
    args = parser.parse_args()
    try:
        if args.command == 'refresh':
            if args.profile or args.config or args.check:
                parser.error('refresh reads global model_providers; do not combine with legacy sources or --check')
            report = asyncio.run(refresh(args.codex_home, args.cache))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print('Restart codex-model-bridge to load the refreshed cache.')
            if not any(row['status'] == 'updated' for row in report.values()):
                parser.exit(1, 'No provider was refreshed; existing matching cache entries were retained.\n')
            return
        settings = load_settings(args.codex_home, args.profile, config_path=args.config, cache_path=args.cache)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, 'Configuration/cache invalid: check global providers and run refresh; legacy sources require valid catalogs.\n')
    names = ', '.join(route.model['slug'] for route in settings.routes)
    if not settings.routes:
        print('No usable cached custom models; serving official models only. Run codex-model-bridge refresh.')
    if args.check:
        print(f'Configuration OK; custom models: {names}')
        return
    # No access logs or exception tracebacks containing upstream URLs/headers.
    logging.getLogger('aiohttp').setLevel(logging.CRITICAL)
    print(f'codex-model-bridge: http://127.0.0.1:{args.port}/v1; models: {names}', flush=True)
    web.run_app(create_app(settings), host='127.0.0.1', port=args.port, access_log=None,
                print=None, handler_cancellation=True)


if __name__ == '__main__':
    main()

"""Dynamic Codex catalog and isolated Responses routing."""

import asyncio
from copy import deepcopy
import json
import io
import re
from urllib.parse import urlencode

import aiohttp
import zstandard
from aiohttp import web


from .config import Route, Settings, load_settings
from .compaction import CompactionExporter, CompactionExportError
from .portable import restore, is_compaction, compact


HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


def forwarding_headers(headers):
    blocked = HOP_HEADERS | {h.strip().lower() for h in headers.get('Connection', '').split(',')}
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def error(status, message):
    return web.json_response({'error': {'message': message, 'type': 'bridge_error'}}, status=status)


def custom_body(body):
    body = deepcopy(body)
    body.pop('service_tier', None)
    body.pop('previous_response_id', None)
    if isinstance(body.get('input'), list):
        items = []
        for item in body['input']:
            if isinstance(item, dict) and item.get('type') in ('compaction', 'compaction_summary'):
                raise ValueError('Encrypted compaction history cannot cross providers. Start a new conversation with a text summary.')
            if isinstance(item, dict) and item.get('type') == 'reasoning':
                item.pop('encrypted_content', None)
                if not item.get('summary') and not item.get('content'):
                    continue
            items.append(item)
        body['input'] = items
    return body


SESSION = web.AppKey('session', aiohttp.ClientSession)
COMPACTION = web.AppKey('compaction', CompactionExporter)


def native_body(body):
    """Adapt foreign reasoning to the official schema; preserve native encrypted items.

    Official reasoning content must be empty. Responses-compatible upstreams can
    emit reasoning_text content instead. Its presence proves a non-native item;
    replay complete message/tool contents without foreign backend item references.
    """
    items = body.get('input')
    if not isinstance(items, list) or not any(isinstance(i, dict) and i.get('type') == 'reasoning'
                                            and i.get('content') for i in items):
        return body
    clean = deepcopy(body)
    clean.pop('previous_response_id', None)
    result = []
    for item in clean['input']:
        if isinstance(item, dict):
            item.pop('id', None)
            if item.get('type') == 'reasoning':
                if item.pop('content', None):
                    item.pop('encrypted_content', None)
                if not item.get('encrypted_content') and not item.get('summary'):
                    continue
        result.append(item)
    clean['input'] = result
    return clean


def create_app(settings: Settings) -> web.Application:
    limit = 64 * 1024 * 1024
    app = web.Application(client_max_size=limit, handler_args={'auto_decompress': False})

    async def session_context(app):
        # trust_env honors HTTPS_PROXY/NO_PROXY; cookies never persist between upstreams.
        async with aiohttp.ClientSession(trust_env=True, cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=600)) as session:
            app[SESSION] = session
            app[COMPACTION] = CompactionExporter(session, settings.official_url)
            try:
                yield
            finally:
                await app[COMPACTION].close()

    app.cleanup_ctx.append(session_context)

    async def handle(request):
        if request.headers.get('Origin'):
            return error(403, 'Browser origins are not accepted')
        if request.path == '/health' and request.method == 'GET':
            return web.json_response({'status': 'ok'})
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or not auth[7:].strip():
            return error(401, 'Codex Bearer authentication is required')
        if request.path == '/v1/responses' and request.method == 'GET':
            return error(426, 'Use HTTP Responses streaming; WebSockets are not supported')
        catalog = request.path == '/v1/models' and request.method == 'GET'
        endpoints = {'/v1/responses', '/v1/responses/compact', '/v1/alpha/search',
                     '/v1/images/generations', '/v1/images/edits'}
        if not catalog and (request.method != 'POST' or request.path not in endpoints):
            return error(404, 'Unsupported route')
        headers = forwarding_headers(request.headers)
        headers['Accept-Encoding'] = 'identity'
        raw = None if catalog else await request.read()
        custom = None
        if request.path in ('/v1/responses', '/v1/responses/compact'):
            try:
                decoded = raw
                encoding = request.headers.get('Content-Encoding', 'identity').lower()
                if encoding == 'zstd':
                    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
                        decoded = reader.read(limit + 1)
                    if len(decoded) > limit:
                        return error(413, 'Decoded request exceeds 64 MiB')
                elif encoding != 'identity':
                    return error(415, 'Supported request encodings: identity, zstd')
                body = json.loads(decoded)
                if not isinstance(body, dict) or not isinstance(body.get('model'), str):
                    return error(400, 'A JSON object with a model is required')
                body, restored = restore(body)
            except (ValueError, UnicodeDecodeError, zstandard.ZstdError):
                return error(400, 'Invalid JSON request')
            custom = next((route for route in settings.routes if route.model['slug'] == body['model']), None)
            if custom:
                try:
                    compaction_requested = is_compaction(body)
                    portable = await app[COMPACTION].translate(body, headers)
                    outgoing = custom_body(portable)
                    outgoing['model'] = custom.upstream_model
                    if not custom.reasoning_passthrough:
                        outgoing.pop('reasoning', None)
                    raw = json.dumps(outgoing).encode()
                except CompactionExportError as exc:
                    return error(502, str(exc))
                except ValueError as exc:
                    return error(409, str(exc))
                # Start from a clean header set. Never leak ChatGPT tokens, cookies or account IDs.
                headers = {k: v for k, v in custom.headers.items()
                           if k.lower() not in HOP_HEADERS | {'authorization', 'cookie', 'chatgpt-account-id'}}
                headers.update({'Authorization': f'Bearer {custom.token}', 'Content-Type': 'application/json',
                                'Accept': 'text/event-stream', 'Accept-Encoding': 'identity'})
                if compaction_requested:
                    try:
                        return await compact(app[SESSION], custom, outgoing, headers, body['model'], body.get('stream', True))
                    except CompactionExportError as exc:
                        return error(502, str(exc))
            else:
                clean = native_body(body)
                if clean is not body or restored:
                    raw = json.dumps(clean).encode()
                    headers = {k: v for k, v in headers.items() if k.lower() != 'content-encoding'}
        if catalog:
            headers = {k: v for k, v in headers.items() if k.lower() not in ('if-none-match', 'if-modified-since')}
        base = custom.base_url if custom else settings.official_url
        url = base.rstrip('/') + request.path.removeprefix('/v1')
        query = list(request.query.items())
        if catalog and 'client_version' not in request.query:
            version = re.match(r'(?:codex_cli_rs|codex-tui|codex_vscode|codex_atlas|codex_chatgpt_desktop)/(\d{1,6}\.\d{1,6}\.\d{1,6})(?:[-+][\w.-]+)?(?:\s|$)',
                               request.headers.get('User-Agent', ''))
            if version:
                query.append(('client_version', version[1]))
        if query:
            url += '?' + urlencode(query)
        response = None
        stream_completed = False
        line_tail = b''
        try:
            async with app[SESSION].request(request.method, url, headers=headers, data=raw,
                    allow_redirects=False, auto_decompress=catalog) as upstream:
                if catalog and upstream.status == 200:
                    try:
                        data = await upstream.json(content_type=None)
                        if not isinstance(data, dict) or not isinstance(data.get('models'), list):
                            raise ValueError()
                        if any(m.get('slug') in {r.model['slug'] for r in settings.routes} for m in data['models']):
                            return error(502, 'Custom model conflicts with an official model slug')
                        template = next((m for m in data['models'] if isinstance(m, dict) and m.get('visibility') == 'list'), None)
                        for route in settings.routes:
                            if route.use_template and template is None:
                                return error(502, 'Official catalog has no model template')
                            model = {**deepcopy(template or {}), **deepcopy(route.model)} if route.use_template else deepcopy(route.model)
                            if route.use_template:
                                model.pop('comp_hash', None)
                                model.pop('availability_nux', None)
                                model.update(upgrade=None, auto_review_model_override=None, priority=100)
                            model.update(visibility='list', supported_in_api=True, prefer_websockets=False,
                                         service_tiers=[], additional_speed_tiers=[], default_service_tier=None)
                            data['models'].append(model)
                        return web.json_response(data, headers={'Cache-Control': 'no-store'})
                    except (ValueError, TypeError, AttributeError):
                        return error(502, 'Official model catalog is invalid')
                reply_headers = forwarding_headers(upstream.headers)
                if custom:
                    reply_headers = {k: v for k, v in reply_headers.items()
                                     if not k.lower().startswith('x-codex-') and k.lower() != 'set-cookie'}
                response = web.StreamResponse(status=upstream.status, headers=reply_headers)
                await response.prepare(request)
                inspect_sse = 'text/event-stream' in upstream.headers.get('Content-Type', '').lower() and not upstream.headers.get('Content-Encoding')
                async for chunk in upstream.content.iter_any():
                    if inspect_sse:
                        lines = (line_tail + chunk).split(b'\n')
                        stream_completed |= any(line.rstrip(b'\r') == b'data: [DONE]' for line in lines[:-1])
                        # Only retain enough tail to recognize a split terminator; never accumulate tokens.
                        line_tail = lines[-1] if len(lines[-1]) <= 32 else b'!not-a-terminator!'
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            if response is not None and response.prepared:
                if stream_completed or line_tail.rstrip(b'\r') == b'data: [DONE]':
                    await response.write_eof()
                    return response
                # A partial SSE stream must fail visibly, never acquire a synthetic completion.
                if request.transport:
                    request.transport.close()
                return response
            return error(502, 'Upstream connection failed')

    app.router.add_route('*', '/{path:.*}', handle)
    return app

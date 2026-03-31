#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx",
#     "pyyaml",
#     "starlette",
#     "uvicorn",
# ]
# ///

import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class StdioConnection:
    def __init__(self, cmd: str, env: dict[str, str] | None):
        self.cmd = cmd
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.pending: dict[Any, asyncio.Future] = {}
        self.reader_task: asyncio.Task | None = None

    async def start(self):
        full_env = os.environ.copy()
        if self.env:
            full_env.update(self.env)
        self.process = await asyncio.create_subprocess_shell(
            self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        logger.info('Started stdio process for command: %s (pid=%d)', self.cmd, self.process.pid)
        self.reader_task = asyncio.create_task(self.read_stdout())
        asyncio.create_task(self.drain_stderr())

    async def drain_stderr(self):
        while self.process and self.process.stderr:
            line = await self.process.stderr.readline()
            if not line:
                break
            logger.debug('stdio stderr: %s', line.decode(errors='replace').rstrip())

    async def read_stdout(self):
        while self.process and self.process.stdout:
            line = await self.process.stdout.readline()
            if not line:
                break
            logger.debug('stdio raw output from %s: %s', self.cmd,
                         line.decode(errors='replace').rstrip())
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning('Non-JSON line from stdio process: %s',
                               line.decode(errors='replace').rstrip())
                continue
            message_id = message.get('id')
            if message_id is not None and message_id in self.pending:
                self.pending[message_id].set_result(line)
            else:
                logger.debug('Unroutable stdio message (notification or unknown id): %s',
                             line.decode(errors='replace').rstrip())
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError('stdio process exited'))
        self.pending.clear()

    async def ensure_running(self):
        if self.process is None or self.process.returncode is not None:
            async with self.lock:
                if self.process is None or self.process.returncode is not None:
                    self.pending.clear()
                    await self.start()

    async def send(self, body: bytes, request_id: Any = None) -> bytes | None:
        await self.ensure_running()
        future = None
        if request_id is not None:
            future = asyncio.get_event_loop().create_future()
            self.pending[request_id] = future
        async with self.write_lock:
            self.process.stdin.write(body)
            if not body.endswith(b'\n'):
                self.process.stdin.write(b'\n')
            await self.process.stdin.drain()
        if future is None:
            return None
        try:
            return await future
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
            logger.info('Stopped stdio process for command: %s', self.cmd)


class StreamingHttpConnection:
    def __init__(self, url: str, headers: dict[str, str] | None):
        self.url = url
        self.headers = headers or {}
        self.client: httpx.AsyncClient | None = None

    async def start(self):
        self.client = httpx.AsyncClient(
            base_url=self.url,
            headers=self.headers,
            timeout=httpx.Timeout(300, connect=30),
        )
        logger.info('Created persistent HTTP client for url: %s', self.url)

    async def ensure_running(self):
        if self.client is None:
            await self.start()

    async def close(self):
        if self.client:
            await self.client.aclose()
            logger.info('Closed HTTP client for url: %s', self.url)


class ToolServer:
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.connection: StdioConnection | StreamingHttpConnection | None = None

    def is_stdio(self) -> bool:
        return 'cmd' in self.config

    async def start(self):
        if self.is_stdio():
            self.connection = StdioConnection(
                cmd=self.config['cmd'],
                env=self.config.get('env'),
            )
        else:
            self.connection = StreamingHttpConnection(
                url=self.config['url'],
                headers=self.config.get('headers'),
            )
        await self.connection.ensure_running()

    async def close(self):
        if self.connection:
            await self.connection.close()


def load_config(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        msg = 'Config file must contain a YAML list of tool server entries'
        raise ValueError(msg)
    return data


def build_app(config_path: str) -> Starlette:  # noqa
    servers: dict[str, ToolServer] = {}

    @asynccontextmanager
    async def lifespan(app: Starlette):
        config_entries = load_config(config_path)
        for entry in config_entries:
            name = entry['name']
            server = ToolServer(name, entry)
            await server.start()
            servers[name] = server
            logger.info('Registered tool server: %s', name)
        yield
        for server in servers.values():
            await server.close()
        servers.clear()

    async def handle_mcp(request: Request) -> Response:
        server_name = request.path_params['server_name']

        if server_name not in servers:
            return Response(status_code=404, content=f'Unknown tool server: {server_name}')

        tool_server = servers[server_name]

        if request.method == 'OPTIONS':
            return Response(
                status_code=204,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                    'Access-Control-Allow-Headers': '*',
                },
            )

        cors_headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': '*',
        }

        if tool_server.is_stdio():
            return await handle_stdio_request(request, tool_server, cors_headers)
        return await handle_http_request(request, tool_server, cors_headers)

    async def handle_stdio_request(
        request: Request, tool_server: ToolServer, cors_headers: dict[str, str],
    ) -> Response:
        body = await request.body()
        logger.debug('stdio request to %s: %s', tool_server.name, body.decode(errors='replace'))

        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            return Response(status_code=400, content='Invalid JSON', headers=cors_headers)

        request_id = message.get('id')
        connection: StdioConnection = tool_server.connection
        response_line = await connection.send(body, request_id=request_id)

        if response_line is None:
            logger.debug('Notification sent to %s, no response expected', tool_server.name)
            return Response(status_code=202, headers=cors_headers)

        logger.debug('stdio response from %s: %s', tool_server.name,
                     response_line.decode(errors='replace').rstrip())
        return Response(
            content=response_line,
            media_type='application/json',
            headers=cors_headers,
        )

    async def handle_http_request(
        request: Request, tool_server: ToolServer, cors_headers: dict[str, str],
    ) -> Response:
        connection: StreamingHttpConnection = tool_server.connection
        await connection.ensure_running()

        body = await request.body()
        logger.debug('HTTP request to %s: %s %s', tool_server.name,
                     request.method, body.decode(errors='replace'))

        incoming_headers = dict(request.headers)
        incoming_headers.pop('host', None)
        incoming_headers.pop('transfer-encoding', None)

        upstream_request = connection.client.build_request(
            method=request.method,
            url='/mcp',
            content=body if body else None,
            headers=incoming_headers,
        )

        upstream_response = await connection.client.send(upstream_request, stream=True)

        content_type = upstream_response.headers.get('content-type', '')
        is_event_stream = 'text/event-stream' in content_type

        if is_event_stream:
            async def stream_body():
                try:
                    async for chunk in upstream_response.aiter_bytes():
                        logger.debug('SSE chunk from %s: %s', tool_server.name, chunk[:200])
                        yield chunk
                finally:
                    await upstream_response.aclose()

            response_headers = dict(cors_headers)
            response_headers['content-type'] = content_type
            cache_control = upstream_response.headers.get('cache-control')
            if cache_control:
                response_headers['cache-control'] = cache_control

            return StreamingResponse(
                content=stream_body(),
                status_code=upstream_response.status_code,
                headers=response_headers,
            )
        response_body = await upstream_response.aread()
        await upstream_response.aclose()
        logger.debug('HTTP response from %s: %s', tool_server.name, response_body[:500])

        response_headers = dict(cors_headers)
        if content_type:
            response_headers['content-type'] = content_type

        return Response(
            content=response_body,
            status_code=upstream_response.status_code,
            headers=response_headers,
        )

    routes = [
        Route('/{server_name}/mcp', handle_mcp, methods=['GET', 'POST', 'OPTIONS']),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    return app


if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description='MCP tool server proxy')
    parser.add_argument('config', nargs='?', default='tool-proxy.yaml',
                        help='Path to YAML config file (default: tool-proxy.yaml)')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8000, help='Port to bind to')
    parser.add_argument('--log', help='Append logs to the specified path')
    parser.add_argument('--verbose', '-v', action='count', default=0,
                        help='Increase verbosity')
    args = parser.parse_args()

    if args.log:
        handler = logging.FileHandler(args.log, mode='a')
        handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))

    app = build_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port)

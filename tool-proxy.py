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
import queue
import shlex
import subprocess
import threading
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
    def __init__(self, name: str, cmd: str, env: dict[str, str] | None):
        self.name = name
        self.cmd = cmd
        self.env = env
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.pending: dict[Any, queue.Queue] = {}
        self.should_stop = threading.Event()

    def start(self):
        full_env = os.environ.copy()
        if self.env:
            full_env.update(self.env)
        self.should_stop.clear()
        self.process = subprocess.Popen(
            # self.cmd,
            shlex.split(self.cmd),
            # shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            bufsize=0,
        )
        threading.Thread(target=self.read_stdout, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()

    def read_stderr(self):
        while not self.should_stop.is_set() and self.process:
            line = self.process.stderr.readline()
            if not line:
                break
            logger.debug('%s: %s', self.name, line.decode(errors='replace').rstrip())

    def read_stdout(self):
        while not self.should_stop.is_set() and self.process:
            line = self.process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
                message_id = message.get('id')
                if message_id in self.pending:
                    self.pending[message_id].put(line)
            except json.JSONDecodeError:
                logger.warning('Non-JSON line: %s', line.decode(errors='replace').rstrip())
        for q in list(self.pending.values()):
            q.put(None)
        self.pending.clear()

    def ensure_running(self):
        if self.process is None or self.process.poll() is not None:
            with self.lock:
                if self.process is None or self.process.poll() is not None:
                    self.pending.clear()
                    self.start()

    async def send(self, body: bytes, request_id: Any = None) -> bytes | None:
        logger.debug('%s send: %s', self.name, body.decode(errors='ignore').strip())
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.send_sync, body, request_id)

    def send_sync(self, body: bytes, request_id: Any = None) -> bytes | None:
        self.ensure_running()
        response_queue = None
        if request_id is not None:
            response_queue = queue.Queue(maxsize=1)
            self.pending[request_id] = response_queue

        try:
            self.process.stdin.write(body)
            if not body.endswith(b'\n'):
                self.process.stdin.write(b'\n')
            self.process.stdin.flush()
        except Exception:
            if request_id:
                self.pending.pop(request_id, None)
            raise
        if response_queue is None:
            return None
        try:
            response = response_queue.get(timeout=300)
            if response is None:
                msg = 'stdio process exited'
                raise ConnectionError(msg)
            return response
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.close_sync)

    def close_sync(self):
        self.should_stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class StreamingHttpConnection:
    def __init__(self, name: str, url: str, headers: dict[str, str] | None):
        self.name = name
        self.url = url
        self.headers = headers or {}
        self.client: httpx.AsyncClient | None = None
        self.session_id: str | None = None

    async def start(self):
        self.client = httpx.AsyncClient(
            base_url=self.url,
            headers=self.headers,
            timeout=httpx.Timeout(300, connect=30),
        )
        self.session_id = None

    async def ensure_running(self):
        if self.client is None:
            await self.start()

    async def close(self):
        if self.client:
            await self.client.aclose()


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
                name=self.name,
                cmd=self.config['cmd'],
                env=self.config.get('env'),
            )
            await asyncio.get_event_loop().run_in_executor(None, self.connection.start)
        else:
            self.connection = StreamingHttpConnection(
                name=self.name,
                url=self.config['url'],
                headers=self.config.get('headers'),
            )
            await self.connection.start()

    async def close(self):
        if self.connection:
            await self.connection.close()

    async def ensure_running(self):
        if self.connection is None:
            await self.start()


def load_config(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data


def build_app(config_path: str) -> Starlette:  # noqa
    servers: dict[str, ToolServer] = {}

    @asynccontextmanager
    async def lifespan(app: Starlette):
        config_entries = load_config(config_path)
        for entry in config_entries:
            name = entry['name']
            servers[name] = ToolServer(name, entry)
        yield
        for server in servers.values():
            await server.close()
        servers.clear()

    async def handle_mcp(request: Request) -> Response:
        server_name = request.path_params['server_name']
        if server_name not in servers:
            return Response(status_code=404, content=f'Unknown server: {server_name}')
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
        tool_server = servers[server_name]
        await tool_server.ensure_running()
        if tool_server.is_stdio():
            return await handle_stdio(request, tool_server, cors_headers)
        return await handle_http(request, tool_server, cors_headers)

    async def handle_stdio(
        request: Request, tool_server: ToolServer, cors_headers: dict[str, str],
    ) -> Response:
        body = await request.body()
        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            return Response(status_code=400, content='Invalid JSON', headers=cors_headers)
        request_id = message.get('id')
        connection: StdioConnection = tool_server.connection
        response_line = await connection.send(body, request_id=request_id)
        if response_line is None:
            return Response(status_code=202, headers=cors_headers)
        return Response(
            content=response_line,
            media_type='application/json',
            headers=cors_headers,
        )

    async def handle_http(
        request: Request, tool_server: ToolServer, cors_headers: dict[str, str],
    ) -> Response:
        connection: StreamingHttpConnection = tool_server.connection
        await connection.ensure_running()
        body = await request.body()
        incoming_headers = dict(request.headers)
        incoming_headers.pop('host', None)
        incoming_headers.pop('transfer-encoding', None)
        incoming_headers.pop('content-length', None)
        if connection.session_id and 'mcp-session-id' not in incoming_headers:
            incoming_headers['mcp-session-id'] = connection.session_id
        upstream_request = connection.client.build_request(
            method=request.method,
            url='/mcp',
            content=body if body else None,
            headers=incoming_headers,
        )
        if body:
            logger.debug('%s send: %s', tool_server.name, body.decode(errors='ignore').strip())
        upstream_response = await connection.client.send(upstream_request, stream=True)
        session_id = upstream_response.headers.get('mcp-session-id')
        if session_id:
            connection.session_id = session_id
        content_type = upstream_response.headers.get('content-type', '')
        is_event_stream = 'text/event-stream' in content_type
        if is_event_stream:
            proxy_base = str(request.base_url).rstrip('/')
            upstream_base = str(connection.url).rstrip('/')

            async def stream_body():
                try:
                    async for chunk in upstream_response.aiter_bytes():
                        decoded = chunk.decode(errors='replace')
                        rewritten = decoded.replace(
                            upstream_base, f'{proxy_base}/{tool_server.name}')
                        yield rewritten.encode()
                finally:
                    await upstream_response.aclose()

            response_headers = dict(cors_headers)
            response_headers['content-type'] = content_type
            cache_control = upstream_response.headers.get('cache-control')
            if cache_control:
                response_headers['cache-control'] = cache_control
            if session_id:
                response_headers['mcp-session-id'] = session_id
            return StreamingResponse(
                content=stream_body(),
                status_code=upstream_response.status_code,
                headers=response_headers,
            )
        response_body = await upstream_response.aread()
        await upstream_response.aclose()
        response_headers = dict(cors_headers)
        if content_type:
            response_headers['content-type'] = content_type
        if session_id:
            response_headers['mcp-session-id'] = session_id
        return Response(
            content=response_body,
            status_code=upstream_response.status_code,
            headers=response_headers,
        )
    routes = [
        Route('/{server_name}/mcp', handle_mcp, methods=['GET', 'POST', 'OPTIONS']),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description='MCP tool server proxy')
    parser.add_argument('config', nargs='?', default='tool-proxy.yaml')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=3000)
    parser.add_argument('--log', help='Log file path')
    parser.add_argument('--verbose', '-v', action='count', default=0)
    args = parser.parse_args()
    if args.log:
        handler = logging.FileHandler(args.log, mode='a')
        handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    app = build_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port)

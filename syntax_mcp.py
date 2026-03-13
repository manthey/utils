# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.9.0",
#   "esprima>=4.0.1",
#   "javalang>=0.13.0",
#   "tinycss2>=1.3.0",
#   "tree-sitter>=0.24.0",
#   "tree-sitter-language-pack>=0.7.0",
# ]
# ///

import argparse
import asyncio
import importlib
import subprocess
import traceback
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool

app = Server('syntax-validator')


def validate_python(code: str) -> dict[str, Any]:
    source = code.encode('utf-8')
    try:
        compile(source, '<string>', 'exec')
        return {'valid': True, 'errors': []}
    except SyntaxError as e:
        return {
            'valid': False,
            'errors': [
                {
                    'line': e.lineno,
                    'column': e.offset,
                    'message': e.msg,
                },
            ],
        }


def validate_bash(code: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ['bash', '-n', '-'],
            input=code.encode('utf-8'),
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return {'valid': True, 'errors': []}
        stderr = result.stderr.decode('utf-8', errors='replace')
        errors = []
        for line in stderr.splitlines():
            parts = line.split(':', 3)
            if len(parts) >= 3:
                try:
                    lineno = int(parts[2].strip().split()[0])
                except (ValueError, IndexError):
                    lineno = None
                message = parts[-1].strip() if len(parts) > 3 else line
                errors.append({'line': lineno, 'column': None, 'message': message})
            else:
                errors.append({'line': None, 'column': None, 'message': line})
        return {'valid': False, 'errors': errors}
    except FileNotFoundError:
        return {
            'valid': False,
            'errors': [{'line': None, 'column': None, 'message': 'bash not found on this system'}],
        }
    except subprocess.TimeoutExpired:
        return {
            'valid': False,
            'errors': [{'line': None, 'column': None, 'message': 'bash validation timed out'}],
        }


def validate_javascript(code: str) -> dict[str, Any]:
    esprima = importlib.import_module('esprima')
    try:
        esprima.parseScript(code, {'tolerant': False})
        return {'valid': True, 'errors': []}
    except Exception as e:
        error_data = getattr(e, 'data', None)
        if error_data:
            return {
                'valid': False,
                'errors': [
                    {
                        'line': getattr(error_data, 'lineNumber', None),
                        'column': getattr(error_data, 'column', None),
                        'message': str(e),
                    },
                ],
            }
        return {
            'valid': False,
            'errors': [{'line': None, 'column': None, 'message': str(e)}],
        }


def _tree_sitter_validate(language_name: str, code: str) -> dict[str, Any]:
    language_pack = importlib.import_module('tree_sitter_language_pack')
    tree_sitter = importlib.import_module('tree_sitter')

    language = language_pack.get_language(language_name)
    parser = tree_sitter.Parser(language)
    tree = parser.parse(code.encode('utf-8'))

    errors = []
    _collect_tree_sitter_errors(tree.root_node, errors)

    if errors:
        return {'valid': False, 'errors': errors}
    return {'valid': True, 'errors': []}


def _collect_tree_sitter_errors(node: Any, errors: list) -> None:
    if node.type == 'ERROR' or node.is_missing:
        start = node.start_point
        errors.append(
            {
                'line': start[0] + 1,
                'column': start[1] + 1,
                'message': (
                    f"Syntax error at '{node.text.decode('utf-8', errors='replace')}'"
                    if node.text
                    else 'Missing token'
                ),
            },
        )
    for child in node.children:
        _collect_tree_sitter_errors(child, errors)


def validate_typescript(code: str) -> dict[str, Any]:
    return _tree_sitter_validate('typescript', code)


def validate_java(code: str) -> dict[str, Any]:
    javalang = importlib.import_module('javalang')
    try:
        javalang.parse.parse(code)
        return {'valid': True, 'errors': []}
    except javalang.parser.JavaSyntaxError as e:
        return {
            'valid': False,
            'errors': [
                {
                    'line': getattr(e, 'at', {}).get('line')
                    if isinstance(getattr(e, 'at', None), dict) else None,
                    'column': None,
                    'message': str(e),
                },
            ],
        }
    except Exception as e:
        return {
            'valid': False,
            'errors': [{'line': None, 'column': None, 'message': str(e)}],
        }


def validate_css(code: str) -> dict[str, Any]:
    tinycss2 = importlib.import_module('tinycss2')
    rules, encoding = tinycss2.parse_stylesheet_bytes(
        code.encode('utf-8'), skip_comments=True, skip_whitespace=True,
    )
    errors = []
    for rule in rules:
        if rule.type == 'error':
            errors.append(
                {
                    'line': getattr(rule, 'source_line', None),
                    'column': getattr(rule, 'source_column', None),
                    'message': getattr(rule, 'message', 'CSS parse error'),
                },
            )
    if errors:
        return {'valid': False, 'errors': errors}
    return {'valid': True, 'errors': []}


VALIDATORS = {
    'python': validate_python,
    'bash': validate_bash,
    'javascript': validate_javascript,
    'typescript': validate_typescript,
    'java': validate_java,
    'css': validate_css,
}

SUPPORTED_LANGUAGES = list(VALIDATORS.keys())


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name='validate_syntax',
            description=(
                'Validate the syntax of code in a given language. '
                f"Supported languages: {', '.join(SUPPORTED_LANGUAGES)}."
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'language': {
                        'type': 'string',
                        'enum': SUPPORTED_LANGUAGES,
                        'description': 'The programming language of the code.',
                    },
                    'code': {
                        'type': 'string',
                        'description': 'The source code to validate.',
                    },
                },
                'required': ['language', 'code'],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name != 'validate_syntax':
        msg = f'Unknown tool: {name}'
        raise ValueError(msg)

    language = arguments.get('language', '').lower()
    code = arguments.get('code', '')

    if language not in VALIDATORS:
        return [
            TextContent(
                type='text',
                text=f"Unsupported language '{language}'. Supported: "
                     f"{', '.join(SUPPORTED_LANGUAGES)}",
            ),
        ]

    try:
        result = VALIDATORS[language](code)
    except Exception:
        result = {
            'valid': False,
            'errors': [
                {
                    'line': None,
                    'column': None,
                    'message': f'Validator error: {traceback.format_exc()}',
                },
            ],
        }

    if result['valid']:
        text = f'Syntax is valid ({language}).'
    else:
        lines = [f'Syntax errors found ({language}):']
        for error in result['errors']:
            location = ''
            if error.get('line') is not None:
                location = f" at line {error['line']}"
                if error.get('column') is not None:
                    location += f", column {error['column']}"
            lines.append(f"  -{location}: {error['message']}")
        text = '\n'.join(lines)

    return [TextContent(type='text', text=text)]


async def run_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


async def run_http(host: str, port: int) -> None:
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Mount

    session_manager = StreamableHTTPSessionManager(
        app=app,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    async def lifespan(app_: Any):
        async with session_manager.run():
            yield

    starlette_app = Starlette(
        routes=[
            Mount('/mcp', app=session_manager.handle_request),
        ],
        lifespan=lifespan,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=['*'],
                allow_methods=['*'],
                allow_headers=['*'],
            ),
        ],
    )

    import uvicorn

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level='info',
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser(description='MCP syntax validator server')
    parser.add_argument(
        '--transport', '-t',
        choices=['stdio', 'http'],
        default='stdio',
        help='Transport mode (default: stdio)',
    )
    parser.add_argument('--host', default='127.0.0.1', help='HTTP host (default: 127.0.0.1)')
    parser.add_argument('--port', '-p', type=int, default=3000, help='HTTP port (default: 3000)')
    args = parser.parse_args()

    if args.transport == 'stdio':
        asyncio.run(run_stdio())
    else:
        asyncio.run(run_http(args.host, args.port))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.9.0",
#   "esprima>=4.0.1",
#   "javalang>=0.13.0",
#   "mermaid-py>=0.5.0",
#   "tinycss2>=1.3.0",
#   "tree-sitter>=0.24.0",
#   "tree-sitter-language-pack>=0.7.0; sys_platform != 'android'",
# ]
# ///

import argparse
import asyncio
import json
import logging
import subprocess
import traceback
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

app = Server('syntax-validator')


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


def validate_css(code: str) -> dict[str, Any]:
    import tinycss2
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


def validate_java(code: str) -> dict[str, Any]:
    import javalang

    def _parse(source: str) -> tuple[bool, list[dict]]:
        try:
            javalang.parse.parse(source)
            return True, []
        except javalang.parser.JavaSyntaxError as e:
            token = getattr(e, 'at', None)
            line = getattr(token, 'position', (None,))[0] if token else None
            col = getattr(token, 'position', (None, None))[1] if token else None
            msg = str(e).strip() or (
                f"Syntax error at token '{token.value}'" if token else 'Unknown syntax error'
            )
            return False, [{'line': line, 'column': col, 'message': msg}]
        except Exception as e:
            return False, [{'line': None, 'column': None, 'message': str(e)}]

    success, errors = _parse(code)
    if success:
        return {'valid': True, 'errors': []}
    wrapped = 'public class _Wrapper_ {\n' + code + '\n}'
    if _parse(wrapped)[0]:
        return {'valid': True, 'errors': []}
    wrapped_method = 'public class _Wrapper_ {\n  public void _wrapper_() {\n' + code + '\n  }\n}'
    if _parse(wrapped_method)[0]:
        return {'valid': True, 'errors': []}
    return {'valid': False, 'errors': errors}


def validate_javascript(code: str) -> dict[str, Any]:
    import esprima
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


def validate_mermaid(code: str) -> dict[str, Any]:
    import mermaid
    import requests

    try:
        mermaid.Mermaid(code.rstrip() + '\n')
        return {'valid': True, 'errors': []}
    except (ImportError, OSError, requests.exceptions.RequestException):
        return _tree_sitter_validate('mermaid', code)
    except Exception as exc:
        return {
            'valid': False,
            'errors': [{'line': None, 'column': None, 'message': str(exc)}],
        }


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


def _tree_sitter_validate(language_name: str, code: str) -> dict[str, Any]:
    import tree_sitter
    import tree_sitter_language_pack

    language = tree_sitter_language_pack.get_language(language_name)
    parser = tree_sitter.Parser(language)
    tree = parser.parse((code.rstrip() + '\n').encode('utf-8'))

    errors = []
    _collect_tree_sitter_errors(tree.root_node, errors)

    if errors:
        return {'valid': False, 'errors': errors}
    return {'valid': True, 'errors': []}


def _collect_tree_sitter_errors(node: Any, errors: list) -> None:
    if node.type == 'ERROR' or node.is_missing:
        for child in node.children:
            if child.type == 'ERROR' or child.is_missing:
                return
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
        return
    for child in node.children:
        _collect_tree_sitter_errors(child, errors)


def _populate_tree_sitter_validators(validators: dict[str, Any]) -> None:
    try:
        import typing

        import tree_sitter_language_pack

        for lang in typing.get_args(tree_sitter_language_pack.SupportedLanguage):
            if lang not in validators:
                validators[lang] = lambda code, l=lang: _tree_sitter_validate(l, code)
    except Exception:
        pass


VALIDATORS = {
    'bash': validate_bash,
    'css': validate_css,
    'java': validate_java,
    'javascript': validate_javascript,
    'mermaid': validate_mermaid,
    'python': validate_python,
}

REPORTED_LANGUAGES = set(VALIDATORS)
_populate_tree_sitter_validators(VALIDATORS)
SUPPORTED_LANGUAGES = sorted(VALIDATORS)
REPORTED_LANGUAGES = sorted(REPORTED_LANGUAGES | (set(SUPPORTED_LANGUAGES) & {
    'typescript', 'cmake', 'c', 'cpp', 'csv', 'dockerfile', 'json',
    'rst', 'markdown', 'yaml', 'html'}))


VALIDATE_OUTPUT_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'properties': {
        'valid': {
            'type': 'boolean',
            'description': 'Whether the syntax is valid.',
        },
        'errors': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'line': {
                        'type': ['integer', 'null'],
                        'description': 'Line number of the error, or null if unknown.',
                    },
                    'column': {
                        'type': ['integer', 'null'],
                        'description': 'Column number of the error, or null if unknown.',
                    },
                    'message': {
                        'type': 'string',
                        'description': 'Description of the error.',
                    },
                },
                'required': ['line', 'column', 'message'],
                'additionalProperties': False,
            },
            'description': 'List of syntax errors found (empty if valid). Truncated to 5 entries.',
        },
    },
    'required': ['valid', 'errors'],
    'additionalProperties': False,
}


def tool_result(content: dict[str, Any]) -> CallToolResult:
    """Return a CallToolResult with both structuredContent and a TextContent fallback."""
    logger.info('  Result: %s', repr(content)[:60])
    text = json.dumps(content)
    logger.debug('  Full result\n%s', text)
    return CallToolResult(
        content=[TextContent(type='text', text=text)],
        structuredContent=content,
        isError=not content.get('valid', True) if 'valid' in content else False,
    )


@app.list_tools()
async def list_tools() -> list[Tool]:
    logger.info('Listing tools')
    return [
        Tool(
            name='list_supported_languages',
            description=(
                'List all languages supported by the validate_syntax tool. '
                'Use this if you are unsure whether a specific language is supported.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {},
            },
            outputSchema={
                'type': 'object',
                'properties': {
                    'languages': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'All supported language identifiers.',
                    },
                },
                'required': ['languages'],
                'additionalProperties': False,
            },
        ),
        Tool(
            name='validate_syntax',
            description=(
                'Validate the syntax of code in a given language. '
                f"Common languages include languages: {', '.join(REPORTED_LANGUAGES)}.  "
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'language': {
                        'type': 'string',
                        'description':
                            'The programming language of the code in '
                            'lowercase.  Common values: ' +
                            ', '.join(REPORTED_LANGUAGES) + '.',
                    },
                    'code': {
                        'type': 'string',
                        'description': 'The source code to validate.',
                    },
                },
                'required': ['language', 'code'],
            },
            outputSchema=VALIDATE_OUTPUT_SCHEMA,
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == 'list_supported_languages':
        logger.info(name)
        return tool_result({'languages': SUPPORTED_LANGUAGES})
    if name != 'validate_syntax':
        msg = f'Unknown tool: {name}'
        raise ValueError(msg)

    language = arguments.get('language', '').lower()
    code = arguments.get('code', '')
    logger.info('%s: %s - %d bytes', name, language, len(code))
    logger.info('  Starts with %s', repr(code)[:60])
    logger.debug(code)
    if code.startswith('```') and code.endswith('```'):
        code = code.split('\n', 1)[1].rstrip('`')

    if language not in VALIDATORS:
        logger.info('  Unknown langauge')
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
    return tool_result(result)


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
    parser.add_argument('--languages', action='store_true', help='Show known languages')
    parser.add_argument('--log', help='Append logs to the specified path')
    parser.add_argument('--verbose', '-v', action='count', default=0,
                        help='Increase verbosity')
    args = parser.parse_args()
    if args.log:
        handler = logging.FileHandler(args.log, mode='a')
        handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))

    if args.languages:
        for key in SUPPORTED_LANGUAGES:
            print(key)
    elif args.transport == 'stdio':
        asyncio.run(run_stdio())
    else:
        asyncio.run(run_http(args.host, args.port))


if __name__ == '__main__':
    main()

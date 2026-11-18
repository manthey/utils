#!/usr/bin/env python3
# /// script
# requires-python = '>=3.11'
# dependencies = []
# ///
"""Approval proxy for the agent sandboxes (agent_docker.py and friends).

serve (the default command) runs an http proxy that the sandboxes use for all
of their network access.  Each destination (host and port) is checked against
a blacklist and a whitelist; anything on neither waits until it is answered
at this program's console:

  1 allow once   2 allow this session   3 add to whitelist
  4 deny once    5 deny this session    6 add to blacklist

A session lasts until the proxy exits.  The two lists are text files with one
rule per line; the proxy only ever appends to them, so remove or change rules
by editing them by hand (they are reread when they change).  Every request is
recorded, with the bytes sent and received, in a json-lines log.

https is tunneled (CONNECT) without being decrypted, so for https the proxy
sees and decides on the host and port only.

relay is a plain tcp forwarder, used by agent_docker.py inside a helper
container to carry a sandbox's connections from its isolated network to the
proxy on the host.
"""

import argparse
import asyncio
import fnmatch
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_PORT = 3128
HEAD_LIMIT = 65536
HEAD_TIMEOUT = 30
CONNECT_TIMEOUT = 20
LINGER_TIMEOUT = 120
LOG_BYTES = 10 * 1024 * 1024
LOG_FILES = 5
ACTIVE = set()
LABEL_PREFIX = b'AGENT '
ANSWERS = {
    '1': (True, 'once'),
    '2': (True, 'session'),
    '3': (True, 'list'),
    '4': (False, 'once'),
    '5': (False, 'session'),
    '6': (False, 'list'),
}
MENU = ('  1 allow once   2 allow this session   3 add to whitelist\n'
        '  4 deny once    5 deny this session    6 add to blacklist\n'
        '  (3 and 6 can be followed by a rule, such as: 3 *.example.com)')
RULE_HELP = """# One rule per line.  The proxy only appends to this file; edit it by hand to
# remove or change rules (it is reread when it changes).  The blacklist wins
# over the whitelist.
#   example.com        that host, ports 80 and 443
#   example.com:8080   that host, that port only
#   example.com:*      that host, any port
#   *.example.com      any subdomain, but not example.com itself
#   .example.com       example.com and any subdomain
#   10.1.2.3:5000      an address; ipv6 addresses as [::1]:5000
"""


def split_host_port(text):
    """Split host, host:port, [ipv6] or [ipv6]:port into a lowercase host and
    a port string (None when there is no port).
    """
    text = text.strip()
    if text.startswith('['):
        host, _, rest = text[1:].partition(']')
        port = rest[1:] if rest.startswith(':') else ''
    elif ':' in text:
        host, _, port = text.rpartition(':')
    else:
        host, port = text, ''
    return host.lower().rstrip('.'), port or None


def join_host_port(host, port):
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


def is_address(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def printable(text, limit=200):
    """Make text from a sandbox safe to show on the console."""
    text = ''.join(c if ' ' <= c <= '~' else '?' for c in text)
    return text if len(text) <= limit else text[:limit] + '...'


class Rule:
    def __init__(self, text):
        self.text = text
        self.host, self.port = split_host_port(text)

    def matches(self, host, port):
        if self.port is None:
            if port not in (80, 443):
                return False
        elif self.port not in ('*', str(port)):
            return False
        if self.host.startswith('.'):
            return host == self.host[1:] or host.endswith(self.host)
        return fnmatch.fnmatchcase(host, self.host)


class RuleFile:
    """A hand-editable list of rules that is reread when the file changes."""

    def __init__(self, path, title):
        self.path = path
        self.stamp = None
        self.rules = []
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w') as fptr:
                fptr.write(f'# agent_proxy {title}\n{RULE_HELP}')

    def load(self):
        try:
            stat = os.stat(self.path)
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self.stamp:
                return
            with open(self.path) as fptr:
                lines = fptr.read().splitlines()
        except OSError:
            stamp, lines = None, []
        self.stamp = stamp
        self.rules = [Rule(line.split('#')[0].strip()) for line in lines]
        self.rules = [rule for rule in self.rules if rule.host]

    def match(self, host, port):
        """Return the text of the first rule that matches, or None."""
        self.load()
        for rule in self.rules:
            if rule.matches(host, port):
                return rule.text
        return None

    def add(self, text, note):
        with open(self.path, 'a+') as fptr:
            fptr.seek(0, os.SEEK_END)
            if fptr.tell():
                fptr.seek(fptr.tell() - 1)
                if fptr.read(1) != '\n':
                    fptr.write('\n')
            fptr.write(f'{text}  # added {time.strftime("%Y-%m-%d %H:%M")} {note}\n')


class Request:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info('peername')
        self.peer = peer[0] if peer else '?'
        self.label = ''
        self.method = ''
        self.host = ''
        self.port = 0
        self.url = ''
        self.head = b''
        self.allowed = None
        self.reason = ''
        self.error = ''
        self.counts = [0, 0]
        self.start = time.time()

    @property
    def client(self):
        return self.label or self.peer

    @property
    def what(self):
        if self.url:
            return f'{self.method} {printable(self.url)}'
        return f'{self.method} {join_host_port(self.host, self.port)}'


class Pending:
    def __init__(self, key, host, port, what, client):
        self.key = key
        self.host = host
        self.port = port
        self.what = what
        self.client = client
        self.readers = set()
        self.future = asyncio.get_running_loop().create_future()


class ProxyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


async def splice(reader, writer, counts, index):
    """Copy one direction of a connection until it ends."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
            counts[index] += len(data)
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, RuntimeError):
        writer.close()


async def splice_both(reader, writer, up_reader, up_writer, counts):
    """Copy both directions of a connection.  When one direction ends, the
    other carries on, since a client can finish sending while it still waits
    for the reply, until it ends too or nothing moves for LINGER_TIMEOUT.
    """
    # asyncio only keeps weak references to tasks.  Once a client has
    # finished sending, nothing else refers to this one, and without a
    # reference it is destroyed while the reply is still pending.
    task = asyncio.current_task()
    ACTIVE.add(task)
    pending = {
        asyncio.create_task(splice(reader, up_writer, counts, 0)),
        asyncio.create_task(splice(up_reader, writer, counts, 1))}
    try:
        _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        moved = None
        while pending and moved != sum(counts):
            moved = sum(counts)
            _, pending = await asyncio.wait(pending, timeout=LINGER_TIMEOUT)
    finally:
        for other in pending:
            other.cancel()
        ACTIVE.discard(task)
        up_writer.close()


class Proxy:
    def __init__(self, args):
        self.white = RuleFile(args.whitelist, 'whitelist: destinations allowed without asking.')
        self.black = RuleFile(args.blacklist, 'blacklist: destinations refused without asking.')
        self.aliases = {name.lower() for name in args.host_alias}
        self.prompting = args.prompt
        self.session = {}
        self.credits = set()
        self.pending = {}
        self.queue = asyncio.Queue()
        self.lines = asyncio.Queue()
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        # The request log is rotated: the current file and LOG_FILES - 1
        # older ones (proxy.log.1 is the most recent of those).
        handler = logging.handlers.RotatingFileHandler(
            args.log, maxBytes=LOG_BYTES, backupCount=LOG_FILES - 1)
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.log = logging.getLogger(__name__ + '.requests')
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.log.addHandler(handler)

    def known(self, host, port, key):
        """Return (allowed, reason) if this destination needs no question."""
        rule = self.black.match(host, port)
        if rule:
            return False, f'blacklist rule {rule}'
        rule = self.white.match(host, port)
        if rule:
            return True, f'whitelist rule {rule}'
        if key in self.session:
            return self.session[key], self.session_reason(self.session[key])
        if key in self.credits:
            self.credits.discard(key)
            return True, 'allowed once'
        return None

    @staticmethod
    def session_reason(allow):
        return f'{"allowed" if allow else "denied"} for this session'

    async def decide(self, req, host, port, what):
        key = join_host_port(host, port)
        verdict = self.known(host, port, key)
        if verdict:
            return verdict
        if not self.prompting:
            return False, 'not on the whitelist and nobody to ask'
        pending = self.pending.get(key)
        if pending is None:
            pending = self.pending[key] = Pending(key, host, port, what, req.client)
            self.queue.put_nowait(pending)
        pending.readers.add(req.reader)
        try:
            return await asyncio.shield(pending.future)
        finally:
            pending.readers.discard(req.reader)

    def stdin_closed(self):
        """Discard anything typed before a question was shown, so that it
        cannot answer that question, and report whether stdin has ended.
        """
        while not self.lines.empty():
            if self.lines.get_nowait() is None:
                self.prompting = False
        return not self.prompting

    def listed(self, pending):
        """A destination can be put on a list by hand while it waits."""
        rule = self.black.match(pending.host, pending.port)
        if rule:
            return False, f'blacklist rule {rule}'
        rule = self.white.match(pending.host, pending.port)
        if rule:
            return True, f'whitelist rule {rule}'
        return None

    def answer(self, pending, line):
        """Apply a typed answer.  Returns (allowed, reason), or None if the
        answer was not understood.
        """
        parts = line.split(None, 1)
        if not parts or parts[0] not in ANSWERS:
            print('Answer with a number from 1 to 6.')
            return None
        allow, scope = ANSWERS[parts[0]]
        if len(parts) > 1 and scope != 'list':
            print('Only 3 and 6 take a rule.')
            return None
        if scope == 'list':
            rule = pending.key if pending.port not in (80, 443) else pending.host
            rule = parts[1].split('#')[0].strip() if len(parts) > 1 else rule
            if not Rule(rule).matches(pending.host, pending.port):
                print(f'The rule {rule} does not match {pending.key}; nothing was added.')
                return None
            (self.white if allow else self.black).add(rule, f'for {pending.client}')
            return allow, f'{"whitelist" if allow else "blacklist"} rule {rule}'
        if scope == 'session':
            self.session[pending.key] = allow
            return allow, self.session_reason(allow)
        if allow and all(reader.at_eof() for reader in pending.readers):
            # Nothing is waiting any more (the client gave up), so allow the
            # next request instead.
            self.credits.add(pending.key)
            return False, 'no longer waiting; the next request is allowed once'
        return allow, 'allowed once' if allow else 'denied once'

    async def prompter(self):
        """Ask about one waiting destination at a time."""
        while True:
            pending = await self.queue.get()
            verdict = None
            while verdict is None:
                verdict = self.listed(pending)
                if verdict:
                    break
                if self.stdin_closed():
                    verdict = False, 'not on the whitelist and nobody to ask'
                    break
                more = f'   ({self.queue.qsize()} more waiting)' if self.queue.qsize() else ''
                print(f'\n[{pending.client}] {pending.what}{more}\n{MENU}\n> ', end='', flush=True)
                line = await self.lines.get()
                if line is None:
                    self.prompting = False
                    continue
                verdict = self.answer(pending, line)
            print(f'{pending.key}: {verdict[1]}')
            del self.pending[pending.key]
            pending.future.set_result(verdict)

    def read_stdin(self, loop):
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(self.lines.put_nowait, line.strip())
            loop.call_soon_threadsafe(self.lines.put_nowait, None)
        except (OSError, ValueError, RuntimeError):
            pass

    async def read_request(self, req):
        head = await asyncio.wait_for(req.reader.readuntil(b'\r\n\r\n'), HEAD_TIMEOUT)
        if head.startswith(LABEL_PREFIX):
            label, _, head = head.partition(b'\r\n')
            req.label = printable(label[len(LABEL_PREFIX):].decode('latin-1'), 60)
        lines = head.decode('latin-1').split('\r\n')
        parts = lines[0].split(' ')
        if len(parts) != 3:
            raise ProxyError(400, 'malformed request')
        req.method, target = parts[0], parts[1]
        headers = [line for line in lines[1:] if line]
        if req.method == 'CONNECT':
            req.host, port = split_host_port(target)
        else:
            url = urllib.parse.urlsplit(target)
            if url.scheme != 'http' or not url.hostname:
                raise ProxyError(400, 'this is an http proxy; use it with an absolute http '
                                 'url or with CONNECT')
            req.host, port = url.hostname.lower().rstrip('.'), str(url.port or 80)
            req.url = target
            path = (url.path or '/') + (f'?{url.query}' if url.query else '')
            req.head = self.forward_head(f'{req.method} {path} HTTP/1.1', headers)
        if not port or not port.isdigit() or not re.fullmatch(r'[a-z0-9._:%-]+', req.host):
            raise ProxyError(400, 'malformed destination')
        req.port = int(port)

    @staticmethod
    def forward_head(first, headers):
        """The request to send on: headers meant for the proxy are removed,
        and the connection is closed after one request so that each request
        is decided separately.
        """
        names = {line.split(':')[0].strip().lower() for line in headers}
        drop = {'proxy-connection', 'proxy-authorization'}
        if 'upgrade' not in names:
            drop |= {'connection', 'keep-alive'}
        kept = [line for line in headers if line.split(':')[0].strip().lower() not in drop]
        if 'upgrade' not in names:
            kept.append('Connection: close')
        return '\r\n'.join([first, *kept, '', '']).encode('latin-1')

    async def open_upstream(self, req):
        """Decide on a request and connect to its destination.  A name that
        resolves to an address that is not on the public internet (this
        machine, the local network) also needs that address to be allowed,
        so that an allowed name cannot be pointed at local services.
        """
        key = join_host_port(req.host, req.port)
        req.allowed, req.reason = await self.decide(req, req.host, req.port, req.what)
        if not req.allowed:
            raise ProxyError(403, f'{key} is denied ({req.reason})')
        if req.host in self.aliases:
            return await self.connect(req, ['127.0.0.1'])
        try:
            found = await asyncio.get_running_loop().getaddrinfo(
                req.host, req.port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ProxyError(502, f'cannot resolve {req.host}: {exc}')
        addresses = list(dict.fromkeys(entry[4][0] for entry in found))
        local = [addr for addr in addresses if not ipaddress.ip_address(addr).is_global]
        if not local or is_address(req.host):
            return await self.connect(req, addresses)
        error = None
        for address in sorted(local, key=lambda addr: ':' in addr):
            req.allowed, reason = await self.decide(
                req, address, req.port, f'{req.what} (which is the local address {address})')
            if not req.allowed:
                req.reason = f'{req.host} is the local address {address}: {reason}'
                raise ProxyError(403, f'{key} is denied ({req.reason})')
            try:
                return await self.connect(req, [address])
            except ProxyError as exc:
                error = exc
        raise error

    async def connect(self, req, addresses):
        error = None
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, req.port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                error = exc
        raise ProxyError(502, f'cannot connect to {join_host_port(req.host, req.port)}: '
                         f'{error or "no address"}')

    async def serve_one(self, req):
        await self.read_request(req)
        up_reader, up_writer = await self.open_upstream(req)
        if req.method == 'CONNECT':
            req.writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        else:
            up_writer.write(req.head)
            req.counts[0] += len(req.head)
        await splice_both(req.reader, req.writer, up_reader, up_writer, req.counts)

    async def handle(self, reader, writer):
        req = Request(reader, writer)
        try:
            await self.serve_one(req)
        except ProxyError as exc:
            req.error = exc.message
            body = f'agent_proxy: {exc.message}\n'.encode()
            writer.write(
                f'HTTP/1.1 {exc.status} agent_proxy\r\nContent-Type: text/plain\r\n'
                f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                OSError) as exc:
            req.error = f'{type(exc).__name__}: {exc}'
        finally:
            self.record(req)
            try:
                await writer.drain()
            except OSError:
                pass
            writer.close()

    def record(self, req):
        if not req.host:
            logger.debug('%s: no request (%s)', req.client, req.error)
            return
        entry = {
            'time': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(req.start)),
            'client': req.client,
            'method': req.method,
            'host': req.host,
            'port': req.port,
            'allowed': req.allowed,
            'reason': req.reason,
            'sent': req.counts[0],
            'received': req.counts[1],
            'seconds': round(time.time() - req.start, 3),
        }
        if req.url:
            entry['url'] = req.url
        if req.error and req.allowed:
            entry['error'] = req.error
        self.log.info(json.dumps(entry))
        if req.allowed is False:
            print(f'DENIED [{req.client}] {req.what}: {req.reason}')
        else:
            logger.info('[%s] %s: %s; sent %d, received %d', req.client, req.what,
                        req.error or req.reason, req.counts[0], req.counts[1])


def docker_bridge_address():
    """The address of the docker0 interface on linux, which is where docker
    containers reach this machine; None when there is no such interface.
    """
    try:
        import fcntl
        import struct

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack('256s', b'docker0'))
        return socket.inet_ntoa(packed[20:24])
    except (ImportError, OSError):
        return None


async def serve(args):
    proxy = Proxy(args)
    listen = args.listen
    if not listen:
        listen = ['127.0.0.1'] + [addr for addr in [docker_bridge_address()] if addr]
    servers = []
    for address in listen:
        try:
            servers.append(await asyncio.start_server(
                proxy.handle, address, args.port, limit=HEAD_LIMIT))
        except OSError as exc:
            print(f'Cannot listen on {join_host_port(address, args.port)}: {exc}')
    if not servers:
        msg = 'Not listening on any address'
        raise SystemExit(msg)
    shown = ', '.join(join_host_port(*sock.getsockname()[:2])
                      for server in servers for sock in server.sockets)
    print(f'agent_proxy listening on {shown}')
    print(f'whitelist {args.whitelist}\nblacklist {args.blacklist}\nlog       {args.log}')
    if proxy.prompting:
        threading.Thread(
            target=proxy.read_stdin, args=(asyncio.get_running_loop(),), daemon=True).start()
    else:
        print('Not asking: destinations that are not on the whitelist are denied.')
    await proxy.prompter()


def parse_forward(spec):
    """Parse a relay operand of the form PORT=HOST:PORT[@LABEL]."""
    try:
        listen, target = spec.split('=', 1)
        target, _, label = target.partition('@')
        host, port = split_host_port(target)
        return int(listen), host, int(port), label
    except (TypeError, ValueError):
        msg = f'relay operands are PORT=HOST:PORT[@LABEL], not {spec}'
        raise SystemExit(msg)


async def relay(specs):
    """Forward tcp ports.  A forward with a label announces the label to the
    proxy it connects to, which shows it as the name of the client.
    """
    async def forward(reader, writer, host, port, label):
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning('Cannot connect to %s: %s', join_host_port(host, port), exc)
            writer.close()
            return
        if label:
            up_writer.write(LABEL_PREFIX + label.encode() + b'\r\n')
        try:
            await splice_both(reader, writer, up_reader, up_writer, [0, 0])
        finally:
            writer.close()

    servers = []
    for spec in specs:
        listen, host, port, label = parse_forward(spec)

        def handler(reader, writer, host=host, port=port, label=label):
            return forward(reader, writer, host, port, label)

        servers.append(await asyncio.start_server(handler, None, listen))
        logger.warning('Forwarding port %d to %s', listen, join_host_port(host, port))
    if not servers:
        msg = 'relay needs at least one PORT=HOST:PORT[@LABEL] operand'
        raise SystemExit(msg)
    await asyncio.gather(*(server.serve_forever() for server in servers))


def main():
    base = os.path.join(os.path.expanduser('~'), '.agent_proxy')
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        'command', nargs='?', default='serve', choices=['serve', 'relay'],
        help='Command.  serve (the default) runs the approval proxy.  relay '
        'forwards tcp ports.')
    parser.add_argument(
        'operands', nargs='*', metavar='operand',
        help='For relay: one or more forwards of the form PORT=HOST:PORT, '
        'which listen on PORT and connect to HOST:PORT.  Adding @LABEL names '
        'the client to the proxy that the forward connects to.')
    parser.add_argument(
        '--port', type=int, default=DEFAULT_PORT,
        help='The port to listen on.  Default %(default)s.')
    parser.add_argument(
        '--listen', action='append', default=[],
        help='An address to listen on; repeat for several.  The default is '
        '127.0.0.1 and, on linux, the address of the docker0 interface if '
        'there is one (docker containers reach the host there).')
    parser.add_argument(
        '--dir', default=base,
        help='Directory for the whitelist, blacklist and log.  Default %(default)s.')
    parser.add_argument('--whitelist', help='Whitelist file.  Default <dir>/whitelist.txt.')
    parser.add_argument('--blacklist', help='Blacklist file.  Default <dir>/blacklist.txt.')
    parser.add_argument(
        '--log',
        help='Request log (json lines).  It is rotated at 10 MB, keeping 5 '
        'files.  Default <dir>/proxy.log.')
    parser.add_argument(
        '--host-alias', action='append', default=['host.docker.internal'],
        help='A name that sandboxes use for this machine; requests to it '
        'connect to 127.0.0.1.  Repeat for several.  Default %(default)s.')
    parser.add_argument(
        '--prompt', action='store_true', default=True,
        help='Ask about destinations that are on neither list (default).')
    parser.add_argument(
        '--no-prompt', dest='prompt', action='store_false',
        help='Deny destinations that are not on the whitelist without asking.')
    parser.add_argument(
        '--verbose', '-v', action='count', default=0,
        help='Increase verbosity; -v shows each allowed request.')
    args = parser.parse_intermixed_args()
    sys.stdout.reconfigure(line_buffering=True)
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    logger.addHandler(logging.StreamHandler(sys.stderr))
    args.whitelist = args.whitelist or os.path.join(args.dir, 'whitelist.txt')
    args.blacklist = args.blacklist or os.path.join(args.dir, 'blacklist.txt')
    args.log = args.log or os.path.join(args.dir, 'proxy.log')
    try:
        if args.command == 'relay':
            asyncio.run(relay(args.operands))
        else:
            asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

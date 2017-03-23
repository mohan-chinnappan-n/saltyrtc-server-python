import asyncio
import os
import socket
import ssl
import struct
import subprocess
import sys
from contextlib import closing

import libnacl.public
import logbook
import pytest
import umsgpack
import websockets

from saltyrtc.server import (
    NONCE_FORMATTER,
    NONCE_LENGTH,
    SubProtocol,
    serve,
    util,
)


class CalledProcessError(subprocess.CalledProcessError):
    def __str__(self):
        return "Command '{}' returned non-zero exit status {}:\n{}".format(
            self.cmd, self.returncode, self.output)


def pytest_addoption(parser):
    help_ = 'loop: Use a different event loop, supported: asyncio, uvloop'
    parser.addoption("--loop", action="store", help=help_)


def pytest_report_header(config):
    return 'Using event loop: {}'.format(default_event_loop(config=config))


def pytest_namespace():
    try:
        # noinspection PyPackageRequirements,PyUnresolvedReferences
        import uvloop  # noqa
        have_uvloop = True
    except ImportError:
        have_uvloop = False
    saltyrtc = {
        'have_uvloop': pytest.mark.skipif(not have_uvloop, reason='requires uvloop'),
        'no_uvloop': pytest.mark.skipif(
            have_uvloop, reason='requires uvloop to be not installed'),
        'ip': '127.0.0.1',
        'port': 8766,
        'external_server': False,
        'cli_path': os.path.join(sys.exec_prefix, 'bin', 'saltyrtc-server'),
        'cert': os.path.normpath(
            os.path.join(os.path.abspath(__file__), os.pardir, 'cert.pem')),
        'dh_params': os.path.normpath(
            os.path.join(os.path.abspath(__file__), os.pardir, 'dh2048.pem')),
        'permanent_key_primary': os.path.normpath(
            os.path.join(os.path.abspath(__file__), os.pardir, 'permanent.key')),
        'permanent_key_secondary':
            'b452e8a5abf54c5258db323b88d03cb9e002a4a84ba6f37715678901c20411c7',
        'subprotocols': [
            SubProtocol.saltyrtc_v1.value
        ],
        'timeout': 1.0,
        'run_long_tests': False,
    }
    saltyrtc['have_internal'] = pytest.mark.skipif(
        saltyrtc['external_server'] is None,
        reason='requires the usage of the internal server')
    saltyrtc['long_test'] = pytest.mark.skipif(
        not saltyrtc['run_long_tests'],
        reason='requires the usage of the internal server')
    return {'saltyrtc': saltyrtc}


def default_event_loop(request=None, config=None):
    if request is not None:
        config = request.config
    loop = config.getoption("--loop")
    if loop == 'uvloop':
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    else:
        loop = 'asyncio'
    return loop


def unused_tcp_port():
    """
    Find an unused localhost TCP port from 1024-65535 and return it.
    """
    if pytest.saltyrtc.external_server:
        return pytest.saltyrtc.port
    with closing(socket.socket()) as sock:
        sock.bind((pytest.saltyrtc.ip, 0))
        return sock.getsockname()[1]


def url(host, port):
    """
    Return the URL where the server can be reached.
    """
    return 'wss://{}:{}'.format(host, port)


def key_pair():
    """
    Return a NaCl key pair.
    """
    return libnacl.public.SecretKey()


def key_path(key_pair):
    """
    Return the hexadecimal key path from a key pair using the public
    key.

    Arguments:
        - `key_pair`: A :class:`libnacl.public.SecretKey` instance.
    """
    return key_pair.hex_pk().decode()


def random_cookie():
    """
    Return a random cookie for the client.
    """
    return os.urandom(16)


def _get_timeout(timeout=None):
    """
    Return the defined timeout. In case 'debug' has been activated,
    the timeout will be multiplied by 10.
    """
    if timeout is None:
        timeout = pytest.saltyrtc.timeout
    return timeout


@asyncio.coroutine
def _sleep(timeout=None):
    """
    Sleep *timeout* seconds.
    """
    yield from asyncio.sleep(_get_timeout(timeout))


@pytest.fixture(scope='module')
def event_loop(request):
    """
    Create an instance of the requested event loop.
    """
    default_event_loop(request=request)

    # Close previous event loop
    policy = asyncio.get_event_loop_policy()
    policy.get_event_loop().close()

    # Create new event loop
    _event_loop = policy.new_event_loop()
    policy.set_event_loop(_event_loop)

    def fin():
        _event_loop.close()

    # Add finaliser and return new event loop
    request.addfinalizer(fin)
    return _event_loop


@pytest.fixture(scope='module')
def timeout_factory():
    return _get_timeout


@pytest.fixture(scope='module')
def server_permanent_keys():
    """
    Return the server's permanent test NaCl key pairs.
    """
    return [
        util.load_permanent_key(pytest.saltyrtc.permanent_key_primary),
        util.load_permanent_key(pytest.saltyrtc.permanent_key_secondary)
    ]


@pytest.fixture(scope='module')
def server_key():
    """
    Return a server NaCl key pair to be used by the server only.
    """
    return key_pair()


@pytest.fixture(scope='module')
def initiator_key():
    """
    Return a client NaCl key pair to be used by the initiator only.
    """
    return key_pair()


@pytest.fixture(scope='module')
def responder_key():
    """
    Return a client NaCl key pair to be used by the responder only.
    """
    return key_pair()


@pytest.fixture(scope='module')
def sleep():
    """
    Sleep *timeout* seconds.
    """
    return _sleep


@pytest.fixture(scope='module')
def cookie_factory():
    """
    A cookie factory for random cookies.
    """
    return random_cookie


@pytest.fixture(scope='module')
def url_factory(server):
    """
    Return the URL where the server can be reached.
    """
    server_ = server

    def _url_factory(server=None):
        if server is None:
            server = server_
        return url(*server.address)
    return _url_factory


@pytest.fixture(scope='module')
def server_factory(request, event_loop, server_permanent_keys):
    """
    Return a factory to create :class:`saltyrtc.Server` instances.
    """
    # Enable asyncio debug logging
    os.environ['PYTHONASYNCIODEBUG'] = '1'

    # Enable logging
    util.enable_logging(level=logbook.NOTICE, redirect_loggers={
        'asyncio': logbook.WARNING,
        'websockets': logbook.WARNING,
    })

    # Push handler
    logging_handler = logbook.StderrHandler()
    logging_handler.push_application()

    _server_instances = []

    def _server_factory(permanent_keys=None):
        if permanent_keys is None:
            permanent_keys = server_permanent_keys

        # Setup server
        port = unused_tcp_port()
        coroutine = serve(
            util.create_ssl_context(
                pytest.saltyrtc.cert, dh_params_file=pytest.saltyrtc.dh_params),
            permanent_keys,
            host=pytest.saltyrtc.ip,
            port=port,
            loop=event_loop
        )
        server_ = event_loop.run_until_complete(coroutine)
        # Inject address (little bit of a hack but meh...)
        server_.address = (pytest.saltyrtc.ip, port)

        _server_instances.append(server_)

        def fin():
            server_.close()
            event_loop.run_until_complete(server_.wait_closed())
            _server_instances.remove(server_)
            if len(_server_instances) == 0:
                logging_handler.pop_application()

        request.addfinalizer(fin)
        return server_
    return _server_factory


class ExternalServer:
    @property
    def address(self):
        return pytest.saltyrtc.ip, pytest.saltyrtc.port


@pytest.fixture(scope='module')
def server(server_factory):
    """
    Return a :class:`saltyrtc.Server` instance.
    """
    if pytest.saltyrtc.external_server:
        return ExternalServer()
    else:
        return server_factory()


@pytest.fixture(scope='module')
def server_no_key(server_factory):
    """
    Return a :class:`saltyrtc.Server` instance that has no permanent
    key pair.
    """
    return server_factory(permanent_keys=[])


class _DefaultBox:
    pass


class Client:
    def __init__(self, ws_client, pack_message, unpack_message, timeout=None):
        self.ws_client = ws_client
        self.pack_and_send = pack_message
        self.recv_and_unpack = unpack_message
        self.timeout = _get_timeout(timeout)
        self.session_key = None
        self.box = None

    def send(self, nonce, message, box=_DefaultBox, timeout=None, pack=True):
        if timeout is None:
            timeout = self.timeout
        return (yield from self.pack_and_send(
            self.ws_client, nonce, message,
            box=self.box if box == _DefaultBox else box, timeout=timeout, pack=pack
        ))

    def recv(self, box=_DefaultBox, timeout=None):
        if timeout is None:
            timeout = self.timeout
        return (yield from self.recv_and_unpack(
            self.ws_client,
            box=self.box if box == _DefaultBox else box, timeout=timeout
        ))

    def close(self):
        return self.ws_client.close()


@pytest.fixture(scope='module')
def ws_client_factory(initiator_key, event_loop, server):
    """
    Return a simplified :class:`websockets.client.connect` wrapper
    where no parameters are required.
    """
    # Note: The `server` argument is only required to fire up the server.
    server_ = server

    # Create SSL context
    ssl_context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=pytest.saltyrtc.cert)
    ssl_context.load_dh_params(pytest.saltyrtc.dh_params)

    def _ws_client_factory(server=None, path=None, **kwargs):
        if server is None:
            server = server_
        if path is None:
            path = '{}/{}'.format(url(*server.address), key_path(initiator_key))
        _kwargs = {
            'subprotocols': pytest.saltyrtc.subprotocols,
            'ssl': ssl_context,
            'loop': event_loop,
        }
        _kwargs.update(kwargs)
        return websockets.connect(path, **_kwargs)
    return _ws_client_factory


@pytest.fixture(scope='module')
def client_factory(
        initiator_key, event_loop, server, server_permanent_keys, responder_key,
        pack_nonce, pack_message, unpack_message
):
    """
    Return a simplified :class:`websockets.client.connect` wrapper
    where no parameters are required.
    """
    # Note: The `server` argument is only required to fire up the server.
    server_ = server

    # Create SSL context
    ssl_context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=pytest.saltyrtc.cert)
    ssl_context.load_dh_params(pytest.saltyrtc.dh_params)

    @asyncio.coroutine
    def _client_factory(
            server=None, ws_client=None,
            path=initiator_key, timeout=None, csn=None, cookie=None, permanent_key=None,
            ping_interval=None, explicit_permanent_key=False,
            initiator_handshake=False, responder_handshake=False,
            **kwargs
    ):
        if server is None:
            server = server_
        if cookie is None:
            cookie = random_cookie()
        if permanent_key is None:
            permanent_key = server_permanent_keys[0].pk
        _kwargs = {
            'subprotocols': pytest.saltyrtc.subprotocols,
            'ssl': ssl_context,
            'loop': event_loop,
        }
        _kwargs.update(kwargs)
        if ws_client is None:
            ws_client = yield from websockets.connect(
                '{}/{}'.format(url(*server.address), key_path(path)),
                **_kwargs
            )
        client = Client(
            ws_client, pack_message, unpack_message,
            timeout=timeout
        )
        nonces = {}

        if not initiator_handshake and not responder_handshake:
            return client

        # Do handshake
        key = initiator_key if initiator_handshake else responder_key

        # server-hello
        message, nonce, sck, s, d, start_scsn = yield from client.recv(timeout=timeout)
        ssk = message['key']
        nonces['server-hello'] = nonce

        cck, ccsn = cookie, 2 ** 32 - 1 if csn is None else csn
        if responder_handshake:
            # client-hello
            nonce = pack_nonce(cck, 0x00, 0x00, ccsn)
            nonces['client-hello'] = nonce
            yield from client.send(nonce, {
                'type': 'client-hello',
                'key': responder_key.pk,
            }, timeout=timeout)
            ccsn += 1

        # client-auth
        client.box = libnacl.public.Box(sk=key, pk=ssk)
        nonce = pack_nonce(cck, 0x00, 0x00, ccsn)
        nonces['client-auth'] = nonce
        payload = {
            'type': 'client-auth',
            'your_cookie': sck,
            'subprotocols': pytest.saltyrtc.subprotocols,
        }
        if ping_interval is not None:
            payload['ping_interval'] = ping_interval
        if explicit_permanent_key is not None:
            payload['your_key'] = permanent_key
        yield from client.send(nonce, payload, timeout=timeout)
        ccsn += 1

        # server-auth
        client.sign_box = libnacl.public.Box(sk=key, pk=permanent_key)
        message, nonce, ck, s, d, scsn = yield from client.recv(timeout=timeout)
        nonces['server-auth'] = nonce

        # Return client and additional data
        additional_data = {
            'id': d,
            'sck': sck,
            'start_scsn': start_scsn,
            'cck': cck,
            'ccsn': ccsn,
            'ssk': ssk,
            'nonces': nonces,
            'signed_keys': message['signed_keys']
        }
        if initiator_handshake:
            additional_data['responders'] = message['responders']
        else:
            additional_data['initiator_connected'] = message['initiator_connected']
        return client, additional_data
    return _client_factory


@pytest.fixture(scope='module')
def unpack_message(event_loop):
    @asyncio.coroutine
    def _unpack_message(client, box=None, timeout=None):
        timeout = _get_timeout(timeout)
        data = yield from asyncio.wait_for(client.recv(), timeout, loop=event_loop)
        nonce = data[:NONCE_LENGTH]
        (cookie,
         source, destination,
         combined_sequence_number) = struct.unpack(NONCE_FORMATTER, nonce)
        combined_sequence_number, *_ = struct.unpack(
            '!Q', b'\x00\x00' + combined_sequence_number)
        data = data[NONCE_LENGTH:]
        if box is not None:
            data = box.decrypt(data, nonce=nonce)
        else:
            nonce = None
        message = umsgpack.unpackb(data)
        return (
            message,
            nonce,
            cookie,
            source, destination,
            combined_sequence_number
        )
    return _unpack_message


@pytest.fixture(scope='module')
def pack_nonce():
    def _pack_nonce(cookie, source, destination, combined_sequence_number):
        return struct.pack(
            NONCE_FORMATTER,
            cookie,
            source, destination,
            struct.pack('!Q', combined_sequence_number)[2:]
        )
    return _pack_nonce


@pytest.fixture(scope='module')
def pack_message(event_loop):
    @asyncio.coroutine
    def _pack_message(client, nonce, message, box=None, timeout=None, pack=True):
        if pack:
            data = umsgpack.packb(message)
        else:
            data = message
        if box is not None:
            _, data = box.encrypt(data, nonce=nonce, pack_nonce=False)
        data = b''.join((nonce, data))
        timeout = _get_timeout(timeout)
        yield from asyncio.wait_for(client.send(data), timeout, loop=event_loop)
        return data
    return _pack_message


@pytest.fixture(scope='module')
def cli(event_loop):
    @asyncio.coroutine
    def _call_cli(*args, input=None, timeout=3.0, signal=None, env=None):
        # Prepare environment
        if env is None:
            env = os.environ.copy()

        # Call CLI in subprocess and get output
        parameters = [sys.executable, pytest.saltyrtc.cli_path] + list(args)
        if isinstance(input, str):
            input = input.encode('utf-8')

        # Create process
        create = asyncio.create_subprocess_exec(
            *parameters, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        process = yield from create

        # Wait for process to terminate
        task = event_loop.create_task(process.communicate(input=input))
        maybe_shielded_task = task
        if signal is not None:
            maybe_shielded_task = asyncio.shield(maybe_shielded_task, loop=event_loop)
        output = None
        timeout_exc = False
        try:
            output, _ = yield from asyncio.wait_for(
                maybe_shielded_task, timeout, loop=event_loop)
        except asyncio.TimeoutError:
            if signal is None:
                raise
            timeout_exc = True

        # Send signal (if requested)
        if timeout_exc and signal is not None:
            try:
                signals = list(signal)
            except TypeError:
                signals = [signal]
            length = len(signals)
            for i, signal in enumerate(signals):
                shielded_task = asyncio.shield(task, loop=event_loop)
                process.send_signal(signal)
                try:
                    output, _ = yield from asyncio.wait_for(
                        shielded_task, timeout, loop=event_loop)
                except asyncio.TimeoutError:
                    if i == (length - 1):
                        task.cancel()
                        raise

        # Process output
        output = output.decode('utf-8')

        # Strip leading empty lines and pydev debugger output
        rubbish = [
            'pydev debugger: process',
            'Traceback (most recent call last):',
        ]
        lines = []
        skip_following_empty_lines = True
        for line in output.splitlines(keepends=True):
            if any((line.startswith(s) for s in rubbish)):
                skip_following_empty_lines = True
            elif not skip_following_empty_lines or len(line.strip()) > 0:
                lines.append(line)
                skip_following_empty_lines = False

        # Strip trailing empty lines
        empty_lines_count = 0
        for line in reversed(lines):
            if len(line.strip()) > 0:
                break
            empty_lines_count += 1
        if empty_lines_count > 0:
            lines = lines[:-empty_lines_count]
        output = ''.join(lines)

        # Check return code
        if process.returncode != 0:
            raise CalledProcessError(
                process.returncode, parameters, output=output)
        return output
    return _call_cli


@pytest.fixture(scope='function')
def fake_logbook_env(tmpdir):
    tmpdir.join("logbook.py").write("raise ImportError('h3h3')")
    env = os.environ.copy()
    env['PYTHONPATH'] = ':'.join((str(tmpdir), env.get('PYTHONPATH', '')))
    return env

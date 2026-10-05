#-*- coding:utf-8 -*-
import sys, os
sys.path.append(os.path.realpath(os.path.dirname(__file__))+"/../..")

import shutil
import socket
import tempfile
import threading
import pytest
from autoremovetorrents import logger
from autoremovetorrents.compatibility.xmlrpc_ import xmlrpc_client

RPC_URL = 'http://rtorrent.test/RPC2'
RUTORRENT_URL = 'http://rtorrent.test/rutorrent'

class FakeRTorrent(object):
    """A tiny in-memory rTorrent which answers XML-RPC requests"""
    def __init__(self):
        self.torrents = {}
        self.calls = []        # Every method called (including those in system.multicall)
        self.erasedata = []    # Requests sent to ruTorrent's erasedata endpoint
        self.erasedata_status = 200
        self.erasedata_keeps = set() # Hashes erasedata refuses to erase
        self.methods = set(['d.multicall2', 'd.load_date', 'd.free_diskspace', 'system.api_version'])
        self.password = None
        self.introspection = True
        self.erasedata_plugin = 'ok' # ok, failed or missing
        self.getplugins_calls = 0

    def add(self, hash_, **kwargs):
        torrent = {
            'd.hash': hash_, 'd.name': 'Torrent', 'd.custom1': '', 'd.state': 1, 'd.is_active': 1,
            'd.is_open': 1, 'd.complete': 1, 'd.hashing': 0, 'd.message': '', 'd.size_bytes': 1000,
            'd.completed_bytes': 1000, 'd.ratio': 1500, 'd.up.total': 1500, 'd.down.total': 1000,
            'd.up.rate': 10, 'd.down.rate': 0, 'd.peers_complete': 2, 'd.peers_connected': 5,
            'd.timestamp.started': 1000, 'd.timestamp.finished': 2000, 'd.directory': '/downloads/Torrent',
            'd.load_date': 900, 'd.free_diskspace': 123456, 'd.base_path': '/downloads/Torrent',
            'd.is_multi_file': 1,
            'files': [['/downloads/Torrent/a.mkv', 'a.mkv']],
            'trackers': [['http://tracker1/announce', 1, 10, 20], ['dht://', 1, 99, 99]],
        }
        torrent.update(kwargs)
        self.torrents[hash_] = torrent
        return torrent

    def _torrent(self, hash_):
        for h in self.torrents:
            if h.upper() == hash_.upper():
                return self.torrents[h]
        raise xmlrpc_client.Fault(-501, 'Could not find info-hash.')

    def dispatch(self, method, params):
        self.calls.append((method, params))
        if method == 'system.methodExist':
            if not self.introspection:
                raise xmlrpc_client.Fault(-506, "Method '%s' not defined" % method)
            return params[0] in self.methods
        if method in ('d.multicall2', 'd.load_date', 'd.free_diskspace', 'system.api_version') \
            and method not in self.methods:
            raise xmlrpc_client.Fault(-506, "Method '%s' not defined" % method)
        if method in ('d.load_date', 'd.free_diskspace') and len(params) == 0:
            raise xmlrpc_client.Fault(-500, 'Unsupported target type found.')
        if method == 'system.multicall':
            results = []
            for call in params[0]:
                try:
                    results.append([self.dispatch(call['methodName'], call['params'])])
                except xmlrpc_client.Fault as f:
                    results.append({'faultCode': f.faultCode, 'faultString': f.faultString})
            return results
        if method == 'system.client_version':
            return '0.9.8'
        if method == 'system.library_version':
            return '0.13.8'
        if method == 'system.api_version':
            return 10
        if method.startswith('throttle.'):
            return 100
        if method in ('d.multicall2', 'd.multicall'):
            if method == 'd.multicall2':
                params = params[1:]
            fields = [c.rstrip('=') for c in params[1:]]
            return [[t[f] for f in fields] for t in self.torrents.values()]
        if method == 't.multicall':
            return [list(t) for t in self._torrent(params[0])['trackers']]
        if method == 'f.multicall':
            return [list(f) for f in self._torrent(params[0])['files']]
        if method in ('d.stop', 'd.close', 'd.delete_tied'):
            self._torrent(params[0])
            return 0
        if method == 'd.erase':
            t = self._torrent(params[0])
            del self.torrents[t['d.hash']]
            return 0
        if method.startswith('d.'):
            return self._torrent(params[0])[method]
        raise xmlrpc_client.Fault(-506, "Method '%s' not defined" % method)

    # requests_mock callbacks
    def rpc_callback(self, request, context):
        if self.password is not None and request.headers.get('Authorization') is None:
            context.status_code = 401
            return b''
        params, method = xmlrpc_client.loads(request.body)
        try:
            response = xmlrpc_client.dumps((self.dispatch(method, params),), methodresponse=True)
        except xmlrpc_client.Fault as f:
            response = xmlrpc_client.dumps(f, methodresponse=True)
        return response.encode('utf-8')

    def erasedata_callback(self, request, context):
        body = request.body if isinstance(request.body, str) else request.body.decode()
        self.erasedata.append(body)
        context.status_code = self.erasedata_status
        if self.erasedata_status == 200:
            for part in body.split('&'):
                key, value = part.split('=')
                if key == 'hash' and value not in self.erasedata_keeps:
                    self.dispatch('d.erase', [value])
        return '[]'

    def getplugins_callback(self, request, context):
        self.getplugins_calls += 1
        other = "(function(){var plugin=new rPlugin('edit',3.0,'Novik','...');plugin.loadLang();})();"
        if self.erasedata_plugin == 'missing':
            return other
        erasedata = "(function(){var plugin=new rPlugin('erasedata',5.1,'Novik','...',256,'');"
        if self.erasedata_plugin == 'ok':
            erasedata += "plugin.enableForceDeletion=0;plugin.replaceRemoveTorrent=0;plugin.loadLang();})();"
        else:
            erasedata += "plugin.disable(); noty('erasedata: '+theUILang.pluginCantStart,'error');})();"
        return other + erasedata + other

    def called(self, method):
        return [p for m, p in self.calls if m == method]

@pytest.fixture(scope='function')
def rtorrent(requests_mock, tmp_path):
    logger.Logger.init(log_path=str(tmp_path))
    fake = FakeRTorrent()
    requests_mock.post(RPC_URL, content=fake.rpc_callback)
    requests_mock.post(RUTORRENT_URL + '/plugins/erasedata/action.php', text=fake.erasedata_callback)
    requests_mock.get(RUTORRENT_URL + '/php/getplugins.php', text=fake.getplugins_callback)
    return fake


class FakeSCGIServer(object):
    """Serves the fake rTorrent over SCGI (a unix socket or TCP on 127.0.0.1)"""
    def __init__(self, fake, family):
        self.fake = fake
        self.requests = 0
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        if family == socket.AF_UNIX:
            # Short directory: unix socket paths are limited to ~100 characters
            self.dir = tempfile.mkdtemp(prefix='art')
            path = os.path.join(self.dir, 'scgi.sock')
            self.sock.bind(path)
            self.url = 'scgi://' + path
        else:
            self.dir = None
            self.sock.bind(('127.0.0.1', 0))
            self.url = 'scgi://127.0.0.1:%d' % self.sock.getsockname()[1]
        self.sock.listen(5)
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b''
                while b':' not in data:
                    data += conn.recv(4096)
                length, rest = data.split(b':', 1)
                length = int(length)
                while len(rest) < length + 1:
                    rest += conn.recv(4096)
                fields = rest[:length].split(b'\0')
                headers = dict(zip(fields[0::2], fields[1::2]))
                body = rest[length+1:]
                while len(body) < int(headers[b'CONTENT_LENGTH']):
                    body += conn.recv(4096)
                self.requests += 1
                params, method = xmlrpc_client.loads(body)
                try:
                    response = xmlrpc_client.dumps((self.fake.dispatch(method, params),), methodresponse=True)
                except xmlrpc_client.Fault as f:
                    response = xmlrpc_client.dumps(f, methodresponse=True)
                response = response.encode('utf-8')
                conn.sendall(b'Status: 200 OK\r\nContent-Type: text/xml\r\nContent-Length: %d\r\n\r\n' % len(response) + response)

    def close(self):
        self.sock.close()
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)

@pytest.fixture(params=['unix', 'tcp'])
def scgi_server(request, rtorrent):
    server = FakeSCGIServer(rtorrent, socket.AF_UNIX if request.param == 'unix' else socket.AF_INET)
    yield server
    server.close()

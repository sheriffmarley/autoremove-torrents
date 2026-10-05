#-*- coding:utf-8 -*-
# Integration tests against a real rTorrent + ruTorrent (crazymax/rtorrent-rutorrent).
#
# Required environment variables (the tests are skipped without them):
#   RTORRENT_HOST       XML-RPC URL, e.g. http://localhost:18000/RPC2
#   RTORRENT_USERNAME   HTTP basic auth of XML-RPC and ruTorrent
#   RTORRENT_PASSWORD
#   RUTORRENT_HOST      ruTorrent URL, e.g. http://localhost:18080
#   RTORRENT_DOWNLOADS  Local path of the directory mounted as /downloads in the container
# Optional:
#   RTORRENT_HTTPRPC_HOST  XML-RPC through ruTorrent's httprpc plugin, e.g.
#                          http://localhost:18080/plugins/httprpc/action.php
#   RTORRENT_SCGI_HOST     rTorrent's SCGI socket, e.g. scgi:///tmp/rtorrent/run/scgi.socket
#                          (the client is tested through every configured endpoint)
#
# Only fake torrents of random data are used, see faketorrents.py.
import os
import shutil
import time
import uuid
import pytest
from faketorrents import FakeTorrent, FAKE_TRACKER
from autoremovetorrents import logger
from autoremovetorrents.client.rtorrent import rTorrent
from autoremovetorrents.compatibility.xmlrpc_ import xmlrpc_client
from autoremovetorrents.exception.loginfailure import LoginFailure
from autoremovetorrents.torrentstatus import TorrentStatus
from autoremovetorrents.task import Task

ENV = ['RTORRENT_HOST', 'RTORRENT_USERNAME', 'RTORRENT_PASSWORD', 'RUTORRENT_HOST', 'RTORRENT_DOWNLOADS']
pytestmark = pytest.mark.skipif(not all(os.environ.get(k) for k in ENV),
    reason='rTorrent integration environment is not configured')

REMOTE_DOWNLOADS = '/downloads' # The mount point in the container

def env(key):
    return os.environ[key].strip()

class Environment(object):
    # endpoint: the environment variable holding the XML-RPC URL of the client under test.
    # The test torrents are always added through RTORRENT_HOST, because ruTorrent's proxy
    # doesn't allow adding raw torrents.
    def __init__(self, endpoint='RTORRENT_HOST'):
        logger.Logger.init()
        # A directory of its own for every test, as rTorrent sees it and as we see it
        self.run = 'autoremove-test-' + uuid.uuid4().hex[:8]
        self.remote_dir = REMOTE_DOWNLOADS + '/' + self.run
        self.local_dir = os.path.join(env('RTORRENT_DOWNLOADS'), self.run)
        os.makedirs(self.local_dir)
        os.chmod(self.local_dir, 0o777)
        self.endpoint = endpoint
        self.admin = self._login(rTorrent(env('RTORRENT_HOST')))
        self.client = self.new_client()
        self.torrents = []

    def _login(self, client):
        client.login(env('RTORRENT_USERNAME'), env('RTORRENT_PASSWORD'))
        return client

    def new_client(self, **options):
        client = rTorrent(env(self.endpoint), **options)
        client.login(env('RTORRENT_USERNAME'), env('RTORRENT_PASSWORD'))
        return client

    def add(self, name, files=None, subdir=''):
        local = os.path.join(self.local_dir, subdir) if subdir else self.local_dir
        remote = self.remote_dir + ('/' + subdir if subdir else '')
        torrent = FakeTorrent(local, name, files)
        for root, dirs, _ in os.walk(local):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o777)
        torrent.remote_base = remote + '/' + name
        self.admin._call('load.raw_start_verbose', '', xmlrpc_client.Binary(torrent.torrent),
            'd.directory.set="%s"' % remote, 'd.custom1.set=Test%20Label')
        self.torrents.append(torrent)
        # Wait until rTorrent has checked the data
        for _ in range(60):
            try:
                if self.admin._call('d.complete', torrent.hash) == 1:
                    return torrent
            except Exception:
                pass
            time.sleep(0.5)
        raise AssertionError('rTorrent did not finish checking %s' % name)

    def hashes(self):
        return set(h.upper() for h in self.admin._hashes())

    def cleanup(self):
        remaining = self.hashes()
        for torrent in self.torrents:
            if torrent.hash in remaining:
                self.admin._call('d.erase', torrent.hash)
        shutil.rmtree(self.local_dir, ignore_errors=True)

@pytest.fixture(params=['RTORRENT_HOST', 'RTORRENT_HTTPRPC_HOST', 'RTORRENT_SCGI_HOST'])
def rt(request):
    if not os.environ.get(request.param):
        pytest.skip('%s is not configured' % request.param)
    environment = Environment(request.param)
    yield environment
    environment.cleanup()

def wait_until(predicate, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(1)
    return False

@pytest.mark.parametrize('endpoint', ['RTORRENT_HOST', 'RTORRENT_HTTPRPC_HOST'])
def test_login_failure(endpoint):
    if not os.environ.get(endpoint):
        pytest.skip('%s is not configured' % endpoint)
    client = rTorrent(env(endpoint))
    with pytest.raises(LoginFailure):
        client.login(env('RTORRENT_USERNAME'), 'wrong password')

def test_properties(rt):
    single = rt.add('single.bin')
    multi = rt.add('Multi', ['a.bin', 'sub/b.bin'])
    client = rt.client
    assert 'rTorrent' in client.version()
    hashes = client.torrents_list()
    assert single.hash in hashes and multi.hash in hashes
    for torrent, size in ((single, 40000), (multi, 80000)):
        t = client.torrent_properties(torrent.hash)
        assert t.name == torrent.name
        assert t.size == size
        assert t.progress == 1.0
        assert t.status == TorrentStatus.Uploading
        assert t.category == ['Test Label']
        assert t.tracker == [FAKE_TRACKER]
        assert abs(time.time() - t.create_time) < 120
        assert 0 <= t.seeding_time < 120
        str(t)
    assert client.remote_free_space(rt.remote_dir) > 0
    str(client.client_status())

def test_remove_keeps_data(rt):
    torrent = rt.add('Keep', ['a.bin'])
    assert rt.client.remove_torrents([torrent.hash], False) == ([torrent.hash], [])
    assert torrent.hash not in rt.hashes()
    assert all(os.path.exists(f) for f in torrent.files)

def test_delete_local(rt):
    multi = rt.add('LocalMulti', ['a.bin', 'sub/b.bin'])
    single = rt.add('local-single.bin')
    foreign = os.path.join(multi.base, 'not-from-the-torrent.txt')
    with open(foreign, 'w') as f:
        f.write('keep me')
    client = rt.new_client(delete_mode='local', allowed_paths=[rt.remote_dir],
        path_mapping={rt.remote_dir: rt.local_dir})
    success, failed = client.remove_torrents([multi.hash, single.hash], True)
    assert failed == [] and sorted(success) == sorted([multi.hash, single.hash])
    assert not any(os.path.exists(f) for f in multi.files + single.files)
    assert os.listdir(multi.base) == ['not-from-the-torrent.txt'] # Foreign files are kept
    assert rt.hashes().isdisjoint([multi.hash, single.hash])

def test_delete_rutorrent(rt):
    multi = rt.add('RuMulti', ['a.bin', 'sub/b.bin'])
    single = rt.add('ru-single.bin')
    client = rt.new_client(delete_mode='rutorrent', rutorrent_url=env('RUTORRENT_HOST'), allowed_paths=[rt.remote_dir])
    success, failed = client.remove_torrents([multi.hash, single.hash], True)
    assert failed == [] and sorted(success) == sorted([multi.hash, single.hash])
    assert rt.hashes().isdisjoint([multi.hash, single.hash])
    # ruTorrent deletes the data in the background
    assert wait_until(lambda: not any(os.path.exists(f) for f in multi.files + single.files))
    assert wait_until(lambda: not os.path.exists(multi.base))

@pytest.mark.parametrize('mode', ['local', 'rutorrent'])
def test_refuse_outside_allowed_paths(rt, mode):
    torrent = rt.add('Outside', ['a.bin'])
    options = {'delete_mode': mode, 'allowed_paths': [rt.remote_dir + '/only-here']}
    if mode == 'local':
        options['path_mapping'] = {rt.remote_dir: rt.local_dir}
    else:
        options['rutorrent_url'] = env('RUTORRENT_HOST')
    success, failed = rt.new_client(**options).remove_torrents([torrent.hash], True)
    assert success == [] and failed[0]['reason'].startswith('Refused to delete data')
    assert torrent.hash in rt.hashes()
    time.sleep(2)
    assert all(os.path.exists(f) for f in torrent.files)

def test_local_refuses_symlink(rt):
    torrent = rt.add('Linked', ['sub/a.bin'])
    # Something outside of the torrent that must survive
    outside = os.path.join(rt.local_dir, 'precious')
    os.makedirs(outside)
    precious = os.path.join(outside, 'a.bin')
    with open(precious, 'w') as f:
        f.write('precious')
    # Replace the directory of the torrent with a symlink pointing outside
    shutil.rmtree(os.path.join(torrent.base, 'sub'))
    os.symlink(outside, os.path.join(torrent.base, 'sub'))
    client = rt.new_client(delete_mode='local', allowed_paths=[rt.remote_dir],
        path_mapping={rt.remote_dir: rt.local_dir})
    success, failed = client.remove_torrents([torrent.hash], True)
    assert success == [] and 'symlink' in failed[0]['reason']
    assert torrent.hash in rt.hashes()
    with open(precious) as f:
        assert f.read() == 'precious'

def test_task(rt):
    torrent = rt.add('TaskTorrent', ['a.bin'])
    task = Task('rtorrent-live', {
        'client': 'rtorrent',
        'host': env(rt.endpoint),
        'username': env('RTORRENT_USERNAME'),
        'password': env('RTORRENT_PASSWORD'),
        'delete_data': True,
        'client_options': {
            'delete_mode': 'local',
            'allowed_paths': [rt.remote_dir],
            'path_mapping': {rt.remote_dir: rt.local_dir},
        },
        'strategies': {'remove-test-label': {'categories': ['Test Label'], 'ratio': -1}},
    })
    task.execute()
    assert torrent.hash in [t.hash for t in task.get_removed_torrents()]
    assert torrent.hash not in rt.hashes()
    assert not any(os.path.exists(f) for f in torrent.files)

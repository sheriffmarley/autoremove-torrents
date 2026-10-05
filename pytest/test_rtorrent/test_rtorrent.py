#-*- coding:utf-8 -*-
import os
import time
import pytest
from autoremovetorrents.client.rtorrent import rTorrent
from autoremovetorrents.task import Task
from autoremovetorrents.torrentstatus import TorrentStatus
from autoremovetorrents.exception.connectionfailure import ConnectionFailure
from autoremovetorrents.exception.invalidconfiguration import InvalidConfiguration
from autoremovetorrents.exception.loginfailure import LoginFailure
from autoremovetorrents.exception.remotefailure import RemoteFailure
from autoremovetorrents.util.safedelete import local_deletion_supported

# Same URLs as the fake rTorrent in conftest.py
RPC_URL = 'http://rtorrent.test/RPC2'
RUTORRENT_URL = 'http://rtorrent.test/rutorrent'

H1 = 'A' * 40
H2 = 'B' * 40

needs_local = pytest.mark.skipif(not local_deletion_supported(), reason='dir_fd is not supported')

def make_client(**options):
    client = rTorrent(RPC_URL, **options)
    client.login('', '')
    return client

# ---------- Reading ----------

def test_properties(rtorrent):
    rtorrent.add(H1, **{'d.custom1': 'TV%20Shows', 'd.name': 'My Torrent'})
    client = make_client()
    assert client.version() == 'rTorrent 0.9.8 (libtorrent 0.13.8)'
    assert client.api_version() == '10'
    assert client.torrents_list() == [H1]
    t = client.torrent_properties(H1)
    assert t.name == 'My Torrent'
    assert t.category == ['TV Shows']
    assert t.tracker == ['http://tracker1/announce'] # DHT is not a tracker
    assert t.status == TorrentStatus.Uploading
    assert t.ratio == 1.5
    assert t.size == 1000 and t.progress == 1.0
    assert t.seeder == 10 and t.leecher == 20
    assert t.connected_seeder == 2 and t.connected_leecher == 3
    assert t.create_time == 900 # d.load_date
    assert abs(t.seeding_time - (time.time() - 2000)) < 5
    assert not t.stalled
    str(t) # Printable although some properties are not provided

def test_optional_fields_missing(rtorrent):
    rtorrent.methods = set() # An old rTorrent
    rtorrent.add(H1)
    client = make_client()
    client.torrents_list()
    t = client.torrent_properties(H1)
    assert t.create_time == 1000 # d.timestamp.started
    assert client.api_version() == 'not provided'
    with pytest.raises(RemoteFailure):
        client.remote_free_space('/downloads')
    # d.multicall instead of d.multicall2
    assert len(rtorrent.called('d.multicall')) == 1

@pytest.mark.parametrize('methods', [set(['d.multicall2', 'd.load_date']), set()])
def test_without_introspection(rtorrent, methods):
    rtorrent.introspection = False
    rtorrent.methods = methods
    rtorrent.add(H1)
    client = make_client()
    client.torrents_list()
    assert client.torrent_properties(H1).create_time == (900 if methods else 1000)
    listings = [p for p in rtorrent.called('d.multicall2' if methods else 'd.multicall') if len(p) > 0]
    assert len(listings) == 1

def test_invalid_hashes(rtorrent):
    client = make_client()
    success, failed = client.remove_torrents([H1 + '\n', 'a_hash', H1[:-1] + 'g'], True)
    assert success == [] and len(failed) == 3

def test_unknown_create_time_never_triggers_removal(rtorrent):
    rtorrent.add(H1, **{'d.load_date': 0, 'd.timestamp.started': 0})
    client = make_client()
    client.torrents_list()
    assert time.time() - client.torrent_properties(H1).create_time < 5

@pytest.mark.parametrize('fields, status', [
    ({'d.hashing': 1}, TorrentStatus.Checking),
    ({'d.message': 'Storage error: disk full'}, TorrentStatus.Error),
    ({'d.message': 'Tracker: [Timeout was reached]'}, TorrentStatus.Uploading),
    ({'d.state': 0}, TorrentStatus.Stopped),
    ({'d.is_active': 0}, TorrentStatus.Paused),
    ({'d.complete': 0}, TorrentStatus.Downloading),
])
def test_status(rtorrent, fields, status):
    rtorrent.add(H1, **fields)
    client = make_client()
    client.torrents_list()
    assert client.torrent_properties(H1).status == status

def test_login_failure(rtorrent):
    rtorrent.password = 'secret'
    with pytest.raises(LoginFailure):
        make_client()

def test_remote_free_space(rtorrent):
    rtorrent.add(H1, **{'d.free_diskspace': 500})
    rtorrent.add(H2, **{'d.free_diskspace': 300, 'd.directory': '/other/T'})
    client = make_client()
    client.torrents_list()
    assert client.remote_free_space('/downloads/') == 500
    assert client.remote_free_space('/') == 300
    with pytest.raises(RemoteFailure):
        client.remote_free_space('/downloads2') # No torrent stored there

def test_closed_torrents_are_not_used_for_free_space(rtorrent):
    rtorrent.add(H1, **{'d.free_diskspace': 0, 'd.is_open': 0})
    client = make_client()
    client.torrents_list()
    with pytest.raises(RemoteFailure):
        client.remote_free_space('/downloads')

# ---------- Configuration ----------

@pytest.mark.parametrize('options', [
    {'delete_mode': 'rm-rf'},
    {'delete_mode': 'rutorrent', 'rutorrent_url': RUTORRENT_URL},                  # No allowed_paths
    {'delete_mode': 'rutorrent', 'allowed_paths': ['/downloads']},                 # No rutorrent_url
    {'delete_mode': 'rutorrent', 'rutorrent_url': RUTORRENT_URL, 'allowed_paths': ['/']},
    {'delete_mode': 'rutorrent', 'rutorrent_url': RUTORRENT_URL, 'allowed_paths': ['/a/../b']},
    {'delete_mode': 'local', 'allowed_paths': ['/downloads'], 'path_mapping': {'/other': '/mnt'}},
    {'allowed_paths': ['/downloads'], 'path_mapping': {'/downloads': '/mnt'}},     # Mapping without local mode
])
def test_invalid_configuration(options):
    with pytest.raises(InvalidConfiguration):
        rTorrent(RPC_URL, **options)

def test_client_options_for_other_clients():
    task = Task('t', {'client': 'qbittorrent', 'host': 'http://localhost', 'client_options': {'delete_mode': 'local'}})
    with pytest.raises(InvalidConfiguration):
        task.execute()

# ---------- Removing ----------

def test_remove_without_data(rtorrent):
    rtorrent.add(H1)
    client = make_client()
    assert client.remove_torrents([H1, 'a_hash'], False) == (
        [H1], [{'hash': 'a_hash', 'reason': 'This is not a valid torrent hash.'}])
    assert rtorrent.called('d.delete_tied') == [[H1]]
    assert H1 not in rtorrent.torrents

def test_remove_nonexistent_torrent(rtorrent):
    client = make_client()
    success, failed = client.remove_torrents([H1], False)
    assert success == [] and len(failed) == 1

def test_delete_data_requires_delete_mode(rtorrent):
    rtorrent.add(H1)
    success, failed = make_client().remove_torrents([H1], True)
    assert success == [] and 'delete_mode' in failed[0]['reason']
    assert H1 in rtorrent.torrents

def rutorrent_client():
    return make_client(delete_mode='rutorrent', rutorrent_url=RUTORRENT_URL + '/', allowed_paths=['/downloads/'])

def test_rutorrent_delete(rtorrent):
    rtorrent.add(H1.lower())
    client = rutorrent_client()
    assert client.deferred_data_deletion
    assert client.remove_torrents([H1.lower()], True) == ([H1.lower()], [])
    assert rtorrent.erasedata == ['mode=removewithdata&hash=%s&v=1' % H1]
    assert rtorrent.getplugins_calls == 1 # The plugins were initialized first

@pytest.mark.parametrize('state', ['missing', 'failed'])
def test_rutorrent_without_running_erasedata(rtorrent, state):
    rtorrent.add(H1)
    rtorrent.erasedata_plugin = state
    success, failed = rutorrent_client().remove_torrents([H1], True)
    assert success == [] and 'erasedata plugin' in failed[0]['reason']
    assert rtorrent.erasedata == [] # Nothing was sent to ruTorrent
    assert H1 in rtorrent.torrents

@pytest.mark.parametrize('fields', [
    {'d.base_path': '/etc', 'd.directory': '/etc', 'files': [['/etc/passwd', 'passwd']]},
    {'d.base_path': '/downloads', 'd.directory': '/downloads', 'files': [['/downloads/x', 'x']]},
    {'files': [['/downloads/Torrent/../../etc/passwd', '../../etc/passwd']]},
    {'files': [['/downloads/Other/a.mkv', 'a.mkv']]},
    {'d.base_path': '/downloads2/T', 'd.directory': '/downloads2/T', 'files': [['/downloads2/T/a', 'a']]},
])
def test_rutorrent_refuses_unsafe_paths(rtorrent, fields):
    rtorrent.add(H1, **fields)
    success, failed = rutorrent_client().remove_torrents([H1], True)
    assert success == [] and failed[0]['reason'].startswith('Refused to delete data')
    assert rtorrent.erasedata == [] # Nothing was sent to ruTorrent
    assert H1 in rtorrent.torrents

def test_rutorrent_fallback_paths_of_unopened_torrents(rtorrent):
    # d.base_path and f.frozen_path are empty until rTorrent opens the download
    rtorrent.add(H1, **{'d.base_path': '', 'd.is_multi_file': 0, 'd.directory': '/downloads',
        'd.name': 'movie.mkv', 'files': [['', 'movie.mkv']]})
    assert rutorrent_client().remove_torrents([H1], True) == ([H1], [])

def test_rutorrent_without_erasedata(rtorrent):
    rtorrent.add(H1)
    rtorrent.erasedata_status = 404
    success, failed = rutorrent_client().remove_torrents([H1], True)
    assert success == [] and '5.3.9' in failed[0]['reason']
    assert H1 in rtorrent.torrents

def test_rutorrent_keeps_torrent(rtorrent):
    rtorrent.add(H1)
    rtorrent.add(H2)
    rtorrent.erasedata_keeps.add(H2)
    success, failed = rutorrent_client().remove_torrents([H1, H2], True)
    assert success == [H1] and failed[0]['hash'] == H2

def test_task_logs_deferred_deletion(rtorrent, caplog):
    rtorrent.add(H1, **{'d.name': 'Deferred'})
    task = Task('t', {
        'client': 'rtorrent', 'host': RPC_URL, 'delete_data': True,
        'client_options': {'delete_mode': 'rutorrent', 'rutorrent_url': RUTORRENT_URL, 'allowed_paths': ['/downloads']},
        'strategies': {'all': {'ratio': 1}},
    })
    task.execute()
    assert len(task.get_removed_torrents()) == 1
    assert 'Deferred has been removed and its data has been queued for deletion' in caplog.text

# ---------- Local deletion ----------

def _make(path):
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path, 'w') as f:
        f.write('x')

@pytest.fixture
def local_tree(tmp_path):
    root = str(tmp_path / 'local')
    outside = str(tmp_path / 'outside')
    _make(os.path.join(root, 'Torrent', 'a.mkv'))
    _make(os.path.join(root, 'Torrent', 'sub', 'b.mkv'))
    _make(os.path.join(outside, 'precious.txt'))
    return root, outside

def add_local_torrent(rtorrent, **fields):
    files = [['/downloads/Torrent/a.mkv', 'a.mkv'], ['/downloads/Torrent/sub/b.mkv', 'sub/b.mkv']]
    fields.setdefault('files', files)
    rtorrent.add(H1, **fields)

def local_client(root):
    return make_client(delete_mode='local', allowed_paths=['/downloads'], path_mapping={'/downloads': root})

@needs_local
def test_local_delete(rtorrent, local_tree):
    root, outside = local_tree
    add_local_torrent(rtorrent)
    client = local_client(root)
    assert not client.deferred_data_deletion
    assert client.remove_torrents([H1], True) == ([H1], [])
    assert os.listdir(root) == []
    assert os.path.exists(os.path.join(outside, 'precious.txt'))
    # Stopped before deleting, erased after deleting
    methods = [m for m, _ in rtorrent.calls]
    assert methods.index('d.close') < methods.index('d.erase')

@needs_local
def test_local_delete_refuses_symlink(rtorrent, local_tree):
    root, outside = local_tree
    os.rename(os.path.join(root, 'Torrent', 'sub'), os.path.join(root, 'sub-moved'))
    os.symlink(outside, os.path.join(root, 'Torrent', 'sub'))
    add_local_torrent(rtorrent, files=[['/downloads/Torrent/sub/precious.txt', 'sub/precious.txt']])
    success, failed = local_client(root).remove_torrents([H1], True)
    assert success == [] and 'symlink' in failed[0]['reason']
    assert os.path.exists(os.path.join(outside, 'precious.txt'))
    # The torrent was neither stopped nor erased
    assert rtorrent.called('d.stop') == [] and H1 in rtorrent.torrents

@needs_local
def test_local_delete_refuses_path_outside(rtorrent, local_tree):
    root, outside = local_tree
    add_local_torrent(rtorrent, **{'d.base_path': outside, 'd.directory': outside,
        'files': [[os.path.join(outside, 'precious.txt'), 'precious.txt']]})
    success, failed = local_client(root).remove_torrents([H1], True)
    assert success == [] and failed[0]['reason'].startswith('Refused to delete data')
    assert os.path.exists(os.path.join(outside, 'precious.txt'))
    assert H1 in rtorrent.torrents

@needs_local
@pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0, reason='root ignores file permissions')
def test_local_delete_failure_keeps_torrent(rtorrent, local_tree):
    root, _ = local_tree
    add_local_torrent(rtorrent)
    sub = os.path.join(root, 'Torrent', 'sub')
    os.chmod(sub, 0o500) # b.mkv cannot be deleted
    try:
        success, failed = local_client(root).remove_torrents([H1], True)
    finally:
        os.chmod(sub, 0o700)
    assert success == [] and 'kept' in failed[0]['reason']
    assert H1 in rtorrent.torrents

# ---------- SCGI ----------

def test_scgi(rtorrent, scgi_server):
    rtorrent.add(H1, **{'d.name': 'Over SCGI'})
    client = rTorrent(scgi_server.url)
    client.login('', '')
    assert client.version() == 'rTorrent 0.9.8 (libtorrent 0.13.8)'
    assert client.torrents_list() == [H1]
    assert client.torrent_properties(H1).name == 'Over SCGI'
    assert client.remove_torrents([H1], False) == ([H1], [])
    assert H1 not in rtorrent.torrents
    assert scgi_server.requests > 0

def test_scgi_with_rutorrent_delete(rtorrent, scgi_server):
    # rTorrent by SCGI, data deleted by ruTorrent (with the credentials of ruTorrent)
    rtorrent.add(H1)
    client = rTorrent(scgi_server.url, delete_mode='rutorrent', rutorrent_url=RUTORRENT_URL, allowed_paths=['/downloads'])
    client.login('admin', 'secret')
    assert client.remove_torrents([H1], True) == ([H1], [])
    assert rtorrent.erasedata == ['mode=removewithdata&hash=%s&v=1' % H1]

@pytest.mark.parametrize('url', [
    'scgi://192.168.1.10:5000',
    'scgi://10.0.0.1:5000',
    'scgi://0.0.0.0:5000',
    'scgi://[::]:5000',
    'scgi://[::ffff:10.0.0.1]:5000',
    'scgi://example.com:5000',
    'scgi://127.0.0.1',             # No port
    'scgi://127.0.0.1:5000/RPC2',   # Path on TCP
    'scgi://user:pass@127.0.0.1:5000',
    'scgi://relative/path.sock',
    'scgi:///run/rtorrent.sock?x=1',
])
def test_scgi_only_local(url):
    with pytest.raises(InvalidConfiguration):
        rTorrent(url)

def test_scgi_remote_host_in_task():
    task = Task('t', {'client': 'rtorrent', 'host': 'scgi://192.168.1.10:5000'})
    with pytest.raises(InvalidConfiguration):
        task.execute()

def test_scgi_connection_failure(tmp_path):
    client = rTorrent('scgi://' + str(tmp_path / 'missing.sock'))
    with pytest.raises(ConnectionFailure):
        client.login('', '')

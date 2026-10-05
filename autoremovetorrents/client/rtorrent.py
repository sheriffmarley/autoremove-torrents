#-*- coding:utf-8 -*-
import re
import socket
import time
import requests
from .. import logger
from ..torrent import Torrent
from ..clientstatus import ClientStatus
from ..torrentstatus import TorrentStatus
from ..compatibility.unquote_ import unquote_
from ..compatibility.urlparse_ import urlparse_
from ..compatibility.xmlrpc_ import xmlrpc_client
from ..exception.connectionfailure import ConnectionFailure
from ..exception.invalidconfiguration import InvalidConfiguration
from ..exception.loginfailure import LoginFailure
from ..exception.nosuchtorrent import NoSuchTorrent
from ..exception.remotefailure import RemoteFailure
from ..exception.unsafepath import UnsafePath
from ..util.safedelete import check_torrent_paths, find_root, map_path, \
    normalize_config_dir, local_deletion_supported, LocalDataDeleter

# rTorrent reports info hashes as 40 hex characters
HASH_PATTERN = re.compile(r'^[0-9A-Fa-f]{40}\Z')

# The erasedata endpoint of ruTorrent (available since ruTorrent 5.3.9)
ERASEDATA_PATH = '/plugins/erasedata/action.php'
# Loading the plugins of ruTorrent, as its web UI does when it's opened
GETPLUGINS_PATH = '/php/getplugins.php'

# Timeout of SCGI requests in seconds
SCGI_TIMEOUT = 60

# Parse an SCGI URL: scgi:///path/to/socket or scgi://127.0.0.1:5000
# SCGI has no authentication at all (whoever reaches it can run any command
# through rTorrent), so only unix sockets and loopback addresses are accepted.
# Returns a list of (address family, socket address)
def parse_scgi_url(url):
    parsed = urlparse_(url)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise InvalidConfiguration("The SCGI address '%s' can't contain credentials, queries or fragments." % url)

    # Unix socket
    if parsed.netloc == '':
        if not parsed.path.startswith('/'):
            raise InvalidConfiguration("The SCGI socket in '%s' must be an absolute path." % url)
        if not hasattr(socket, 'AF_UNIX'):
            raise InvalidConfiguration('Unix sockets are not supported on this system.')
        return [(socket.AF_UNIX, parsed.path)]

    # TCP on the loopback interface
    if parsed.path not in ('', '/'):
        raise InvalidConfiguration("The SCGI address '%s' can't contain a path." % url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        raise InvalidConfiguration("The SCGI address '%s' needs a port, e.g. scgi://127.0.0.1:5000." % url)
    try:
        import ipaddress
    except ImportError: # Python 2.7
        raise InvalidConfiguration('SCGI over TCP requires Python 3.3+.')
    host = parsed.hostname
    if host == 'localhost':
        try:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except socket.error as e:
            raise InvalidConfiguration("Cannot resolve localhost: %s" % e)
        addresses = [(info[0], info[4]) for info in infos]
    else:
        try:
            ip = ipaddress.ip_address(u'%s' % host)
        except ValueError:
            raise InvalidConfiguration("SCGI is only allowed on localhost or a loopback IP address, not '%s'." % host)
        addresses = [(socket.AF_INET6, (host, port, 0, 0)) if ip.version == 6 else (socket.AF_INET, (host, port))]
    for _, address in addresses:
        if not ipaddress.ip_address(u'%s' % address[0].split('%')[0]).is_loopback:
            raise InvalidConfiguration("SCGI is only allowed on the loopback interface, but '%s' is %s." % (host, address[0]))
    return addresses

class rTorrent(object):
    # Properties fetched for every torrent; none of them can fault on any rTorrent 0.9.x
    FIELDS = [
        'd.hash',
        'd.name',
        'd.custom1', # Label of ruTorrent
        'd.state',
        'd.is_active',
        'd.is_open',
        'd.complete',
        'd.hashing',
        'd.message',
        'd.size_bytes',
        'd.completed_bytes',
        'd.ratio',
        'd.up.total',
        'd.down.total',
        'd.up.rate',
        'd.down.rate',
        'd.peers_complete',
        'd.peers_connected',
        'd.timestamp.started',
        'd.timestamp.finished',
        'd.directory',
    ]
    # Properties which are not available in every version of rTorrent
    OPTIONAL_FIELDS = [
        'd.load_date',
        'd.free_diskspace',
    ]

    def __init__(self, host, delete_mode=None, allowed_paths=None, path_mapping=None, rutorrent_url=None):
        # Logger
        self._logger = logger.Logger.register(__name__)
        # URL of the XML-RPC endpoint, e.g. https://example.com/RPC2 or scgi:///run/rtorrent.sock
        self._host = host
        # Addresses of the SCGI socket, if rTorrent is accessed by SCGI directly
        self._scgi = parse_scgi_url(host) if host.lower().startswith('scgi:') else None
        # Requests Session
        self._session = requests.Session()
        # Available methods
        self._multicall = None
        self._fields = list(rTorrent.FIELDS)
        # Torrent Properties Cache
        self._torrent_cache = {}
        self._refresh_expire_time = 30
        self._last_refresh = 0

        # Data deletion settings
        if delete_mode is not None and delete_mode not in ('rutorrent', 'local'):
            raise InvalidConfiguration("Unknown delete_mode '%s'. Use 'rutorrent' or 'local'." % delete_mode)
        self._delete_mode = delete_mode
        try:
            if allowed_paths is None:
                allowed_paths = []
            elif not isinstance(allowed_paths, list):
                allowed_paths = [allowed_paths]
            self._allowed_paths = [normalize_config_dir(p) for p in allowed_paths]
            self._path_mapping = {}
            for remote, local in (path_mapping or {}).items():
                self._path_mapping[normalize_config_dir(remote)] = normalize_config_dir(local)
        except UnsafePath as e:
            raise InvalidConfiguration(str(e))
        self._rutorrent_url = rutorrent_url.rstrip('/') if rutorrent_url else None

        if delete_mode is not None and len(self._allowed_paths) == 0:
            raise InvalidConfiguration('allowed_paths is required when delete_mode is set.')
        if delete_mode == 'rutorrent' and self._rutorrent_url is None:
            raise InvalidConfiguration("rutorrent_url is required when delete_mode is 'rutorrent'.")
        if delete_mode == 'local':
            if not local_deletion_supported():
                raise InvalidConfiguration("delete_mode 'local' requires Python 3.3+ on a POSIX system.")
            for root in self._allowed_paths:
                if map_path(root, self._path_mapping) is None:
                    raise InvalidConfiguration("path_mapping doesn't cover the allowed path '%s'." % root)
        elif len(self._path_mapping) > 0:
            raise InvalidConfiguration("path_mapping is only used when delete_mode is 'local'.")

    # ruTorrent deletes data asynchronously, so the task shouldn't claim it's gone
    @property
    def deferred_data_deletion(self):
        return self._delete_mode == 'rutorrent'

    # Login to rTorrent
    def login(self, username, password):
        # SCGI has no authentication; the credentials are still used for ruTorrent
        if username:
            self._session.auth = (username, password)
        # Make sure we can talk to rTorrent and find out which methods it supports
        self._multicall = 'd.multicall2' if self._method_exists('d.multicall2') else 'd.multicall'
        for field in rTorrent.OPTIONAL_FIELDS:
            if self._method_exists(field):
                self._fields.append(field)

    # Send an XML-RPC request
    def _call(self, method, *params):
        body = xmlrpc_client.dumps(tuple(params), method)
        if not isinstance(body, bytes):
            body = body.encode('utf-8')
        if self._scgi is not None:
            status_code, content = self._scgi_request(body)
        else:
            try:
                response = self._session.post(self._host, data=body, headers={'Content-Type': 'text/xml'})
            except Exception as exc:
                raise ConnectionFailure(str(exc))
            status_code, content = response.status_code, response.content
        if status_code == 401:
            raise LoginFailure('Unauthorized user.')
        if status_code != 200:
            raise RemoteFailure('The server responsed %d on method %s.' % (status_code, method))
        try:
            return xmlrpc_client.loads(content)[0][0]
        except xmlrpc_client.Fault as fault:
            raise RemoteFailure('%s (code %d)' % (fault.faultString, fault.faultCode))
        except Exception as exc:
            raise RemoteFailure('Invalid XML-RPC response on method %s: %s' % (method, exc))

    # Send a request to rTorrent's SCGI socket
    # Returns the status code and the body of the response
    def _scgi_request(self, body):
        headers = ('CONTENT_LENGTH\0%d\0SCGI\x001\0REQUEST_METHOD\0POST\0REQUEST_URI\0/RPC2\0' % len(body)).encode('ascii')
        request = ('%d:' % len(headers)).encode('ascii') + headers + b',' + body
        error = None
        for family, address in self._scgi:
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(SCGI_TIMEOUT)
                sock.connect(address)
                sock.sendall(request)
                chunks = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                response = b''.join(chunks)
                break
            except (socket.error, socket.timeout) as e:
                error = e
            finally:
                sock.close()
        else:
            raise ConnectionFailure('Cannot connect to the SCGI socket %s: %s' % (self._host, error))

        # The response has CGI headers, e.g. "Status: 200 OK"
        for separator in (b'\r\n\r\n', b'\n\n'):
            position = response.find(separator)
            if position != -1:
                head, content = response[:position], response[position+len(separator):]
                break
        else:
            raise RemoteFailure('Invalid SCGI response from %s.' % self._host)
        status_code = 200
        for line in head.decode('latin-1').splitlines():
            if line.lower().startswith('status:'):
                try:
                    status_code = int(line.split(':', 1)[1].split()[0])
                except (IndexError, ValueError):
                    raise RemoteFailure('Invalid SCGI status line: %s' % line)
        return status_code, content

    # Call several methods in one request
    # Returns a list of (succeeded, value or reason)
    def _call_multi(self, calls):
        if len(calls) == 0:
            return []
        results = self._call('system.multicall',
            [{'methodName': method, 'params': list(params)} for method, params in calls])
        return [
            (False, '%s (code %s)' % (r.get('faultString'), r.get('faultCode'))) if isinstance(r, dict) \
            else (True, r[0])
            for r in results
        ]

    # Only used for getters, which have no side effects
    def _method_exists(self, method):
        try:
            return bool(self._call('system.methodExist', method))
        except RemoteFailure:
            pass
        # Introspection isn't available: call the method and see whether rTorrent knows it
        try:
            self._call(method)
            return True
        except RemoteFailure as e:
            return 'not defined' not in str(e) and '(code -506)' not in str(e)

    # Get client status
    def client_status(self):
        status = self._call_multi([
            ('throttle.global_down.rate', []),
            ('throttle.global_down.total', []),
            ('throttle.global_up.rate', []),
            ('throttle.global_up.total', []),
        ])
        cs = ClientStatus()
        # Remote free space checker
        cs.free_space = self.remote_free_space
        for attr, (ok, value) in zip(['download_speed', 'total_downloaded', 'upload_speed', 'total_uploaded'], status):
            if ok:
                setattr(cs, attr, value)
        return cs

    # Get rTorrent version
    def version(self):
        return 'rTorrent %s (libtorrent %s)' % (
            self._call('system.client_version'),
            self._call('system.library_version'),
        )

    # Get API version
    def api_version(self):
        if self._method_exists('system.api_version'):
            return str(self._call('system.api_version'))
        return 'not provided'

    # Get all the hashes in the client
    def _hashes(self):
        params = ['', 'main', 'd.hash='] if self._multicall == 'd.multicall2' else ['main', 'd.hash=']
        return [row[0] for row in self._call(self._multicall, *params)]

    # Get torrent list
    def torrents_list(self):
        commands = [field + '=' for field in self._fields]
        params = ['', 'main'] + commands if self._multicall == 'd.multicall2' else ['main'] + commands
        rows = self._call(self._multicall, *params)

        cache = {}
        for row in rows:
            torrent = dict(zip(self._fields, row))
            torrent['trackers'] = []
            cache[torrent['d.hash']] = torrent

        # Get trackers of all the torrents in one request; a failure only loses the trackers
        hashes = list(cache)
        trackers = self._call_multi([
            ('t.multicall', [h, '', 't.url=', 't.is_enabled=', 't.scrape_complete=', 't.scrape_incomplete='])
            for h in hashes
        ])
        for h, (ok, value) in zip(hashes, trackers):
            if ok:
                cache[h]['trackers'] = value

        self._torrent_cache = cache
        self._last_refresh = time.time()
        return hashes

    # Get Torrent Properties
    def torrent_properties(self, torrent_hash):
        # Check cache expiration
        if time.time() - self._last_refresh > self._refresh_expire_time:
            self.torrents_list()
        if torrent_hash not in self._torrent_cache:
            raise NoSuchTorrent("No such torrent of hash '%s'." % torrent_hash)
        torrent = self._torrent_cache[torrent_hash]
        now = time.time()

        # Create torrent object
        torrent_obj = Torrent()
        torrent_obj.hash = torrent['d.hash']
        torrent_obj.name = torrent['d.name']
        label = unquote_(torrent['d.custom1'])
        torrent_obj.category = [label] if len(label) > 0 else []
        # rTorrent lists DHT as a pseudo tracker
        trackers = [t for t in torrent['trackers'] if t[1] and not t[0].startswith('dht://')]
        torrent_obj.tracker = [t[0] for t in trackers]
        torrent_obj.status = rTorrent._judge_status(torrent)
        torrent_obj.size = torrent['d.size_bytes']
        torrent_obj.ratio = torrent['d.ratio'] / 1000.0
        torrent_obj.uploaded = torrent['d.up.total']
        torrent_obj.downloaded = torrent['d.down.total']
        torrent_obj.upload_speed = torrent['d.up.rate']
        torrent_obj.download_speed = torrent['d.down.rate']
        torrent_obj.stalled = bool(torrent['d.is_active']) and (
            torrent['d.up.rate'] == 0 if torrent['d.complete'] else torrent['d.down.rate'] == 0)
        torrent_obj.seeder = sum([t[2] for t in trackers])
        torrent_obj.connected_seeder = torrent['d.peers_complete']
        torrent_obj.leecher = sum([t[3] for t in trackers])
        torrent_obj.connected_leecher = torrent['d.peers_connected'] - torrent['d.peers_complete']
        torrent_obj.progress = float(torrent['d.completed_bytes']) / torrent['d.size_bytes'] \
            if torrent['d.size_bytes'] > 0 else 0
        # Time of adding: an unknown time is treated as "just added", so it never triggers a removal
        create_time = torrent.get('d.load_date', 0) or torrent['d.timestamp.started']
        torrent_obj.create_time = create_time if create_time > 0 else int(now)
        # rTorrent doesn't count the seeding time. We use the time since the download was
        # finished (or started, if it was already complete when it was added) instead.
        if torrent['d.complete']:
            since = torrent['d.timestamp.finished'] or torrent['d.timestamp.started']
            torrent_obj.seeding_time = max(0, int(now - since)) if since > 0 else 0
        else:
            torrent_obj.seeding_time = 0
        # Not provided by rTorrent: last_activity, downloading_time, average_upload_speed, average_download_speed

        return torrent_obj

    # Judge Torrent Status
    @staticmethod
    def _judge_status(torrent):
        if torrent['d.hashing'] != 0:
            return TorrentStatus.Checking
        # Tracker messages also land in d.message, but they aren't errors of the torrent
        if len(torrent['d.message']) > 0 and not torrent['d.message'].startswith('Tracker:'):
            return TorrentStatus.Error
        if torrent['d.state'] == 0:
            return TorrentStatus.Stopped
        if torrent['d.is_active'] == 0:
            return TorrentStatus.Paused
        if torrent['d.complete']:
            return TorrentStatus.Uploading
        return TorrentStatus.Downloading

    # Get free space
    # rTorrent can only report the free space of the devices where a torrent's files are,
    # so the open torrents stored in the given path are used to measure it.
    def remote_free_space(self, path):
        if 'd.free_diskspace' not in self._fields:
            raise RemoteFailure('This version of rTorrent cannot report free space.')
        if time.time() - self._last_refresh > self._refresh_expire_time:
            self.torrents_list()
        path = path.rstrip('/') or '/'
        values = [
            t['d.free_diskspace'] for t in self._torrent_cache.values()
            if t['d.is_open'] and (path == '/' or t['d.directory'] == path or t['d.directory'].startswith(path + '/'))
        ]
        if len(values) == 0:
            # Returning a guess could remove torrents for nothing
            raise RemoteFailure("rTorrent cannot report the free space of '%s' because no open torrent is stored there." % path)
        return min(values)

    # Get the name of a torrent for logs
    def _name(self, torrent_hash):
        torrent = self._torrent_cache.get(torrent_hash)
        return torrent['d.name'] if torrent is not None else torrent_hash

    # Collect the base path and files of the torrents
    # Returns a dict: {hash: (base, files, is_multi)} and a list of failures
    def _collect_paths(self, hashes):
        calls = []
        for h in hashes:
            calls += [
                ('d.base_path', [h]),
                ('d.directory', [h]),
                ('d.name', [h]),
                ('d.is_multi_file', [h]),
                ('f.multicall', [h, '', 'f.frozen_path=', 'f.path=']),
            ]
        results = self._call_multi(calls)
        paths = {}
        failures = []
        for i, h in enumerate(hashes):
            chunk = results[i*5:(i+1)*5]
            failed = [value for ok, value in chunk if not ok]
            if len(failed) > 0:
                failures.append({'hash': h, 'reason': failed[0]})
                continue
            base_path, directory, name, is_multi, files = [value for _, value in chunk]
            is_multi = bool(is_multi)
            frozen = [f[0] for f in files]
            # d.base_path and f.frozen_path are empty when the download hasn't been opened
            # since rTorrent started; then use d.directory and f.path instead
            if len(base_path) > 0 and len(frozen) > 0 and all(len(f) > 0 for f in frozen):
                paths[h] = (base_path, frozen, is_multi)
            else:
                directory = directory.rstrip('/')
                base = directory if is_multi else directory + '/' + name
                paths[h] = (base, [directory + '/' + f[1] for f in files], is_multi)
        return paths, failures

    # Batch Remove Torrents
    # Return values: (success_hash_list, failed_hash_list : [{hash: reason}, ...])
    def remove_torrents(self, torrent_hash_list, remove_data):
        success = []
        failed = []
        valid = []
        for h in torrent_hash_list:
            if HASH_PATTERN.match(h):
                valid.append(h)
            else:
                failed.append({'hash': h, 'reason': 'This is not a valid torrent hash.'})
        if len(valid) == 0:
            return (success, failed)

        if not remove_data:
            s, f = self._erase(valid)
        elif self._delete_mode is None:
            s, f = [], [{
                'hash': h,
                'reason': 'Deleting data with rTorrent requires the client option delete_mode.',
            } for h in valid]
        else:
            try:
                paths, f = self._collect_paths(valid)
            except Exception as e:
                return (success, failed + [{'hash': h, 'reason': str(e)} for h in valid])
            checked = []
            for h in valid:
                if h not in paths:
                    continue
                base, files, is_multi = paths[h]
                try:
                    check_torrent_paths(base, files, is_multi, self._allowed_paths)
                    checked.append(h)
                except UnsafePath as e:
                    f.append({'hash': h, 'reason': 'Refused to delete data: %s' % e})
            if self._delete_mode == 'rutorrent':
                s, f2 = self._remove_by_rutorrent(checked, paths)
            else:
                s, f2 = self._remove_locally(checked, paths)
            f += f2
        return (success + s, failed + f)

    # Remove torrents (and their .torrent files in watch directories) but keep their data
    def _erase(self, hashes):
        calls = []
        for h in hashes:
            # Without removing the tied .torrent file, a watch directory would add the torrent again
            calls += [('d.delete_tied', [h]), ('d.erase', [h])]
        try:
            results = self._call_multi(calls)
        except Exception as e:
            return ([], [{'hash': h, 'reason': str(e)} for h in hashes])
        success = []
        failed = []
        for i, h in enumerate(hashes):
            ok, value = results[i*2+1]
            if ok:
                success.append(h)
            else:
                failed.append({'hash': h, 'reason': value})
        return (success, failed)

    # Initialize ruTorrent's plugins (the same as opening the web UI) and make sure erasedata is running.
    # Without this, ruTorrent may not know the rTorrent version of this user yet (and fail),
    # or its erasedata cleaner may not be scheduled (and the data would never be deleted).
    # Returns None if erasedata is ready, otherwise the reason
    def _prepare_erasedata(self):
        try:
            response = self._session.get(self._rutorrent_url + GETPLUGINS_PATH)
        except Exception as e:
            return 'Cannot reach ruTorrent: %s' % e
        if response.status_code == 401:
            return 'ruTorrent rejected the login.'
        if response.status_code != 200:
            return 'ruTorrent responsed %d when loading its plugins.' % response.status_code
        text = response.text
        start = text.find("rPlugin('erasedata'")
        if start == -1:
            return 'The erasedata plugin is not enabled in ruTorrent.'
        end = text.find('rPlugin(', start + 1)
        block = text[start:end] if end != -1 else text[start:]
        # A started erasedata reports its settings; a failed one disables itself
        if 'pluginCantStart' in block or 'enableForceDeletion' not in block:
            return 'The erasedata plugin could not start in ruTorrent.'
        return None

    # Let ruTorrent's erasedata plugin remove the torrents and delete their data
    def _remove_by_rutorrent(self, hashes, paths):
        if len(hashes) == 0:
            return ([], [])
        reason = self._prepare_erasedata()
        if reason is not None:
            return ([], [{'hash': h, 'reason': reason} for h in hashes])
        data = [('mode', 'removewithdata')] + [('hash', h.upper()) for h in hashes] + [('v', '1')] # Never force deletion
        try:
            response = self._session.post(self._rutorrent_url + ERASEDATA_PATH, data=data)
        except Exception as e:
            return ([], [{'hash': h, 'reason': 'Cannot reach ruTorrent: %s' % e} for h in hashes])
        if response.status_code == 404:
            return ([], [{
                'hash': h,
                'reason': 'ruTorrent has no erasedata endpoint. ruTorrent 5.3.9 or later with the erasedata plugin is required.',
            } for h in hashes])
        if response.status_code == 401:
            return ([], [{'hash': h, 'reason': 'ruTorrent rejected the login.'} for h in hashes])

        # erasedata skips the torrents it cannot handle safely, so check which are gone
        try:
            remaining = set(x.upper() for x in self._hashes())
        except Exception as e:
            return ([], [{'hash': h, 'reason': 'Cannot check the result: %s' % e} for h in hashes])
        success = []
        failed = []
        for h in hashes:
            if h.upper() in remaining:
                failed.append({
                    'hash': h,
                    'reason': 'ruTorrent kept the torrent (HTTP %d); see the ruTorrent log.' % response.status_code,
                })
            else:
                success.append(h)
                self._logger.info("The data of %s (%s) will be deleted by ruTorrent's erasedata plugin.",
                    self._name(h), paths[h][0])
        return (success, failed)

    # Delete the data on this machine, then remove the torrents
    def _remove_locally(self, hashes, paths):
        success = []
        failed = []
        for h in hashes:
            base, files, is_multi = paths[h]
            try:
                # Translate the paths seen by rTorrent into local paths
                remote_root = find_root(base, self._allowed_paths)
                local = [map_path(p, self._path_mapping) for p in [remote_root, base] + files]
                if None in local:
                    raise UnsafePath("path_mapping doesn't cover '%s'." % base)
                deleter = LocalDataDeleter(local[0], local[1], local[2:], is_multi)
                deleter.verify()
            except UnsafePath as e:
                failed.append({'hash': h, 'reason': 'Refused to delete data: %s' % e})
                continue

            # Stop the torrent so that rTorrent doesn't write into the files any more.
            # Called one by one: ruTorrent's httprpc proxy refuses them inside system.multicall
            try:
                self._call('d.stop', h)
                self._call('d.close', h)
            except Exception as e:
                failed.append({'hash': h, 'reason': 'Cannot stop the torrent: %s' % e})
                continue

            result = deleter.delete()
            for dir_ in result['kept_dirs']:
                self._logger.warning("The directory %s contains files which don't belong to %s, so it was kept.",
                    dir_, self._name(h))
            if len(result['errors']) > 0:
                # Keep the torrent, so that the remaining data can still be found
                failed.append({
                    'hash': h,
                    'reason': 'Cannot delete %s: %s; the torrent was stopped and kept.' % result['errors'][0],
                })
                continue
            self._logger.info('Deleted %d file(s) of %s in %s.', len(result['deleted']), self._name(h), local[1])

            s, f = self._erase([h])
            success += s
            failed += f
        return (success, failed)

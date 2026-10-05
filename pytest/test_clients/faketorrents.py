#-*- coding:utf-8 -*-
# Creates fake torrents for the integration tests.
#
# The content is random bytes generated on the fly, and the .torrent files are
# built here, so no real content is ever downloaded or shared. The torrents are
# private (no DHT/PEX) and their only tracker is on 127.0.0.1, so they never
# leave the test machine.
import hashlib
import os

PIECE_LENGTH = 16384
FAKE_TRACKER = 'http://127.0.0.1:1/announce'

def bencode(obj):
    if isinstance(obj, int):
        return b'i%de' % obj
    if isinstance(obj, str):
        obj = obj.encode('utf-8')
    if isinstance(obj, bytes):
        return b'%d:%s' % (len(obj), obj)
    if isinstance(obj, list):
        return b'l' + b''.join(bencode(x) for x in obj) + b'e'
    if isinstance(obj, dict):
        items = sorted((k.encode('utf-8') if isinstance(k, str) else k, v) for k, v in obj.items())
        return b'd' + b''.join(bencode(k) + bencode(v) for k, v in items) + b'e'
    raise TypeError('Cannot bencode %r' % (obj,))

def _write(path, content):
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path, 'wb') as f:
        f.write(content)

class FakeTorrent(object):
    """A torrent of random data, written below `directory` (a local path)

    files: a list of relative paths (multi-file torrent), or None for a single-file torrent
    """
    def __init__(self, directory, name, files=None, size=40000):
        self.name = name
        self.is_multi = files is not None
        self.files = []  # Local paths of the data
        contents = []
        file_entries = []
        for rel in (files if self.is_multi else [name]):
            content = os.urandom(size)
            path = os.path.join(directory, name, rel) if self.is_multi else os.path.join(directory, name)
            _write(path, content)
            self.files.append(path)
            contents.append(content)
            file_entries.append({'length': size, 'path': rel.split('/')})

        data = b''.join(contents)
        pieces = b''.join(hashlib.sha1(data[i:i+PIECE_LENGTH]).digest() for i in range(0, len(data), PIECE_LENGTH))
        info = {'name': name, 'piece length': PIECE_LENGTH, 'pieces': pieces, 'private': 1}
        if self.is_multi:
            info['files'] = file_entries
        else:
            info['length'] = size
        self.base = os.path.join(directory, name)
        self.hash = hashlib.sha1(bencode(info)).hexdigest().upper()
        self.torrent = bencode({'announce': FAKE_TRACKER, 'info': info})

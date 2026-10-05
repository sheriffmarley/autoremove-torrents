#-*- coding:utf-8 -*-
import os
import pytest
from autoremovetorrents.exception.unsafepath import UnsafePath
from autoremovetorrents.util.safedelete import check_torrent_paths, map_path, \
    normalize_config_dir, LocalDataDeleter, local_deletion_supported

needs_local = pytest.mark.skipif(not local_deletion_supported(), reason='dir_fd is not supported')

# ---------- String checks ----------

@pytest.mark.parametrize('base, files, is_multi, roots', [
    ('/data2/t', ['/data2/t/a'], True, ['/data']),           # prefix is not component-wise
    ('/data', ['/data/a'], True, ['/data']),                 # base equals root
    ('/', ['/etc/passwd'], True, ['/data']),                 # base is /
    ('/data/t/../../etc', ['/etc/passwd'], True, ['/data']), # ..
    ('/data/t', ['/data/t/../../etc/passwd'], True, ['/data']),
    ('/data//t', ['/data//t/a'], True, ['/data']),           # //
    ('//data/t', ['//data/t/a'], True, ['/data']),
    ('/data/t', ['/data/t/a\n/etc/passwd'], True, ['/data']), # line break
    ('/data/t', ['/data/t/a\0'], True, ['/data']),           # NUL
    ('/data/t', ['/data/other/a'], True, ['/data']),         # file outside of base
    ('/data/t', ['/data/t'], True, ['/data']),               # multi-file: file equals base
    ('/data/t', ['/data/t2/a'], True, ['/data']),            # prefix of base is not component-wise
    ('/data/t.mkv', ['/data/x.mkv'], False, ['/data']),      # single-file: file isn't base
    ('/data/t.mkv', ['/data/t.mkv', '/data/u.mkv'], False, ['/data']),
    ('/data/t', [], True, ['/data']),                        # no files
    ('data/t', ['data/t/a'], True, ['/data']),               # relative
    ('/data/t', ['/data/t/a'], True, []),                    # no allowed paths
])
def test_unsafe_paths_are_rejected(base, files, is_multi, roots):
    with pytest.raises(UnsafePath):
        check_torrent_paths(base, files, is_multi, roots)

def test_safe_paths_are_accepted():
    check_torrent_paths('/data/t', ['/data/t/a', '/data/t/sub/b'], True, ['/other', '/data'])
    check_torrent_paths('/data/t.mkv', ['/data/t.mkv'], False, ['/data'])

@pytest.mark.parametrize('path', ['/', '', 'data', '/data/../etc', '//data', '/da\nta', None])
def test_bad_config_dirs_are_rejected(path):
    with pytest.raises(UnsafePath):
        normalize_config_dir(path)

def test_config_dir_trailing_slash():
    assert normalize_config_dir('/data/') == '/data'

def test_map_path_is_component_wise():
    mapping = {'/data': '/mnt/a', '/data/x': '/mnt/b'}
    assert map_path('/data/t/f', mapping) == '/mnt/a/t/f'
    assert map_path('/data/x/f', mapping) == '/mnt/b/f'  # longest prefix wins
    assert map_path('/data', mapping) == '/mnt/a'
    assert map_path('/data2/f', mapping) is None
    assert map_path('/data2/f', {}) == '/data2/f'

# ---------- Local deletion ----------

def _make(path, content='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)

def _snapshot(path):
    result = set()
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            result.add(os.path.relpath(os.path.join(dirpath, name), path))
    return result

@pytest.fixture
def tree(tmp_path):
    root = str(tmp_path / 'downloads')
    outside = str(tmp_path / 'outside')
    _make(os.path.join(outside, 'precious.txt'))
    _make(os.path.join(outside, 'sub', 'precious2.txt'))
    os.makedirs(root)
    return root, outside

@needs_local
def test_multi_file_torrent_is_deleted(tree):
    root, _ = tree
    base = os.path.join(root, 'Torrent')
    files = [os.path.join(base, 'a.mkv'), os.path.join(base, 'sub', 'b.nfo')]
    for f in files:
        _make(f)
    deleter = LocalDataDeleter(root, base, files, True)
    assert deleter.verify() == 2
    result = deleter.delete()
    assert result['errors'] == []
    assert sorted(result['deleted']) == sorted(files)
    assert os.listdir(root) == []

@needs_local
def test_single_file_torrent_is_deleted(tree):
    root, _ = tree
    base = os.path.join(root, 'movie.mkv')
    _make(base)
    _make(os.path.join(root, 'other.mkv'))
    result = LocalDataDeleter(root, base, [base], False).delete()
    assert result['errors'] == []
    assert os.listdir(root) == ['other.mkv']

@needs_local
def test_foreign_files_are_kept(tree):
    root, _ = tree
    base = os.path.join(root, 'Torrent')
    _make(os.path.join(base, 'a.mkv'))
    _make(os.path.join(base, 'extracted.mkv'))  # Created by another program
    result = LocalDataDeleter(root, base, [os.path.join(base, 'a.mkv')], True).delete()
    assert result['errors'] == []
    assert result['kept_dirs'] == [base]
    assert os.listdir(base) == ['extracted.mkv']

@needs_local
def test_symlink_as_intermediate_directory_is_refused(tree):
    root, outside = tree
    base = os.path.join(root, 'Torrent')
    os.makedirs(base)
    os.symlink(outside, os.path.join(base, 'sub'))  # Torrent/sub -> outside
    before = _snapshot(outside)
    deleter = LocalDataDeleter(root, base, [os.path.join(base, 'sub', 'precious.txt')], True)
    with pytest.raises(UnsafePath):
        deleter.verify()
    # Even if verify() was skipped, delete() never follows the link
    result = deleter.delete()
    assert result['deleted'] == []
    assert _snapshot(outside) == before

@needs_local
def test_symlink_as_base_is_refused(tree):
    root, outside = tree
    base = os.path.join(root, 'Torrent')
    os.symlink(outside, base)  # Torrent -> outside
    before = _snapshot(outside)
    files = [os.path.join(base, 'precious.txt'), os.path.join(base, 'sub', 'precious2.txt')]
    deleter = LocalDataDeleter(root, base, files, True)
    with pytest.raises(UnsafePath):
        deleter.verify()
    result = deleter.delete()
    assert result['deleted'] == []
    assert _snapshot(outside) == before
    assert os.path.islink(base)

@needs_local
def test_symlink_as_leaf_removes_only_the_link(tree):
    root, outside = tree
    base = os.path.join(root, 'Torrent')
    os.makedirs(base)
    link = os.path.join(base, 'a.mkv')
    os.symlink(os.path.join(outside, 'precious.txt'), link)
    before = _snapshot(outside)
    deleter = LocalDataDeleter(root, base, [link], True)
    assert deleter.verify() == 1
    result = deleter.delete()
    assert result['deleted'] == [link]
    assert not os.path.lexists(link)
    assert _snapshot(outside) == before

@needs_local
def test_directory_as_leaf_is_refused(tree):
    root, _ = tree
    base = os.path.join(root, 'Torrent')
    _make(os.path.join(base, 'a.mkv', 'inner.txt'))  # a.mkv is a directory
    deleter = LocalDataDeleter(root, base, [os.path.join(base, 'a.mkv')], True)
    with pytest.raises(UnsafePath):
        deleter.verify()
    result = deleter.delete()
    assert result['deleted'] == []
    assert os.path.exists(os.path.join(base, 'a.mkv', 'inner.txt'))

@needs_local
def test_base_outside_of_root_is_refused(tree):
    root, outside = tree
    with pytest.raises(UnsafePath):
        LocalDataDeleter(root, outside, [os.path.join(outside, 'precious.txt')], True)
    with pytest.raises(UnsafePath):
        LocalDataDeleter(root, root, [os.path.join(root, 'x')], True)

@needs_local
def test_missing_files_are_ignored(tree):
    root, _ = tree
    base = os.path.join(root, 'Torrent')
    files = [os.path.join(base, 'a.mkv')]
    deleter = LocalDataDeleter(root, base, files, True)
    assert deleter.verify() == 0
    result = deleter.delete()
    assert result == {'deleted': [], 'errors': [], 'kept_dirs': []}

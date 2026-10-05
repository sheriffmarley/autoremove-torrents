#-*- coding:utf-8 -*-
# Guarded deletion of torrent data.
#
# Two guarantees are enforced here:
#   1. Only the files a torrent reports as its own are deleted, and only when
#      every one of them lies inside the torrent's base path.
#   2. The base path itself must lie inside one of the directories the user
#      allowed explicitly (allowed_paths).
#
# The path checks are done on the strings first (no "..", no "//", no line
# breaks, component-wise prefix matching). The local deletion then walks every
# path one directory at a time with O_NOFOLLOW and works relative to directory
# file descriptors, so a symlink anywhere below the allowed directory can never
# redirect a deletion to a place outside of it, even if it appears after the
# check (no time-of-check/time-of-use gap).
import errno
import os
import posixpath
import stat
from ..exception.unsafepath import UnsafePath

try: # for Python 2.7
    _string_types = basestring
except NameError: # for Python 3
    _string_types = str

# Is it a clean, absolute and normalized POSIX path?
def is_clean_path(path):
    return isinstance(path, _string_types) \
        and path.startswith('/') \
        and not path.startswith('//') \
        and not any(c in path for c in ('\0', '\n', '\r')) \
        and posixpath.normpath(path) == path

# Does the path lie strictly inside root (component-wise, never equal)?
def is_strictly_under(path, root):
    return root != '/' and path.startswith(root + '/') and len(path) > len(root) + 1

# Normalize a directory given in the configuration
def normalize_config_dir(path):
    if not isinstance(path, _string_types):
        raise UnsafePath('The path %r is not a string.' % (path,))
    path = path.rstrip('/') if path != '/' else path
    if not is_clean_path(path) or path == '/':
        raise UnsafePath("The path '%s' must be an absolute and normalized path, and it can't be '/'." % path)
    return path

# Find the allowed directory which contains the path
def find_root(path, roots):
    for root in roots:
        if is_strictly_under(path, root):
            return root
    return None

# Check the paths reported by the client, raises UnsafePath if anything looks wrong
def check_torrent_paths(base, files, is_multi, allowed_roots):
    if len(allowed_roots) == 0:
        raise UnsafePath('No allowed_paths are configured, refusing to delete any data.')
    if not is_clean_path(base) or base == '/':
        raise UnsafePath("The base path '%s' is not a clean absolute path." % base)
    if find_root(base, allowed_roots) is None:
        raise UnsafePath("The base path '%s' is outside of the allowed paths (%s)." %
            (base, ', '.join(allowed_roots)))
    if len(files) == 0:
        raise UnsafePath("The client didn't report any files of '%s'." % base)
    for file_ in files:
        if not is_clean_path(file_):
            raise UnsafePath("The file path '%s' is not a clean absolute path." % file_)
        if is_multi:
            if not is_strictly_under(file_, base):
                raise UnsafePath("The file '%s' is outside of the base path '%s'." % (file_, base))
        elif file_ != base:
            raise UnsafePath("The file '%s' of a single-file torrent isn't its base path '%s'." % (file_, base))

# Map a path seen by the client to a local path; returns None if no mapping matches
def map_path(path, mapping):
    if len(mapping) == 0:
        return path
    for remote in sorted(mapping, key=len, reverse=True):
        if path == remote or path.startswith(remote + '/'):
            return mapping[remote] + path[len(remote):]
    return None

# Is the local deletion supported on this platform?
def local_deletion_supported():
    supports_dir_fd = getattr(os, 'supports_dir_fd', set())
    return all(func in supports_dir_fd for func in (os.open, os.stat, os.unlink, os.rmdir)) \
        and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')

def _dir_flags():
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)

# Errors raised when a path component is a symlink (or not a directory) under O_NOFOLLOW
_NOT_A_REAL_DIRECTORY = (errno.ELOOP, errno.ENOTDIR, getattr(errno, 'EMLINK', errno.ELOOP))

# Open root/components[0]/components[1]/... without following any symlink
def _open_dir_chain(root_fd, components):
    fd = os.dup(root_fd)
    try:
        for component in components:
            next_fd = os.open(component, _dir_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise

class LocalDataDeleter(object):
    # root: the allowed directory (local path), base: base path of the torrent,
    # files: files of the torrent, is_multi: whether the torrent is a multi-file torrent
    def __init__(self, root, base, files, is_multi):
        if not local_deletion_supported():
            raise UnsafePath('Deleting local data requires Python 3.3+ on a POSIX system.')
        root = normalize_config_dir(root)
        check_torrent_paths(base, files, is_multi, [root])
        self._root = root
        self._base = base
        self._files = sorted(set(files))
        self._is_multi = is_multi

    # Split a path into its parent components (relative to root) and its leaf
    def _split(self, path):
        components = path[len(self._root)+1:].split('/')
        return components[:-1], components[-1]

    # Check every file without changing anything; raises UnsafePath
    # Returns the number of files which still exist
    def verify(self):
        existing = 0
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for file_ in self._files:
                if self._inspect(root_fd, file_):
                    existing += 1
        finally:
            os.close(root_fd)
        return existing

    # Returns True if the file exists, False if it's gone; raises UnsafePath
    def _inspect(self, root_fd, file_):
        parents, leaf = self._split(file_)
        try:
            fd = _open_dir_chain(root_fd, parents)
        except OSError as e:
            if e.errno == errno.ENOENT:
                return False
            if e.errno in _NOT_A_REAL_DIRECTORY:
                raise UnsafePath("A component of '%s' is a symlink or not a directory." % file_)
            raise UnsafePath("Can't check '%s': %s" % (file_, e))
        try:
            st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        except OSError as e:
            if e.errno == errno.ENOENT:
                return False
            raise UnsafePath("Can't check '%s': %s" % (file_, e))
        finally:
            os.close(fd)
        if stat.S_ISDIR(st.st_mode):
            raise UnsafePath("'%s' is a directory, not a file." % file_)
        return True

    # Delete the files and the directories they leave empty
    # Returns a dict: deleted (list), errors (list of (path, reason)), kept_dirs (list)
    def delete(self):
        result = {'deleted': [], 'errors': [], 'kept_dirs': []}
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for file_ in self._files:
                self._unlink(root_fd, file_, result)
            if self._is_multi:
                for dir_ in self._dirs_to_remove():
                    self._rmdir(root_fd, dir_, result)
        finally:
            os.close(root_fd)
        return result

    def _unlink(self, root_fd, file_, result):
        parents, leaf = self._split(file_)
        try:
            fd = _open_dir_chain(root_fd, parents)
        except OSError as e:
            if e.errno != errno.ENOENT:
                result['errors'].append((file_, str(e)))
            return
        try:
            st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                result['errors'].append((file_, 'it is a directory'))
                return
            # A symlink as the leaf is removed as the link itself, its target stays untouched
            os.unlink(leaf, dir_fd=fd)
            result['deleted'].append(file_)
        except OSError as e:
            if e.errno != errno.ENOENT:
                result['errors'].append((file_, str(e)))
        finally:
            os.close(fd)

    # Directories inside the base path (base included), the deepest first
    def _dirs_to_remove(self):
        dirs = set([self._base])
        for file_ in self._files:
            parent = posixpath.dirname(file_)
            while is_strictly_under(parent, self._base):
                dirs.add(parent)
                parent = posixpath.dirname(parent)
        return sorted(dirs, key=lambda d: d.count('/'), reverse=True)

    def _rmdir(self, root_fd, dir_, result):
        parents, leaf = self._split(dir_)
        try:
            fd = _open_dir_chain(root_fd, parents)
        except OSError:
            return
        try:
            # rmdir never follows symlinks and only removes empty directories,
            # so files which don't belong to the torrent are always kept
            os.rmdir(leaf, dir_fd=fd)
        except OSError as e:
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                result['kept_dirs'].append(dir_)
        finally:
            os.close(fd)

"""Shared in-memory remote path and file safety helpers."""

# This source uses only modules already in the remote Python standard library.
SOURCE = r'''
import os, sys, json, stat, hashlib, codecs, difflib
LIMIT = 4 * 1024 * 1024
CAP = 24000
published = False
streaming = False

def under(p, r):
    return p == r or p.startswith(r.rstrip('/') + '/')

def canonical(p):
    # os.path.realpath(strict=True) exists from Python 3.10; keep the same
    # existence guarantee on older interpreters without weakening the check.
    try:
        return os.path.realpath(p, strict=True)
    except TypeError:
        real = os.path.realpath(p)
        if not os.path.exists(real):
            raise ValueError('路径不存在')
        return real
    except (FileNotFoundError, NotADirectoryError):
        raise ValueError('路径不存在')

def guarded(p):
    if not isinstance(p, str) or not p or '\x00' in p:
        raise ValueError('无效路径')
    if p == '~' or p.startswith('~/'):
        p = roots[0] + p[1:]
    if not p.startswith('/'):
        p = roots[0] + '/' + p
    p = os.path.normpath(p)
    authorized = [os.path.normpath(r) for r in roots]
    canonical_roots = []
    for r in authorized:
        target = os.path.realpath(r)
        # A root symlink must not widen the configured authorization to a
        # parent, /, or another user's directory.
        if not any(under(target, original) for original in authorized):
            raise ValueError('根目录符号链接目标越界：必须仍位于授权 roots 内')
        canonical_roots.append(target)
    if (not any(under(p, r) for r in authorized)
            and not any(under(p, r) for r in canonical_roots)):
        raise ValueError('路径越界：只允许配置的代码、数据和临时目录')
    real = canonical(p)
    # Roots themselves may be symlinks; accept both the configured spelling
    # and its canonical target so resolved paths can be checked again.
    if not any(under(real, r) for r in canonical_roots):
        raise ValueError('符号链接目标越界')
    for area in ('/home', '/data'):
        if under(real, area) and not any(under(real, r) for r in canonical_roots[:2]):
            raise ValueError('禁止访问其他用户目录')
    if any(under(real, r) for r in canonical_roots[2:]) and os.stat(real).st_uid != os.getuid():
        raise ValueError('临时文件或目录不属于当前服务器用户')
    return real

def directory(p):
    # Walk the canonical path with O_NOFOLLOW: ancestor replacement cannot
    # redirect an operation through a symlink after realpath validation.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in p.split('/'):
            if part:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise

def opened(p):
    parent = directory(os.path.dirname(p))
    try:
        fd = os.open(os.path.basename(p), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode):
            os.close(fd)
            raise ValueError('只支持普通文件')
        if any(under(p, r) for r in roots[2:]) and s.st_uid != os.getuid():
            os.close(fd)
            raise ValueError('临时文件不属于当前服务器用户')
        return parent, os.fdopen(fd, 'rb')
    except BaseException:
        os.close(parent)
        raise

def identity(s):
    return dict(device=s.st_dev, inode=s.st_ino, size=s.st_size,
                mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns)

def meta(s):
    return dict(size=s.st_size, mtime_ns=s.st_mtime_ns)

def same(f, p, parent, before):
    return identity(os.fstat(f.fileno())) == before and identity(os.stat(os.path.basename(p), dir_fd=parent, follow_symlinks=False)) == before

def emit(value):
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()
'''

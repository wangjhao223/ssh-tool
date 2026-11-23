"""Trusted roots and local request path validation."""
import os

from . import config
from .validation import FileToolError


def roots_config(server=None, context=None):
    return config.trusted_roots(config.effective_config(server) if context is None else context)


def request_path(path, roots):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise FileToolError("无效路径")
    if path == "~" or path.startswith("~/"):
        path = roots[0] + path[1:]
    if not path.startswith("/"):
        path = roots[0] + "/" + path
    path = os.path.normpath(path)
    if not any(path == r or path.startswith(r + "/") for r in roots):
        raise FileToolError("路径越界：只允许代码、数据和当前用户的临时目录")
    return path

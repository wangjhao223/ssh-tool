"""Read bounded local help topics without remote access or directory creation."""
import json
import os
import stat
from pathlib import Path

import file_tools
from remote_ws import VERSION, config

TOPIC_DIR = Path(__file__).resolve().parents[1] / 'docs' / 'topics'
TITLES = {
    'connection': '登录与连接',
    'files': '文件与编辑',
    'downloads': '下载、进度与续传',
    'jobs': '命令、后台任务和 GPU',
    'troubleshooting': '故障处理',
    'environment': '服务器环境使用与历史记录',
    'maintenance': '本地结构与维护',
}
TOPICS = set(TITLES)
MAX_CHARS = 6000
MAX_FILE_BYTES = 128 * 1024


def _read_local(root, parts, optional=False, private=False, metadata_only=False):
    descriptors = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for index, part in enumerate(parts):
            current = os.fstat(descriptor)
            if private and (current.st_uid != os.getuid() or current.st_mode & 0o022):
                raise file_tools.FileToolError('本地说明目录须属当前用户且不可被其他用户写入')
            if metadata_only and index == len(parts) - 1:
                before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                break
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        if not metadata_only:
            before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise file_tools.FileToolError('本地指南/说明须为无符号链接的普通文件')
        if private and (before.st_uid != os.getuid() or before.st_mode & 0o022):
            raise file_tools.FileToolError('本地说明须属当前用户且不可被其他用户写入')
        if before.st_size > MAX_FILE_BYTES:
            raise file_tools.FileToolError('本地指南/说明过大')
        if metadata_only:
            return True
        chunks, size = [], 0
        while size <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, min(16384, MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if size > MAX_FILE_BYTES:
            raise file_tools.FileToolError('本地指南/说明过大')
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise file_tools.FileToolError('本地指南/说明读取期间变化')
        text = b''.join(chunks).decode('utf-8')
        if '\x00' in text:
            raise file_tools.FileToolError('本地指南/说明不是 UTF-8 文本')
        return text
    except FileNotFoundError as error:
        if optional:
            return None
        raise file_tools.FileToolError('本地指南主题缺失') from error
    except (OSError, UnicodeError) as error:
        raise file_tools.FileToolError('无法安全读取本地 UTF-8 指南/说明') from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_text(root, parts, optional=False, private=False):
    return _read_local(root, parts, optional=optional, private=private)


def _field(context, name, default=None):
    return context.get(name.upper(), default)


def _context(server):
    return config.resolve_context(server, allow_unconfigured=True)


def _server_info(context):
    name = _field(context, 'server')
    if not isinstance(name, str) or not config.SERVER_RE.fullmatch(name):
        raise file_tools.FileToolError('配置返回无效服务器名')
    configured = _field(context, 'configured', True)
    return dict(server=name, project=_field(context, 'project'),
                server_source=_field(context, 'server_source'),
                project_source=_field(context, 'project_source'),
                source=_field(context, 'source', _field(context, 'server_source')),
                configured=configured, status='configured' if configured else 'unconfigured',
                configuration_status='已配置' if configured else '未配置（仅保留服务器名录）')


def _workspace(context, verbose):
    value = {key: item for key, item in _server_info(context).items()
             if key not in {'source', 'status', 'configuration_status'}}
    task = file_tools.task_name()
    code_root = _field(context, 'code_root') if value['configured'] else None
    data_root = _field(context, 'data_root') if value['configured'] else None
    value.update(code_root=code_root, data_root=data_root,
                 conda_root=_field(context, 'conda_root') if value['configured'] else None,
                 conda_init=_field(context, 'conda_init') if value['configured'] else None, task=task,
                 download_directory=str(Path(config.DOWNLOAD_ROOT) / task))
    if not value['configured']:
        value['unconfigured_reason'] = '未配置：仅保留服务器名录，缺少完整的独立服务器配置'
    if verbose:
        value.update(servers=config.configured_servers(),
                     tool_root=config.PROJECT_ROOT, config_directory=config.CONFIG_DIR,
                     state_directory=config.STATE_DIR, download_root=config.DOWNLOAD_ROOT,
                     tool_version=VERSION, project_config='不需要项目级注册；直接共用版使用用户级 MCP 注册',
                     config_source=_field(context, 'config_source', value['server_source']))
    return value


def _page(topic, lines, start, limit):
    selected, used = [], 0
    for line in lines[start-1:start-1+limit]:
        cost = len(json.dumps(line, ensure_ascii=False))
        if used + cost > MAX_CHARS:
            if not selected:
                raise file_tools.FileToolError('指南单行过长；请本地查看对应主题文件')
            break
        selected.append(line)
        used += cost
    following = start + len(selected)
    return dict(topic=topic, title=TITLES[topic], start_line=start,
                text=''.join(selected), next_line=following if following <= len(lines) else None)


def get_guide(args):
    if set(args) - {'topic', 'start_line', 'max_lines', 'server', 'verbose'}:
        raise file_tools.FileToolError('指南仅接受 topic/start_line/max_lines/server/verbose')
    topic = args.get('topic', 'index')
    if not isinstance(topic, str) or topic not in TOPICS | {'index', 'workspace'}:
        raise file_tools.FileToolError('未知指南主题；省略 topic 查看目录')
    if 'server' in args and not isinstance(args['server'], str):
        raise file_tools.FileToolError('server 必须是字符串')
    server = args.get('server') or None
    verbose = args.get('verbose', False)
    if type(verbose) is not bool:
        raise file_tools.FileToolError('verbose 必须是布尔值')
    start = file_tools.integer(args, 'start_line', 1, 1, 2**31-1)
    limit = file_tools.integer(args, 'max_lines', 80, 1, 2**31-1)
    if topic == 'index':
        value = dict(topics=[{'topic': 'workspace', 'title': '当前本地配置'}] +
                     [dict(topic=name, title=title) for name, title in TITLES.items()])
    elif topic == 'workspace':
        value = _workspace(_context(server), verbose)
    else:
        text = _read_text(TOPIC_DIR.parent, ('topics', topic + '.md'))
        metadata = {}
        if topic == 'environment':
            context = _context(server)
            metadata = _server_info(context)
            config.check_config_dir()
            parts = ('server-guides', metadata['server'] + '.md')
            if verbose:
                notes = _read_text(config.CONFIG_DIR, parts, optional=True, private=True)
                metadata['server_notes_available'] = notes is not None
                if notes is not None:
                    text += ('\n' if text and not text.endswith('\n') else '')
                    text += '\n## ' + metadata['server'] + ' — 本地服务器说明（非实时诊断）\n\n' + notes
                metadata['server_notes_path'] = str(Path(config.CONFIG_DIR).joinpath(*parts))
            else:
                metadata['server_notes_available'] = _read_local(
                    config.CONFIG_DIR, parts, optional=True, private=True, metadata_only=True) is not None
        value = _page(topic, text.splitlines(keepends=True), start, limit)
        value.update(metadata)
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if len(text) > MAX_CHARS + 1500:
        raise file_tools.FileToolError('指南或配置元数据过长')
    return text


def tools():
    return [('get_guide', get_guide,
             '本地只读指南；workspace 查配置；environment 默认摘要，verbose 才读所选说明；分页用 start_line/max_lines 与 next_line。',
             {'type': 'object', 'properties': {
                 'topic': {'type': 'string', 'enum': ['index', 'workspace'] + sorted(TOPICS),
                           'default': 'index', 'description': '主题；默认 index'},
                 'start_line': {'type': 'integer', 'minimum': 1, 'default': 1,
                                'description': '起始行'},
                 'max_lines': {'type': 'integer', 'minimum': 1, 'default': 80,
                               'description': '最多请求行数；按字符预算截断，可用 next_line 续读'},
                 'server': {'type': 'string', 'description': 'workspace/environment 服务器；默认当前服务器'},
                 'verbose': {'type': 'boolean', 'default': False,
                             'description': '展开 workspace 名录或读取所选 environment 说明'}},
              'additionalProperties': False})]

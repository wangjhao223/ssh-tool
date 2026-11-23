"""Remote file tools and compatibility entry points; Python stdlib only.

REMOTE_SOURCE is sent to python3 -c in memory. No helper is saved on the server.
User values travel solely as JSON on SSH stdin, never in the shell command.
"""
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import local_connection as connection
from remote_ws import config, downloads, download_stream, paths, transport
from remote_ws.sources.files import FILE_OPERATION_SOURCE, REMOTE_SOURCE
from remote_ws.validation import FileToolError, bad_args, clamped_integer, validate_fields

from remote_ws.download_state import (
    ACTIVE, ROW_KEYS, SUMMARY_KEYS, TASK_RE, active_lock, alive, atomic_state,
    busy_report, downloads_dir, integer, jobs_dir, load_state, pid_identity, report,
    report_state, safe_local, task_name, task_path, verbosity, verify_local_file,
)
from remote_ws.download_stream import Cancelled
from remote_ws import download_state

MAX_TEXT = 24000
EDIT_LIMIT = 4 * 1024 * 1024
READ_COMPAT_KEYS = ('offset', 'limit', 'query', 'file_glob', 'context_lines',
                    'case_sensitive', 'include_sha256', 'files', 'depth')
BRANCH_TYPES = {
    'list': dict(offset=int, limit=int, compact=bool, depth=int),
    'read': dict(start_line=int, max_lines=int, ranges=list, max_chars=int, compact=bool),
    'search': dict(query=str, file_glob=str, context_lines=int, limit=int, max_chars=int,
                   case_sensitive=bool, include_sha256=bool),
    'project': dict(files=list, depth=int, offset=int, limit=int, max_chars=int),
}
COMMON_TYPES = dict(action=str, path=str, server=str)
QUERY_TYPES = {**COMMON_TYPES,
               **{key: kind for fields in BRANCH_TYPES.values() for key, kind in fields.items()}}
QUERY_FIELD_ORDER = ('offset', 'limit', 'start_line', 'max_lines', 'ranges', 'max_chars',
                     'compact', 'query', 'file_glob', 'context_lines', 'case_sensitive',
                     'include_sha256', 'files', 'depth')
REDIRECT_TARGETS = {
    'offset': 'list/project', 'limit': 'list/search/project',
    'start_line': 'read', 'max_lines': 'read', 'ranges': 'read',
    'max_chars': 'read/search/project', 'compact': 'read',
    'query': 'search', 'file_glob': 'search', 'context_lines': 'search',
    'case_sensitive': 'search', 'include_sha256': 'search',
    'files': 'project', 'depth': 'project',
}


def dump_value(value, notes=(), overflow='元数据或输出过长，请缩小分页范围'):
    notes = [note for note in notes if note]
    if notes:
        value = dict(value)
        value['parameter_note'] = '；'.join(notes)
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if len(text) > MAX_TEXT:
        raise FileToolError(f'{overflow}（上限 {MAX_TEXT} 字符）')
    return text


def attach_note(text, notes):
    notes = [note for note in notes if note]
    if not notes:
        return text
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise FileToolError('无法附加兼容提示：远端响应不是有效 JSON') from error
    if not isinstance(value, dict):
        raise FileToolError('无法附加兼容提示：远端响应不是 JSON 对象')
    return dump_value(value, notes)


def _neutral_value(key, value):
    kind = QUERY_TYPES[key]
    if kind is bool:
        return value is False
    if kind is int:
        return type(value) is int and value == 0
    if kind is str:
        return value == ''
    if kind is list:
        return value == []
    return False


def _neutral_literal(key):
    kind = QUERY_TYPES[key]
    if kind is bool:
        return 'false'
    if kind is int:
        return '0'
    if kind is str:
        return '""'
    if kind is list:
        return '[]'
    return '中性值'


def _neutral_note(action, key):
    return f'{key}={_neutral_literal(key)} 已忽略：action={action} 不使用该字段'


def _cross_branch_error(action, key):
    target = REDIRECT_TARGETS.get(key, '对应 action')
    return bad_args(FileToolError,
                    f'action={action} 不接受 {key}；保留 action={action} 时请删除 {key}。'
                    f'仅需要该字段的功能时改用 action={target}。')


def ensure_login(server):
    return transport.ensure_login(server)


def ssh_command(server, source=None):
    return transport.ssh_command(server, REMOTE_SOURCE if source is None else source)


def remote_json(server, payload, timeout=120, source=None):
    return transport.remote_json(server, payload, timeout=timeout,
                                 source=REMOTE_SOURCE if source is None else source)


def file_call(operation, args):
    context = config.resolve_context(args.get('server'))
    server = context['SERVER']
    roots = roots_config(context=context)
    payload = dict(operation=operation, roots=roots, path=request_path(args.get('path'), roots))
    if operation == 'list':
        payload.update(offset=integer(args, 'offset', 0, 0, 2**63-1), limit=integer(args, 'limit', 25, 1, 1000))
    elif operation == 'read':
        compact=args.get('compact',True)
        if type(compact) is not bool:
            raise FileToolError('compact 必须是布尔值')
        multi='ranges' in args
        if multi and ('start_line' in args or 'max_lines' in args or not compact):
            raise FileToolError('ranges 不与 start_line/max_lines 或 compact=false 混用')
        ranges=args.get('ranges',[dict(start_line=integer(args,'start_line',1,1,2**63-1),max_lines=integer(args,'max_lines',100,1,1000))])
        if not isinstance(ranges,list) or not 1<=len(ranges)<=8:
            raise FileToolError('ranges 必须含 1–8 个 start_line/max_lines 行段')
        values=[]
        for item in ranges:
            if not isinstance(item,dict) or set(item)!={'start_line','max_lines'}:
                raise FileToolError('行段仅接受 start_line/max_lines')
            values.append(dict(start_line=integer(item,'start_line',1,1,2**63-1),max_lines=integer(item,'max_lines',100,1,1000)))
        if sum(r['max_lines'] for r in values)>2000 or any(
            values[i]['start_line']<values[i-1]['start_line']+values[i-1]['max_lines'] for i in range(1,len(values))):
            raise FileToolError('行段须按顺序且不重叠，总计最多 2000 行')
        payload.update(ranges=values,multi_range=multi,compact=compact,max_chars=integer(args,'max_chars',3000,256,20000))
    elif operation == 'edit':
        edits = args.get('edits')
        digest = args.get('expected_sha256', '')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
            raise FileToolError('expected_sha256 必须是整文件 SHA-256')
        if not isinstance(edits, list) or not 1 <= len(edits) <= 100 or any(
            not isinstance(e, dict) or not isinstance(e.get('old_text'), str) or not e['old_text']
            or not isinstance(e.get('new_text'), str) for e in edits):
            raise FileToolError('edits 必须含 1–100 项 old_text/new_text 精确替换')
        if sum(len(e['old_text'].encode()) + len(e['new_text'].encode()) for e in edits) > 2 * EDIT_LIMIT:
            raise FileToolError('替换参数过大')
        include=args.get('include_diff',False)
        if type(include) is not bool:
            raise FileToolError('include_diff 必须是布尔值')
        payload.update(edits=edits,expected_sha256=digest.lower(),include_diff=include,
                       max_chars=integer(args,'max_chars',1000,256,6000))
    value = remote_json(server, payload)
    return dump_value(value)


def roots_config(server=None, context=None):
    return paths.roots_config(server, context=context)


def request_path(path, roots):
    return paths.request_path(path, roots)


def atomic_state(state):
    return download_state.atomic_state(state, sync=os.fsync, replace=os.replace)


def destination(server, remote_path, local_path):
    return downloads.destination(server, remote_path, local_path)


def start_worker(state, lock_fd):
    return downloads.start_worker(state, lock_fd)


def download_file(args):
    return downloads.download_file(args, login=ensure_login, launch_worker=start_worker,
                                   resolve_roots=roots_config, resolve_path=request_path)


def download_query(args):
    return downloads.download_query(args)


def download_status(args):
    return downloads.download_status(args)


def list_downloads(args):
    return downloads.list_downloads(args)


def download_control(args):
    return downloads.download_control(args, login=ensure_login, launch_worker=start_worker,
                                      resolve_roots=roots_config, resolve_path=request_path)


def worker(task_id, lock_fd):
    return download_stream.worker(task_id, lock_fd, command=ssh_command)


def validate_arguments(args, types, required, label):
    validate_fields(args, types, required, error_type=FileToolError, label=label)


def file_query(args):
    if not isinstance(args, dict):
        raise FileToolError('file_query 参数必须是对象')
    action = args.get('action')
    if not isinstance(action, str) or action not in BRANCH_TYPES:
        raise bad_args(FileToolError, 'action 必填，且必须是 list/read/search/project')
    required = ['action', 'path'] + (['query'] if action == 'search' else [])
    validate_arguments(args, QUERY_TYPES, required, f'action={action}')
    parameters = {key: value for key, value in args.items() if key != 'action'}
    notes = []
    if action != 'read':
        native = {'path', 'server'} | set(BRANCH_TYPES[action])
        for key in QUERY_FIELD_ORDER:
            if key in parameters and key not in native:
                if not _neutral_value(key, parameters[key]):
                    raise _cross_branch_error(action, key)
                notes.append(_neutral_note(action, key))
                parameters.pop(key)
    if action == 'list':
        parameters.pop('compact', None)
        if 'depth' in parameters:
            integer(parameters, 'depth', 2, 0, 10)
            parameters.pop('depth')
            notes.append('depth 已忽略：list 仅列当前目录，未递归；需要递归项目索引请改用 action=project。')
        parameters['limit'], clamped = clamped_integer(parameters, 'limit', 25, 1, 1000)
        if clamped:
            notes.append('limit 超过 1000，已截为 1000')
        return attach_note(file_call(action, parameters), notes)
    if action == 'read':
        ranges = parameters.get('ranges')
        if isinstance(ranges, list) and ranges:
            for item in ranges:
                validate_arguments(item, dict(start_line=int, max_lines=int),
                                   ['start_line', 'max_lines'], 'ranges 行段')
        ignored = [key for key in READ_COMPAT_KEYS if key in parameters]
        for key in ignored:
            parameters.pop(key)
        if parameters.get('ranges') == []:
            parameters.pop('ranges')
        notes = []
        if ignored:
            notes.append('read 忽略兼容参数 ' + '/'.join(ignored) + '（未发送远程）')
        if 'max_lines' in parameters:
            parameters['max_lines'], clamped = clamped_integer(parameters, 'max_lines', 100, 1, 1000)
            if clamped:
                notes.append('max_lines 超过 1000，已截为 1000')
        if isinstance(parameters.get('ranges'), list):
            for item in parameters['ranges']:
                item['max_lines'], clamped = clamped_integer(item, 'max_lines', 100, 1, 1000)
                if clamped:
                    notes.append('ranges.max_lines 超过 1000，已截为 1000')
        if parameters.get('max_chars', 0) > 20000:
            parameters['max_chars'] = 20000
            notes.append('max_chars 超出 20000，已本地截为 20000')
        text = file_call(action, parameters)
        if not notes:
            return text
        notes.append('read 操作未改变。')
        return attach_note(text, notes)
    if action == 'search':
        import text_tools
        return text_tools.search_text(parameters, notes=notes)
    import project_tools
    return project_tools.read_project(parameters, notes=notes)


def workspace_write(args):
    if not isinstance(args, dict):
        raise FileToolError('workspace_write 参数必须是对象')
    branches = {
        'edit': dict(path=str, expected_sha256=str, edits=list, include_diff=bool, max_chars=int),
        'init': dict(project=str),
    }
    action = args.get('action')
    if not isinstance(action, str) or action not in branches:
        raise FileToolError('action 必填，且必须是 edit 或 init')
    required = ['action', 'path', 'expected_sha256', 'edits'] if action == 'edit' else ['action', 'project']
    validate_arguments(args, dict(action=str, server=str, **branches[action]), required, f'action={action}')
    if action == 'edit':
        for item in args['edits']:
            validate_arguments(item, dict(old_text=str, new_text=str), ['old_text', 'new_text'], 'edits 替换')
    parameters = {key: value for key, value in args.items() if key != 'action'}
    if action == 'edit':
        return file_call('edit', parameters)
    import project_tools
    return project_tools.t_init_project(parameters)


def tools():
    def schema(properties, required=()):
        value = dict(type='object', properties=properties, additionalProperties=False)
        if required:
            value['required'] = list(required)
        return value

    def string_prop(description=None, pattern=None, default=None):
        value = {'type': 'string'}
        if description:
            value['description'] = description
        if pattern is not None:
            value['pattern'] = pattern
        if default is not None:
            value['default'] = default
        return value

    def integer_prop(description=None, minimum=None, maximum=None, default=None):
        value = {'type': 'integer'}
        if description:
            value['description'] = description
        if minimum is not None:
            value['minimum'] = minimum
        if maximum is not None:
            value['maximum'] = maximum
        if default is not None:
            value['default'] = default
        return value

    def boolean_prop(description=None, default=None, enum=None):
        value = {'type': 'boolean'}
        if description:
            value['description'] = description
        if default is not None:
            value['default'] = default
        if enum is not None:
            value['enum'] = enum
        return value

    def array_prop(description, items, min_items=None, max_items=None):
        value = {'type': 'array', 'items': items}
        if description:
            value['description'] = description
        if min_items is not None:
            value['minItems'] = min_items
        if max_items is not None:
            value['maxItems'] = max_items
        return value

    server = string_prop('服务器名。',
                         pattern='^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?$')
    file_query_schema = schema(dict(
        action={'type': 'string', 'enum': ['list', 'read', 'search', 'project']},
        path=string_prop('路径。'), server=server,
        offset=integer_prop('分页偏移。', minimum=0),
        limit=integer_prop('分页条数。', minimum=0),
        start_line=integer_prop('read 起始行。', minimum=0),
        max_lines=integer_prop('read 行数。', minimum=0),
        ranges=array_prop('', schema(dict(
            start_line=integer_prop('起始行。', minimum=1),
            max_lines=integer_prop('读取行数。', minimum=1)),
            ['start_line', 'max_lines']), max_items=8),
        max_chars=integer_prop('输出字符预算。', minimum=0),
        compact=boolean_prop(),
        query=string_prop('搜索关键词。'),
        file_glob=string_prop('search 文件名通配符。'),
        context_lines=integer_prop('', minimum=0),
        case_sensitive=boolean_prop(),
        include_sha256=boolean_prop(),
        files=array_prop('批读文件。', schema(dict(
            path=string_prop('项目内相对路径。'),
            start_line=integer_prop('起始行。', minimum=1, default=1),
            max_lines=integer_prop('读取行数。', minimum=1, default=120))),
            max_items=8),
        depth=integer_prop('索引递归深度。', minimum=0, maximum=10),
    ), ['action', 'path'])

    workspace_schema = schema(dict(
        action={'type': 'string', 'enum': ['edit', 'init']},
        path=string_prop('edit 目标路径。'), server=server,
        expected_sha256=string_prop('整文件 SHA-256。', pattern='^[0-9a-fA-F]{64}$'),
        edits=array_prop('精确替换项。', schema(dict(
            old_text=string_prop('唯一原文本。'), new_text=string_prop('替换文本。')),
            ['old_text', 'new_text']), min_items=1, max_items=100),
        include_diff=boolean_prop(),
        max_chars=integer_prop('diff 字符预算。', minimum=256, maximum=6000),
        project=string_prop('项目名。', pattern='^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'),
    ), ['action'])

    return [
        ('file_query', file_query, '列目录、读文件、字面搜索或索引/批读；只传当前 action 所需参数。',
         file_query_schema),
        ('workspace_write', workspace_write,
         'edit 按整文件 SHA-256 与唯一原文替换；init 创建项目目录。', workspace_schema),
        ('download_query', download_query, '查询下载记录。',
         schema(dict(
             action={'type': 'string', 'enum': ['list', 'status']},
             state={'type': 'string', 'enum': sorted(ACTIVE | {'completed', 'cancelled', 'interrupted'})},
             offset=integer_prop('', minimum=0),
             limit=integer_prop('', minimum=1),
             server=string_prop('服务器名。', pattern='^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?$'),
             task_id=string_prop('完整任务 ID。', pattern='^([0-9a-f]{32})?$'),
             wait_sec=integer_prop('仅 status：有界等待。', minimum=0),
             verbose=boolean_prop('', default=False),
             verify_local=boolean_prop(),
         ), ['action'])),
        ('download_control', download_control, '启动、取消或续传下载。',
         schema(dict(
             action={'type': 'string', 'enum': ['start', 'cancel', 'resume']},
             remote_path=string_prop('远端路径。'),
             server=string_prop('服务器名。', pattern='^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?$'),
             local_path=string_prop('本地路径。'),
             task_id=string_prop('完整任务 ID。', pattern='^([0-9a-f]{32})?$'),
             verbose=boolean_prop(enum=[False]),
             wait_sec=integer_prop('', minimum=0, maximum=0),
             verify_local=boolean_prop(enum=[False]),
         ), ['action'])),
    ]

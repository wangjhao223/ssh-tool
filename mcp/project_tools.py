"""Project inventory and bounded batch reading without remote deployment."""
import json
import file_tools
import local_connection
from remote_ws import commands, config, transport
from remote_ws.validation import ToolError
from remote_ws.sources.files import FILE_OPERATION_SOURCE

REMOTE_SOURCE = FILE_OPERATION_SOURCE + r'''
import errno, time
try:
    a=json.load(sys.stdin);roots=a['roots'];base=guarded(a['path'])
    if not os.path.isdir(base) or os.stat(base).st_uid!=os.getuid():
        raise ValueError('项目目录必须属于当前服务器用户')
    if not a.get('files'):
        stack=[(base,0)];entries=[];excluded=0;restricted=0;depth_limited=0;seen=0;reason=None
        deadline=time.monotonic()+5
        while stack and not reason:
            folder,depth=stack.pop();fd=directory(folder)
            try:
                if os.fstat(fd).st_uid!=os.getuid():
                    restricted+=1;continue
                with os.scandir(fd) as items:
                    for item in items:
                        if seen>=5000 or time.monotonic()>=deadline:
                            reason='scan_limit';break
                        seen+=1;s=item.stat(follow_symlinks=False)
                        rel=os.path.relpath(folder+'/'+item.name,base)
                        if item.name in {'.git','__pycache__','.venv','venv','.jobs'}:
                            excluded+=1;continue
                        if s.st_uid!=os.getuid():restricted+=1;continue
                        kind='link' if stat.S_ISLNK(s.st_mode) else 'dir' if stat.S_ISDIR(s.st_mode) else 'file' if stat.S_ISREG(s.st_mode) else 'other'
                        entries.append((rel,kind,s.st_size))
                        if kind=='dir':
                            if depth<a['depth']:stack.append((folder+'/'+item.name,depth+1))
                            else:depth_limited+=1
            finally:os.close(fd)
        entries.sort()
        selected=[];used=0
        for rel,kind,_ in entries[a['offset']:a['offset']+a['limit']]:
            line=f'{kind} {json.dumps(rel,ensure_ascii=False)}\n'
            cost=len(json.dumps(line,ensure_ascii=False))
            if used+cost>a['max_chars']:
                if not selected:raise ValueError('路径过长，请提高 max_chars')
                break
            selected.append(line);used+=cost
        following=a['offset']+len(selected)
        value=dict(path=base,index=''.join(selected),total=len(entries),
            next_offset=following if following<len(entries) else None,
            incomplete=bool(reason or restricted or depth_limited),reason=reason,
            excluded=excluded,restricted=restricted,depth_limited=depth_limited)
    else:
        results=[];pending=[];errors=[];used=0;sources=[]
        def failed(path,error,items):
            items.append(dict(path=path,error=str(error)[:200]))
        def safety(error):
            if isinstance(error,PermissionError):return True
            text=str(error)
            return text.startswith(('路径越界','符号链接目标越界','禁止访问其他用户目录',
                '临时文件或目录不属于当前服务器用户','临时文件不属于当前服务器用户',
                '临时目录不属于当前服务器用户','批量读取必须在指定项目真实目录内',
                '文件不属于当前服务器用户','无效路径'))
        def size(obj):
            return len(json.dumps(obj,ensure_ascii=False,separators=(',',':')))
        for index,item in enumerate(a['files']):
            # Reserve the envelope and continuations before spending the page on text.
            waiting=pending+a['files'][index:]
            remaining=a['max_chars']-used-size(dict(path=base,files=[],pending=waiting,complete=False))-24
            path=item['path']
            try:
                p=guarded(base+'/'+path)
                if not under(p,base):raise ValueError('批量读取必须在指定项目真实目录内')
                if os.stat(p).st_uid!=os.getuid():raise ValueError('文件不属于当前服务器用户')
            except (FileNotFoundError,NotADirectoryError,IsADirectoryError) as error:
                failed(path,error,errors);continue
            except PermissionError:
                raise
            except ValueError as error:
                if safety(error):raise
                failed(path,error,errors);continue
            skeleton=dict(path=path,sha256='0'*64,
                blocks=[dict(start_line=item['start_line'],text='',next_line=10**18)],next_line=10**18)
            room=remaining-len(json.dumps(skeleton,ensure_ascii=False,separators=(',',':')))
            if room<1:
                pending.append(item);continue
            try:
                value=operation(dict(operation='read',path=p,ranges=[dict(start_line=item['start_line'],max_lines=item['max_lines'])],
                    compact=True,multi_range=False,max_chars=room,scope_root=base,require_owner=True))
            except PermissionError:
                raise
            except (FileNotFoundError,NotADirectoryError,IsADirectoryError) as error:
                failed(path,error,errors);continue
            except ValueError as error:
                if safety(error):raise
                if str(error)=='单行超过本页剩余预算':
                    pending.append(item);continue
                failed(path,error,errors);continue
            except OSError as error:
                if error.errno in (errno.EACCES,errno.EPERM,errno.ELOOP):raise
                failed(path,error,errors);continue
            value['path']=path
            value.pop('version',None);value.pop('editable',None)
            for block in value.get('blocks',[]):
                if block.get('next_line') is None:block.pop('next_line',None)
            if value.get('next_line') is None:value.pop('next_line',None)
            cost=len(json.dumps(value,ensure_ascii=False,separators=(',',':')))
            if cost>remaining:
                pending.append(item);continue
            results.append(value);sources.append((item,value));used+=cost
            if value.get('next_line'):
                entry=dict(path=path,start_line=value['next_line'],max_lines=item['max_lines'])
                pending.append(entry)
                used+=len(json.dumps(entry,ensure_ascii=False,separators=(',',':')))
        error_count=len(errors)
        def result():
            value=dict(path=base,files=results,pending=pending,
                complete=not pending and not error_count)
            if errors:value['errors']=errors
            if len(errors)<error_count:
                value.update(error_count=error_count,errors_truncated=True)
            return value
        value=result()
        while size(value)>a['max_chars']:
            if errors:
                errors.pop()
            elif results:
                item,dropped=sources.pop();results.pop()
                if dropped.get('next_line'):
                    for index,entry in enumerate(pending):
                        if (entry.get('path')==item['path'] and entry.get('start_line')==dropped['next_line']
                                and entry.get('max_lines')==item['max_lines']):
                            pending.pop(index);break
                pending.insert(0,item)
            else:
                raise ValueError('分页元数据超过 max_chars；请提高预算或减少 files 数量')
            value=result()
        if pending and not results and not error_count:
            raise ValueError('本页无法推进；请提高 max_chars 或减少 files 数量，不要原样续读')
    emit(dict(ok=True,**value))
except Exception as error:
    emit(dict(ok=False,error=str(error)));sys.exit(1)
'''

def read_project(args, notes=()):
    context=config.resolve_context(args.get('server'))
    server=context['SERVER']
    roots=file_tools.roots_config(context=context)
    max_chars,clamped=file_tools.clamped_integer(args,'max_chars',4000,512,20000)
    payload=dict(operation='project',roots=roots,path=file_tools.request_path(args.get('path'),roots),
                 max_chars=max_chars)
    notes=list(notes)
    if clamped:
        notes.append('max_chars 超过 20000，已截为 20000')
    files=args.get('files') if 'files' in args else None
    if files is not None and type(files) is not list:
        raise file_tools.FileToolError('files 必须是数组')
    if files:
        if not 1<=len(files)<=8:
            raise file_tools.FileToolError('files 须有 1–8 个项目相对文件行段')
        ignored=[]
        for key in ('depth','offset','limit'):
            if key in args:
                if type(args[key]) is not int:
                    raise file_tools.FileToolError(f'{key} 必须是整数')
                ignored.append(key)
        hints={'depth':'项目索引深度','offset':'项目索引偏移','limit':'项目索引条数'}
        for key in ignored:
            notes.append(f'{key} 已忽略：files 批读不使用{hints[key]}（未发送远程）')
        values=[]
        for item in files:
            file_tools.validate_arguments(item,dict(path=str,start_line=int,max_lines=int),
                                          ['path'],'project files 项')
            path=item.get('path')
            if not path or path.startswith('/') or '\x00' in path or '..' in path.split('/'):
                raise file_tools.FileToolError('文件必须是项目内相对路径，不含 ..')
            max_lines, clamped = file_tools.clamped_integer(item,'max_lines',120,1,2000)
            if clamped:
                notes.append('files.max_lines 超过 2000，已截为 2000')
            values.append(dict(path=path,start_line=file_tools.integer(item,'start_line',1,1,2**63-1),
                               max_lines=max_lines))
        payload['files']=values
    else:
        limit, clamped = file_tools.clamped_integer(args,'limit',60,1,500)
        if clamped:
            notes.append('limit 超过 500，已截为 500')
        payload.update(depth=file_tools.integer(args,'depth',2,0,10),offset=file_tools.integer(args,'offset',0,0,2**63-1),
                       limit=limit)
    value=transport.remote_json(server,payload,source=REMOTE_SOURCE)
    value.pop('ok',None)
    return file_tools.dump_value(value,notes,'批量元数据超过限额，请减少文件数或 max_chars')

def t_init_project(args, *, run=None):
    run = transport.ssh_run if run is None else run
    project = (args.get("project") or "").strip()
    if not commands.NAME_RE.match(project):
        raise ToolError("project 只能包含字母、数字、_ . -")
    c = config.resolve_context(args.get("server"), project)
    name = c['SERVER']
    project = c['PROJECT']
    code = f"{c['CODE_ROOT']}/{project}"
    data = f"{c['DATA_ROOT']}/{project}"
    subdirs = " ".join(f"{commands.qpath(data)}/{d}" for d in ("data", "checkpoints", "results", "logs"))
    script = f"mkdir -p {commands.qpath(code)} {subdirs} || exit 1\n"
    rc, out, err, el = run(name, script, 60, max_output_chars=6000)
    if rc != 0:
        return transport.fmt_result(rc, out, err, el), True
    return (f"已创建项目 {project}（{name}）\n"
            f"代码: {code}\n"
            f"数据: {data}（data/checkpoints/results/logs）")


def tools():
    return []

"""Project inventory and bounded batch reading without remote deployment."""
import json
import file_tools
import local_connection

REMOTE_SOURCE = file_tools.REMOTE_SOURCE.split('try:\n    a = json.load(sys.stdin)',1)[0] + r'''
import time
try:
    a=json.load(sys.stdin);roots=a['roots'];base=guarded(a['path'])
    if not os.path.isdir(base) or os.stat(base).st_uid!=os.getuid():
        raise ValueError('项目目录必须属于当前服务器用户')
    if a.get('files') is None:
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
        results=[];pending=[];used=0;sources=[]
        for item in a['files']:
            remaining=a['max_chars']-used
            if remaining<256:
                pending.append(item);continue
            p=guarded(base+'/'+item['path'])
            if not under(p,base):raise ValueError('批量读取必须在指定项目真实目录内')
            if os.stat(p).st_uid!=os.getuid():raise ValueError('文件不属于当前服务器用户')
            skeleton=dict(path=item['path'],sha256='0'*64,
                blocks=[dict(start_line=item['start_line'],text='',next_line=10**18)],next_line=10**18)
            room=remaining-len(json.dumps(skeleton,ensure_ascii=False,separators=(',',':')))
            if room<256:
                pending.append(item);continue
            value=operation(dict(operation='read',path=p,ranges=[dict(start_line=item['start_line'],max_lines=item['max_lines'])],
                compact=True,multi_range=False,max_chars=room,scope_root=base,require_owner=True))
            value['path']=item['path']
            value.pop('version',None);value.pop('editable',None)
            for block in value.get('blocks',[]):
                if block.get('next_line') is None:block.pop('next_line',None)
            if value.get('next_line') is None:value.pop('next_line',None)
            cost=len(json.dumps(value,ensure_ascii=False,separators=(',',':')))
            if cost>remaining:
                pending.append(item);continue
            results.append(value);sources.append((item,value));used+=cost
            if value.get('next_line'):
                entry=dict(path=item['path'],start_line=value['next_line'],max_lines=item['max_lines'])
                pending.append(entry)
                used+=len(json.dumps(entry,ensure_ascii=False,separators=(',',':')))
        value=dict(path=base,files=results,pending=pending,complete=not pending)
        while results and len(json.dumps(value,ensure_ascii=False,separators=(',',':')))>a['max_chars']:
            item,dropped=sources.pop();results.pop()
            if dropped.get('next_line'):
                for index,entry in enumerate(pending):
                    if (entry.get('path')==item['path'] and entry.get('start_line')==dropped['next_line']
                            and entry.get('max_lines')==item['max_lines']):
                        pending.pop(index);break
            pending.insert(0,item)
            value=dict(path=base,files=results,pending=pending,complete=not pending)
    emit(dict(ok=True,**value))
except Exception as error:
    emit(dict(ok=False,error=str(error)));sys.exit(1)
'''

def read_project(args):
    roots=file_tools.roots_config()
    payload=dict(operation='project',roots=roots,path=file_tools.request_path(args.get('path'),roots),
                 max_chars=file_tools.integer(args,'max_chars',12000,512,20000))
    if 'files' in args:
        files=args['files']
        if not isinstance(files,list) or not 1<=len(files)<=8:
            raise file_tools.FileToolError('files 须有 1–8 个项目相对文件行段')
        values=[]
        for item in files:
            if not isinstance(item,dict) or set(item)-{'path','start_line','max_lines'}:
                raise file_tools.FileToolError('文件仅接受 path/start_line/max_lines')
            path=item.get('path')
            if not isinstance(path,str) or not path or path.startswith('/') or '\x00' in path or '..' in path.split('/'):
                raise file_tools.FileToolError('文件必须是项目内相对路径，不含 ..')
            values.append(dict(path=path,start_line=file_tools.integer(item,'start_line',1,1,2**63-1),
                               max_lines=file_tools.integer(item,'max_lines',1000,1,2000)))
        payload['files']=values
    else:
        payload.update(depth=file_tools.integer(args,'depth',4,0,10),offset=file_tools.integer(args,'offset',0,0,2**63-1),
                       limit=file_tools.integer(args,'limit',150,1,500))
    value=file_tools.remote_json(local_connection.resolve_server(args.get('server')),payload,source=REMOTE_SOURCE)
    value.pop('ok',None)
    text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    if len(text)>file_tools.MAX_TEXT:
        raise file_tools.FileToolError('批量元数据超过限额，请减少文件数或 max_chars')
    return text

def tools():
    return [('read_project',read_project,'项目索引；传 files 批量通读源码，pending 给续读位置。覆盖不全不代表通读完成。',
        dict(type='object',required=['path'],additionalProperties=False,properties=dict(
            path={'type':'string'},server={'type':'string','description':local_connection.server_arg_desc()},
            files={'type':'array','minItems':1,'maxItems':8,'items':dict(type='object',required=['path'],additionalProperties=False,properties=dict(
                path={'type':'string'},start_line={'type':'integer','minimum':1},max_lines={'type':'integer','minimum':1,'maximum':2000,'default':1000}))},
            depth={'type':'integer','minimum':0,'maximum':10,'default':4},
            offset={'type':'integer','minimum':0},limit={'type':'integer','minimum':1,'maximum':500,'default':150},
            max_chars={'type':'integer','minimum':512,'maximum':20000,'default':12000}))) ]

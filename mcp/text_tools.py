"""Bounded literal text search; helpers execute remotely only in memory."""
import json
import file_tools
import local_connection

REMOTE_SOURCE = file_tools.REMOTE_SOURCE.split('def operation(a):', 1)[0] + r'''
import fnmatch, io, time
a = json.load(sys.stdin)
roots = a['roots']
deadline = time.monotonic()+5
used_bytes, entries_seen, matches, output_size = 0, 0, 0, 0
skipped = dict(symlink=0, binary=0, oversized=0, unreadable=0, excluded=0)
reason = None
EXCLUDED = {'.git','__pycache__','.venv','venv','.jobs','data','datasets','checkpoints','weights'}

def candidates(p):
    global entries_seen, reason
    if os.path.isfile(p):
        yield p
        return
    stack = [p]
    while stack:
        folder = stack.pop()
        fd = directory(folder)
        try:
            with os.scandir(fd) as items:
                for entry in items:
                    if time.monotonic() >= deadline or entries_seen >= 5000:
                        reason = 'scan_limit'
                        return
                    entries_seen += 1
                    s = entry.stat(follow_symlinks=False)
                    path = folder+'/'+entry.name
                    if stat.S_ISLNK(s.st_mode):
                        skipped['symlink'] += 1
                    elif s.st_uid != os.getuid():
                        skipped['unreadable'] += 1
                    elif stat.S_ISDIR(s.st_mode):
                        if entry.name in EXCLUDED:
                            skipped['excluded'] += 1
                        else:
                            stack.append(path)
                    elif stat.S_ISREG(s.st_mode) and fnmatch.fnmatchcase(entry.name,a['file_glob']):
                        yield path
                    else:
                        skipped['excluded'] += 1
        finally:
            os.close(fd)

try:
    root = guarded(a['path'])
    results = []
    query = a['query'] if a['case_sensitive'] else a['query'].casefold()
    for candidate in candidates(root):
        if reason:
            break
        if time.monotonic() >= deadline:
            reason = 'time_limit'; break
        parent, f = None, None
        try:
            p = guarded(candidate)
            parent, f = opened(p)
            s = os.fstat(f.fileno())
            if s.st_uid != os.getuid():
                skipped['unreadable'] += 1; continue
            if s.st_size > 8*1024*1024:
                skipped['oversized'] += 1; continue
            if used_bytes+s.st_size > 32*1024*1024:
                reason='byte_limit'; break
            raw = f.read(8*1024*1024+1)
            used_bytes += len(raw)
            if len(raw)>8*1024*1024:
                skipped['oversized'] += 1; continue
            if b'\x00' in raw:
                skipped['binary'] += 1; continue
            try:
                text=raw.decode('utf-8')
            except UnicodeDecodeError:
                skipped['binary'] += 1; continue
            intervals=[];hit_lines=[]
            for number,line in enumerate(io.StringIO(text),1):
                if number%256 == 0 and time.monotonic()>=deadline:
                    reason='time_limit'; break
                if query in (line if a['case_sensitive'] else line.casefold()):
                    if matches>=a['limit']:
                        reason='match_limit'; break
                    matches += 1
                    hit_lines.append(number)
                    start,end=max(1,number-a['context_lines']),number+a['context_lines']
                    if intervals and start<=intervals[-1][1]+1:
                        intervals[-1][1]=max(intervals[-1][1],end)
                    else:
                        intervals.append([start,end])
            if not intervals:
                continue
            blocks=[];index=0
            for number,line in enumerate(io.StringIO(text),1):
                while index<len(intervals) and number>intervals[index][1]: index+=1
                if index==len(intervals): break
                if number>=intervals[index][0]:
                    cost=len(json.dumps(line,ensure_ascii=False))
                    if output_size+cost>a['max_chars']:
                        reason='output_limit';break
                    if len(blocks)<=index:
                        blocks.append(dict(start_line=number,text=''))
                    blocks[-1]['text']+=line
                    output_size+=cost
            if not same(f,p,parent,identity(s)):
                raise ValueError('搜索期间文件发生变化，请重新搜索')
            if blocks:
                results.append(dict(path=p,match_lines=hit_lines,sha256=hashlib.sha256(raw).hexdigest() if s.st_size<=LIMIT else None,
                                    blocks=blocks))
        except (PermissionError, FileNotFoundError):
            skipped['unreadable']+=1
        finally:
            if f: f.close()
            if parent is not None: os.close(parent)
        if reason: break
    emit(dict(ok=True,path=root,files=results,matches=matches,scanned_bytes=used_bytes,
              scanned_entries=entries_seen,skipped=skipped,
              incomplete=bool(reason or skipped['oversized'] or skipped['unreadable'] or skipped['symlink']),
              reason=reason))
except Exception as error:
    emit(dict(ok=False,error=str(error)))
    sys.exit(1)
'''

def search_text(args):
    query=args.get('query')
    pattern=args.get('file_glob','*')
    sensitive=args.get('case_sensitive',True)
    if not isinstance(query,str) or not query or len(query)>256 or '\n' in query or '\x00' in query:
        raise file_tools.FileToolError('query 必须是 1–256 字符的单行字面关键词')
    if not isinstance(pattern,str) or not pattern or len(pattern)>128 or '/' in pattern or '\x00' in pattern:
        raise file_tools.FileToolError('file_glob 必须是文件名通配符，如 *.py')
    if type(sensitive) is not bool:
        raise file_tools.FileToolError('case_sensitive 必须是布尔值')
    roots=file_tools.roots_config()
    payload=dict(operation='search',roots=roots,path=file_tools.request_path(args.get('path'),roots),
                 query=query,file_glob=pattern,case_sensitive=sensitive,
                 context_lines=file_tools.integer(args,'context_lines',2,0,10),
                 limit=file_tools.integer(args,'limit',20,1,100),
                 max_chars=file_tools.integer(args,'max_chars',6000,256,12000))
    value=file_tools.remote_json(local_connection.resolve_server(args.get('server')),payload,source=REMOTE_SOURCE)
    value.pop('ok',None)
    text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    if len(text)>file_tools.MAX_TEXT:
        raise file_tools.FileToolError('搜索元数据过多，请缩小范围或 limit')
    return text

def tools():
    return [('search_text',search_text,'目录/文件字面搜索，默认 20 处及上下各 2 行；incomplete 表示未完整覆盖。',
        dict(type='object',required=['path','query'],additionalProperties=False,properties=dict(
            path={'type':'string'},query={'type':'string'},server={'type':'string'},file_glob={'type':'string','default':'*'},
            context_lines={'type':'integer','minimum':0,'maximum':10,'default':2},
            limit={'type':'integer','minimum':1,'maximum':100,'default':20},
            max_chars={'type':'integer','minimum':256,'maximum':12000,'default':6000},
            case_sensitive={'type':'boolean','default':True}))) ]

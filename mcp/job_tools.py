"""Process identity checks; all helpers remain local and execute in memory."""
import json
import file_tools

HEADER_SOURCE = r'''
import os, json, datetime
p = os.getppid()
fields = open('/proc/%d/stat' % p).read().rsplit(')', 1)[1].split()
meta = json.loads(os.environ['REMOTE_WS_JOB_META'])
meta.update(pid=str(p), uid=os.getuid(), pgid=int(fields[2]), sid=int(fields[3]),
            start_ticks=fields[19], boot_id=open('/proc/sys/kernel/random/boot_id').read().strip(),
            start=datetime.datetime.now(datetime.timezone.utc).isoformat())
print('# remote-ws job-v2 ' + json.dumps(meta, ensure_ascii=True), flush=True)
'''

# Reuse canonical path and secure descriptor helpers from the file tools.
REMOTE_SOURCE = file_tools.REMOTE_SOURCE.split('def operation(a):', 1)[0] + r'''
import re, time, signal, select, datetime, base64
signals_sent = False

def proc(pid):
    try:
        s = os.stat('/proc/%d' % pid)
        if s.st_uid != os.getuid():
            return dict(pid=str(pid), uid=s.st_uid, state='?', pgid=None, sid=None, start_ticks=None)
        fields = open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()
        return dict(pid=str(pid), uid=s.st_uid, pgid=int(fields[2]), sid=int(fields[3]),
                    start_ticks=fields[19], state=fields[0])
    except (FileNotFoundError, ProcessLookupError):
        return None

def matches(meta, current):
    return bool(current and current['state'] not in ('Z', 'X') and
                meta.get('boot_id') == open('/proc/sys/kernel/random/boot_id').read().strip() and
                all(meta.get(k) == current.get(k) for k in ('pid', 'uid', 'pgid', 'sid', 'start_ticks')) and
                meta.get('uid') == os.getuid() and meta.get('pgid') == int(meta['pid']) and meta.get('sid') == int(meta['pid']))

def log_record(path):
    p = guarded(path)
    parent, f = opened(p)
    try:
        if os.fstat(f.fileno()).st_uid != os.getuid():
            raise ValueError('日志不属于当前服务器用户')
        head = f.readline(65536).decode('utf-8', 'replace').rstrip('\r\n')
        if head.startswith('# remote-ws job-v2 '):
            meta = json.loads(head[len('# remote-ws job-v2 '):])
            secure = True
        elif head.startswith('# remote-ws job '):
            meta = dict(re.findall(r'(\w+)=(\S+)', head))
            secure = False
        else:
            return None
        s = os.fstat(f.fileno())
        f.seek(max(0, s.st_size-8192))
        tail = f.read(8192).decode('utf-8', 'replace')
        pattern = r'^# remote-ws exit=(\d+)\b'
        if secure:
            pattern += r' job_id=' + re.escape(meta['job_id']) + r'\b'
        exits = re.findall(pattern, tail, re.MULTILINE)
        current = proc(int(meta['pid'])) if str(meta.get('pid', '')).isdigit() else None
        verified = secure and matches(meta, current)
        if exits:
            state = 'exit=' + exits[-1]
        elif verified:
            state = 'running'
        elif secure:
            state = 'identity_mismatch' if current and current['state'] not in ('Z','X') else 'stopped/killed'
        else:
            state = 'legacy/unverified'
        return dict(meta, log=p, last_write=datetime.datetime.fromtimestamp(s.st_mtime, datetime.timezone.utc).isoformat(),
                    status=state, identity_verified=verified, identity_version=2 if secure else 1)
    finally:
        f.close()
        os.close(parent)

def filtered_tail(a,p,parent,f,before):
    fingerprint=hashlib.sha256(json.dumps([a.get('query'),a.get('errors'),a['context_lines']],ensure_ascii=False).encode()).hexdigest()[:16]
    if a.get('cursor'):
        try:
            token=json.loads(base64.b64decode(a['cursor'],validate=True))
            if (token.get('server')!=a['source_server'] or token.get('path')!=p or token.get('device')!=before.st_dev or
                token.get('inode')!=before.st_ino or token.get('filter')!=fingerprint or
                type(token.get('offset')) is not int or not 0<=token['offset']<=before.st_size):
                raise ValueError()
            start=token['offset']
        except (ValueError,TypeError,AttributeError):
            raise ValueError('游标与文件/过滤条件不符；省略 cursor 重新读取')
    else:
        start=max(0,before.st_size-1024*1024)
        if start:
            f.seek(start)
            line=f.readline(65537)
            if not line.endswith(b'\n') and start+len(line)<before.st_size:
                raise ValueError('日志单行过长，请下载查看')
            start+=len(line)
    offset=start;records=[];pending=False;deadline=time.monotonic()+5
    job=a.get('job_meta');active=bool(job and matches(job,proc(int(job['pid']))))
    f.seek(offset)
    while offset<before.st_size and len(records)<2000 and offset-start<1024*1024 and time.monotonic()<deadline:
        data=f.readline(min(65537,before.st_size-offset))
        if not data.endswith(b'\n') and offset+len(data)<before.st_size:
            raise ValueError('日志单行过长，请下载查看')
        if not data.endswith(b'\n') and active:
            pending=True;break
        records.append((offset,offset+len(data),data.decode('utf-8')))
        offset+=len(data)
    groups=[];context=a['context_lines'];pending_exception=False;exception_incomplete=False
    final_error=re.compile(r'^\s*(?:[\w.]+(?:Error|Exception|Warning)|ExceptionGroup|KeyboardInterrupt|SystemExit)(?::|\s*$)')
    if a.get('errors'):
        index=0
        while index<len(records):
            text=records[index][2]
            if 'Traceback (most recent call last)' in text:
                end=index+1
                while end<len(records) and not final_error.match(records[end][2]): end+=1
                if end==len(records):
                    if offset<before.st_size and not pending:
                        raise ValueError('异常块超过扫描预算；请用 read_file 分段或下载查看')
                    if active:
                        pending_exception=True;offset=records[index][0];break
                    end=len(records)-1;exception_incomplete=True
                groups.append([max(0,index-context),min(len(records)-1,end+context)])
                index=end+1
            else:
                if re.search(r'\b(error|exception|failed|failure)\b',text,re.IGNORECASE) or final_error.match(text):
                    groups.append([max(0,index-context),min(len(records)-1,index+context)])
                index+=1
    else:
        query=a['query'].casefold()
        for index,(_,_,text) in enumerate(records):
            if query in text.casefold():
                groups.append([max(0,index-context),min(len(records)-1,index+context)])
    merged=[]
    for first,last in groups:
        if pending_exception and records[last][1]>offset:
            last=next((i-1 for i in range(first,last+1) if records[i][0]>=offset),last)
        if last<first: continue
        if merged and first<=merged[-1][1]+1 and len(json.dumps(
            ''.join(row[2] for row in records[merged[-1][0]:last+1]),ensure_ascii=False))<=a['max_chars']:
            merged[-1][1]=max(merged[-1][1],last)
        else:
            merged.append([first,last])
    pieces=[];used=0;returned=0;cut=False
    for first,last in merged:
        text=''.join(row[2] for row in records[first:last+1])
        cost=len(json.dumps(text,ensure_ascii=False))
        if returned>=a['lines'] or used+cost>a['max_chars']:
            if not pieces:
                raise ValueError('匹配/异常块超过字符限额；增大 max_chars 或下载查看')
            offset=records[first][0];cut=True;break
        pieces.append(text);used+=cost;returned+=1
    current=os.fstat(f.fileno())
    path_stat=os.stat(os.path.basename(p),dir_fd=parent,follow_symlinks=False)
    if (current.st_dev,current.st_ino)!=(path_stat.st_dev,path_stat.st_ino) or current.st_size<before.st_size:
        raise ValueError('读取期间日志已替换或缩短；省略 cursor 重新读取')
    token=base64.b64encode(json.dumps(dict(server=a['source_server'],path=p,device=before.st_dev,
        inode=before.st_ino,offset=offset,filter=fingerprint),separators=(',',':')).encode()).decode()
    return dict(text=''.join(pieces),cursor=token,more=cut or (offset<before.st_size and not pending and not pending_exception),
                pending_line=pending,pending_exception=pending_exception,exception_incomplete=exception_incomplete,
                scanned_bytes=offset-start,window_start=start,matched_blocks=returned)


def tail_page(a):
    p = guarded(a['log'])
    parent, f = opened(p)
    try:
        before = os.fstat(f.fileno())
        if before.st_uid != os.getuid():
            raise ValueError('日志不属于当前服务器用户')
        if a.get('query') or a.get('errors'):
            return filtered_tail(a,p,parent,f,before)
        cursor = a.get('cursor')
        if cursor:
            try:
                value = json.loads(base64.b64decode(cursor, validate=True))
                if (value.get('filter') is not None or value.get('server') != a['source_server'] or value.get('path') != p or
                    value.get('device') != before.st_dev or value.get('inode') != before.st_ino or
                    type(value.get('offset')) is not int or not 0 <= value['offset'] <= before.st_size):
                    raise ValueError()
                offset = value['offset']
            except (ValueError, KeyError, TypeError):
                raise ValueError('游标与日志不符或日志已替换/缩短；省略 cursor 重新读取')
        else:
            # Bound the backwards scan, including a log with no newlines.
            offset = before.st_size
            block = b''
            while offset and len(block) <= 1024*1024:
                length = min(65536, offset)
                offset -= length
                f.seek(offset)
                block = f.read(length) + block
                if block.count(b'\n') >= a['lines'] + 1:
                    break
            fragments = block.splitlines(keepends=True)
            if offset:
                fragments = fragments[1:]  # First fragment may be incomplete.
            selected = b''.join(fragments[-a['lines']:])
            offset = before.st_size-len(selected)
            if not selected and before.st_size:
                raise ValueError('日志单行过长，请下载查看')
        f.seek(offset)
        pieces, used, count, pending = [], 0, 0, False
        while offset < before.st_size and count < a['lines']:
            # Read only a complete UTF-8 line within the output budget.
            data = f.readline(min(4*a['max_chars']+1, before.st_size-offset))
            if not data.endswith(b'\n') and offset+len(data) < before.st_size:
                raise ValueError('日志单行过长，请下载查看')
            job = a.get('job_meta')
            if not data.endswith(b'\n') and job and matches(job, proc(int(job['pid']))):
                pending = True
                break
            text = data.decode('utf-8')
            cost = len(json.dumps(text, ensure_ascii=False))
            if used+cost > a['max_chars']:
                if not pieces:
                    raise ValueError('日志单行超过字符限额；增大 max_chars 或下载查看')
                break
            pieces.append(text)
            used += cost
            offset += len(data)
            count += 1
        current = os.fstat(f.fileno())
        path_stat = os.stat(os.path.basename(p), dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (path_stat.st_dev, path_stat.st_ino) or current.st_size < before.st_size:
            raise ValueError('读取期间日志已替换或缩短；省略 cursor 重新读取')
        token = base64.b64encode(json.dumps(dict(server=a['source_server'],path=p,device=before.st_dev,
            inode=before.st_ino,offset=offset), separators=(',',':')).encode()).decode()
        return dict(text=''.join(pieces), cursor=token, more=offset<before.st_size and not pending,
                    pending_line=pending)
    finally:
        f.close()
        os.close(parent)


def listing(a):
    data = a['data_root']
    paths = []
    try:
        data = guarded(data)
    except FileNotFoundError:
        return []
    directories = [data + '/logs']
    fd = directory(data)
    try:
        for entry in os.listdir(fd):
            s = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(s.st_mode) and s.st_uid == os.getuid():
                directories.append(data + '/' + entry + '/logs')
    finally:
        os.close(fd)
    for folder in directories:
        try:
            folder = guarded(folder)
            fd = directory(folder)
        except (OSError, ValueError):
            continue
        try:
            for entry in os.listdir(fd):
                if entry.endswith('.log'):
                    s = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid():
                        paths.append((s.st_mtime_ns, folder + '/' + entry))
        finally:
            os.close(fd)
    paths.sort(reverse=True)
    rows = []
    for _, path in paths:
        try:
            row = log_record(path)
            if row:
                rows.append(row)
                if len(rows) >= a['limit']:
                    break
        except (OSError, ValueError, KeyError):
            continue
    return rows

def stopped(a):
    global signals_sent
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('服务器现有运行环境不支持安全 pidfd 停止；不安装组件，不退回仅靠 PID 的停止方式')
    row = log_record(a['log'])
    if not row or row.get('identity_version') != 2:
        raise ValueError('旧任务日志缺少进程身份，拒绝自动停止；请先核实实际进程')
    if any(row.get(k) != a['expected'].get(k) for k in ('job_id','pid','uid','pgid','sid','start_ticks','boot_id')):
        raise ValueError('任务日志身份发生变化，未发送信号')
    if not matches(row, proc(int(row['pid']))):
        raise ValueError('任务进程已退出或 PID/属主/启动时间/进程组不匹配，未发送信号')
    handles = []
    try:
        leader = int(row['pid'])
        # Pin every currently observed member with pidfd, then recheck its
        # identity. Never use killpg or a numeric-PID fallback.
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            p = int(entry)
            fd = None
            try:
                before = proc(p)
                if not before or before['state'] in ('Z','X') or before['uid'] != os.getuid() or before['pgid'] != leader or before['sid'] != leader:
                    continue
                fd = os.pidfd_open(p)
                after = proc(p)
                if not after or any(before[k] != after[k] for k in ('pid','uid','pgid','sid','start_ticks')):
                    continue
                handles.append((p,fd))
                fd = None
            except (ProcessLookupError, PermissionError, FileNotFoundError):
                continue
            finally:
                if fd is not None:
                    os.close(fd)
        if not any(p == leader for p, _ in handles) or not matches(row, proc(leader)):
            raise ValueError('停止前进程身份发生变化，未发送信号')
        handles.sort(key=lambda pair: pair[0] == leader)
        sig = signal.SIGKILL if a.get('force') else signal.SIGTERM
        sent = []
        for p, fd in handles:
            try:
                signal.pidfd_send_signal(fd, sig)
                signals_sent = True
                sent.append(p)
            except ProcessLookupError:
                pass
        deadline = time.monotonic()+2
        remaining = []
        while True:
            remaining = [p for p,fd in handles if not select.select([fd], [], [], 0)[0]]
            if not remaining or time.monotonic() >= deadline:
                break
            time.sleep(.05)
        return dict(name=row.get('name'), signal=sig.name, signalled_pids=sent,
                    remaining_pids=remaining, status='still-running' if remaining else 'stopped',
                    message='已核对任务身份并停止本次检查到的进程组成员；停止期间新派生的进程不自动追杀')
    finally:
        for _,fd in handles:
            os.close(fd)

try:
    a = json.load(sys.stdin)
    roots = a['roots']
    if a['operation'] == 'jobs':
        emit(dict(ok=True, jobs=listing(a)))
    elif a['operation'] == 'job':
        emit(dict(ok=True, job=log_record(a['log'])))
    elif a['operation'] == 'stop':
        emit(dict(ok=True, **stopped(a)))
    elif a['operation'] == 'tail':
        emit(dict(ok=True, **tail_page(a)))
    else:
        raise ValueError('未知任务操作')
except Exception as error:
    emit(dict(ok=False, error=str(error), uncertain=signals_sent))
    sys.exit(1)
'''


def request(server, operation, **params):
    return file_tools.remote_json(server, dict(operation=operation, roots=file_tools.roots_config(), **params),
                                  timeout=60, source=REMOTE_SOURCE)

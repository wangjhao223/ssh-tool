import remote_common

FILE_OPERATION_SOURCE = remote_common.SOURCE + r'''
def operation(a):
    global published, streaming
    p = guarded(a['path'])
    if a.get('scope_root') and not under(p, a['scope_root']):
        raise ValueError('批量读取必须在指定项目真实目录内')
    op = a['operation']
    if op == 'list':
        fd = directory(p)
        try:
            if any(under(p, r) for r in roots[2:]) and os.fstat(fd).st_uid != os.getuid():
                raise ValueError('临时目录不属于当前服务器用户')
            names = sorted(os.listdir(fd))
            offset, limit = a['offset'], a['limit']
            entries = []
            used = 0
            for name in names[offset:offset + limit]:
                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                kind = 'symlink' if stat.S_ISLNK(s.st_mode) else 'directory' if stat.S_ISDIR(s.st_mode) else 'file' if stat.S_ISREG(s.st_mode) else 'other'
                item = dict(name=name, type=kind)
                if kind != 'directory':
                    item['size'] = s.st_size
                item['modified_time'] = int(s.st_mtime)
                if any(under(p, r) for r in roots[2:]) and s.st_uid != os.getuid():
                    item = dict(name=name, type='restricted')
                cost = len(json.dumps(item, ensure_ascii=False))
                if used + cost > CAP - 2000:
                    break
                entries.append(item)
                used += cost
            nxt = offset + len(entries)
            return dict(path=p, entries=entries, total=len(names), next_offset=nxt if nxt < len(names) else None)
        finally:
            os.close(fd)
    parent, f = opened(p)
    temporary = None
    try:
        s = os.fstat(f.fileno())
        if a.get('require_owner') and s.st_uid != os.getuid():
            raise ValueError('文件不属于当前服务器用户')
        before = identity(s)
        result = dict(path=p, version=meta(s))
        if op == 'read':
            if s.st_size <= LIMIT:
                raw = f.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise ValueError('读取期间文件发生变化')
                try:
                    raw.decode('utf-8')
                except UnicodeDecodeError:
                    raise ValueError('不是 UTF-8 文本，请下载查看')
                if b'\x00' in raw:
                    raise ValueError('二进制文件，请下载查看')
                result['sha256'] = hashlib.sha256(raw).hexdigest()
                f.seek(0)
            else:
                result['sha256'] = None
                sample = f.read(8192)
                if b'\x00' in sample:
                    raise ValueError('二进制文件，请下载查看')
                try:
                    codecs.getincrementaldecoder('utf-8')().decode(sample, final=False)
                except UnicodeDecodeError:
                    raise ValueError('不是 UTF-8 文本，请下载查看')
                f.seek(0)
            result['editable'] = s.st_size <= LIMIT and s.st_uid == os.getuid() and s.st_nlink == 1
            ranges = a['ranges']
            blocks = [dict(start_line=r['start_line'],text='') for r in ranges]
            lines, number, used, range_index, exhausted = [], 0, 0, 0, False
            next_line = None
            while True:
                raw = f.readline(CAP * 4 + 4)
                if not raw:
                    break
                number += 1
                while range_index<len(ranges) and number>=ranges[range_index]['start_line']+ranges[range_index]['max_lines']:
                    range_index+=1
                if range_index==len(ranges):
                    if not a['multi_range']:
                        next_line=number
                    break
                if number < ranges[range_index]['start_line']:
                    if len(raw) > CAP * 4:
                        raise ValueError('遇到超长单行，请下载查看')
                    continue
                if len(raw) > CAP * 4 or b'\x00' in raw:
                    raise ValueError('二进制或超长单行，请下载查看')
                text = raw.decode('utf-8')
                item = dict(line=number, text=text)
                cost = len(json.dumps(text if a['compact'] else item, ensure_ascii=False))
                if cost > a['max_chars']:
                    if not used:
                        if a.get('scope_root'):
                            raise ValueError('单行超过本页剩余预算')
                        raise ValueError('超长单行，请下载查看')
                    if a['multi_range']:
                        blocks[range_index]['next_line'] = number
                    next_line,exhausted = number,True
                    break
                if used + cost > a['max_chars']:
                    if a['multi_range']:
                        blocks[range_index]['next_line'] = number
                    next_line,exhausted = number,True
                    break
                lines.append(item)
                blocks[range_index]['text']+=text
                used += cost
            if exhausted:
                for index in range(range_index+1,len(ranges)):
                    blocks[index]['next_line']=ranges[index]['start_line']
            if not same(f, p, parent, before):
                raise ValueError('读取期间文件发生变化，请重新读取')
            return dict(result, **({'blocks':blocks} if a['compact'] else {'lines':lines}), next_line=next_line)
        if op == 'edit':
            if s.st_uid != os.getuid():
                raise ValueError('只能编辑当前服务器用户拥有的文件')
            if s.st_size > LIMIT:
                raise ValueError('编辑上限为 4 MiB')
            if s.st_nlink != 1:
                raise ValueError('不编辑具有多个硬链接的文件')
            raw = f.read(LIMIT + 1)
            if len(raw) > LIMIT or b'\x00' in raw:
                raise ValueError('文件超限或为二进制')
            digest = hashlib.sha256(raw).hexdigest()
            if digest != a['expected_sha256']:
                raise ValueError('版本冲突：SHA-256 不匹配，原文件未修改')
            text = raw.decode('utf-8')
            crlf = '\r\n' in text and '\n' not in text.replace('\r\n', '') and '\r' not in text.replace('\r\n', '')
            lf = '\r' not in text
            preview, positions, preview_used, preview_truncated = [], [], 0, False
            for edit in a['edits']:
                old, new = edit['old_text'], edit['new_text']
                if crlf:
                    old = old.replace('\r\n', '\n').replace('\n', '\r\n')
                    new = new.replace('\r\n', '\n').replace('\n', '\r\n')
                elif lf:
                    old = old.replace('\r\n', '\n')
                    new = new.replace('\r\n', '\n')
                if not old or text.count(old) != 1:
                    raise ValueError('每项原文本必须唯一匹配，原文件未修改')
                if a['include_diff']:
                    position=text.index(old)
                    if len(positions)<20:
                        positions.append(text[:position].count('\n')+1)
                    else:
                        preview_truncated=True
                    old_preview=old[:4096].splitlines(keepends=True)[:100]
                    new_preview=new[:4096].splitlines(keepends=True)[:100]
                    if len(old)>4096 or len(new)>4096 or old.count('\n')>100 or new.count('\n')>100:
                        preview_truncated=True
                    for index,line in enumerate(difflib.unified_diff(old_preview,new_preview,fromfile='old',tofile='new',n=1)):
                        if index<2: continue
                        if not line.endswith('\n'): line+='\n'
                        cost=len(json.dumps(line,ensure_ascii=False))
                        if preview_used+cost>a['max_chars']:
                            preview_truncated=True;break
                        preview.append(line);preview_used+=cost
                text = text.replace(old, new, 1)
            data = text.encode('utf-8')
            if len(data) > LIMIT or b'\x00' in data:
                raise ValueError('编辑结果超过 4 MiB 或包含二进制内容')
            temporary = '.remote-ws-edit-' + os.urandom(16).hex()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                out.flush()
                os.fchown(out.fileno(), s.st_uid, s.st_gid)
                os.fchmod(out.fileno(), stat.S_IMODE(s.st_mode))
                os.fsync(out.fileno())
            f.seek(0)
            if not same(f, p, parent, before) or hashlib.sha256(f.read(LIMIT + 1)).hexdigest() != digest:
                raise ValueError('写入前版本冲突，原文件未修改')
            # Optimistic check, not a universal CAS against unrelated editors.
            os.replace(temporary, os.path.basename(p), src_dir_fd=parent, dst_dir_fd=parent)
            published = True
            temporary = None
            os.fsync(parent)
            result=dict(path=p, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            if a['include_diff']:
                result.update(diff=''.join(preview),change_lines=positions,diff_truncated=preview_truncated)
            return result
        if op == 'download':
            offset = a.get('offset', 0)
            if a.get('identity') is not None and before != a['identity']:
                raise ValueError('源文件发生变化，拒绝续传')
            if offset < 0 or offset > s.st_size:
                raise ValueError('续传偏移无效')
            h = hashlib.sha256()
            remaining = offset
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('源文件发生变化')
                h.update(chunk)
                remaining -= len(chunk)
            if offset and h.hexdigest() != a.get('prefix_sha256'):
                raise ValueError('已下载部分与源文件不一致，拒绝续传')
            if not same(f, p, parent, before):
                raise ValueError('源文件发生变化')
            # From the first header byte onward stdout is the binary protocol;
            # later failures may only go to stderr.
            streaming = True
            emit(dict(ok=True, protocol=1, path=p, identity=before, offset=offset, length=s.st_size-offset))
            remaining = s.st_size - offset
            while remaining:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('源文件截短')
                sys.stdout.buffer.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
            sys.stdout.buffer.flush()
            stable = same(f, p, parent, before)
            emit(dict(ok=True, sha256=h.hexdigest(), stable=stable))
            streaming = False
            return None
        raise ValueError('未知操作')
    finally:
        if temporary:
            os.unlink(temporary, dir_fd=parent)
        f.close()
        os.close(parent)
'''

REMOTE_SOURCE = FILE_OPERATION_SOURCE + r'''
try:
    a = json.load(sys.stdin)
    roots = a['roots']
    value = operation(a)
    if value is not None:
        emit(dict(ok=True, **value))
except Exception as error:
    # After the protocol header, stdout carries file bytes; an extra JSON line
    # would corrupt the local partial file. Report failures via stderr only.
    if streaming:
        sys.stderr.write('远端下载在字节流开始后失败: ' + str(error) + '\n')
        sys.stderr.flush()
    else:
        emit(dict(ok=False, error=str(error), uncertain=published))
    sys.exit(1)
'''

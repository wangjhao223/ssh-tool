"""Read bounded local help topics. No remote connection or arbitrary paths."""
import json
import re
from pathlib import Path

import file_tools
import local_connection

GUIDE = Path(__file__).with_name('guide.md')
TOPICS = {'connection', 'files', 'downloads', 'jobs', 'troubleshooting', 'environment', 'maintenance'}
MAX_CHARS = 6000


def sections():
    if GUIDE.stat().st_size > 128 * 1024:
        raise file_tools.FileToolError('本地指南过大')
    values, current = {}, None
    for line in GUIDE.read_text(encoding='utf-8').splitlines(keepends=True):
        heading = re.fullmatch(r'## ([a-z_]+) — (.+)\n?', line)
        if heading:
            current = heading[1] if heading[1] in TOPICS else None
            if current:
                if current in values or len(heading[2]) > 100:
                    raise file_tools.FileToolError('本地指南主题重复或标题过长')
                values[current] = {'title': heading[2].rstrip(), 'lines': []}
        elif current:
            values[current]['lines'].append(line)
    if set(values) != TOPICS:
        raise file_tools.FileToolError('本地指南主题缺失')
    return values


def get_guide(args):
    if set(args) - {'topic','start_line','max_lines'}:
        raise file_tools.FileToolError('指南仅接受 topic/start_line/max_lines')
    topic = args.get('topic', 'index')
    if not isinstance(topic, str) or topic not in TOPICS | {'index', 'workspace'}:
        raise file_tools.FileToolError('未知指南主题；省略 topic 查看目录')
    start = file_tools.integer(args, 'start_line', 1, 1, 2**31-1)
    limit = file_tools.integer(args, 'max_lines', 80, 1, 200)
    if topic == 'workspace':
        config = local_connection.read_config()
        value = dict(topic=topic, default_server=local_connection.resolve_server(),
                     servers=local_connection.configured_servers(),
                     directories=dict(zip(('CODE_ROOT','DATA_ROOT'), file_tools.roots_config()[:2])),
                     conda_root=config.get('CONDA_ROOT', '/home/wjh/miniconda3'),
                     local_workspace=local_connection.PROJECT_ROOT,
                     download_directory=str(file_tools.DOWNLOADS), project_config='.kimi-code/mcp.json')
    else:
        contents = sections()
        if topic == 'index':
            value = dict(topics=[{'topic':'workspace','title':'当前本地配置'}] +
                         [dict(topic=k,title=v['title']) for k,v in contents.items()])
        else:
            lines = contents[topic]['lines']
            selected, used = [], 0
            for line in lines[start-1:start-1+limit]:
                cost = len(json.dumps(line, ensure_ascii=False))
                if used + cost > MAX_CHARS:
                    if not selected:
                        raise file_tools.FileToolError('指南单行过长；请本地查看 guide.md')
                    break
                selected.append(line)
                used += cost
            following = start + len(selected)
            value = dict(topic=topic, title=contents[topic]['title'], start_line=start,
                         text=''.join(selected), next_line=following if following <= len(lines) else None)
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if len(text) > MAX_CHARS + 1500:
        raise file_tools.FileToolError('指南或配置元数据过长')
    return text


def tools():
    return [('get_guide', get_guide, '按主题读取本地帮助；省略 topic 只列目录，workspace 读当前配置。无网络，按需分页。',
             {'type':'object','properties':{
                 'topic':{'type':'string','enum':['index','workspace']+sorted(TOPICS),'default':'index'},
                 'start_line':{'type':'integer','minimum':1,'default':1},
                 'max_lines':{'type':'integer','minimum':1,'maximum':200,'default':80}},
              'additionalProperties':False})]

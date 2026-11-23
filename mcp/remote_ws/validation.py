"""Shared validation and failure metadata."""


class RemoteWsError(Exception):
    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class LoginError(RemoteWsError):
    pass


class FileToolError(RemoteWsError):
    pass


class ToolError(RemoteWsError):
    pass


NOT_RUN = '未执行'
UNKNOWN = '结果未知'
REMOTE_RETURNED = '远端已返回非零；可能已部分执行，重试前先核验'
TYPE_LABELS = {str: '字符串', int: '整数', bool: '布尔值', list: '数组', dict: '对象'}


def format_failure(code, status, message):
    return (f'{code} · {status}' if status else code) + '\n' + message


def bad_args(error_type, message):
    return error_type(message, code='BAD_ARGS', status=NOT_RUN)


def type_label(expected):
    if isinstance(expected, tuple):
        return '或'.join(dict.fromkeys(type_label(item) for item in expected))
    return TYPE_LABELS.get(expected, getattr(expected, '__name__', str(expected)))


def validate_fields(args, fields, required=(), *, error_type=ToolError, label='arguments'):
    if not isinstance(args, dict):
        raise bad_args(error_type, f'{label} 必须是对象')
    missing = [key for key in required if key not in args]
    extra = sorted(set(args) - set(fields))
    problems = []
    if missing:
        problems.append('缺少必填字段: ' + ', '.join(missing))
    if extra:
        names = ', '.join(repr(key[:48]) if len(key) > 48 or not key.isidentifier() else key
                          for key in extra[:5])
        problems.append('不允许字段: ' + names + (f'（共 {len(extra)} 项）' if len(extra) > 5 else ''))
    if problems:
        raise bad_args(error_type, '；'.join(problems) + '。补齐必填项，删除不支持的字段。')
    errors = []
    for key, value in args.items():
        expected = fields[key]
        choices = expected if isinstance(expected, tuple) else (expected,)
        if type(value) not in choices:
            errors.append(f'{key} 期望{type_label(expected)}，收到{type(value).__name__}')
    if errors:
        detail = '；'.join(errors[:5]) + (f'（共 {len(errors)} 项）' if len(errors) > 5 else '')
        raise bad_args(error_type, '参数类型错误: ' + detail)


def clamped_integer(args, name, default, low, high, *, error_type=FileToolError):
    value = args.get(name, default)
    if type(value) is not int or value < low:
        raise bad_args(error_type, f'{name} 必须是不小于 {low} 的整数')
    return min(value, high), value > high

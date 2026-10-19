import logging

_LOGGER = logging.getLogger(__name__)


def try_read_as_bool(value):
    """Convert a value coming from Haier cloud into a Python bool.

    Haier uses multiple representations across models / fields:
      * boolean True / False directly
      * strings "true" / "false" (lowercase)
      * integers 1 / 0
      * numeric strings "1" / "0"
      * Chinese "开" / "关", "开启" / "关闭", "启用" / "禁用"
    Anything unrecognizable raises ValueError so the caller can mark
    the entity unavailable instead of silently misreading state.
    """
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        if int(value) == 1:
            return True
        if int(value) == 0:
            return False
        raise ValueError('[{}] 无法被转为bool'.format(value))

    if isinstance(value, str):
        v = value.strip()
        if v.lower() in ('true', 'on', 'open', 'yes'):
            return True
        if v.lower() in ('false', 'off', 'close', 'no'):
            return False
        if v in ('1', '开', '开启', '启用', '启动'):
            return True
        if v in ('0', '关', '关闭', '禁用', '停止'):
            return False
        # Some fields are numbers expressed as strings like "35" - don't guess here
        raise ValueError('[{}] 无法被转为bool'.format(value))

    raise ValueError('[{}] 无法被转为bool'.format(value))


def equals_ignore_case(value, target):
    if isinstance(value, str) and isinstance(target, str):
        return value.lower() == target.lower()

    return value == target


def contains_any_ignore_case(value, targets: list):
    for target in targets:
        if equals_ignore_case(value, target):
            return True

    return False


def _normalize_cmd_value(value):
    """Return the value we should send back to Haier for a given raw value.

    The gateway simply forwards whatever is in cmdArgs to the cloud.  If we
    send a Python True when the field expects "1" (or vice versa) the cloud
    silently ignores the command.  This helper guarantees that the data type
    we send matches the original type we read.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        # Numeric strings (e.g. "1", "0", "35") should be forwarded as-is;
        # everything else stays as a string.
        return value
    return value

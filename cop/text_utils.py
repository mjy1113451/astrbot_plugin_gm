"""L0 文本工具层：无状态纯函数（中文数字 / 时长解析 / 格式化 / QQ 提取）。

全部函数逐字迁移自 main.py 原第 55-223 行，保留原名、类型注解与注释语义。
"""

import re

from .constants import (
    _CN_DIGITS,
    _CN_UNITS,
    _MUTE_MAX_MINUTES,
)


def _parse_qq_list(text: str) -> list:
    """从文本中提取所有合法的QQ号（5-12位数字）。"""
    return list({m.group(1) for m in re.finditer(r"(\d{5,12})", text or "")})


def normalize_qq_list(values) -> list:
    """把配置里的 QQ 名单归一化为「去首尾空白、丢掉空项与 None」的字符串列表。

    WebUI / JSON 里常出现 " 111 "、""、null 这类值：只判断 strip 后是否为空
    而不真正 strip，会把 " 111 " 当成收件人发出去（协议端必然失败）；不同
    调用方（通知发送 / 日志统计 / 诊断输出）也会因此对同一份配置得出不同
    结论（PR #292 审查：inconsistent-recipient-normalization）。
    """
    result = []
    for value in values or []:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result.append(text)
    return result


def _cn_number_to_int(s: str):
    """把中文数字（支持 两/廿/十/十五/一百二十三）转为整数，失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None

    def _decode_chunk(chunk: str):
        """解析一段（不含「万」）中文数字，如 一百二十三。"""
        section = 0
        number = 0
        for ch in chunk:
            if ch in _CN_DIGITS:
                number = _CN_DIGITS[ch]
            elif ch in _CN_UNITS:
                unit = _CN_UNITS[ch]
                if number == 0:
                    number = 1  # 「十五」= 15
                section += number * unit
                number = 0
            else:
                return None
        return section + number

    if "万" in s:
        left, _, right = s.partition("万")
        left_val = 1 if left == "" else _decode_chunk(left)
        if left_val is None:
            return None
        right_val = _decode_chunk(right)
        if right_val is None:
            return None
        return left_val * 10000 + right_val
    return _decode_chunk(s)


def _parse_duration_minutes(text: str):
    """#254：从文本解析口语化中文时长，统一换算为分钟。

    覆盖阿拉伯数字与中文数字混用、分钟/小时/天/周/月、半小时、一个半小时、
    两小时、一天、永久/无限等常见说法。返回 int（分钟）或 None（未识别）。
    """
    if not text:
        return None
    t = re.sub(r"\s+", "", str(text))
    if not t:
        return None

    # 1) 永久 / 无限 / 最大值 / 31 天等：按 OneBot 上限长期禁言
    if re.search(r"永久|无限|永远|一辈子|最大(?:值|时长)?|31天|三十一天", t):
        return _MUTE_MAX_MINUTES

    def _val(token: str):
        token = (token or "").strip()
        if not token:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            return float(token)
        return _cn_number_to_int(token)

    n = r"(\d+(?:\.\d+)?)"
    c = r"([零〇一壹二两俩贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+)"
    num = rf"(?:{n}|{c})"

    def _first_or_cn(match) -> str:
        """兼容两个捕获组（阿拉伯数字 / 中文数字），返回命中的那一个。"""
        return match.group(1) if match.group(1) is not None else match.group(2)

    total_minutes = 0.0
    matched = False

    # 2) 「一个/半个/两个 小时」口语小时表达
    for m in re.finditer(r"([一二两俩半])个(?:小时|钟头|時)", t):
        matched = True
        token = m.group(1)
        total_minutes += 30 if token == "半" else 60 * (_val(token) or 0)
    t = re.sub(r"[一二两俩半]个(?:小时|钟头|時)", " ", t)

    # 3) 「一个小时」「两个半小时」等 X 个半小时口语表达（先于裸「半小时」匹配）
    for m in re.finditer(r"([一二两俩])个(?:半(?:小时|钟头|時)|半个钟头)", t):
        matched = True
        total_minutes += ((_val(m.group(1)) or 0) + 0.5) * 60
    t = re.sub(r"[一二两俩]个(?:半(?:小时|钟头|時)|半个钟头)", " ", t)

    # 4) 「半小时」「半钟头」「半天」等半/整量词
    half_map = [
        (r"半小时|半个钟头|半個小時", 30),
        (r"半天", 12 * 60),
        (r"半周|半星期|半礼拜", 3 * 24 * 60 + 12 * 60),
        (r"半月|半个月", 15 * 24 * 60),
    ]
    for pattern, minutes in half_map:
        if re.search(pattern, t):
            matched = True
            total_minutes += minutes
            t = re.sub(pattern, " ", t)

    # 4) 数值 + 单位（月/周/天/小时/分钟），支持 2小时30分 累加
    unit_specs = [
        (r"(?:个月|月|各月)", 30 * 24 * 60),
        (r"(?:星期|礼拜|周)", 7 * 24 * 60),
        (r"(?:天|日)", 24 * 60),
        (r"(?:小时|钟头|時|个点|个钟)", 60),
        (r"(?:分钟|分|min|m)", 1),
    ]
    for unit_pattern, minutes_per in unit_specs:
        pattern = rf"{num}{unit_pattern}"
        for m in list(re.finditer(pattern, t, flags=re.IGNORECASE)):
            value = _val(_first_or_cn(m))
            if value is None:
                continue
            matched = True
            total_minutes += value * minutes_per
        t = re.sub(pattern, " ", t, flags=re.IGNORECASE)

    # 5) 英文缩写小时/天（h/d），放在分钟（m）之后避免误吞
    for unit_pattern, minutes_per in ((r"h", 60), (r"d", 24 * 60)):
        pattern = rf"(\d+(?:\.\d+)?){unit_pattern}"
        for m in list(re.finditer(pattern, t, flags=re.IGNORECASE)):
            matched = True
            total_minutes += float(m.group(1)) * minutes_per
        t = re.sub(pattern, " ", t, flags=re.IGNORECASE)

    if not matched:
        return None
    minutes = int(round(total_minutes))
    if minutes <= 0:
        return None
    return minutes


def _format_minutes(minutes: int) -> str:
    """#254：把分钟数格式化为易读中文（用于口语化禁言的提示）。"""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return f"{minutes}分钟"
    if minutes <= 0:
        return "0分钟"
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if mins or not parts:
        parts.append(f"{mins}分钟")
    return "".join(parts)

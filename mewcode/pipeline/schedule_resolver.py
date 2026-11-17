"""自然语言排期 → cron 的两级解析（规则层）。

规则层（零 token、确定性）：覆盖中英文常见排期表达，直接产出
5 字段 cron；覆盖不了的返回 None，由 api 层决定是否走 LLM 兜底。

支持示例：
- 直接 cron 透传："0 10 * * *"
- 每N分钟/每N小时："每5分钟"、"every 2 hours"
- 每天 + 时刻："每天10点"、"每天早上9点半"、"daily at 10:30"
- 工作日/周末 + 时刻："工作日9点半"、"weekdays at 9:30"
- 每周指定天 + 时刻："每周一三五 10点"、"every monday at 9am"
- 每月N号 + 时刻："每月1号 8点"

口径：
- cron 按本地时区求值（与 PipelineTrigger.next_fire / CronCreateTool 一致）
- 周字段 0=周日
- 时刻缺失时规则层不猜（返回 None 交给 LLM），避免静默错排
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from mewcode.scheduler.cron import CronExpression

SOURCE_CRON = "cron"
"""输入本身就是合法 cron，原样透传。"""
SOURCE_RULE = "rule"
"""规则层解析。"""
SOURCE_LLM = "llm"
"""LLM 兜底解析（api 层填充）。"""
SOURCE_ONCE = "once"
"""明确的绝对日期时间，由规则层解析为一次性触发。"""
SOURCE_INTERVAL = "interval"
"""带起始时间的秒/分/小时间隔调度，可选最大触发次数。"""


@dataclass
class ResolvedSchedule:
    """一次排期解析的结果。"""

    cron: str
    summary: str
    """人话回显（给用户确认用）。"""
    source: str
    """解析来源：cron / rule / llm。"""
    run_at: str = ""
    """一次性或间隔调度的起始时间（UTC ISO 8601）；cron 时为空。"""
    interval_seconds: int = 0
    """固定间隔秒数；0 表示非间隔调度。"""
    max_runs: int = 0
    """最多触发次数；0 表示不限制。"""
    model: str = ""
    """LLM 兜底实际使用的模型；规则解析时为空。"""


# ---------------------------------------------------------------------------
# 中文数字
# ---------------------------------------------------------------------------

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 数字 token：阿拉伯数字或中文数字（含「十」组合，如 十、十五、二十三）
_NUM = r"(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"


def _to_int(token: str) -> int | None:
    """数字 token → int（支持中文数字 0-99）。"""
    if token.isdigit():
        return int(token)
    if "十" in token:
        left, _, right = token.partition("十")
        tens = _CN_DIGITS.get(left, None) if left else 1
        ones = _CN_DIGITS.get(right, None) if right else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    value = 0
    for ch in token:
        if ch not in _CN_DIGITS:
            return None
        value = value * 10 + _CN_DIGITS[ch]
    return value if token else None


# ---------------------------------------------------------------------------
# 归一化与时刻解析
# ---------------------------------------------------------------------------

_FULLWIDTH = str.maketrans("０１２３４５６７８９：．", "0123456789:.")


def _normalize(text: str) -> str:
    """全角转半角、去多余空白、统一小写（保留中文）。"""
    text = text.translate(_FULLWIDTH)
    text = re.sub(r"\s+", " ", text.strip())
    return text.lower()


# 时段词 → 是否加 12 小时（凌晨/早上/上午 原样；下午/晚上等 +12）
_MERIDIEM_PM = ("下午", "傍晚", "晚上", "晚间", "夜里", "深夜", "pm")
_MERIDIEM_AM = ("凌晨", "早上", "早晨", "上午", "清晨", "am")
_MERIDIEM = "|".join(_MERIDIEM_PM + _MERIDIEM_AM + ("中午",))

# 时刻：H点 / H点M分 / H点半 / H:MM（可带时段前缀）
_TIME_RE = re.compile(
    rf"(?:(?P<mer>{_MERIDIEM})\s*)?"
    rf"(?P<hour>{_NUM})\s*(?:[点时]|:)\s*"
    rf"(?:(?P<half>半)|(?P<quarter>一刻)|(?P<tquarter>三刻)|(?P<minute>{_NUM})\s*分?)?"
    rf"\s*(?P<mer2>am|pm)?"
)


def _parse_time(segment: str) -> tuple[int, int] | None:
    """从片段解析 (hour, minute)；无法确定时返回 None。"""
    m = _TIME_RE.search(segment)
    if m is None:
        return None
    hour = _to_int(m.group("hour"))
    if hour is None or hour > 23:
        return None
    if m.group("half"):
        minute = 30
    elif m.group("quarter"):
        minute = 15
    elif m.group("tquarter"):
        minute = 45
    elif m.group("minute"):
        minute = _to_int(m.group("minute"))
        if minute is None or minute > 59:
            return None
    else:
        minute = 0

    mer = m.group("mer") or m.group("mer2") or ""
    if mer in _MERIDIEM_PM and hour < 12:
        hour += 12
    elif mer == "中午" and hour < 3:
        # 「中午12点」hour=12 原样；「中午1点」按 13 点
        hour += 12
    elif mer in _MERIDIEM_AM and hour == 12:
        hour = 0
    return hour, minute


# ---------------------------------------------------------------------------
# 周几解析
# ---------------------------------------------------------------------------

_CN_DOW = {"日": 0, "天": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_EN_DOW = {
    "sunday": 0, "sun": 0, "monday": 1, "mon": 1, "tuesday": 2, "tue": 2,
    "wednesday": 3, "wed": 3, "thursday": 4, "thu": 4, "friday": 5,
    "fri": 5, "saturday": 6, "sat": 6,
}
_DOW_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

# 每周一三五 / 周一、三、五 / 星期一和周三
_CN_DOW_RE = re.compile(r"(?:每?(?:周|星期|礼拜))([一二三四五六日天][、和,一二三四五六日天\s]*)")


def _parse_cn_dows(text: str) -> list[int] | None:
    """提取中文周几列表（去重保序）；无则 None。"""
    days: list[int] = []
    for m in _CN_DOW_RE.finditer(text):
        for ch in m.group(1):
            d = _CN_DOW.get(ch)
            if d is not None and d not in days:
                days.append(d)
    return days or None


def _parse_en_dows(text: str) -> list[int] | None:
    days: list[int] = []
    for name, d in _EN_DOW.items():
        if re.search(rf"\b{name}s?\b", text) and d not in days:
            days.append(d)
    return days or None


# ---------------------------------------------------------------------------
# cron → 人话（简版，仅覆盖规则层产出的形态）
# ---------------------------------------------------------------------------


def describe_cron(cron: str) -> str:
    """把 5 字段 cron 转成简短人话描述（尽力而为，失败返回原文）。"""
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, _month, dow = parts

    if minute.startswith("*/") and hour == "*":
        return f"每{minute[2:]}分钟"
    if minute == "*" and hour == "*":
        return "每分钟"
    if hour.startswith("*/"):
        return f"每{hour[2:]}小时（整点后第{minute}分）"
    if hour == "*":
        return f"每小时的第{minute}分"

    try:
        time_str = f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return cron

    if dow == "1-5":
        day_str = "工作日"
    elif dow in ("0,6", "6,0"):
        day_str = "周末"
    elif dow != "*":
        try:
            names = [_DOW_NAMES[int(d)] for d in dow.split(",")]
            day_str = "每" + "、".join(names)
        except (ValueError, IndexError):
            day_str = f"周{dow}"
    elif dom != "*":
        day_str = f"每月{dom}号"
    else:
        day_str = "每天"
    return f"{day_str} {time_str}"


# ---------------------------------------------------------------------------
# 规则层入口
# ---------------------------------------------------------------------------


def _valid_cron(cron: str) -> bool:
    try:
        return CronExpression.parse(cron).validate()
    except Exception:
        return False


def _resolve_once(text: str, now: datetime | None = None) -> ResolvedSchedule | None:
    """解析 ``2026-07-28 19:30 执行一次`` 这类明确的本地时间。"""
    match = re.fullmatch(
        r"\s*(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?"
        r"\s+(?:at\s+)?(\d{1,2})\s*[:点时]\s*(\d{1,2})?\s*分?"
        r"\s*(?:执行一次|运行一次|跑一次|run once|once)?\s*",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        reference = now or datetime.now().astimezone()
        local = datetime(
            *(int(value or 0) for value in match.groups()[:5]),
            tzinfo=reference.astimezone().tzinfo,
        )
    except ValueError:
        return None
    return ResolvedSchedule(
        cron="",
        summary=f"{local:%Y-%m-%d %H:%M} 执行一次",
        source=SOURCE_ONCE,
        run_at=local.astimezone(timezone.utc).isoformat(),
    )


def _resolve_interval(
    text: str, now: datetime | None = None
) -> ResolvedSchedule | None:
    """解析带起点的有限/无限间隔，例如“从12:03开始每10秒，共5次”。

    仅写时刻而未写日期时，按下一次该本地时刻理解。秒级表达无法用
    5 字段 cron 表示，因此即使没有次数限制也会进入间隔协议。
    """
    t = _normalize(text)
    interval_match = re.search(
        r"每(?:隔)?\s*(\d{1,7})\s*"
        r"(秒(?:钟)?|s(?:ec(?:ond)?s?)?|分钟|分|min(?:ute)?s?|"
        r"小时|钟头|h(?:ours?)?)",
        t,
    )
    if interval_match is None:
        return None

    amount = int(interval_match.group(1))
    unit = interval_match.group(2)
    if unit.startswith(("秒", "s")):
        multiplier = 1
    elif unit in ("分钟", "分") or unit.startswith("min"):
        multiplier = 60
    else:
        multiplier = 3600
    interval_seconds = amount * multiplier
    if not 1 <= interval_seconds <= 31_536_000:
        return None

    count_match = re.search(
        r"(?:总共|一共|共|总计)?\s*(?:触发|执行|运行|跑)?\s*"
        r"(\d{1,7})\s*次",
        t,
    )
    max_runs = int(count_match.group(1)) if count_match else 0
    if max_runs > 1_000_000:
        return None

    start_match = re.search(
        r"(?:从|自)\s*"
        r"(?:(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*"
        r"(\d{1,2})\s*日?\s+)?"
        r"(\d{1,2})\s*[:点时]\s*(\d{1,2})?\s*分?"
        r"\s*(?:开始|起)",
        t,
    )
    starts_now = re.search(r"(?:从|自)\s*现在\s*(?:开始|起)", t) is not None
    is_seconds = multiplier == 1
    if start_match is None and not starts_now:
        # 普通“每 5 分钟”仍交给 cron；秒级无法由 5 字段 cron 表达，
        # 默认从下一秒开始。
        if not is_seconds and count_match is None:
            return None
        starts_now = True

    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=datetime.now().astimezone().tzinfo)
    else:
        reference = reference.astimezone()

    if starts_now:
        local = reference.replace(microsecond=0) + timedelta(seconds=1)
    else:
        assert start_match is not None
        year, month, day, hour, minute = start_match.groups()
        try:
            if year:
                local = datetime(
                    int(year), int(month), int(day), int(hour), int(minute or 0),
                    tzinfo=reference.tzinfo,
                )
            else:
                local = datetime(
                    reference.year, reference.month, reference.day,
                    int(hour), int(minute or 0), tzinfo=reference.tzinfo,
                )
                if local <= reference:
                    local += timedelta(days=1)
        except ValueError:
            return None

    if max_runs:
        run_text = f"，共 {max_runs} 次"
    else:
        run_text = "，持续执行"
    return ResolvedSchedule(
        cron="",
        summary=(
            f"{local:%Y-%m-%d %H:%M:%S} 起每 {interval_seconds} 秒"
            f"{run_text}"
        ),
        source=SOURCE_INTERVAL,
        run_at=local.astimezone(timezone.utc).isoformat(),
        interval_seconds=interval_seconds,
        max_runs=max_runs,
    )


def resolve_schedule_text(
    text: str, now: datetime | None = None
) -> ResolvedSchedule | None:
    """规则层解析：确定性表达直接产出 cron，覆盖不了返回 None。"""
    raw = text.strip()
    if not raw:
        return None

    interval = _resolve_interval(raw, now)
    if interval is not None:
        return interval

    once = _resolve_once(raw, now)
    if once is not None:
        return once

    # 0. 直接 cron 透传
    if len(raw.split()) == 5 and _valid_cron(raw):
        return ResolvedSchedule(
            cron=raw, summary=describe_cron(raw), source=SOURCE_CRON
        )

    t = _normalize(raw)

    # 1. 每N分钟 / 每分钟
    m = re.search(rf"每(?:隔)?\s*{_NUM}\s*分钟", t) or re.search(
        r"every\s+(\d{1,2})\s*min(?:ute)?s?", t
    )
    if m:
        n = _to_int(m.group(1))
        if n and 1 <= n <= 59:
            cron = f"*/{n} * * * *" if n > 1 else "* * * * *"
            return ResolvedSchedule(cron, describe_cron(cron), SOURCE_RULE)
    if re.search(r"每分钟|every minute", t):
        return ResolvedSchedule("* * * * *", "每分钟", SOURCE_RULE)

    # 2. 每N小时 / 每小时
    m = re.search(rf"每(?:隔)?\s*{_NUM}\s*(?:个)?\s*(?:小时|钟头)", t) or re.search(
        r"every\s+(\d{1,2})\s*hours?", t
    )
    if m:
        n = _to_int(m.group(1))
        if n and 1 <= n <= 23:
            cron = f"0 */{n} * * *" if n > 1 else "0 * * * *"
            return ResolvedSchedule(cron, describe_cron(cron), SOURCE_RULE)
    if re.search(r"每(?:个)?小时|every hour|hourly", t):
        return ResolvedSchedule("0 * * * *", "每小时", SOURCE_RULE)

    # 3. 日期范围 + 时刻（时刻缺失则不猜，交给 LLM）
    time_hm = _parse_time(t)

    # 3a. 每月N号
    m = re.search(rf"每月\s*{_NUM}\s*[号日]", t)
    if m:
        dom = _to_int(m.group(1))
        if dom and 1 <= dom <= 31 and time_hm:
            cron = f"{time_hm[1]} {time_hm[0]} {dom} * *"
            return ResolvedSchedule(cron, describe_cron(cron), SOURCE_RULE)
        return None

    # 3b. 工作日 / 周末
    if re.search(r"工作日|weekdays?", t):
        if time_hm:
            cron = f"{time_hm[1]} {time_hm[0]} * * 1-5"
            return ResolvedSchedule(cron, describe_cron(cron), SOURCE_RULE)
        return None
    if re.search(r"周末|weekends?", t):
        if time_hm:
            cron = f"{time_hm[1]} {time_hm[0]} * * 0,6"
            return ResolvedSchedule(cron, describe_cron(cron), SOURCE_RULE)
        return None

    # 3c. 每周指定天
    dows = _parse_cn_dows(t) or _parse_en_dows(t)
    if dows:
        if time_hm:
            dow_str = ",".join(str(d) for d in sorted(set(dows)))
            cron = f"{time_hm[1]} {time_hm[0]} * * {dow_str}"
            return ResolvedSchedule(cron, describe_cron(cron), SOURCE_RULE)
        return None

    # 3d. 每天
    if re.search(r"每天|天天|每日|daily|every ?day", t):
        if time_hm:
            cron = f"{time_hm[1]} {time_hm[0]} * * *"
            return ResolvedSchedule(cron, describe_cron(cron), SOURCE_RULE)
        return None

    return None


# ---------------------------------------------------------------------------
# LLM 兜底（prompt 构造与回复解析；调用由 api 层负责）
# ---------------------------------------------------------------------------

_LLM_PROMPT_TEMPLATE = """你是排期解析器 Agent。尽量把用户自由表达的排期意图解析为结构化调度。

规则：
- 字段顺序：分 时 日 月 周；本地时区；周字段 0=周日 1=周一 ... 6=周六
- 只输出一行 JSON，不要任何其他文字。
- 固定周期：
  {{"type":"cron","cron":"分 时 日 月 周","run_at":"","interval_seconds":0,"max_runs":0,"summary":"简短人话描述"}}
- 明确的绝对日期时间按本地时区理解：
  {{"type":"once","cron":"","run_at":"带时区的 ISO 8601 时间","interval_seconds":0,"max_runs":1,"summary":"执行一次"}}
- 秒/分/小时间隔、带起点或有限次数的调度：
  {{"type":"interval","cron":"","run_at":"首次触发的带时区 ISO 8601 时间","interval_seconds":10,"max_runs":5,"summary":"从何时起每10秒，共5次"}}
  max_runs=0 表示不限制次数。只有时刻没有日期时，解析为未来最近一次该时刻。
- 可以结合当前时间做合理消歧并在 summary 中说明，不要因为无法转成 cron 就拒绝。
- 只有依赖未知外部事件且无法确定首次时间（如「实验结束后4小时」）时才输出：
  {{"type":"unresolved","cron":"","run_at":"","interval_seconds":0,"max_runs":0,"summary":"无法确定首次时间的原因"}}

当前本地时间：{now}
用户描述：{text}"""


def build_llm_prompt(text: str, now_str: str) -> str:
    """构造 LLM 兜底解析的 prompt。"""
    return _LLM_PROMPT_TEMPLATE.format(now=now_str, text=text.strip())


def parse_llm_reply(reply: str) -> ResolvedSchedule | None:
    """解析 Agent 回复中的 cron / once / interval 结构。"""
    import json

    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if m is None:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    cron = str(data.get("cron", "")).strip()
    summary = str(data.get("summary", "")).strip() or describe_cron(cron)
    if cron:
        if not _valid_cron(cron):
            return None
        return ResolvedSchedule(cron=cron, summary=summary, source=SOURCE_LLM)
    run_at = str(data.get("run_at", "")).strip()
    if not run_at:
        return None
    try:
        value = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    try:
        interval_seconds = int(data.get("interval_seconds", 0) or 0)
        max_runs = int(data.get("max_runs", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not 0 <= interval_seconds <= 31_536_000:
        return None
    if not 0 <= max_runs <= 1_000_000:
        return None
    if max_runs and not interval_seconds and max_runs != 1:
        return None
    return ResolvedSchedule(
        cron="",
        summary=summary or (
            f"每 {interval_seconds} 秒"
            + (f"，共 {max_runs} 次" if max_runs else "")
            if interval_seconds else "执行一次"
        ),
        source=SOURCE_LLM,
        run_at=value.astimezone(timezone.utc).isoformat(),
        interval_seconds=interval_seconds,
        max_runs=max_runs if interval_seconds else 0,
    )

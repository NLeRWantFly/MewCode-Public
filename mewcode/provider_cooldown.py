"""Provider wait instructions survive retries and checkpoint resumes."""
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

# Provider-directed cooldowns are distinct from the 30-second local backoff.
# Ordinary rate limits may wait in the current request, with its finite retry
# budget. Multi-hour/day quota resets still return to the persisted host gate.
MAX_INLINE_RETRY = 300.0


def retry_seconds(value, *, now=None):
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(str(value))
            seconds = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def record_cooldown(record, model, seconds, *, now=None):
    from mewcode.kernel.events import digest
    binding = model.as_dict() if hasattr(model, 'as_dict') else dict(model)
    row = {'model': binding, 'retry_after_seconds': seconds,
           'retry_not_before': (time.time() if now is None else now) + seconds}
    key = digest([binding['provider_key'], binding['model_id']])
    previous = record.setdefault('provider_cooldowns', {}).get(key, {})
    if previous.get('retry_not_before', 0) > row['retry_not_before']:
        return previous
    record['provider_cooldowns'][key] = row
    return row


def cooldown_remaining(record, model, *, now=None):
    from mewcode.kernel.events import digest
    binding = model.as_dict() if hasattr(model, 'as_dict') else (model or {})
    key = digest([binding.get('provider_key'), binding.get('model_id')])
    row = record.get('provider_cooldowns', {}).get(key, {})
    return max(0.0, row.get('retry_not_before', 0) - (time.time() if now is None else now))


def cooldown_message(seconds):
    return f'服务商要求等待至少 {math.ceil(seconds)} 秒。已停止自动重试；请等待限流到期或明确选择其他模型后继续。'

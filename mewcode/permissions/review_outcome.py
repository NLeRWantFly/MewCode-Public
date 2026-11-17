"""A valid but inconclusive review is not a transport or configuration failure."""


class ReviewResponseFormatError(ValueError):
    """A reviewer replied, but its response is not a usable safety verdict."""

    def __init__(self, message: str, *, field: str, decision: str = ""):
        super().__init__(message)
        self.field = field
        self.decision = decision


class ReviewInconclusive(ValueError):
    def __init__(self, message, *, route, unresolved):
        super().__init__(message)
        self.route = route
        self.unresolved = list(unresolved)


class ReviewContextTooLarge(ValueError):
    """Local admission failure; retrying identical input cannot recover it."""

    def __init__(self, *, state_bytes, largest_question_bytes, total_question_bytes, field_bytes):
        self.sizes = {
            "state_bytes": state_bytes,
            "largest_question_bytes": largest_question_bytes,
            "total_question_bytes": total_question_bytes,
            "field_bytes": field_bytes,
            "single_limit_bytes": 30_000,
            "total_limit_bytes": 60_000,
        }
        fields = ", ".join(f"{key}={size}" for key, size in field_bytes.items())
        super().__init__(
            "Jev 评审上下文过长（UTF-8 字节）："
            f"state={state_bytes}，state+最大问题={state_bytes + largest_question_bytes}/30000，"
            f"state+全部问题={state_bytes + total_question_bytes}/60000；"
            f"字段大小：{fields}。请求未发送，未截断证据，未作出放行判定"
        )

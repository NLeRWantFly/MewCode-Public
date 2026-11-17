from __future__ import annotations

import pytest

from mewcode.validator import ConfigError, validate_pipeline


def test_pipeline_inline_output_limit_is_configurable() -> None:
    config = validate_pipeline({"max_inline_output_chars": 12_000})

    assert config["max_inline_output_chars"] == 12_000


@pytest.mark.parametrize("value", [True, 999, 1_000_001])
def test_pipeline_inline_output_limit_rejects_invalid_values(value) -> None:
    with pytest.raises(ConfigError):
        validate_pipeline({"max_inline_output_chars": value})

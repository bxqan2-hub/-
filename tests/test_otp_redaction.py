from core.otp_utils import mask_otp, redact_otp_text


def test_mask_otp_preserves_length_without_revealing_digits() -> None:
    assert mask_otp("123456") == "******"
    assert mask_otp("123456") != "123456"


def test_redact_otp_text_masks_json_and_chinese_adjacent_codes() -> None:
    text = 'OTP=123456; "code":"654321"；验证码000111'
    redacted = redact_otp_text(text)
    assert "123456" not in redacted
    assert "654321" not in redacted
    assert "000111" not in redacted
    assert redacted.count("******") == 3


def test_redact_otp_text_does_not_truncate_long_numeric_ids() -> None:
    text = "request_id=1234567"
    assert redact_otp_text(text) == text


def test_redact_otp_text_handles_non_string_values() -> None:
    assert redact_otp_text({"code": "123456"}) == "{'code': '******'}"

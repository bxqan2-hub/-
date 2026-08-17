import json
import urllib.parse

from paypal.fingerprint import build_fn_sync_data, send_device_fingerprint
from paypal.models import SessionState


class CaptureSession:
    country = "TH"
    locale = "th_TH"

    def __init__(self, state):
        self.state = state
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))


def _decode_fn_sync(value):
    return json.loads(urllib.parse.unquote(value))


def test_fraudnet_and_form_payload_share_reference_screen_and_user_agent():
    state = SessionState(paypal_client_metadata_id="stable-task")
    capture = CaptureSession(state)
    send_device_fingerprint(capture, "BA-TOKEN")
    first_browser = capture.calls[0][1]["json"]["fp2"]["browser"]

    send_device_fingerprint(capture, "EC-TOKEN")
    second_browser = capture.calls[3][1]["json"]["fp2"]["browser"]
    assert first_browser == second_browser

    sync_data = _decode_fn_sync(
        build_fn_sync_data("BA-TOKEN")
    )
    compact_profile = json.loads(sync_data["dc"])
    assert compact_profile["screen"]["width"] == first_browser["screenResolution"][1]
    assert compact_profile["screen"]["height"] == first_browser["screenResolution"][0]
    assert compact_profile["ua"] == first_browser["ua"]

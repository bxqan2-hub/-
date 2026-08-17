from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_protocol_payment_form_uses_session_memory_after_country_load():
    script = (
        ROOT
        / "integrations"
        / "paypal_agreement_protocol"
        / "web_static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert "paypal.protocol.form.${privateBraintreeEnabled ? 'private' : 'standard'}.v1" in script
    assert "await paypalCountriesReady;" in script
    assert script.index("await paypalCountriesReady;") < script.index(
        "restoreProtocolFormState();"
    )
    assert "saveProtocolFormState(); closeCountryPicker();" in script

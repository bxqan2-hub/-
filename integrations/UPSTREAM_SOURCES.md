# Upstream sources and local adaptation policy

This site runs exactly two bundled link-extraction services:

1. [PAY.153 Checkout Link](https://github.com/1537271403/pay153-checkout-link)
   - Local path: `integrations/pay153_checkout`
   - Locked upstream commit: `e8b36626162f09363f29b85af42de98cc8114c9b`
2. [PayPal Agreement Protocol](https://github.com/1537271403/paypal-agreement-protocol)
   - Local path: `integrations/paypal_agreement_protocol`
   - Locked upstream commit: `4719066ec6fd56b57a5bd9599758366836c9dc0a`

Kakao OAICS protocol reference:

- [kakao_oaics_source](https://github.com/m1243808154/kakao_oaics_source)
  - Reference commit: `60e42034b7b7af7fad008da0578071e43cab855e`
  - Not a third service and not copied wholesale.
  - Its state-machine design is adapted in
    `pay153_checkout/kakao_oaics.py`: route bootstrap, real Elements Session,
    zero-due verification, tax update, inline ConfirmationToken, ChatGPT
    confirm, Stripe Intent continuation, redirect allow-listing, and a strict
    no-retry boundary after confirm is sent.
  - Account-page OAICS/CSLIVE detection reuses only the source project's
    early-stop classification boundary. It creates one DE/EUR custom Plus
    Checkout without a promotion, classifies `oaics_*` / `cs_live_*`, and stops
    before taxes, PaymentMethod, ctoken, or confirm. KR is needed for the Kakao
    payment method, not for Checkout classification.

PayPal OAICS extraction core:

- [link-pp](https://github.com/eatWhitePorridge/link-pp)
  - Vendored source commit: `16bad8784a5548d3c388ad50f592193cbed21c11`
  - Latest audited upstream commit: `c7933d80a4f043858d6bb749f95a86dbad89248e`
    (2026-08-14). This revision adds independent proxy/billing-country inputs
    plus unrelated Stripe promotion and Go-worker changes. The local adapter
    adopts only the compatible country-input contract and keeps the vendored
    PayPal OAICS protocol core unchanged.
  - Vendored under `pay153_checkout/paypal_oaics_link_pp` and exposed only as
    the PAY.153 `paypal_oaics` run mode.
  - Only `countries`, `engine`, `gateway`, `proxies`, `security`, and `protocol`
    core files are copied. Its Flask app, job manager, static console, Docker
    files, launch scripts, and standalone port `5572` are not deployed.
  - The mode keeps the source flow contract: selected-country proxy preflight,
    independently selected zero-due OAICS billing country (default BR proxy and
    DE/EUR billing), PayPal redirect/BA extraction, then immediate stop before
    PayPal registration, SMS, authorization callback, or payment-result polling.

## Local runtime completion

The original PayPal protocol-payment entry in the PAY.153 console now opens the
new OAICS protocol implementation provided by the full
`paypal-agreement-protocol` source imported from the verified local package
`C:\Users\Administrator\Downloads\协议控制台-独立版-20260812-205638` on
2026-08-13. Its immutable source manifest verified 39 files. Standalone port
launchers, caches, logs, audit keys, metrics, and authorization history were
excluded. The protocol Web UI is mounted in-process at `/paypal-pay/` on the
main WebUI port; it is not exposed as an Extract Center mode and no PayPal
sidecar port is started. Local Windows Chromium,
SOCKS5/fake-IP DNS, same-origin iframe, and main-site authentication patches
remain layered over the imported source.

The PayPal integration preserves raw `host:port:username:password` entries in
the pool. Generic four-part entries become HTTP proxy URLs when
`ProxyEntry.url` is consumed, while the verified `*.1024proxy.io:3000` gateway
uses `socks5h` so DNS and PayPal traffic traverse that proxy under Windows TUN
mode. Explicit `http://`, `https://`, `socks5://`, or `socks5h://` URLs retain
their explicit scheme. This provider-specific transport selection is isolated
to proxy parsing and does not alter registration, identity elevation,
authorization, or settlement steps.
The protocol session also preserves the upstream transport selection for every
proxy scheme: `curl_cffi` remains the default for HTTP, HTTPS, SOCKS5, and
SOCKS5H proxies, with `httpx` used only when explicitly requested or when
`curl_cffi` is unavailable. This keeps the TLS/HTTP fingerprint aligned with
the standalone reference project during PayPal edge and GraphQL requests.

The PayPal registration and identity-elevation core is copied byte-for-byte
from locked upstream commit `4719066ec6fd56b57a5bd9599758366836c9dc0a`.
This includes the OTP loop, card-bearing `SignUpNewMemberMutation`, country
fields, buyer-context hydration, and authorization sequence for every country.
The upstream decision rules remain unchanged: `NEED_CREDIT_CARD` preserves a
valid buyer and proceeds to authorization, while `PAYER_ACCOUNT_RESTRICTED` is
terminal. The extra pre-navigation buyer query present in upstream is limited
to its AE/BA branch and is not converted into a general retry for TH or other
countries. PAY.153 does not implement this registration state machine.

Every Python file under the `paypal` package except `proxy.py` is copied
byte-for-byte from the locked upstream commit. `proxy.py` carries only the
verified `*.1024proxy.io:3000` four-part-line `socks5h` transport override; its
pool selection and all other proxy formats retain upstream behavior. The
embedded `web.py` remains the main-site mount, job UI, and downstream
completion adapter; it does not override the upstream registration or
identity-elevation methods.

The locked PAY.153 revision imports `jsdom` from `gen_token_jsdom.js`, but its
upstream repository does not currently include a `package.json` or document the
Node dependency in its installation steps. This site therefore carries a local
`package.json` plus `package-lock.json` that pin `jsdom` 26.1.0 and `undici`
8.7.0 (Node.js 22.19+). The latter is required by the embedded link-pp Sentinel
bridge.
`一键安装.bat` / `install-integrations.bat` install that lock with `npm ci`,
install the Python and Playwright runtimes for both projects, and run the
integration self-check before the site can start.

## Required update workflow

Before modifying payment/extraction code:

1. Fetch and read both PAY.153 upstream repositories at their current HEADs.
2. For Kakao work, fetch and read `kakao_oaics_source` too.
3. Compare relevant upstream files against the vendored paths; do not replace
   local files without reviewing local proxy, authentication, reverse-proxy,
   retry, and safety changes.
4. Update `upstream-lock.json` and the commit values above when intentionally
   syncing a newer revision.
5. Run focused provider tests and syntax/import checks before handoff.

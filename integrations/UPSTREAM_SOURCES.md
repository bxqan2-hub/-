# Upstream sources and local adaptation policy

## Optional local Roxy browser component

- [wangshen233/roxy-unlimited-windows](https://github.com/wangshen233/roxy-unlimited-windows)
- Locked commit: `81f7da873fd0a3a9550ec769d2b37e26e28f2715`
- Vendored path: `integrations/roxy_unlimited_windows`; original LICENSE and
  attribution retained. This is a browser lifecycle service, not another
  payment/extraction service.
- `ROXY_LOCAL_COMPONENT` selects this loopback component or the existing
  official API. See `roxy_unlimited_windows/LOCAL_ADAPTATIONS.md` before updating:
  project-owned profiles, request-origin checks, proxy parsing, installed-core
  selection, launch/cleanup handling and health identity are deliberate patches.
- Fetch the locked upstream, compare the three modified runtime scripts, and
  run `tests/test_roxy_local_component.py` plus the Roxy/config/cache/WebUI
  regressions before updating. Do not replace these files blindly.

## Payment/extraction sources

This site runs exactly one bundled link-extraction service:

1. [PAY.153 Checkout Link](https://github.com/1537271403/pay153-checkout-link)
   - Local path: `integrations/pay153_checkout`
   - Locked upstream commit: `e8b36626162f09363f29b85af42de98cc8114c9b`
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
    early-stop classification boundary. It creates one custom Plus Checkout
    without a promotion, matching the selected proxy country and currency
    (direct requests retain the DE/EUR fallback), classifies `oaics_*` /
    `cs_live_*`, and stops before taxes, PaymentMethod, ctoken, or confirm. KR
    is needed for the Kakao payment method, not for Checkout classification.

GCash qualification reference:

- [qualification-test](https://github.com/yeying-xingchen/qualification-test)
  - Audited reference commit: `aaeb077ec2b6b6db68ac3339bf6e4181ebdd22dc`
  - Reference only; it is not deployed as another runtime service.
  - The account-page GCash detector keeps its Checkout Sentinel/SO requirement,
    Firefox 144 session continuity, HTTP-to-HTTPS proxy transport fallback, and
    the upstream's known opaque GCash `cpmt_*` identifier.
  - For lower latency, the local detector intentionally diverges from the
    upstream's OAICS-prefix requirement and three-step state polling. It creates
    one PH/PHP Checkout, reads explicit GCash evidence from that creation
    response, and stops without OAICS classification, tax updates,
    PaymentMethod, ctoken, start, or confirm calls. The local bulk default is
    eight workers (maximum 32) with whole-pool proxy rotation on retryable
    creation failures.

GoPay qualification reference:

- [link-gp](https://github.com/eatWhitePorridge/link-gp)
  - Audited reference commit: `3d2af69d848e6f292ef5abcb763c89dac3fbbea5`
  - Reference only; its standalone Flask workbench, batch scheduler, payment
    confirmation, approval, polling, and redirect extraction are not deployed.
  - The account-page detector reuses the source Checkout/Stripe boundary:
    create one promoted ID/IDR custom Plus Checkout, require a Stripe `cs_*`
    session, initialize Stripe once, and report eligible when currency is IDR
    and `gopay` is published. Per the local qualification requirement, the
    Checkout amount is diagnostic only and does not affect eligibility. It stops before Elements,
    taxes, PaymentMethod, confirm, approval, polling, or redirect extraction.
  - GoPay requires an Indonesia (`ID`) exit. Settings therefore keep one
    qualification card with separate per-qualification pools: GCash uses
    `GCASH_CHECK_PROXY_PROFILES`/`PH`, GoPay uses
    `GOPAY_CHECK_PROXY_PROFILES`/`ID`, and future detectors can add another
    independent pool inside the same card.

MoMo qualification reference:

- Local reference: `C:/Users/Administrator/Downloads/check_momo_eligibility..py`
- The account-page detector mirrors the local GCash early-stop boundary first,
  then follows the document's session split only when creation omits methods:
  OAICS reads its custom Checkout state, while `cs_live_*`/`cs_test_*` performs
  one Stripe init. It reports eligible when any inspected response publishes
  `momo`, and stops before taxes, pre_confirm, PaymentMethod, confirm, approval,
  polling, or redirect extraction. MoMo uses its own
  `MOMO_CHECK_PROXY_PROFILES` pool and fixed VN exit.

UPI qualification reference:

- Re-audited PAY.153 HEAD on 2026-09-17; it remains the locked
  `e8b36626162f09363f29b85af42de98cc8114c9b` revision. No upstream version change.
- `provider_checkout.py:stripe_to_provider` checks `upi` in the Stripe init
  methods before Elements, promotion updates, taxes or payment continuation.
  The local account detector stops at that availability boundary: create an
  IN/INR Plus Checkout without a promotion through the independent
  `UPI_CHECK_PROXY_PROFILES` IN pool, read methods directly when present,
  otherwise reuse the same session/proxy for one Stripe init or OAICS state read.
- A published UPI method in INR means eligible; amount/trial status is not
  an eligibility condition. Missing/malformed sessions and transport failures
  are failed checks, distinct from completed checks without UPI. Diagnostics
  exclude response bodies, tokens, checkout URLs and proxy credentials.
- Compared current upstream `app.py`, `provider_checkout.py`,
  `stripe_checkout.py`, and `upi_go_runner.py`. The last three remain identical;
  the intentional local routing/provider patches in `app.py` are retained.
  UPI qualification adds no runtime service and never enters extraction jobs.

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

The locked PAY.153 revision imports `jsdom` from `gen_token_jsdom.js`, but its
upstream repository does not currently include a `package.json` or document the
Node dependency in its installation steps. This site therefore carries a local
`package.json` plus `package-lock.json` that pin `jsdom` 26.1.0 and `undici`
8.7.0 (Node.js 22.19+). The latter is required by the embedded link-pp Sentinel
bridge.
`一键安装.bat` / `install-integrations.bat` install that lock with `npm ci`,
install the PAY.153 and site Python/Playwright runtimes, and run the integration
self-check before the site can start.

## Required update workflow

Before modifying payment/extraction code:

1. Fetch and read PAY.153 plus any affected reference upstream at current HEAD.
2. For Kakao work, fetch and read `kakao_oaics_source` too.
3. Compare relevant upstream files against the vendored paths; do not replace
   local files without reviewing local proxy, authentication, reverse-proxy,
   retry, and safety changes.
4. Update `upstream-lock.json` and the commit values above when intentionally
   syncing a newer revision.
5. Run focused provider tests and syntax/import checks before handoff.

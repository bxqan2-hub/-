# Bundled payment services

This directory vendors two upstream projects and loads them inside the `web.py` process:

- `pay153_checkout`, exposed through `/pay153/`
- `paypal_agreement_protocol`, exposed through `/paypal-pay/`

The main WebUI owns authentication and dispatches both apps in-process. Only
the configured WebUI port listens; no child service ports are used. Starting
and stopping `web.py` starts and stops the complete site.

On Windows, double-click `一键安装.bat` after copying the project to a new
computer or after dependency updates. It recreates the portable `.venv`,
installs all three Python requirement sets, installs the locked Node/jsdom
Sentinel runtime with `npm ci`, installs Playwright Chromium, prepares the
optional UPI engine, and runs `tools/check_integrations.py`. The WebUI startup
script runs the same fast self-check and refuses to start with an incomplete
runtime.

Upstream revisions:

- `paypal-agreement-protocol`: `4719066ec6fd56b57a5bd9599758366836c9dc0a`
- `pay153-checkout-link`: `e8b36626162f09363f29b85af42de98cc8114c9b`

Kakao `oaics_*` support is adapted inside `pay153_checkout` from
[`kakao_oaics_source`](https://github.com/m1243808154/kakao_oaics_source) at
`60e42034b7b7af7fad008da0578071e43cab855e`. It is not a third runtime app.
See `UPSTREAM_SOURCES.md` and `upstream-lock.json` before updating either
vendored project.

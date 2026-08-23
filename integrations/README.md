# Bundled payment service

This directory vendors the PAY.153 Checkout Link Router and loads it inside the
main `web.py` process:

- `pay153_checkout`, exposed through `/pay153/`

The main WebUI owns authentication and dispatches PAY.153 in-process. Only the
configured WebUI port listens; no child service port is used.

On Windows, double-click `一键安装.bat` after copying the project to a new
computer or after dependency updates. It recreates the portable `.venv`,
installs the site and PAY.153 requirements, installs the locked Node/jsdom
Sentinel runtime with `npm ci`, installs Playwright Chromium, prepares the
optional UPI engine, and runs `tools/check_integrations.py`.

Locked upstream revision:

- `pay153-checkout-link`: `e8b36626162f09363f29b85af42de98cc8114c9b`

Kakao `oaics_*` support is adapted inside `pay153_checkout` from
[`kakao_oaics_source`](https://github.com/m1243808154/kakao_oaics_source) at
`60e42034b7b7af7fad008da0578071e43cab855e`. It is not a second runtime app.
See `UPSTREAM_SOURCES.md` and `upstream-lock.json` before updating the vendored
project.

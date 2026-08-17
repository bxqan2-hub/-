# UPI Go Elements/B engine

This directory contains the Go source used by the UPI zero-due extraction
strategy. The Flask service invokes the compiled binary through
`upi_go_runner.py`.

Build on Linux:

```bash
cd tools/upi_go
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o pix_extract_slot .
chmod 700 pix_extract_slot
```

Runtime inputs are supplied through environment variables. Access tokens,
proxy credentials and billing details are not passed on the command line.

The default route is:

```text
IN checkout -> VN promotion -> IN Stripe/UPI
```

The surrounding Flask job owns full Checkout retries. The Go engine handles
TLS fingerprinting, regional proxy rewriting, Elements session creation,
tax-region updates, inline UPI confirmation, approval and result polling.

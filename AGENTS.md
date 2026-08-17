# Repository instructions

## Change delivery and rollback

- Do not create local backup copies, rollback fixtures, duplicate project
  trees, or `artifacts/` backup directories before or after a modification.
- Use commits in `https://github.com/bxqan2-hub/-.git` as the rollback history.
- After every completed modification, run the relevant validation, commit the
  finished change to the current branch, and push it directly to `origin`.
- Keep runtime data, credentials, logs, caches, and generated account files out
  of Git according to `.gitignore`.
- Before handoff, verify that the working tree is clean and that the local HEAD
  matches the pushed remote branch.

## Payment / extraction integrations

The only runtime implementations for payment-link extraction are:

- `integrations/pay153_checkout`, upstream: https://github.com/1537271403/pay153-checkout-link
- `integrations/paypal_agreement_protocol`, upstream: https://github.com/1537271403/paypal-agreement-protocol

Before changing either integration, payment routing, the Extract Center, or any
Kakao/PayPal/PIX/GCash provider logic, first read this file and
`integrations/UPSTREAM_SOURCES.md`, then fetch/read both upstream repositories
and compare the relevant upstream files with the vendored copies. Record any new
upstream commit in `integrations/upstream-lock.json` and the source document.

For Kakao OAICS changes, also read
https://github.com/m1243808154/kakao_oaics_source first. It is a protocol and
attribution source only; do not add it as a third runtime service. Preserve its
credit in the Extract Center and source documentation.

Never overwrite the vendored integrations blindly. Review the upstream diff,
preserve deliberate local routing/security patches, enforce the one-shot
Kakao confirm boundary, and run the focused integration tests plus the WebUI
tests affected by the change.

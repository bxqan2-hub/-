package main

import (
	"strings"
	"testing"
)

func TestResolveChannelUPIRoute(t *testing.T) {
	t.Setenv("PIX_CHANNEL", "upi")
	t.Setenv("PIX_PROMOTION_COUNTRY", "VN")
	ch := resolveChannel()
	if ch.code != "upi" {
		t.Fatalf("code=%q, want upi", ch.code)
	}
	if ch.country != "IN" || ch.promotionCountry != "VN" || ch.paymentMethod != "upi" {
		t.Fatalf("route=%s/%s method=%s, want IN/VN upi", ch.country, ch.promotionCountry, ch.paymentMethod)
	}
	if ch.routeLabel != "IN → VN → IN" {
		t.Fatalf("routeLabel=%q", ch.routeLabel)
	}
}

func TestProxyForRegionKeepsOnlyRequestedCountry(t *testing.T) {
	seed := "http://user-zone-br-session-old:pass@example.com:10000"
	provider := proxyForRegion(seed, "IN")
	promotion := proxyForRegion(seed, "VN")
	if provider == promotion {
		t.Fatalf("provider and promotion proxy should differ: %q", provider)
	}
	if !containsCI(provider, "zone-IN") || containsCI(provider, "zone-VN") {
		t.Fatalf("provider proxy not IN-only: %q", provider)
	}
	if !containsCI(promotion, "zone-VN") || containsCI(promotion, "zone-IN") {
		t.Fatalf("promotion proxy not VN-only: %q", promotion)
	}
}

func TestUPIDefaultStageSIDAttemptsUsesInternalRetry(t *testing.T) {
	t.Setenv("PIX_CHANNEL", "upi")
	t.Setenv("PIX_STAGE_SID_MAX_ATTEMPTS", "")
	t.Setenv("UPI_INTERNAL_SID_RETRY_MAX", "")
	if got := stageSIDMaxAttempts(); got != 5 {
		t.Fatalf("UPI default stage attempts=%d, want 5", got)
	}
	t.Setenv("UPI_INTERNAL_SID_RETRY_MAX", "7")
	if got := stageSIDMaxAttempts(); got != 7 {
		t.Fatalf("UPI env stage attempts=%d, want 7", got)
	}
}

func TestProxyWithFreshSIDRotatesFirstUse(t *testing.T) {
	seed := "http://user-session-old:pass@example.com:10000"
	first := proxyWithFreshSID(seed)
	if first == "" || first == seed || strings.Contains(first, "session-old") {
		t.Fatalf("first SID rotation failed: seed=%q first=%q", seed, first)
	}
	second := proxyWithFreshSID(first)
	if second == "" || second == first || strings.Contains(second, "session-old") {
		t.Fatalf("subsequent SID rotation failed: first=%q second=%q", first, second)
	}
}

func TestUPIStageRetryClassifierBoundaries(t *testing.T) {
	if !isStageSIDRetryableBody(nil, 403, "checkout failed: http 403: html_blocked_or_waf") {
		t.Fatalf("WAF 403 should retry inside the same Go slot")
	}
	if isStageSIDRetryableBody(nil, 402, `{"decline_code":"generic_decline"}`) {
		t.Fatalf("generic_decline must burn the outer attempt, not SID-loop forever")
	}
	if isStageSIDRetryableBody(nil, 401, "authentication token is expired") {
		t.Fatalf("expired token must be terminal")
	}
}

func TestProxyForRegionStrictRejectsNonTemplate(t *testing.T) {
	if _, err := proxyForRegionStrict("http://user:pass@example.com:10000", "IN"); err == nil {
		t.Fatalf("expected strict proxy selector rejection")
	}
}

func TestNewDeviceIDsUseUUID5SessionAndBareCookie(t *testing.T) {
	deviceID, sessionID, cookie := newDeviceIDs("")
	if deviceID == "" || sessionID == "" {
		t.Fatalf("empty device/session: %q %q", deviceID, sessionID)
	}
	if sessionID == deviceID {
		t.Fatalf("session id should be UUID5-derived, not device id")
	}
	if !strings.HasPrefix(cookie, "oai-did="+deviceID) {
		t.Fatalf("cookie %q missing oai-did", cookie)
	}
	if strings.Contains(cookie, "oai-sc=") {
		t.Fatalf("reference cookie should not synthesize oai-sc: %q", cookie)
	}
}

func TestChatGPTCookieIncludesSessionToken(t *testing.T) {
	cookie := chatGPTCookie("device-1", "session-1")
	if !strings.Contains(cookie, "oai-did=device-1") || !strings.Contains(cookie, "__Secure-next-auth.session-token=session-1") {
		t.Fatalf("session cookie missing fields: %q", cookie)
	}
}

func TestIsIPRoyalProxy(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"T4TtSzc42PuGarZs:sjjakjal123_country-br@geo.iproyal.com:12321", true},
		{"http://user:pass_country-jp@geo.iproyal.com:12321", true},
		{"socks5h://user:pass@iproyal.com:12321", true},
		{"http://user:pass@hk.rrp.bestgo.work:10000", false},
		{"http://user:pass@example.com:10000", false},
		{"", false},
	}
	for _, tc := range cases {
		if got := isIPRoyalProxy(tc.in); got != tc.want {
			t.Fatalf("isIPRoyalProxy(%q)=%v want %v", tc.in, got, tc.want)
		}
	}
}

func TestProxyWithFreshSIDIPRoyalPasswordSide(t *testing.T) {
	// Bare IPRoyal: session must land after country-XX in the password, not on username.
	raw := "http://T4TtSzc42PuGarZs:sjjakjal123_country-br@geo.iproyal.com:12321"
	out := proxyWithFreshSID(raw)
	if out == raw {
		t.Fatalf("expected SID insert, got unchanged %q", out)
	}
	if !strings.Contains(strings.ToLower(out), "country-br_session-") {
		t.Fatalf("IPRoyal bare SID not password-side: %q", out)
	}
	if strings.Contains(out, "T4TtSzc42PuGarZs-session-") || strings.Contains(out, "T4TtSzc42PuGarZs_session-") {
		t.Fatalf("IPRoyal SID incorrectly inserted into username: %q", out)
	}
	// Existing password-side session is rotated in place.
	seeded := "http://T4TtSzc42PuGarZs:sjjakjal123_country-IN_session-deadbeef@geo.iproyal.com:12321"
	rotated := proxyWithFreshSID(seeded)
	if !strings.Contains(strings.ToLower(rotated), "country-in_session-") {
		t.Fatalf("rotated IPRoyal lost country/session shape: %q", rotated)
	}
	if strings.Contains(rotated, "session-deadbeef") {
		t.Fatalf("expected session rotation, still deadbeef: %q", rotated)
	}
	if strings.HasPrefix(strings.ToLower(rotated), "http://t4ttszc42pugarzs-session-") {
		t.Fatalf("rotation moved session onto username: %q", rotated)
	}
}

func TestProxyWithFreshSIDBestgoUsernameSide(t *testing.T) {
	// Non-IPRoyal bare user:pass keeps username-side session insert (bestgo-like).
	raw := "http://USER411934-zone-custom-region-BR:secret@hk.rrp.bestgo.work:10000"
	out := proxyWithFreshSID(raw)
	if !strings.Contains(out, "USER411934-zone-custom-region-BR-session-") {
		t.Fatalf("bestgo-like username SID insert missing: %q", out)
	}
	if strings.Contains(out, ":secret_session-") || strings.Contains(out, "secret-session-") {
		t.Fatalf("bestgo SID incorrectly landed in password: %q", out)
	}
}

func TestUpdateStripeIDsPreservesInitialCheckoutConfig(t *testing.T) {
	ids := stripeIDs{}
	updateStripeIDs(&ids, map[string]any{"config_id": "cfg_initial"})
	updateStripeIDs(&ids, map[string]any{"config_id": "cfg_latest"})
	if ids.checkoutConfigID != "cfg_initial" {
		t.Fatalf("checkoutConfigID=%q, want initial", ids.checkoutConfigID)
	}
	if ids.configID != "cfg_latest" {
		t.Fatalf("configID=%q, want latest", ids.configID)
	}
}

func TestTerminalGoResultIncludesCheckoutNotActive(t *testing.T) {
	if !isTerminalGoResult(map[string]any{"error": "checkout_not_active_session"}) {
		t.Fatalf("checkout_not_active_session should be terminal")
	}
}

func TestUPIEffectiveAttemptBoundary(t *testing.T) {
	if !isEffectiveGoAttempt(map[string]any{"ok": true}) {
		t.Fatalf("success must count as effective attempt")
	}
	if !isEffectiveGoAttempt(map[string]any{"declineCode": "generic_decline"}) {
		t.Fatalf("generic_decline must count as effective attempt")
	}
	if isEffectiveGoAttempt(map[string]any{"error": "checkout failed: http 403: html_blocked_or_waf"}) {
		t.Fatalf("WAF/network failure must not count as effective attempt")
	}
	if isEffectiveGoAttempt(map[string]any{"approveTerminal": true, "error": "checkout approve rejected: result=blocked"}) {
		t.Fatalf("approve blocked blacklist terminal is not a valid Stripe attempt")
	}
}

func TestTerminalGoResultBoundary(t *testing.T) {
	for _, item := range []map[string]any{
		{"error": "checkout_not_upi_trial currency=inr amount=169407 methods=[card upi]"},
		{"error": "token_expired"},
		{"error": "token revoked"},
		{"approveTerminal": true},
	} {
		if !isTerminalGoResult(item) {
			t.Fatalf("expected terminal result for %#v", item)
		}
	}
	if isTerminalGoResult(map[string]any{"error": "checkout failed: http 403: html_blocked_or_waf"}) {
		t.Fatalf("WAF/network failure should rotate SID/retry, not terminate")
	}
}

func TestEnrichDeclineFromCurrentPollJSON(t *testing.T) {
	result := map[string]any{}
	enrichDeclineFromObj(result, map[string]any{
		"submission_attempt": map[string]any{
			"state": "failed",
			"error": map[string]any{
				"code": "checkout_approval_payment_failure_with_payment_error",
				"payment_error": map[string]any{
					"code":         "setup_attempt_failed",
					"decline_code": "generic_decline",
				},
			},
		},
		"setup_intent": map[string]any{
			"status": "requires_payment_method",
			"last_setup_error": map[string]any{
				"code":         "setup_attempt_failed",
				"decline_code": "generic_decline",
				"message":      "The latest attempt to set up the payment method has failed.",
			},
		},
	})
	if result["declineCode"] != "generic_decline" || result["paymentErrorCode"] != "setup_attempt_failed" {
		t.Fatalf("decline fields missing: %#v", result)
	}
	if !hasGenericDeclineResult(result) || !isEffectiveGoAttempt(result) {
		t.Fatalf("generic_decline poll must count as effective attempt: %#v", result)
	}
}

func TestJsonSuccessFalse(t *testing.T) {
	if !jsonSuccessFalse([]byte(`{"success":false}`)) {
		t.Fatalf("success=false payload should fail validation")
	}
	if jsonSuccessFalse([]byte(`{"success":true}`)) {
		t.Fatalf("success=true payload should not fail validation")
	}
}

func containsCI(s, sub string) bool {
	return strings.Contains(strings.ToLower(s), strings.ToLower(sub))
}

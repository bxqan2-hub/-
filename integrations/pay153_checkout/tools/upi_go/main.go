// PIX extract slot binary: TLS/H2 aligned to curl_cffi firefox135 + full PIX link path.
//
//	go build -mod=mod -o bin/pix_extract_slot .
//	./bin/pix_extract_slot -slot -token "$TOKEN" -proxy "$PROXY"
//	# -slot: quiet stdout JSON (pix_link_once-compatible). exit 0 only on real PIX material.
package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"html"
	"io"
	mrand "math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	fhttp "github.com/bogdanfinn/fhttp"
	"github.com/bogdanfinn/fhttp/http2"
	tls_client "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
	tls "github.com/bogdanfinn/utls"
	"github.com/bogdanfinn/utls/dicttls"
	"github.com/google/uuid"
)

const defaultURL = "https://tls.peet.ws/api/all"

const (
	// Aligned to E:/Download/upi_extract.py (2026-07-21).
	defaultUA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0"
	stripeVer  = "2020-08-27;custom_checkout_beta=v1; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
	stripeVerB = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
	oaiVer     = "prod-fb4a8a2a751dfec391053cfd7b01c52699ccf78c"
	oaiBuild   = "8370486"
	runtimeVer = "5507c504c1"
	countryBR  = "BR"
)

// channelConfig is PIX (default) or UPI. PIX keeps one slot attempt;
// UPI owns its 5-attempt outer loop plus per-stage SID retries inside Go.
type channelConfig struct {
	code             string
	country          string
	currency         string
	paymentMethod    string
	browserLocale    string
	acceptLanguage   string
	elementsLocale   string
	timezone         string
	oaiLanguage      string
	oaiSC            string
	promotionCountry string
	requireTaxID     bool
	routeLabel       string
}

func resolveChannel() channelConfig {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv("PIX_CHANNEL")))
	if raw == "" {
		if strings.TrimSpace(os.Getenv("UPI_SLOT")) == "1" || strings.EqualFold(strings.TrimSpace(os.Getenv("UPI_EXTRACT")), "1") {
			raw = "upi"
		} else {
			raw = "pix"
		}
	}
	promo := strings.ToUpper(strings.TrimSpace(os.Getenv("PIX_PROMOTION_COUNTRY")))
	if raw == "upi" {
		if promo == "" {
			promo = strings.ToUpper(strings.TrimSpace(os.Getenv("UPI_PROMOTION_COUNTRY")))
		}
		if promo == "" {
			promo = "VN"
		}
		return channelConfig{
			code: "upi", country: "IN", currency: "INR", paymentMethod: "upi",
			// India locale (upi_extract default hi-IN).
			browserLocale: "hi-IN", acceptLanguage: "hi-IN,hi;q=0.9,en-US;q=0.8,en;q=0.7", elementsLocale: "en",
			timezone: "Asia/Kolkata", oaiLanguage: "hi-IN", oaiSC: "in",
			promotionCountry: promo, requireTaxID: false,
			routeLabel: "IN → " + promo + " → IN",
		}
	}
	if promo == "" {
		promo = "BR"
	}
	return channelConfig{
		code: "pix", country: "BR", currency: "BRL", paymentMethod: "pix",
		browserLocale: "pt-BR", acceptLanguage: "pt-BR,pt;q=0.9,en;q=0.8", elementsLocale: "pt-BR",
		timezone: "America/Sao_Paulo", oaiLanguage: "pt-BR", oaiSC: "br",
		promotionCountry: promo, requireTaxID: true,
		routeLabel: "BR → " + promo + " → BR",
	}
}

// stripeAPIHeaders builds Stripe API headers. UPI/B follows upi_extract_latest.py:
// curl_cffi chrome136 transport plus Firefox UA, no sec-fetch headers on Stripe API.
func stripeAPIHeaders(csID, mode string) fhttp.Header {
	mode = strings.ToLower(strings.TrimSpace(mode))
	al := resolveChannel().acceptLanguage
	base := map[string]string{
		"accept": "application/json", "content-type": "application/x-www-form-urlencoded",
		"user-agent": defaultUA, "accept-language": al,
		"pragma": "no-cache", "cache-control": "no-cache",
	}
	if mode == "b" {
		base["origin"] = "https://js.stripe.com"
		base["referer"] = "https://js.stripe.com/"
		return curlCFFIFirefox135Headers(base)
	}
	base["origin"] = "https://pay.openai.com"
	base["referer"] = "https://pay.openai.com/c/pay/" + csID
	return curlCFFIFirefox135Headers(base)
}

func resolveProtocolMode() string {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv("PIX_PROTOCOL_MODE")))
	if raw == "b" || raw == "elements" || raw == "route_b" || raw == "payment_method_data" {
		return "b"
	}
	return "a"
}

func envIntClamped(names []string, def, min, max int) int {
	for _, name := range names {
		raw := strings.TrimSpace(os.Getenv(name))
		if raw == "" {
			continue
		}
		n, err := strconv.Atoi(raw)
		if err != nil {
			continue
		}
		if n < min {
			return min
		}
		if n > max {
			return max
		}
		return n
	}
	return def
}

func envBool(names []string, def bool) bool {
	for _, name := range names {
		raw := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
		if raw == "" {
			continue
		}
		switch raw {
		case "1", "true", "yes", "on":
			return true
		case "0", "false", "no", "off":
			return false
		}
	}
	return def
}

func fullpageFallbackEnabled() bool {
	if resolveChannel().code == "upi" {
		return envBool([]string{"UPI_FULLPAGE_FALLBACK_ENABLED", "PIX_FULLPAGE_FALLBACK_ENABLED"}, true)
	}
	return envBool([]string{"PIX_FULLPAGE_FALLBACK_ENABLED"}, true)
}

func approveDeviceRotateEnabled() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("PIX_APPROVE_DEVICE_ROTATE")))
	if resolveChannel().code == "upi" {
		v = strings.ToLower(strings.TrimSpace(firstNonEmpty(os.Getenv("UPI_APPROVE_DEVICE_ROTATE"), os.Getenv("PIX_APPROVE_DEVICE_ROTATE"))))
	}
	return v == "1" || v == "true" || v == "yes" || v == "on"
}

func approveInnerMax() int {
	if resolveChannel().code == "upi" {
		return envIntClamped([]string{"UPI_APPROVE_RETRY_MAX", "UPI_INTERNAL_SID_RETRY_MAX", "PIX_APPROVE_INNER_MAX"}, 5, 1, 10)
	}
	return envIntClamped([]string{"PIX_APPROVE_INNER_MAX"}, 5, 1, 8)
}

func approveBlockedMax() int {
	if resolveChannel().code == "upi" {
		return envIntClamped([]string{"UPI_MAX_APPROVE_BLOCKED", "PIX_APPROVE_BLOCKED_MAX"}, 1, 1, 10)
	}
	return envIntClamped([]string{"PIX_APPROVE_BLOCKED_MAX"}, 5, 1, 10)
}

func pollBudgetSeconds() int {
	if resolveChannel().code == "upi" {
		return envIntClamped([]string{"UPI_POLL_TIMEOUT", "PIX_POLL_TIMEOUT"}, 5, 1, 120)
	}
	return envIntClamped([]string{"PIX_POLL_TIMEOUT"}, 5, 1, 120)
}

func pollMaxQueries() int {
	if resolveChannel().code == "upi" {
		return envIntClamped([]string{"UPI_POLL_MAX_QUERIES", "PIX_POLL_MAX_QUERIES"}, 5, 1, 30)
	}
	return envIntClamped([]string{"PIX_POLL_MAX_QUERIES"}, 10, 1, 30)
}

func newDeviceIDs(sessionToken string) (deviceID, sessionID, cookie string) {
	deviceID = newUUID()
	// upi_extract_latest.py: oai-session-id = uuid5(NAMESPACE_URL, device_id).
	sessionID = uuid.NewSHA1(uuid.NameSpaceURL, []byte(deviceID)).String()
	cookie = chatGPTCookie(deviceID, sessionToken)
	return
}

func chatGPTCookie(deviceID, sessionToken string) string {
	cookie := "oai-did=" + strings.TrimSpace(deviceID)
	if st := strings.TrimSpace(sessionToken); st != "" {
		cookie += "; __Secure-next-auth.session-token=" + st
	}
	return cookie
}

func normalizeAccessAndSessionToken(token string) (string, string) {
	raw := strings.TrimSpace(token)
	sessionToken := firstNonEmpty(os.Getenv("PIX_SESSION_TOKEN"), os.Getenv("UPI_SESSION_TOKEN"))
	if strings.HasPrefix(raw, "{") {
		var data map[string]any
		if json.Unmarshal([]byte(raw), &data) == nil {
			raw = firstNonEmpty(firstString(data, "access_token", "accessToken", "token"), raw)
			if sessionToken == "" {
				sessionToken = firstString(data, "session_token", "sessionToken", "__Secure-next-auth.session-token")
			}
		}
	}
	return raw, sessionToken
}

type billingAddress struct {
	line1, line2, city, state, postalCode string
}

// Landmark BR addresses rotated per attempt (aligned with local_pix_api defaults).
var brBillingAddresses = []billingAddress{
	{"Avenida Paulista, 1578", "Bela Vista", "São Paulo", "SP", "01310-200"},
	{"Avenida Rebouças, 3970", "Pinheiros", "São Paulo", "SP", "05402-600"},
	{"Rua Oscar Freire, 379", "Jardins", "São Paulo", "SP", "01426-001"},
	{"Avenida Brigadeiro Faria Lima, 3477", "Itaim Bibi", "São Paulo", "SP", "04538-133"},
	{"Avenida Atlântica, 1702", "Copacabana", "Rio de Janeiro", "RJ", "22021-001"},
	{"Avenida Rio Branco, 156", "Centro", "Rio de Janeiro", "RJ", "20040-003"},
	{"Rua Visconde de Pirajá, 550", "Ipanema", "Rio de Janeiro", "RJ", "22410-002"},
	{"Avenida do Contorno, 6594", "Savassi", "Belo Horizonte", "MG", "30110-044"},
	{"Avenida Afonso Pena, 1500", "Centro", "Belo Horizonte", "MG", "30130-005"},
	{"Rua Marechal Deodoro, 630", "Centro", "Curitiba", "PR", "80010-010"},
	{"Avenida Sete de Setembro, 2775", "Rebouças", "Curitiba", "PR", "80230-010"},
	{"Avenida Borges de Medeiros, 510", "Centro Histórico", "Porto Alegre", "RS", "90020-022"},
	{"Rua dos Andradas, 1001", "Centro Histórico", "Porto Alegre", "RS", "90020-007"},
	{"Avenida Sete de Setembro, 1809", "Vitória", "Salvador", "BA", "40080-002"},
	{"Avenida Tancredo Neves, 1487", "Caminho das Árvores", "Salvador", "BA", "41820-021"},
	{"Avenida Boa Viagem, 5100", "Boa Viagem", "Recife", "PE", "51030-000"},
	{"Rua do Bom Jesus, 135", "Recife Antigo", "Recife", "PE", "50030-170"},
	{"SCS Quadra 2 Bloco C", "Asa Sul", "Brasília", "DF", "70302-000"},
	{"SHN Quadra 1 Bloco A", "Asa Norte", "Brasília", "DF", "70701-000"},
	{"Avenida Beira Mar Norte, 4060", "Centro", "Florianópolis", "SC", "88015-700"},
	{"Rua Felipe Schmidt, 303", "Centro", "Florianópolis", "SC", "88010-000"},
	{"Avenida Goiás, 1991", "Setor Central", "Goiânia", "GO", "74063-010"},
	{"Avenida T-4, 1470", "Setor Bueno", "Goiânia", "GO", "74230-030"},
	{"Avenida Beira Mar, 3470", "Meireles", "Fortaleza", "CE", "60165-121"},
	{"Avenida Dom Luís, 1200", "Aldeota", "Fortaleza", "CE", "60160-230"},
	{"Avenida Paulista, 2064", "Consolação", "São Paulo", "SP", "01310-928"},
	{"Avenida Vieira Souto, 460", "Ipanema", "Rio de Janeiro", "RJ", "22420-006"},
	{"Avenida Afonso Pena, 3940", "Cruzeiro", "Belo Horizonte", "MG", "30130-009"},
}

var brFirstNames = []string{
	"Lucas", "Gabriel", "Rafael", "Mateus", "Bruno", "Pedro", "Thiago", "Felipe", "Gustavo", "André",
	"Ana", "Maria", "Julia", "Camila", "Beatriz", "Larissa", "Fernanda", "Amanda", "Carolina", "Isabela",
}

var brLastNames = []string{
	"Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Alves", "Pereira",
	"Lima", "Gomes", "Costa", "Ribeiro", "Martins", "Carvalho", "Rocha", "Almeida",
}

// emitSlotJSON: when true, writeResult prints one JSON object to stdout (Python sidecar contract).
var emitSlotJSON bool
var captureSlotResult bool
var capturedSlotResult map[string]any

func firefox135CFFI() profiles.ClientProfile {
	helloID := tls.ClientHelloID{
		Client:               "Firefox",
		RandomExtensionOrder: false,
		Version:              "135-cffi",
		Seed:                 nil,
		SpecFactory: func() (tls.ClientHelloSpec, error) {
			return tls.ClientHelloSpec{
				CipherSuites: []uint16{
					tls.TLS_AES_128_GCM_SHA256,
					tls.TLS_CHACHA20_POLY1305_SHA256,
					tls.TLS_AES_256_GCM_SHA384,
					tls.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,
					tls.TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256,
					tls.TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256,
					tls.TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256,
					tls.TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384,
					tls.TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384,
					tls.TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA,
					tls.TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA,
					tls.TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA,
					tls.TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA,
					tls.TLS_RSA_WITH_AES_128_GCM_SHA256,
					tls.TLS_RSA_WITH_AES_256_GCM_SHA384,
					tls.TLS_RSA_WITH_AES_128_CBC_SHA,
					tls.TLS_RSA_WITH_AES_256_CBC_SHA,
				},
				CompressionMethods: []byte{tls.CompressionNone},
				Extensions: []tls.TLSExtension{
					&tls.SNIExtension{},
					&tls.ExtendedMasterSecretExtension{},
					&tls.RenegotiationInfoExtension{Renegotiation: tls.RenegotiateOnceAsClient},
					&tls.SupportedCurvesExtension{Curves: []tls.CurveID{
						tls.X25519MLKEM768, tls.X25519, tls.CurveP256, tls.CurveP384, tls.CurveP521,
						tls.FAKEFFDHE2048, tls.FAKEFFDHE3072,
					}},
					&tls.SupportedPointsExtension{SupportedPoints: []byte{tls.PointFormatUncompressed}},
					&tls.SessionTicketExtension{},
					&tls.ALPNExtension{AlpnProtocols: []string{"h2", "http/1.1"}},
					&tls.StatusRequestExtension{},
					&tls.DelegatedCredentialsExtension{SupportedSignatureAlgorithms: []tls.SignatureScheme{
						tls.ECDSAWithP256AndSHA256, tls.ECDSAWithP384AndSHA384, tls.ECDSAWithP521AndSHA512, tls.ECDSAWithSHA1,
					}},
					&tls.SCTExtension{},
					&tls.KeyShareExtension{KeyShares: []tls.KeyShare{
						{Group: tls.X25519MLKEM768}, {Group: tls.X25519}, {Group: tls.CurveP256},
					}},
					&tls.SupportedVersionsExtension{Versions: []uint16{tls.VersionTLS13, tls.VersionTLS12}},
					&tls.SignatureAlgorithmsExtension{SupportedSignatureAlgorithms: []tls.SignatureScheme{
						tls.ECDSAWithP256AndSHA256, tls.ECDSAWithP384AndSHA384, tls.ECDSAWithP521AndSHA512,
						tls.PSSWithSHA256, tls.PSSWithSHA384, tls.PSSWithSHA512,
						tls.PKCS1WithSHA256, tls.PKCS1WithSHA384, tls.PKCS1WithSHA512,
						tls.ECDSAWithSHA1, tls.PKCS1WithSHA1,
					}},
					&tls.PSKKeyExchangeModesExtension{Modes: []uint8{tls.PskModeDHE}},
					&tls.FakeRecordSizeLimitExtension{Limit: 0x4001},
					&tls.UtlsCompressCertExtension{Algorithms: []tls.CertCompressionAlgo{
						tls.CertCompressionZlib, tls.CertCompressionBrotli, tls.CertCompressionZstd,
					}},
					&tls.GREASEEncryptedClientHelloExtension{
						CandidateCipherSuites: []tls.HPKESymmetricCipherSuite{
							{KdfId: dicttls.HKDF_SHA256, AeadId: dicttls.AEAD_AES_128_GCM},
							{KdfId: dicttls.HKDF_SHA256, AeadId: dicttls.AEAD_AES_256_GCM},
							{KdfId: dicttls.HKDF_SHA256, AeadId: dicttls.AEAD_CHACHA20_POLY1305},
						},
						CandidatePayloadLens: []uint16{128, 223},
					},
				},
			}, nil
		},
	}
	return profiles.NewClientProfile(
		helloID,
		map[http2.SettingID]uint32{
			http2.SettingHeaderTableSize: 65536, http2.SettingEnablePush: 0,
			http2.SettingInitialWindowSize: 131072, http2.SettingMaxFrameSize: 16384,
		},
		[]http2.SettingID{
			http2.SettingHeaderTableSize, http2.SettingEnablePush,
			http2.SettingInitialWindowSize, http2.SettingMaxFrameSize,
		},
		[]string{":method", ":path", ":authority", ":scheme"},
		12517377, nil, nil,
	)
}

func profileByName(name string) (profiles.ClientProfile, bool) {
	switch name {
	case "firefox_135":
		return profiles.Firefox_135, true
	case "firefox_135_cffi", "firefox_135_plus", "firefox_135_cffi_h2":
		return firefox135CFFI(), true
	case "firefox_133":
		return profiles.Firefox_133, true
	case "firefox_132":
		return profiles.Firefox_132, true
	case "chrome_133", "chrome_136", "chrome_136_cffi":
		return profiles.Chrome_133, true
	case "chrome_131":
		return profiles.Chrome_131, true
	default:
		return profiles.ClientProfile{}, false
	}
}

func main() {
	urlFlag := flag.String("url", defaultURL, "fingerprint collector URL")
	proxy := flag.String("proxy", "", "optional proxy URL")
	profilesCSV := flag.String("profiles", "chrome_136_cffi", "comma-separated profiles")
	outPath := flag.String("out", "pix_tls_probe_go.json", "output JSON path")
	timeoutSec := flag.Int("timeout", 45, "per-request timeout seconds")
	pixSmoke := flag.Bool("pix-smoke", false, "partial smoke: checkout..promo")
	pixExtract := flag.Bool("pix-extract", false, "full PIX extract attempt")
	slotMode := flag.Bool("slot", false, "production slot mode: quiet stdout JSON for Python worker")
	token := flag.String("token", "", "ChatGPT access token")
	quiet := flag.Bool("quiet", false, "suppress progress logs (implied by -slot)")
	flag.Parse()

	if *slotMode || *pixExtract || *pixSmoke {
		full := *slotMode || *pixExtract
		q := *quiet || *slotMode
		code := runPixExtract(*token, *proxy, *timeoutSec, full, q, *slotMode)
		os.Exit(code)
	}

	names := splitCSV(*profilesCSV)
	results := make([]map[string]any, 0, len(names))
	for _, name := range names {
		name = strings.TrimSpace(strings.ToLower(name))
		if name == "" {
			continue
		}
		item := map[string]any{"profile": name, "url": *urlFlag, "proxy": *proxy != ""}
		body, summary, err := probeOne(name, *urlFlag, *proxy, *timeoutSec)
		if err != nil {
			item["ok"] = false
			item["error"] = err.Error()
			results = append(results, item)
			fmt.Fprintf(os.Stderr, "[FAIL] %s: %v\n", name, err)
			continue
		}
		item["ok"] = true
		item["summary"] = summary
		item["raw"] = body
		results = append(results, item)
		fmt.Printf("[OK] %-22s ja3_hash=%v ja4=%v\n", name, summary["ja3_hash"], summary["ja4"])
	}
	payload := map[string]any{"generated_at": time.Now().UTC().Format(time.RFC3339), "collector": *urlFlag, "results": results}
	raw, _ := json.MarshalIndent(payload, "", "  ")
	_ = os.WriteFile(*outPath, raw, 0o644)
	fmt.Printf("wrote %s\n", *outPath)
}

func splitCSV(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func normalizeProxyURL(raw string) string {
	text := strings.TrimSpace(raw)
	if text == "" {
		return ""
	}
	if i := strings.Index(text, "||"); i >= 0 {
		text = strings.TrimSpace(text[:i])
	}
	text = strings.Trim(text, `"'`)
	if strings.Contains(text, "://") {
		return text
	}
	if strings.Contains(text, "@") {
		return "http://" + text
	}
	parts := strings.Split(text, ":")
	if len(parts) == 4 {
		return "http://" + parts[2] + ":" + parts[3] + "@" + parts[0] + ":" + parts[1]
	}
	if len(parts) == 2 {
		return "http://" + parts[0] + ":" + parts[1]
	}
	return text
}

func stageSIDMaxAttempts() int {
	if resolveChannel().code == "upi" {
		return envIntClamped([]string{"UPI_INTERNAL_SID_RETRY_MAX", "PIX_STAGE_SID_MAX_ATTEMPTS"}, 5, 1, 10)
	}
	return envIntClamped([]string{"PIX_STAGE_SID_MAX_ATTEMPTS"}, 5, 1, 10)
}

func isIPRoyalProxy(proxy string) bool {
	value := normalizeProxyURL(proxy)
	if value == "" {
		return false
	}
	host := value
	if i := strings.Index(host, "://"); i >= 0 {
		host = host[i+3:]
	}
	if at := strings.LastIndex(host, "@"); at >= 0 {
		host = host[at+1:]
	}
	if slash := strings.IndexAny(host, "/?#"); slash >= 0 {
		host = host[:slash]
	}
	if colon := strings.LastIndex(host, ":"); colon >= 0 {
		// strip port; keep IPv6 bracket form out of scope for proxy hosts
		host = host[:colon]
	}
	host = strings.ToLower(strings.TrimSpace(host))
	return host == "iproyal.com" || strings.HasSuffix(host, ".iproyal.com")
}

func proxyWithFreshSID(proxy string) string {
	value := normalizeProxyURL(proxy)
	if value == "" {
		return ""
	}
	sid := randHex(12)
	// query form ?session= / &sid=
	reQ := regexp.MustCompile(`(?i)([?&](?:sid|session)=)([^&#]+)`)
	if reQ.MatchString(value) {
		return reQ.ReplaceAllString(value, "${1}"+sid)
	}
	// userinfo session- / sid- (also covers IPRoyal password-side _session-xxx)
	reU := regexp.MustCompile(`(?i)(sid|session)([-_])([a-z0-9{}]+)`)
	if reU.MatchString(value) {
		return reU.ReplaceAllString(value, "${1}${2}"+sid)
	}
	// IPRoyal: sticky session belongs in the password after country/region/zone.
	// Python payment_proxy_chain inserts: country-XX_session-{sid}
	// Username-side -session- is rejected by IPRoyal (407).
	if isIPRoyalProxy(value) {
		if start, end, ok := findProxyRegionToken(value); ok {
			return value[:start] + value[start:end] + "_session-" + sid + value[end:]
		}
	}
	// bestgo-like: insert session- on username before @ if userinfo present
	if at := strings.LastIndex(value, "@"); at > 0 {
		left, right := value[:at], value[at:]
		if schemeIdx := strings.Index(left, "://"); schemeIdx >= 0 {
			rest := left[schemeIdx+3:]
			if strings.Contains(rest, ":") && !strings.Contains(strings.ToLower(rest), "session-") && !strings.Contains(strings.ToLower(rest), "sid-") {
				// USER:PASS -> USER-session-SID:PASS
				u, p, ok := strings.Cut(rest, ":")
				if ok {
					return left[:schemeIdx+3] + u + "-session-" + sid + ":" + p + right
				}
			}
		}
	}
	return value
}

// findProxyRegionToken returns the [start,end) span of the first region/zone/country-XX
// token whose two-letter code is not a prefix of a longer alnum run (RE2-safe boundary).
func findProxyRegionToken(value string) (int, int, bool) {
	re := regexp.MustCompile(`(?i)(?:region|zone|country)[-_][a-z]{2}`)
	searchFrom := 0
	for searchFrom <= len(value) {
		loc := re.FindStringIndex(value[searchFrom:])
		if loc == nil {
			return 0, 0, false
		}
		start, end := searchFrom+loc[0], searchFrom+loc[1]
		if end >= len(value) {
			return start, end, true
		}
		next := value[end]
		if (next >= 'a' && next <= 'z') || (next >= 'A' && next <= 'Z') || (next >= '0' && next <= '9') {
			searchFrom = end
			continue
		}
		return start, end, true
	}
	return 0, 0, false
}

func normalizeProxyRegionCode(region string) string {
	code := strings.ToUpper(strings.TrimSpace(region))
	if matched, _ := regexp.MatchString(`^[A-Z]{2}$`, code); matched {
		return code
	}
	return ""
}

func proxyForRegion(proxy string, region string) string {
	value := normalizeProxyURL(proxy)
	country := normalizeProxyRegionCode(region)
	if value == "" || country == "" {
		return value
	}
	reRegion := regexp.MustCompile(`(?i)(region|zone|country)([-_])([a-z]{2})`)
	if reRegion.MatchString(value) {
		value = reRegion.ReplaceAllString(value, "${1}${2}"+country)
		if country != "JP" {
			reSticky := regexp.MustCompile(`(?i)([-_])st[-_][^-_:?@]+`)
			value = reSticky.ReplaceAllString(value, "")
		}
	}
	return value
}

func proxyForRegionStrict(proxy string, region string) (string, error) {
	value := normalizeProxyURL(proxy)
	if value == "" {
		return "", fmt.Errorf("proxy is empty")
	}
	if _, _, ok := findProxyRegionToken(value); !ok {
		return "", fmt.Errorf("proxy missing country/region/zone selector")
	}
	return proxyForRegion(value, region), nil
}

func isTokenTerminalText(text string) bool {
	low := strings.ToLower(text)
	return strings.Contains(low, "token_expired") || strings.Contains(low, "token revoked") || strings.Contains(low, "token_revoked") || strings.Contains(low, "authentication token is expired")
}

func isCheckoutNotActiveText(text string) bool {
	low := strings.ToLower(text)
	return strings.Contains(low, "checkout_not_active_session") || strings.Contains(low, "session no longer active") || strings.Contains(low, "checkout session is no longer active")
}

func isGenericDeclineText(text string) bool {
	return strings.Contains(strings.ToLower(text), "generic_decline")
}

func isUserAlreadyPaidText(text string) bool {
	return strings.Contains(strings.ToLower(text), "user is already paid")
}

func isStageSIDRetryableBody(err error, status int, body string) bool {
	if isTokenTerminalText(body) || isGenericDeclineText(body) || isUserAlreadyPaidText(body) {
		return false
	}
	lowBody := strings.ToLower(body)
	for _, m := range []string{"html_blocked_or_waf", "cloudflare", "please enable cookies", "access denied", "request blocked"} {
		if strings.Contains(lowBody, m) {
			return true
		}
	}
	if err != nil {
		low := strings.ToLower(err.Error())
		if isTokenTerminalText(low) || isGenericDeclineText(low) || isUserAlreadyPaidText(low) {
			return false
		}
		for _, m := range []string{
			"tls", "ssl", "timeout", "timed out", "gateway timeout", "connection reset", "connection refused",
			"connect tunnel", "proxy connect", "proxy responded with non 200", "non 200 code", "failed to perform", "eof", "broken pipe",
		} {
			if strings.Contains(low, m) {
				return true
			}
		}
	}
	if status == 403 && (strings.Contains(lowBody, "html") || strings.Contains(lowBody, "blocked")) {
		return true
	}
	if status == 408 || status == 409 || status == 425 || status == 429 || status == 500 || status == 502 || status == 503 || status == 504 {
		return true
	}
	return false
}

func isStageSIDRetryable(err error, status int) bool {
	return isStageSIDRetryableBody(err, status, "")
}

func checkExitPolicy(client tls_client.HttpClient, requireResidential bool, expectedCountry string) (ip, country string, residential bool, err error) {
	// Use ippure JSON if available; fallback: no hard fail when endpoint unavailable unless requireResidential.
	req, e := fhttp.NewRequest(fhttp.MethodGet, "https://my.ippure.com/v1/info", nil)
	if e != nil {
		return "", "", false, e
	}
	req.Header.Set("User-Agent", defaultUA)
	resp, e := client.Do(req)
	if e != nil {
		return "", "", false, e
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var data map[string]any
	if json.Unmarshal(b, &data) != nil {
		return "", "", false, fmt.Errorf("ippure bad json")
	}
	ip = strings.TrimSpace(fmt.Sprint(data["ip"]))
	if ip == "" || ip == "<nil>" {
		return "", "", false, fmt.Errorf("empty exit ip")
	}
	parsed := net.ParseIP(ip)
	if parsed == nil || parsed.To4() == nil {
		return ip, "", false, fmt.Errorf("exit ip is not public IPv4: %s", ip)
	}
	if parsed.IsLoopback() || parsed.IsPrivate() || parsed.IsMulticast() || parsed.IsUnspecified() || !parsed.IsGlobalUnicast() {
		// IsPrivate covers RFC1918; also reject link-local etc.
		if parsed.IsLoopback() || parsed.IsPrivate() || parsed.IsMulticast() || parsed.IsUnspecified() || parsed.IsLinkLocalUnicast() {
			return ip, "", false, fmt.Errorf("exit ip is not public IPv4: %s", ip)
		}
	}
	country = strings.ToUpper(strings.TrimSpace(fmt.Sprint(firstString(data, "countryCode", "country_code", "country"))))
	switch v := data["isResidential"].(type) {
	case bool:
		residential = v
	case string:
		residential = strings.EqualFold(v, "true") || v == "1"
	}
	expectedCountry = strings.ToUpper(strings.TrimSpace(expectedCountry))
	if expectedCountry == "" {
		expectedCountry = countryBR
	}
	if country != expectedCountry {
		return ip, country, residential, fmt.Errorf("exit country %s, expected %s", country, expectedCountry)
	}
	if requireResidential && !residential {
		return ip, country, residential, fmt.Errorf("isResidential=false ip=%s", ip)
	}
	if ip == "" {
		return ip, country, residential, fmt.Errorf("empty exit ip")
	}
	return ip, country, residential, nil
}

func rebuildClientWithExitPolicy(proxy string, timeoutSec int, requireResidential bool, expectedCountry string, stage string, attempt int, rec func(string, int, bool, string, error, int64)) (tls_client.HttpClient, string, string, string, bool, error) {
	// Always rotate SID before a Go stage touches the network. Reusing the seed SID can make
	// checkout / checkout_update repeat a blocked IP even when the retry loop is enabled.
	p := proxyWithFreshSID(proxy)
	if p == "" {
		p = proxy
	}
	var last error
	for rot := 0; rot < 8; rot++ {
		if rot > 0 {
			p = proxyWithFreshSID(p)
		}
		c, e := newAlignedClient(p, timeoutSec)
		if e != nil {
			last = e
			rec(fmt.Sprintf("pix.stage_exit_rebuild.%s_%d", stage, attempt), 0, false, "", e, 0)
			continue
		}
		ip, country, res, e := checkExitPolicy(c, requireResidential, expectedCountry)
		if e != nil {
			last = e
			rec(fmt.Sprintf("pix.stage_exit_check.%s_%d", stage, attempt), 0, false, "", e, 0)
			continue
		}
		rec(fmt.Sprintf("pix.stage_exit_ok.%s", stage), 200, true, fmt.Sprintf("ip=%s country=%s residential=%v", ip, country, res), nil, 0)
		return c, p, ip, country, res, nil
	}
	if last == nil {
		last = fmt.Errorf("exit policy exhausted")
	}
	return nil, p, "", "", false, last
}

func doStageWithSIDRetry(
	proxy string,
	timeoutSec int,
	requireResidential bool,
	expectedCountry string,
	stage string,
	rec func(string, int, bool, string, error, int64),
	fn func(client tls_client.HttpClient, proxy string) (status int, raw []byte, err error),
) (tls_client.HttpClient, string, int, []byte, error, int) {
	stageMax := stageSIDMaxAttempts()
	var (
		client tls_client.HttpClient
		status int
		raw    []byte
		err    error
		p      = proxy
	)
	for attempt := 1; attempt <= stageMax; attempt++ {
		c2, p2, _, _, _, e2 := rebuildClientWithExitPolicy(p, timeoutSec, requireResidential, expectedCountry, stage, attempt, rec)
		if e2 != nil {
			err = e2
			if attempt >= stageMax {
				return nil, p, status, raw, err, attempt
			}
			continue
		}
		client, p = c2, p2
		status, raw, err = fn(client, p)
		ok := err == nil && status >= 200 && status < 300
		rec(stage, status, ok, string(raw), err, 0)
		if ok {
			return client, p, status, raw, nil, attempt
		}
		if !isStageSIDRetryableBody(err, status, string(raw)) || attempt >= stageMax {
			return client, p, status, raw, err, attempt
		}
		rec(fmt.Sprintf("pix.stage_sid_retry.%s_%d", stage, attempt+1), status, false, string(raw), err, 0)
	}
	return client, p, status, raw, err, stageMax
}

func newAlignedClient(proxy string, timeoutSec int) (tls_client.HttpClient, error) {
	profileName := strings.ToLower(strings.TrimSpace(os.Getenv("PIX_GO_TLS_PROFILE")))
	if profileName == "" {
		// UPI checkout currently matches curl_cffi Firefox more reliably than the
		// stale Chrome_133 profile available in tls-client v1.9.1.
		profileName = "firefox_135_cffi"
	}
	prof, ok := profileByName(profileName)
	if !ok {
		prof = firefox135CFFI()
	}
	opts := []tls_client.HttpClientOption{
		tls_client.WithTimeoutSeconds(timeoutSec),
		tls_client.WithClientProfile(prof),
		tls_client.WithNotFollowRedirects(),
	}
	if proxy != "" {
		opts = append(opts, tls_client.WithProxyUrl(normalizeProxyURL(proxy)))
	}
	return tls_client.NewHttpClient(tls_client.NewNoopLogger(), opts...)
}

func probeOne(profileName, url, proxy string, timeoutSec int) (map[string]any, map[string]any, error) {
	if profileName == "stdlib" {
		return probeStdlib(url, timeoutSec)
	}
	prof, ok := profileByName(profileName)
	if !ok {
		return nil, nil, fmt.Errorf("unknown profile %q", profileName)
	}
	opts := []tls_client.HttpClientOption{
		tls_client.WithTimeoutSeconds(timeoutSec),
		tls_client.WithClientProfile(prof),
		tls_client.WithNotFollowRedirects(),
	}
	if proxy != "" {
		opts = append(opts, tls_client.WithProxyUrl(normalizeProxyURL(proxy)))
	}
	client, err := tls_client.NewHttpClient(tls_client.NewNoopLogger(), opts...)
	if err != nil {
		return nil, nil, err
	}
	req, err := fhttp.NewRequest(fhttp.MethodGet, url, nil)
	if err != nil {
		return nil, nil, err
	}
	req.Header = curlCFFIFirefox135Headers(map[string]string{
		"accept": "*/*", "accept-language": "pt-BR,pt;q=0.9,en;q=0.8", "user-agent": defaultUA,
		"sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
	})
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, nil, err
	}
	return body, summarize(body), nil
}

func curlCFFIFirefox135Headers(overrides map[string]string) fhttp.Header {
	baseOrder := []string{
		"user-agent", "accept", "accept-language", "accept-encoding",
		"upgrade-insecure-requests", "sec-fetch-dest", "sec-fetch-mode",
		"sec-fetch-site", "sec-fetch-user", "priority", "te",
	}
	h := fhttp.Header{
		"user-agent":                {defaultUA},
		"accept":                    {"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
		"accept-language":           {"en-US,en;q=0.5"},
		"accept-encoding":           {"gzip, deflate, br, zstd"},
		"upgrade-insecure-requests": {"1"},
		"sec-fetch-dest":            {"document"},
		"sec-fetch-mode":            {"navigate"},
		"sec-fetch-site":            {"none"},
		"sec-fetch-user":            {"?1"},
		"priority":                  {"u=0, i"},
		"te":                        {"trailers"},
	}
	extra := make([]string, 0)
	for k, v := range overrides {
		lk := strings.ToLower(k)
		h[lk] = []string{v}
		found := false
		for _, b := range baseOrder {
			if b == lk {
				found = true
				break
			}
		}
		if !found {
			extra = append(extra, lk)
		}
	}
	order := append(append([]string{}, baseOrder...), extra...)
	h[fhttp.HeaderOrderKey] = order
	h[fhttp.PHeaderOrderKey] = []string{":method", ":path", ":authority", ":scheme"}
	return h
}

func probeStdlib(url string, timeoutSec int) (map[string]any, map[string]any, error) {
	client := &http.Client{Timeout: time.Duration(timeoutSec) * time.Second}
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("User-Agent", defaultUA)
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, nil, err
	}
	var body map[string]any
	_ = json.Unmarshal(raw, &body)
	return body, summarize(body), nil
}

func summarize(body map[string]any) map[string]any {
	tlsObj, _ := body["tls"].(map[string]any)
	h2, _ := body["http2"].(map[string]any)
	if tlsObj == nil {
		tlsObj = map[string]any{}
	}
	if h2 == nil {
		h2 = map[string]any{}
	}
	return map[string]any{
		"user_agent": body["user_agent"], "http_version": body["http_version"],
		"ja3": tlsObj["ja3"], "ja3_hash": tlsObj["ja3_hash"], "ja4": tlsObj["ja4"],
		"peetprint_hash":     tlsObj["peetprint_hash"],
		"akamai_fingerprint": h2["akamai_fingerprint"], "akamai_fingerprint_hash": h2["akamai_fingerprint_hash"],
	}
}

// --- full PIX extract ---

type smokeStep struct {
	Stage       string `json:"stage"`
	Status      int    `json:"status"`
	OK          bool   `json:"ok"`
	Error       string `json:"error,omitempty"`
	BodyPreview string `json:"bodyPreview,omitempty"`
	ElapsedMs   int64  `json:"elapsedMs"`
}

type stripeIDs struct {
	guid, muid, sid, clientSessionID, elementsSessionID                        string
	configID, checkoutConfigID, elementsConfigID, initChecksum, runtimeVersion string
}

func upiOuterAttemptMax() int {
	if resolveChannel().code != "upi" {
		return 1
	}
	return envIntClamped([]string{"UPI_MAX_RETRY", "UPI_ATTEMPT_MAX"}, 5, 1, 10)
}

func isTerminalGoResult(result map[string]any) bool {
	if result == nil {
		return false
	}
	terminal, _ := result["approveTerminal"].(bool)
	if terminal {
		return true
	}
	text := resultComparableText(result)
	if isTokenTerminalText(text) || isCheckoutNotActiveText(text) {
		return true
	}
	for _, marker := range []string{
		"checkout_not_upi_trial", "final_not_upi_trial",
		"checkout_not_pix_trial", "final_not_pix_trial",
		"no trial", "not trial", "payment method unavailable",
	} {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

func isEffectiveGoAttempt(result map[string]any) bool {
	if result == nil {
		return false
	}
	if ok, _ := result["ok"].(bool); ok {
		return true
	}
	return hasGenericDeclineResult(result)
}

func hasGenericDeclineResult(result map[string]any) bool {
	return strings.Contains(resultComparableText(result), "generic_decline")
}

func resultComparableText(result map[string]any) string {
	if result == nil {
		return ""
	}
	parts := make([]string, 0, len(result))
	for _, key := range []string{"error", "message", "failureCode", "terminalFailureCode", "declineCode", "checkoutDeclineCode", "paymentErrorCode", "lastSetupErrorCode", "lastSetupErrorMessage", "pollSubmissionState", "setupIntentStatus"} {
		if value, ok := result[key]; ok && value != nil {
			parts = append(parts, fmt.Sprint(value))
		}
	}
	return strings.ToLower(strings.Join(parts, " "))
}

func cloneGoOuterAttempts(attempts []map[string]any) []map[string]any {
	out := make([]map[string]any, 0, len(attempts))
	for _, attempt := range attempts {
		if attempt == nil {
			out = append(out, nil)
			continue
		}
		copyAttempt := make(map[string]any, len(attempt))
		for key, value := range attempt {
			if key == "goOuterAttempts" {
				continue
			}
			copyAttempt[key] = value
		}
		out = append(out, copyAttempt)
	}
	return out
}

func runPixExtract(token, proxy string, timeoutSec int, full, quiet, slotMode bool) int {
	if resolveChannel().code != "upi" {
		return runPixExtractAttempt(token, proxy, timeoutSec, full, quiet, slotMode)
	}
	emitSlotJSON = slotMode
	if strings.TrimSpace(token) == "" && strings.TrimSpace(os.Getenv("PIX_SMOKE_TOKEN")) == "" {
		writeResult(map[string]any{"ok": false, "error": "need -token or PIX_SMOKE_TOKEN"})
		return 2
	}
	maxAttempts := upiOuterAttemptMax()
	effectiveAttempts := 0
	all := make([]map[string]any, 0, maxAttempts)
	lastCode := 1
	var last map[string]any
	outerLoopMax := envIntClamped([]string{"UPI_OUTER_LOOP_MAX"}, maxAttempts*5, maxAttempts, maxAttempts*10)
	for attempt := 1; effectiveAttempts < maxAttempts && attempt <= outerLoopMax; attempt++ {
		captureSlotResult = true
		capturedSlotResult = nil
		code := runPixExtractAttempt(token, proxy, timeoutSec, full, quiet, slotMode)
		captureSlotResult = false
		lastCode = code
		last = capturedSlotResult
		if last == nil {
			last = map[string]any{"ok": false, "error": "go slot produced no result"}
		}
		last["goOuterAttempt"] = attempt
		last["goOuterAttemptMax"] = maxAttempts
		last["effectiveAttempt"] = isEffectiveGoAttempt(last)
		if isEffectiveGoAttempt(last) {
			effectiveAttempts++
		}
		last["effectiveAttemptCount"] = effectiveAttempts
		all = append(all, last)
		if ok, _ := last["ok"].(bool); ok {
			last["goOuterAttempts"] = cloneGoOuterAttempts(all)
			writeResult(last)
			return 0
		}
		if isTerminalGoResult(last) {
			last["goOuterAttempts"] = cloneGoOuterAttempts(all)
			writeResult(last)
			return lastCode
		}
	}
	if last != nil && hasGenericDeclineResult(last) && effectiveAttempts >= maxAttempts {
		last["terminalFailureCode"] = "upi_generic_decline_exhausted"
		last["error"] = fmt.Sprintf("upi_generic_decline_terminal_attempt=%d", maxAttempts)
	}
	if last == nil {
		last = map[string]any{"ok": false, "error": "go outer attempts exhausted"}
	}
	last["goOuterAttempts"] = cloneGoOuterAttempts(all)
	last["goOuterAttemptMax"] = maxAttempts
	last["effectiveAttemptCount"] = effectiveAttempts
	writeResult(last)
	return lastCode
}

func runPixExtractAttempt(token, proxy string, timeoutSec int, full, quiet, slotMode bool) int {
	emitSlotJSON = slotMode
	token, sessionToken := normalizeAccessAndSessionToken(token)
	if token == "" {
		token = strings.TrimSpace(os.Getenv("PIX_SMOKE_TOKEN"))
	}
	if proxy == "" {
		proxy = strings.TrimSpace(os.Getenv("PIX_SMOKE_PROXY"))
		if proxy == "" {
			proxy = strings.TrimSpace(os.Getenv("UPI_DEFAULT_PROXY"))
		}
	}
	if token == "" {
		if slotMode {
			writeResult(map[string]any{"ok": false, "error": "need -token or PIX_SMOKE_TOKEN"})
		} else {
			fmt.Fprintln(os.Stderr, "need -token or PIX_SMOKE_TOKEN")
		}
		return 2
	}
	var client tls_client.HttpClient
	var err error
	client, err = newAlignedClient(proxy, timeoutSec)
	if err != nil {
		if slotMode {
			writeResult(map[string]any{"ok": false, "error": "client: " + err.Error()})
		} else {
			fmt.Fprintf(os.Stderr, "client: %v\n", err)
		}
		return 1
	}

	ch := resolveChannel()
	country := ch.country
	currency := ch.currency
	pmType := ch.paymentMethod
	trialLabel := "pix"
	qrLabel := "pix"
	if ch.code == "upi" {
		trialLabel = "upi"
		qrLabel = "upi"
	}
	promoCountry := ch.promotionCountry
	baseProxy := proxy
	providerProxy := proxyForRegion(baseProxy, country)
	promotionProxy := proxyForRegion(baseProxy, promoCountry)
	if ch.code == "upi" {
		var routeErr error
		providerProxy, routeErr = proxyForRegionStrict(baseProxy, country)
		if routeErr != nil {
			result := map[string]any{"ok": false, "error": "upi proxy route failed: " + routeErr.Error(), "errorStage": "proxy.route", "proxySet": baseProxy != ""}
			writeResult(result)
			return 1
		}
		promotionProxy, routeErr = proxyForRegionStrict(baseProxy, promoCountry)
		if routeErr != nil {
			result := map[string]any{"ok": false, "error": "upi promotion proxy route failed: " + routeErr.Error(), "errorStage": "proxy.route", "proxySet": baseProxy != ""}
			writeResult(result)
			return 1
		}
	}
	proxy = providerProxy
	deviceID, sessionID, cookie := newDeviceIDs(sessionToken)
	ids := stripeIDs{
		guid: stripeBrowserID(), muid: stripeBrowserID(), sid: stripeBrowserID(),
		clientSessionID: newUUID(), elementsSessionID: "elements_session_" + randHex(11),
		runtimeVersion: runtimeVer,
	}
	// Prefer Python-owned pool claim (PIX_BILLING_JSON). Slot mode never invents addresses.
	addr, cpf, name, billingEmail, billingFromPython := loadBillingFromPythonEnv()
	line1, line2, city, state, postalCode := addr.line1, addr.line2, addr.city, addr.state, addr.postalCode
	if !billingFromPython {
		if slotMode {
			writeResult(map[string]any{
				"ok":    false,
				"error": "slot mode requires PIX_BILLING_JSON from Python address pool",
			})
			return 2
		}
		// Standalone smoke only.
		addr = randomBillingAddress()
		line1, line2, city, state, postalCode = addr.line1, addr.line2, addr.city, addr.state, addr.postalCode
		billingEmail = randomBillingEmail()
		if ch.requireTaxID {
			cpf = formatCPF(generateCPF())
		} else {
			cpf = ""
		}
		name = randomBillingName()
		if n := jwtProfileName(token); n != "" && ((ch.code == "pix" && looksLikeBRPersonName(n)) || (ch.code == "upi" && n != "")) {
			name = n
		}
	} else if strings.TrimSpace(name) == "" {
		name = randomBillingName()
		if n := jwtProfileName(token); n != "" && ((ch.code == "pix" && looksLikeBRPersonName(n)) || (ch.code == "upi" && n != "")) {
			name = n
		}
	}

	protocolModeEarly := resolveProtocolMode()
	if ch.code == "upi" {
		// UPI ships Elements/B only (matches Python local_upi_b_api).
		protocolModeEarly = "b"
	}
	steps := make([]smokeStep, 0, 16)
	startedMs := time.Now().UnixMilli()
	result := map[string]any{
		"ok": false, "proxySet": proxy != "", "profile": "firefox_135_cffi",
		"fullExtract": full, "startedAt": time.Now().UTC().Format(time.RFC3339),
		"billingName": name, "billingEmailSet": billingEmail != "", "cpfSet": cpf != "",
		"billingCity": city, "billingState": state, "billingPostal": postalCode,
		"billingLine1": line1, "cpfLast4": cpfLast4(cpf),
		"sidPrefix": truncate(ids.sid, 12), "muidPrefix": truncate(ids.muid, 12),
		"guidPrefix": truncate(ids.guid, 12), "deviceIdPrefix": truncate(deviceID, 12),
		"paymentMethod": pmType,
		"protocolMode":  protocolModeEarly,
		"country":       country, "billing_country": country,
		"billing_name": name,
		"billing_address": map[string]string{
			"line1": line1, "line2": line2, "city": city, "state": state,
			"postal_code": postalCode, "country": country,
		},
		"provider_proxy": providerProxy, "checkout_proxy": providerProxy, "promotion_proxy": promotionProxy,
		"success_provider_proxy": providerProxy, "attempt_seed_proxy": baseProxy,
		"routeChain":      ch.routeLabel,
		"checkoutCountry": country, "promotionCountry": promoCountry, "providerCountry": country,
		"billingFromPython": billingFromPython,
	}
	rec := func(stage string, status int, ok bool, body string, err error, ms int64) {
		preview := redactPreview(body)
		e := ""
		if err != nil {
			e = err.Error()
		}
		steps = append(steps, smokeStep{Stage: stage, Status: status, OK: ok, Error: e, BodyPreview: preview, ElapsedMs: ms})
		if !quiet {
			fmt.Printf("[go] %s status=%d ok=%v err=%v body=%s\n", stage, status, ok, e, truncate(preview, 140))
		}
	}
	_ = startedMs

	// 1 checkout (stage SID retries, max PIX_STAGE_SID_MAX_ATTEMPTS default 5)
	// UPI: align upi_extract create with default PP_PROMO_MODE=campaign.
	coMap := map[string]any{
		"entry_point": "all_plans_pricing_modal", "plan_name": "chatgptplusplan",
		"billing_details":  map[string]string{"country": country, "currency": currency},
		"checkout_ui_mode": "custom",
	}
	if ch.code == "upi" {
		coMap["promo_campaign"] = map[string]any{
			"promo_campaign_id":          "plus-1-month-free",
			"is_coupon_from_query_param": false,
		}
	}
	coBody, _ := json.Marshal(coMap)
	var status int
	var raw []byte
	t0 := time.Now()
	stageMax := stageSIDMaxAttempts()
	checkoutAttempts := 0
	for attempt := 1; attempt <= stageMax; attempt++ {
		checkoutAttempts = attempt
		// Every SID attempt: residential + BR for checkout
		{
			c2, p2, ip2, country2, res2, e2 := rebuildClientWithExitPolicy(proxy, timeoutSec, true, country, "checkout", attempt, rec)
			if e2 != nil {
				err = e2
				rec(fmt.Sprintf("chatgpt.checkout_sid_retry_%d", attempt), 0, false, "", e2, 0)
				if attempt >= stageMax {
					result["steps"] = steps
					result["error"] = "checkout exit policy failed: " + e2.Error()
					result["errorStage"] = "chatgpt.checkout"
					result["checkoutSidAttempts"] = attempt
					writeResult(result)
					return 1
				}
				continue
			}
			client, proxy = c2, p2
			providerProxy = p2
			result["provider_proxy"] = providerProxy
			result["checkout_proxy"] = providerProxy
			result["attempt_seed_proxy"] = providerProxy
			result["checkoutExitIp"] = ip2
			result["checkoutExitCountry"] = country2
			result["checkoutResidential"] = res2
			_ = res2
		}
		coHeaders := curlCFFIFirefox135Headers(map[string]string{
			"authorization": "Bearer " + token, "content-type": "application/json", "accept": "*/*",
			"origin": "https://chatgpt.com", "referer": "https://chatgpt.com/", "oai-language": ch.oaiLanguage,
			"accept-language": ch.acceptLanguage, "user-agent": defaultUA,
			"oai-device-id": deviceID, "oai-session-id": sessionID, "oai-client-version": oaiVer,
			"oai-client-build-number": oaiBuild,
			"sec-fetch-dest":          "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
			"cookie":                cookie,
			"x-openai-target-path":  "/backend-api/payments/checkout",
			"x-openai-target-route": "/backend-api/payments/checkout",
		})
		t0 = time.Now()
		status, raw, err = doReq(client, fhttp.MethodPost, "https://chatgpt.com/backend-api/payments/checkout", coHeaders, coBody)
		rec("chatgpt.checkout", status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
		if err == nil && status >= 200 && status < 300 {
			break
		}
		if !isStageSIDRetryableBody(err, status, string(raw)) || attempt >= stageMax {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("checkout failed", status, string(raw), err)
			result["errorStage"] = "chatgpt.checkout"
			result["checkoutSidAttempts"] = attempt
			writeResult(result)
			return 1
		}
		rec(fmt.Sprintf("pix.stage_sid_retry.checkout_%d", attempt+1), status, false, string(raw), err, 0)
	}
	result["checkoutSidAttempts"] = checkoutAttempts
	var checkout map[string]any
	_ = json.Unmarshal(raw, &checkout)
	csID := firstString(checkout, "checkout_session_id", "id")
	pk := firstString(checkout, "publishable_key", "publishableKey")
	processor := firstString(checkout, "processor_entity")
	if em := firstString(checkout, "checkout_email", "email", "customer_email"); em != "" && strings.Contains(em, "@") {
		billingEmail = em
	}
	result["checkoutSessionId"] = csID
	result["processorEntity"] = processor
	result["publishableKeySet"] = pk != ""
	if csID == "" || pk == "" {
		result["steps"] = steps
		result["error"] = fmt.Sprintf("missing cs/pk body=%s", openaiErrorDetail(string(raw)))
		result["errorStage"] = "chatgpt.checkout"
		writeResult(result)
		return 1
	}

	// 2 hosted page warmup: upi_extract_latest.py does not do this before Stripe init.
	if ch.code != "upi" {
		pageH := curlCFFIFirefox135Headers(map[string]string{
			"accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
			"accept-language": ch.acceptLanguage, "user-agent": defaultUA,
			"referer": "https://chatgpt.com/", "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
			"sec-fetch-site": "cross-site", "upgrade-insecure-requests": "1",
		})
		t0 = time.Now()
		status, _, err = doReq(client, fhttp.MethodGet, "https://checkout.stripe.com/c/pay/"+csID, pageH, nil)
		rec("stripe.checkout_page", status, status >= 200 && status < 400, "", err, time.Since(t0).Milliseconds())
	}
	pageH := curlCFFIFirefox135Headers(map[string]string{
		"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "accept-language": ch.acceptLanguage, "user-agent": defaultUA,
	})
	stripeH := stripeAPIHeaders(csID, protocolModeEarly)

	// 3 init
	initForm := url.Values{"key": {pk}, "eid": {"NA"}, "browser_locale": {ch.browserLocale}, "browser_timezone": {ch.timezone}, "redirect_type": {"url"}}
	t0 = time.Now()
	if protocolModeEarly == "b" {
		initForm.Set("_stripe_version", stripeVerB)
		initForm.Set("elements_session_client[client_betas][0]", "custom_checkout_server_updates_1")
		initForm.Set("elements_session_client[client_betas][1]", "custom_checkout_manual_approval_1")
		initForm.Set("elements_session_client[elements_init_source]", "custom_checkout")
		initForm.Set("elements_session_client[referrer_host]", "chatgpt.com")
		initForm.Set("elements_session_client[stripe_js_id]", ids.clientSessionID)
		initForm.Set("elements_session_client[locale]", ch.browserLocale)
		initForm.Set("elements_session_client[is_aggregation_expected]", "false")
		initForm.Set("elements_options_client[saved_payment_method][enable_save]", "auto")
		initForm.Set("elements_options_client[saved_payment_method][enable_redisplay]", "auto")
		initForm.Del("eid")
		initForm.Del("redirect_type")
	}
	status, raw, err = doReq(client, fhttp.MethodPost, "https://api.stripe.com/v1/payment_pages/"+csID+"/init", stripeH, []byte(initForm.Encode()))
	rec("stripe.init", status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
	if err != nil || status >= 300 {
		result["steps"] = steps
		result["httpStatus"] = status
		result["error"] = stageHTTPError("init failed", status, string(raw), err)
		result["errorStage"] = "stripe.init"
		writeResult(result)
		return 1
	}
	var init1 map[string]any
	_ = json.Unmarshal(raw, &init1)
	updateStripeIDs(&ids, init1)
	cur, amt, methods := inspectInit(init1)
	result["initCurrency"] = cur
	result["initAmount"] = amt
	result["initMethods"] = methods

	// 4 promo / checkout_update (stage SID retries)
	promoBody, _ := json.Marshal(map[string]any{
		"checkout_session_id": csID, "processor_entity": processor,
		"plan_name": "chatgptplusplan", "price_interval": "month", "seat_quantity": 1,
		"promo_campaign": map[string]any{"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": false},
	})
	promoAttempts := 0
	for attempt := 1; attempt <= stageMax; attempt++ {
		promoAttempts = attempt
		// Promotion/update: promo country required (VN for UPI), residential NOT required
		{
			c2, p2, ip2, country2, res2, e2 := rebuildClientWithExitPolicy(promotionProxy, timeoutSec, false, promoCountry, "checkout_update", attempt, rec)
			if e2 != nil {
				err = e2
				rec(fmt.Sprintf("chatgpt.checkout_update_sid_retry_%d", attempt), 0, false, "", e2, 0)
				if attempt >= stageMax {
					result["steps"] = steps
					result["error"] = "checkout_update exit policy failed: " + e2.Error()
					result["errorStage"] = "chatgpt.checkout_update"
					result["promoSidAttempts"] = attempt
					writeResult(result)
					return 1
				}
				continue
			}
			client = c2
			promotionProxy = p2
			result["provider_proxy"] = providerProxy
			result["checkout_proxy"] = providerProxy
			result["promotion_proxy"] = promotionProxy
			result["promoExitIp"] = ip2
			result["promoExitCountry"] = country2
			result["promoResidential"] = res2
			_ = res2
		}
		promoH := curlCFFIFirefox135Headers(map[string]string{
			"authorization": "Bearer " + token, "content-type": "application/json", "accept": "*/*",
			"origin": "https://chatgpt.com", "referer": fmt.Sprintf("https://chatgpt.com/checkout/%s/%s", processor, csID),
			"oai-language": ch.oaiLanguage, "accept-language": ch.acceptLanguage, "user-agent": defaultUA,
			"oai-device-id": deviceID, "oai-session-id": sessionID, "oai-client-version": oaiVer,
			"oai-client-build-number": oaiBuild,
			"sec-fetch-dest":          "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
			"cookie":                cookie,
			"x-openai-target-path":  "/backend-api/payments/checkout/update",
			"x-openai-target-route": "/backend-api/payments/checkout/update",
		})
		t0 = time.Now()
		status, raw, err = doReq(client, fhttp.MethodPost, "https://chatgpt.com/backend-api/payments/checkout/update", promoH, promoBody)
		updateOK := err == nil && status >= 200 && status < 300 && !jsonSuccessFalse(raw)
		rec("chatgpt.checkout_update", status, updateOK, string(raw), err, time.Since(t0).Milliseconds())
		if updateOK {
			break
		}
		if !isStageSIDRetryableBody(err, status, string(raw)) || attempt >= stageMax {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("checkout_update failed", status, string(raw), err)
			if err == nil && status >= 200 && status < 300 && jsonSuccessFalse(raw) {
				result["error"] = "checkout_update failed: success=false"
			}
			result["errorStage"] = "chatgpt.checkout_update"
			result["promoSidAttempts"] = attempt
			writeResult(result)
			return 1
		}
		rec(fmt.Sprintf("pix.stage_sid_retry.checkout_update_%d", attempt+1), status, false, string(raw), err, 0)
	}
	result["promoSidAttempts"] = promoAttempts
	// After promotion: provider path must be residential + provider country again. Promotion VN never bleeds into provider stages.
	{
		c2, p2, ip2, country2, res2, e2 := rebuildClientWithExitPolicy(providerProxy, timeoutSec, true, country, "provider_post_promo", 1, rec)
		if e2 != nil {
			result["steps"] = steps
			result["error"] = "provider_post_promo exit policy failed: " + e2.Error()
			result["errorStage"] = "provider_post_promo"
			writeResult(result)
			return 1
		}
		client, proxy = c2, p2
		providerProxy = p2
		result["provider_proxy"] = providerProxy
		result["checkout_proxy"] = providerProxy
		result["attempt_seed_proxy"] = providerProxy
		result["providerExitIp"] = ip2
		result["providerExitCountry"] = country2
		result["providerResidential"] = res2
		_ = country2
		_ = res2
	}

	// 5 init2 (stage SID + residential BR)
	if ch.code == "upi" {
		// Python creates a fresh Stripe JS id for the post-promo init and reuses it through Elements/tax/confirm.
		ids.clientSessionID = newUUID()
	}
	if protocolModeEarly == "b" {
		initForm.Set("_stripe_version", stripeVerB)
		initForm.Set("elements_session_client[client_betas][0]", "custom_checkout_server_updates_1")
		initForm.Set("elements_session_client[client_betas][1]", "custom_checkout_manual_approval_1")
		initForm.Set("elements_session_client[elements_init_source]", "custom_checkout")
		initForm.Set("elements_session_client[referrer_host]", "chatgpt.com")
		initForm.Set("elements_session_client[stripe_js_id]", ids.clientSessionID)
		initForm.Set("elements_session_client[locale]", ch.browserLocale)
		initForm.Set("elements_session_client[is_aggregation_expected]", "false")
		initForm.Set("elements_options_client[saved_payment_method][enable_save]", "auto")
		initForm.Set("elements_options_client[saved_payment_method][enable_redisplay]", "auto")
		initForm.Del("eid")
		initForm.Del("redirect_type")
	}
	client, proxy, status, raw, err, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, "stripe.init_post_promo", rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
		sh := stripeAPIHeaders(csID, protocolModeEarly)
		stripeH = sh
		return doReq(c, fhttp.MethodPost, "https://api.stripe.com/v1/payment_pages/"+csID+"/init", sh, []byte(initForm.Encode()))
	})
	providerProxy = proxy
	result["provider_proxy"] = providerProxy
	if err != nil || status >= 300 {
		result["steps"] = steps
		result["httpStatus"] = status
		result["error"] = stageHTTPError("init_post_promo failed", status, string(raw), err)
		result["errorStage"] = "stripe.init_post_promo"
		writeResult(result)
		return 1
	}
	var init2 map[string]any
	_ = json.Unmarshal(raw, &init2)
	updateStripeIDs(&ids, init2)
	// init3 is the last full init payload used by route-A confirmReturnURL.
	// UPI reuses post-promo init2; PIX overwrites after init_final.
	init3 := init2
	cur, amt, methods = inspectInit(init2)
	result["postPromoCurrency"] = cur
	result["postPromoAmount"] = amt
	result["postPromoMethods"] = methods
	if !full {
		result["ok"] = true
		result["note"] = "partial smoke only"
		result["steps"] = steps
		writeResult(result)
		return 0
	}

	// hard gate: need 0 + pix
	if fmt.Sprint(amt) != "0" || !containsStr(methods, pmType) {
		result["error"] = fmt.Sprintf("checkout_not_%s_trial currency=%s amount=%v methods=%v", trialLabel, cur, amt, methods)
		result["steps"] = steps
		writeResult(result)
		fmt.Fprintln(os.Stderr, result["error"])
		return 1
	}

	// 6 taxes / elements / tax_region
	// UPI (upi_extract.py): NO chatgpt.checkout_taxes call in live path.
	// Order: post-promo init → elements/sessions → tax_region×4 → snapshot → confirm.
	// PIX keeps: chatgpt taxes → tax_region → init_final → confirm.
	runTaxRegion := func(stageBody url.Values, stageName string) error {
		if protocolModeEarly == "b" {
			// init uses browser locale; tax_region/confirm elements client uses elements locale (en for UPI).
			stageBody.Set("_stripe_version", stripeVerB)
			stageBody.Set("elements_session_client[client_betas][0]", "custom_checkout_server_updates_1")
			stageBody.Set("elements_session_client[client_betas][1]", "custom_checkout_manual_approval_1")
			stageBody.Set("elements_session_client[elements_init_source]", "custom_checkout")
			stageBody.Set("elements_session_client[referrer_host]", "chatgpt.com")
			stageBody.Set("elements_session_client[session_id]", ids.elementsSessionID)
			stageBody.Set("elements_session_client[stripe_js_id]", ids.clientSessionID)
			stageBody.Set("elements_session_client[locale]", ch.elementsLocale)
			stageBody.Set("elements_session_client[is_aggregation_expected]", "false")
			stageBody.Set("elements_options_client[saved_payment_method][enable_save]", "auto")
			stageBody.Set("elements_options_client[saved_payment_method][enable_redisplay]", "auto")
			stageBody.Set("client_attribution_metadata[merchant_integration_additional_elements][0]", "expressCheckout")
			stageBody.Set("client_attribution_metadata[merchant_integration_additional_elements][1]", "payment")
			stageBody.Set("client_attribution_metadata[merchant_integration_additional_elements][2]", "address")
		} else {
			stageBody.Set("eid", "NA")
		}
		var e error
		client, proxy, status, raw, e, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, stageName, rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
			sh := stripeAPIHeaders(csID, protocolModeEarly)
			return doReq(c, fhttp.MethodPost, "https://api.stripe.com/v1/payment_pages/"+csID, sh, []byte(stageBody.Encode()))
		})
		providerProxy = proxy
		result["provider_proxy"] = providerProxy
		if e != nil || status >= 300 {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("tax_region failed", status, string(raw), e)
			result["errorStage"] = stageName
			writeResult(result)
			return fmt.Errorf("tax_region")
		}
		// upi_extract: tax_region responses refresh latest checkout_config_id for confirm.
		var taxResp map[string]any
		if json.Unmarshal(raw, &taxResp) == nil {
			if cfg := firstString(taxResp, "config_id"); cfg != "" {
				ids.configID = cfg
			}
		}
		return nil
	}

	if ch.code == "upi" {
		// UPI: elements session immediately after post-promo init (init2), before tax_region.
		// Use init2 as the amount/methods source (no extra init_final).
		if protocolModeEarly == "b" || resolveProtocolMode() == "b" {
			esParams := url.Values{}
			esParams.Set("client_betas[0]", "custom_checkout_server_updates_1")
			esParams.Set("client_betas[1]", "custom_checkout_manual_approval_1")
			esParams.Set("deferred_intent[mode]", "subscription")
			esParams.Set("deferred_intent[amount]", fmt.Sprint(amt))
			esParams.Set("deferred_intent[currency]", strings.ToLower(currency))
			esParams.Set("deferred_intent[setup_future_usage]", "off_session")
			esParams.Set("currency", strings.ToLower(currency))
			esParams.Set("key", pk)
			esParams.Set("_stripe_version", stripeVerB)
			esParams.Set("elements_init_source", "custom_checkout")
			esParams.Set("referrer_host", "chatgpt.com")
			esParams.Set("stripe_js_id", ids.clientSessionID)
			esParams.Set("locale", ch.elementsLocale) // en (payment_elements_locale)
			esParams.Set("type", "deferred_intent")
			esParams.Set("checkout_session_id", csID)
			pmTypes := methods
			if len(pmTypes) == 0 {
				pmTypes = []string{"card", "upi"}
			}
			for i, m := range pmTypes {
				esParams.Set(fmt.Sprintf("deferred_intent[payment_method_types][%d]", i), m)
			}
			if eo, ok := init3["elements_options"].(map[string]any); ok {
				if pmc := firstString(eo, "payment_method_configuration"); pmc != "" {
					esParams.Set("deferred_intent[payment_method_configuration][id]", pmc)
				}
				if mode := firstString(eo, "mode"); mode != "" {
					esParams.Set("deferred_intent[mode]", mode)
				}
				if a := eo["amount"]; a != nil {
					esParams.Set("deferred_intent[amount]", fmt.Sprint(a))
				}
				if c := firstString(eo, "currency"); c != "" {
					esParams.Set("deferred_intent[currency]", strings.ToLower(c))
					esParams.Set("currency", strings.ToLower(c))
				}
			}
			t0 = time.Now()
			client, proxy, status, raw, err, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, "stripe.elements_session", rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
				sh := stripeAPIHeaders(csID, "b")
				return doReq(c, fhttp.MethodGet, "https://api.stripe.com/v1/elements/sessions?"+esParams.Encode(), sh, nil)
			})
			providerProxy = proxy
			result["provider_proxy"] = providerProxy
			rec("stripe.elements_session", status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
			if err != nil || status >= 300 {
				result["steps"] = steps
				result["httpStatus"] = status
				result["error"] = stageHTTPError("elements session failed", status, string(raw), err)
				result["errorStage"] = "stripe.elements_session"
				writeResult(result)
				return 1
			}
			var esData map[string]any
			_ = json.Unmarshal(raw, &esData)
			if sid := firstString(esData, "session_id", "id"); sid != "" {
				ids.elementsSessionID = sid
			}
			if cfg := firstString(esData, "config_id"); cfg != "" {
				ids.elementsConfigID = cfg
			}
			result["elementsSessionId"] = ids.elementsSessionID
			result["elementsConfigId"] = ids.elementsConfigID
			if ids.elementsSessionID == "" {
				result["steps"] = steps
				result["error"] = "elements session missing session_id"
				result["errorStage"] = "stripe.elements_session"
				writeResult(result)
				return 1
			}
			if ids.elementsConfigID == "" {
				ids.elementsConfigID = ids.configID
			}
		}

		// HAR order: country, country, country+line1, full address
		stepsBodies := []url.Values{
			{"key": {pk}, "tax_region[country]": {country}},
			{"key": {pk}, "tax_region[country]": {country}},
			{"key": {pk}, "tax_region[country]": {country}, "tax_region[line1]": {line1}},
			{"key": {pk}, "tax_region[country]": {country}, "tax_region[line1]": {line1}, "tax_region[city]": {city}, "tax_region[postal_code]": {postalCode}, "tax_region[state]": {state}},
		}
		if strings.TrimSpace(line2) != "" {
			stepsBodies[3].Set("tax_region[line2]", line2)
		}
		for idx, sb := range stepsBodies {
			if err := runTaxRegion(sb, fmt.Sprintf("stripe.tax_region.%d", idx+1)); err != nil {
				return 1
			}
		}
		// Keep final amount/methods from post-promo init.
		result["finalCurrency"] = cur
		result["finalAmount"] = amt
		result["finalMethods"] = methods
	} else {
		// PIX: chatgpt taxes then tax_region then init_final
		taxBody, _ := json.Marshal(map[string]any{
			"checkout_session_id": csID, "checkout_email": billingEmail,
			"billing_country": country, "billing_name": name, "currency": currency,
			"tax_id": nil, "processor_entity": processor,
			"billing_address": map[string]string{
				"line1": line1, "line2": line2, "city": city, "state": state,
				"postal_code": postalCode, "country": country,
			},
		})
		client, proxy, status, raw, err, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, "chatgpt.checkout_taxes", rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
			h := curlCFFIFirefox135Headers(map[string]string{
				"authorization": "Bearer " + token, "content-type": "application/json", "accept": "*/*",
				"origin": "https://chatgpt.com", "referer": fmt.Sprintf("https://chatgpt.com/checkout/%s/%s", processor, csID),
				"oai-language": ch.oaiLanguage, "accept-language": ch.acceptLanguage, "user-agent": defaultUA,
				"oai-device-id": deviceID, "oai-session-id": sessionID, "oai-client-version": oaiVer,
				"oai-client-build-number": oaiBuild,
				"sec-fetch-dest":          "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
				"cookie":                cookie,
				"x-openai-target-path":  "/backend-api/payments/checkout/taxes",
				"x-openai-target-route": "/backend-api/payments/checkout/taxes",
			})
			return doReq(c, fhttp.MethodPost, "https://chatgpt.com/backend-api/payments/checkout/taxes", h, taxBody)
		})
		result["provider_proxy"] = proxy
		if err != nil || status >= 300 {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("taxes failed", status, string(raw), err)
			result["errorStage"] = "chatgpt.checkout_taxes"
			writeResult(result)
			return 1
		}
		{
			c2, p2, ip2, country2, res2, e2 := rebuildClientWithExitPolicy(providerProxy, timeoutSec, true, country, "provider_post_taxes", 1, rec)
			if e2 != nil {
				result["steps"] = steps
				result["error"] = "provider_post_taxes exit policy failed: " + e2.Error()
				result["errorStage"] = "provider_post_taxes"
				writeResult(result)
				return 1
			}
			client, proxy = c2, p2
			providerProxy = p2
			result["provider_proxy"] = providerProxy
			result["providerExitIp"] = ip2
			result["providerExitCountry"] = country2
			result["providerResidential"] = res2
			_ = country2
			_ = res2
		}

		taxRegion := url.Values{
			"key":                 {pk},
			"tax_region[country]": {country}, "tax_region[state]": {state},
			"tax_region[postal_code]": {postalCode}, "tax_region[line1]": {line1},
			"tax_region[city]": {city}, "tax_region[line2]": {line2},
		}
		if err := runTaxRegion(taxRegion, "stripe.tax_region"); err != nil {
			return 1
		}

		// init3 final
		if protocolModeEarly == "b" {
			initForm.Set("_stripe_version", stripeVerB)
			initForm.Set("elements_session_client[client_betas][0]", "custom_checkout_server_updates_1")
			initForm.Set("elements_session_client[client_betas][1]", "custom_checkout_manual_approval_1")
			initForm.Set("elements_session_client[elements_init_source]", "custom_checkout")
			initForm.Set("elements_session_client[referrer_host]", "chatgpt.com")
			initForm.Set("elements_session_client[stripe_js_id]", ids.clientSessionID)
			initForm.Set("elements_session_client[locale]", ch.browserLocale)
			initForm.Set("elements_session_client[is_aggregation_expected]", "false")
			initForm.Set("elements_options_client[saved_payment_method][enable_save]", "auto")
			initForm.Set("elements_options_client[saved_payment_method][enable_redisplay]", "auto")
			initForm.Del("eid")
			initForm.Del("redirect_type")
		}
		client, proxy, status, raw, err, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, "stripe.init_final", rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
			sh := stripeAPIHeaders(csID, protocolModeEarly)
			stripeH = sh
			return doReq(c, fhttp.MethodPost, "https://api.stripe.com/v1/payment_pages/"+csID+"/init", sh, []byte(initForm.Encode()))
		})
		providerProxy = proxy
		result["provider_proxy"] = providerProxy
		if err != nil || status >= 300 {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("init_final failed", status, string(raw), err)
			result["errorStage"] = "stripe.init_final"
			writeResult(result)
			return 1
		}
		rec("stripe.init_final", status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
		_ = json.Unmarshal(raw, &init3)
		updateStripeIDs(&ids, init3)
		cur, amt, methods = inspectInit(init3)
		result["finalCurrency"] = cur
		result["finalAmount"] = amt
		result["finalMethods"] = methods
		if fmt.Sprint(amt) != "0" || !containsStr(methods, pmType) {
			result["error"] = fmt.Sprintf("final_not_%s_trial currency=%s amount=%v methods=%v", trialLabel, cur, amt, methods)
			result["steps"] = steps
			writeResult(result)
			return 1
		}
	}

	// 9 payment_methods type=pix  (route A)  OR snapshot+confirm payment_method_data (route B)
	protocolMode := resolveProtocolMode()
	if ch.code == "upi" {
		// UPI is Elements/B only (matches Python local_upi_b_api + upi_extract).
		protocolMode = "b"
	}
	result["protocolMode"] = protocolMode
	var confirmData map[string]any
	var pmID string
	if protocolMode == "b" {
		// B: snapshot then confirm with payment_method_data (no independent payment_methods)
		snapH := curlCFFIFirefox135Headers(map[string]string{
			"authorization": "Bearer " + token, "content-type": "application/json", "accept": "*/*",
			"origin": "https://chatgpt.com", "referer": fmt.Sprintf("https://chatgpt.com/checkout/%s/%s", processor, csID),
			"oai-language": ch.oaiLanguage, "accept-language": ch.acceptLanguage, "user-agent": defaultUA,
			"oai-device-id": deviceID, "oai-session-id": sessionID, "oai-client-version": oaiVer,
			"oai-client-build-number": oaiBuild,
			"sec-fetch-dest":          "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
			"cookie":                cookie,
			"x-openai-target-path":  "/backend-api/payments/checkout/snapshot",
			"x-openai-target-route": "/backend-api/payments/checkout/snapshot",
		})
		addrMap := map[string]any{
			"line1": line1, "city": city, "country": country, "postal_code": postalCode, "state": state,
		}
		if strings.TrimSpace(line2) != "" {
			addrMap["line2"] = line2
		}
		snapBody, _ := json.Marshal(map[string]any{
			"snapshot": map[string]any{
				"billing_address": map[string]any{"name": name, "address": addrMap},
			},
		})
		t0 = time.Now()
		status, raw, err = doReq(client, fhttp.MethodPost, "https://chatgpt.com/backend-api/payments/checkout/snapshot", snapH, snapBody)
		// 204 is success
		snapOK := err == nil && (status == 204 || (status >= 200 && status < 300))
		rec("chatgpt.checkout_snapshot", status, snapOK, string(raw), err, time.Since(t0).Milliseconds())
		if !snapOK {
			if ch.code != "upi" || isCheckoutNotActiveText(string(raw)) {
				result["steps"] = steps
				result["httpStatus"] = status
				result["error"] = stageHTTPError("snapshot failed", status, string(raw), err)
				result["errorStage"] = "chatgpt.checkout_snapshot"
				writeResult(result)
				return 1
			}
			result["snapshotWarning"] = stageHTTPError("snapshot failed", status, string(raw), err)
		}
		verify := fmt.Sprintf("https://chatgpt.com/checkout/verify?stripe_session_id=%s&processor_entity=%s&plan_type=plus", csID, processor)
		returnURLB := fmt.Sprintf("https://checkout.stripe.com/c/pay/%s?returned_from_redirect=true&ui_mode=custom&return_url=%s", csID, url.QueryEscape(verify))
		amtStr := fmt.Sprint(amt)
		elementsConfigID := ids.elementsConfigID
		if elementsConfigID == "" {
			elementsConfigID = ids.configID
		}
		// Real browser ids (never NA). time_on_page aligned to upi_extract (5–7 min).
		timeOnPage := fmt.Sprintf("%d", 300000+mrand.IntN(120001))
		confirmFormB := url.Values{
			"guid": {ids.guid}, "muid": {ids.muid}, "sid": {ids.sid},
			"expected_amount": {amtStr}, "expected_payment_method_type": {pmType},
			"return_url": {returnURLB}, "version": {ids.runtimeVersion},
			"_stripe_version": {stripeVerB}, "key": {pk},
			"elements_session_client[session_id]":                                                           {ids.elementsSessionID},
			"elements_session_client[locale]":                                                               {ch.elementsLocale},
			"elements_session_client[referrer_host]":                                                        {"chatgpt.com"},
			"elements_session_client[is_aggregation_expected]":                                              {"false"},
			"elements_session_client[elements_init_source]":                                                 {"custom_checkout"},
			"elements_session_client[stripe_js_id]":                                                         {ids.clientSessionID},
			"elements_session_client[client_betas][0]":                                                      {"custom_checkout_server_updates_1"},
			"elements_session_client[client_betas][1]":                                                      {"custom_checkout_manual_approval_1"},
			"elements_options_client[saved_payment_method][enable_save]":                                    {"auto"},
			"elements_options_client[saved_payment_method][enable_redisplay]":                               {"auto"},
			"client_attribution_metadata[client_session_id]":                                                {ids.clientSessionID},
			"client_attribution_metadata[checkout_session_id]":                                              {csID},
			"client_attribution_metadata[checkout_config_id]":                                               {ids.configID},
			"client_attribution_metadata[elements_session_id]":                                              {ids.elementsSessionID},
			"client_attribution_metadata[elements_session_config_id]":                                       {elementsConfigID},
			"client_attribution_metadata[merchant_integration_source]":                                      {"checkout"},
			"client_attribution_metadata[merchant_integration_subtype]":                                     {"payment-element"},
			"client_attribution_metadata[merchant_integration_version]":                                     {"custom"},
			"client_attribution_metadata[payment_intent_creation_flow]":                                     {"deferred"},
			"client_attribution_metadata[payment_method_selection_flow]":                                    {"automatic"},
			"client_attribution_metadata[merchant_integration_additional_elements][0]":                      {"expressCheckout"},
			"client_attribution_metadata[merchant_integration_additional_elements][1]":                      {"payment"},
			"client_attribution_metadata[merchant_integration_additional_elements][2]":                      {"address"},
			"payment_method_data[type]":                                                                     {pmType},
			"payment_method_data[billing_details][name]":                                                    {name},
			"payment_method_data[billing_details][email]":                                                   {billingEmail},
			"payment_method_data[billing_details][address][country]":                                        {country},
			"payment_method_data[billing_details][address][line1]":                                          {line1},
			"payment_method_data[billing_details][address][city]":                                           {city},
			"payment_method_data[billing_details][address][postal_code]":                                    {postalCode},
			"payment_method_data[billing_details][address][state]":                                          {state},
			"payment_method_data[payment_user_agent]":                                                       {fmt.Sprintf("stripe.js/%s; stripe-js-v3/%s; payment-element; deferred-intent", ids.runtimeVersion, ids.runtimeVersion)},
			"payment_method_data[referrer]":                                                                 {"https://chatgpt.com"},
			"payment_method_data[time_on_page]":                                                             {timeOnPage},
			"payment_method_data[client_attribution_metadata][client_session_id]":                           {ids.clientSessionID},
			"payment_method_data[client_attribution_metadata][checkout_session_id]":                         {csID},
			"payment_method_data[client_attribution_metadata][checkout_config_id]":                          {ids.checkoutConfigID},
			"payment_method_data[client_attribution_metadata][elements_session_id]":                         {ids.elementsSessionID},
			"payment_method_data[client_attribution_metadata][elements_session_config_id]":                  {elementsConfigID},
			"payment_method_data[client_attribution_metadata][merchant_integration_source]":                 {"elements"},
			"payment_method_data[client_attribution_metadata][merchant_integration_subtype]":                {"payment-element"},
			"payment_method_data[client_attribution_metadata][merchant_integration_version]":                {"2021"},
			"payment_method_data[client_attribution_metadata][payment_intent_creation_flow]":                {"deferred"},
			"payment_method_data[client_attribution_metadata][payment_method_selection_flow]":               {"automatic"},
			"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": {"expressCheckout"},
			"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": {"payment"},
			"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": {"address"},
			"link_brand": {"link"},
		}
		if strings.TrimSpace(line2) != "" {
			confirmFormB.Set("payment_method_data[billing_details][address][line2]", line2)
		}
		if ids.initChecksum != "" {
			confirmFormB.Set("init_checksum", ids.initChecksum)
		}
		if strings.TrimSpace(line2) != "" {
			confirmFormB.Set("payment_method_data[billing_details][address][line2]", line2)
		}
		if ch.requireTaxID && strings.TrimSpace(cpf) != "" {
			confirmFormB.Set("payment_method_data[billing_details][tax_id]", cpf)
		}

		t0 = time.Now()
		client, proxy, status, raw, err, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, "stripe.confirm_b", rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
			sh := stripeAPIHeaders(csID, protocolModeEarly)
			return doReq(c, fhttp.MethodPost, "https://api.stripe.com/v1/payment_pages/"+csID+"/confirm", sh, []byte(confirmFormB.Encode()))
		})
		providerProxy = proxy
		result["provider_proxy"] = providerProxy
		rec("stripe.confirm_b", status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
		if err != nil || status >= 300 {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("confirm b failed", status, string(raw), err)
			result["errorStage"] = "stripe.confirm_b"
			writeResult(result)
			return 1
		}
		_ = json.Unmarshal(raw, &confirmData)
		result["confirmKeys"] = mapKeys(confirmData)
		if sa, ok := confirmData["submission_attempt"].(map[string]any); ok {
			result["submissionState"] = firstString(sa, "state")
			result["submissionAttemptKeys"] = mapKeys(sa)
		}
		result["confirmStatus"] = firstString(confirmData, "status")
		pmID = ""
	} else {
		// 9 payment_methods type=pix
		pmForm := url.Values{
			"type":                                  {pmType},
			"billing_details[name]":                 {name},
			"billing_details[email]":                {billingEmail},
			"billing_details[address][country]":     {country},
			"billing_details[address][line1]":       {line1},
			"billing_details[address][line2]":       {line2},
			"billing_details[address][city]":        {city},
			"billing_details[address][postal_code]": {postalCode},
			"billing_details[address][state]":       {state},

			"guid": {ids.guid}, "muid": {ids.muid}, "sid": {ids.sid},
			"_stripe_version": {stripeVer}, "key": {pk},
			"payment_user_agent":                                         {fmt.Sprintf("stripe.js/%s; stripe-js-v3/%s; checkout", ids.runtimeVersion, ids.runtimeVersion)},
			"client_attribution_metadata[client_session_id]":             {ids.clientSessionID},
			"client_attribution_metadata[checkout_session_id]":           {csID},
			"client_attribution_metadata[merchant_integration_source]":   {"checkout"},
			"client_attribution_metadata[merchant_integration_version]":  {"custom_checkout"},
			"client_attribution_metadata[payment_method_selection_flow]": {"automatic"},
			"client_attribution_metadata[checkout_config_id]":            {ids.configID},
		}
		t0 = time.Now()
		client, proxy, status, raw, err, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, "stripe.payment_methods", rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
			sh := stripeH
			if protocolModeEarly == "b" {
				sh = stripeAPIHeaders(csID, "b")
			}
			return doReq(c, fhttp.MethodPost, "https://api.stripe.com/v1/payment_methods", sh, []byte(pmForm.Encode()))
		})
		providerProxy = proxy
		result["provider_proxy"] = providerProxy
		if err != nil || status >= 300 {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("payment_methods failed", status, string(raw), err)
			result["errorStage"] = "stripe.payment_methods"
			writeResult(result)
			return 1
		}
		var pmData map[string]any
		_ = json.Unmarshal(raw, &pmData)
		pmID = firstString(pmData, "id")
		result["paymentMethodIdSet"] = pmID != ""
		if pmID == "" {
			result["steps"] = steps
			result["error"] = "pm missing id"
			writeResult(result)
			return 1
		}

		// 10 confirm
		returnURL := confirmReturnURL(csID, init3)
		confirmForm := url.Values{
			"eid": {"NA"}, "payment_method": {pmID},
			"expected_amount": {fmt.Sprint(amt)}, "expected_payment_method_type": {pmType},
			"return_url": {returnURL}, "_stripe_version": {stripeVer},
			"guid": {ids.guid}, "muid": {ids.muid}, "sid": {ids.sid}, "key": {pk},
			"version": {ids.runtimeVersion}, "init_checksum": {ids.initChecksum},
			"client_attribution_metadata[client_session_id]":             {ids.clientSessionID},
			"client_attribution_metadata[checkout_session_id]":           {csID},
			"client_attribution_metadata[merchant_integration_source]":   {"checkout"},
			"client_attribution_metadata[merchant_integration_version]":  {"custom_checkout"},
			"client_attribution_metadata[payment_method_selection_flow]": {"automatic"},
			"client_attribution_metadata[checkout_config_id]":            {ids.configID},
			"link_brand": {"link"},
		}
		t0 = time.Now()
		client, proxy, status, raw, err, _ = doStageWithSIDRetry(providerProxy, timeoutSec, true, country, "stripe.confirm", rec, func(c tls_client.HttpClient, p string) (int, []byte, error) {
			sh := stripeAPIHeaders(csID, protocolModeEarly)
			return doReq(c, fhttp.MethodPost, "https://api.stripe.com/v1/payment_pages/"+csID+"/confirm", sh, []byte(confirmForm.Encode()))
		})
		providerProxy = proxy
		result["provider_proxy"] = providerProxy
		rec("stripe.confirm", status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
		if err != nil || status >= 300 {
			result["steps"] = steps
			result["httpStatus"] = status
			result["error"] = stageHTTPError("confirm failed", status, string(raw), err)
			result["errorStage"] = "stripe.confirm"
			writeResult(result)
			return 1
		}
		_ = json.Unmarshal(raw, &confirmData)
	}
	// keep raw confirm keys for debug (no secrets expected beyond cs/pm ids)
	result["confirmKeys"] = mapKeys(confirmData)
	if sa, ok := confirmData["submission_attempt"].(map[string]any); ok {
		result["submissionState"] = firstString(sa, "state")
		result["submissionAttemptKeys"] = mapKeys(sa)
	}
	result["confirmStatus"] = firstString(confirmData, "status")
	// dump compact confirm for offline QR debug (redact nothing critical beyond size)
	if b, err := json.Marshal(confirmData); err == nil {
		_ = os.WriteFile("/tmp/pix_go_confirm_raw.json", b, 0o644)
	}
	if fail := stripeFailMessage(confirmData); fail != "" {
		result["steps"] = steps
		result["error"] = "confirm rejected: " + fail
		writeResult(result)
		fmt.Fprintln(os.Stderr, result["error"])
		return 1
	}
	qr := sanitizeQR(qrFromObj(confirmData))
	hardStop := hasRealPixMaterial(qr)
	result["hardStopped"] = hardStop
	result["hardStopStage"] = "stripe.confirm"

	// 11 requires_approval → chatgpt approve, then fullpage/poll for QR
	if !hardStop {
		state := strings.ToLower(fmt.Sprint(result["submissionState"]))
		doApprove := func() bool {
			// Same checkout: blocked → rotate SID/proxy internally; does NOT burn outer attempt.
			// After PIX_APPROVE_BLOCKED_MAX blocked hits → terminal marker for Python blacklist.
			innerMax := approveInnerMax()
			maxBlocked := approveBlockedMax()
			rotate := approveDeviceRotateEnabled()
			approved := false
			blockedHits := 0
			transportAttempt := 0
			switchApproveProxy := func(stage string, attempt int) bool {
				c2, p2, ip2, country2, res2, e2 := rebuildClientWithExitPolicy(proxy, timeoutSec, true, country, stage, attempt, rec)
				if e2 != nil {
					result["approveExitError"] = e2.Error()
					result["approveExitStage"] = stage
					return false
				}
				client, proxy = c2, p2
				providerProxy = p2
				result["provider_proxy"] = providerProxy
				result["checkout_proxy"] = providerProxy
				result["promotion_proxy"] = promotionProxy
				result["approveExitIp"] = ip2
				result["approveExitCountry"] = country2
				result["approveResidential"] = res2
				return true
			}
			if !switchApproveProxy("approve_start", 1) {
				return false
			}
			for {
				// best-effort sentinel ping
				pingH := curlCFFIFirefox135Headers(map[string]string{
					"authorization": "Bearer " + token, "content-type": "application/json", "accept": "*/*",
					"origin": "https://chatgpt.com", "referer": "https://chatgpt.com/",
					"oai-language": ch.oaiLanguage, "accept-language": ch.acceptLanguage, "user-agent": defaultUA,
					"oai-device-id": deviceID, "oai-session-id": sessionID, "oai-client-version": oaiVer,
					"oai-client-build-number": oaiBuild,
					"sec-fetch-dest":          "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
					"cookie":                cookie,
					"x-openai-target-path":  "/backend-api/sentinel/ping",
					"x-openai-target-route": "/backend-api/sentinel/ping",
				})
				t0 = time.Now()
				_, _, _ = doReq(client, fhttp.MethodPost, "https://chatgpt.com/backend-api/sentinel/ping", pingH, []byte("{}"))
				rec(fmt.Sprintf("chatgpt.sentinel_ping_%d", blockedHits+transportAttempt+1), 0, true, "", nil, time.Since(t0).Milliseconds())
				if ch.code == "upi" {
					time.Sleep(time.Duration(800+mrand.IntN(801)) * time.Millisecond)
				}

				approveH := curlCFFIFirefox135Headers(map[string]string{
					"authorization": "Bearer " + token, "content-type": "application/json", "accept": "*/*",
					"origin": "https://chatgpt.com", "referer": fmt.Sprintf("https://chatgpt.com/checkout/%s/%s", processor, csID),
					"oai-language": ch.oaiLanguage, "accept-language": ch.acceptLanguage, "user-agent": defaultUA,
					"oai-device-id": deviceID, "oai-session-id": sessionID, "oai-client-version": oaiVer,
					"oai-client-build-number": oaiBuild,
					"sec-fetch-dest":          "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
					"cookie":                cookie,
					"x-openai-target-path":  "/backend-api/payments/checkout/approve",
					"x-openai-target-route": "/backend-api/payments/checkout/approve",
				})
				approveBody, _ := json.Marshal(map[string]any{
					"checkout_session_id": csID,
					"processor_entity":    processor,
				})
				t0 = time.Now()
				status, raw, err = doReq(client, fhttp.MethodPost, "https://chatgpt.com/backend-api/payments/checkout/approve", approveH, approveBody)
				rec(fmt.Sprintf("chatgpt.checkout_approve_%d", blockedHits+transportAttempt+1), status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
				if err == nil && status < 300 {
					var ad map[string]any
					_ = json.Unmarshal(raw, &ad)
					res := strings.ToLower(firstString(ad, "result"))
					result["approveResult"] = res
					if res == "approved" {
						approved = true
						result["approveBlockedHits"] = blockedHits
						providerProxy = proxy
						result["provider_proxy"] = providerProxy
						result["checkout_proxy"] = providerProxy
						result["promotion_proxy"] = promotionProxy
						break
					}
					if res == "blocked" {
						blockedHits++
						result["approveBlockedHits"] = blockedHits
						rec(fmt.Sprintf("chatgpt.checkout_approve.blocked_swap_%d", blockedHits), status, false, string(raw), nil, 0)
						if blockedHits >= maxBlocked {
							result["approveResult"] = "blocked"
							result["approveTerminal"] = true
							break
						}
						// same checkout: fresh IN SID + validated residential IN exit; not an outer rebuild attempt
						if !switchApproveProxy("approve_blocked", blockedHits+1) {
							return false
						}
						deviceID, sessionID, cookie = newDeviceIDs(sessionToken)
						time.Sleep(time.Duration(450+150*(blockedHits-1)) * time.Millisecond)
						continue
					}
					if res == "exception" {
						transportAttempt++
						if transportAttempt >= innerMax {
							break
						}
						if rotate || true {
							if !switchApproveProxy("approve_exception", transportAttempt+1) {
								return false
							}
							deviceID, sessionID, cookie = newDeviceIDs(sessionToken)
						}
						time.Sleep(450 * time.Millisecond)
						continue
					}
				} else {
					transportAttempt++
					if transportAttempt >= innerMax {
						break
					}
					if !switchApproveProxy("approve_transport", transportAttempt+1) {
						return false
					}
					if rotate {
						deviceID, sessionID, cookie = newDeviceIDs(sessionToken)
					}
					time.Sleep(350 * time.Millisecond)
					continue
				}
				time.Sleep(200 * time.Millisecond)
				transportAttempt++
				if transportAttempt >= innerMax {
					break
				}
			}
			result["approveOk"] = approved
			result["approveBlockedHits"] = blockedHits
			return approved
		}

		if state == "requires_approval" {
			if !doApprove() {
				result["steps"] = steps
				if b, _ := result["approveTerminal"].(bool); b {
					result["error"] = fmt.Sprintf("checkout approve rejected: result=blocked; %s_approve_blocked_terminal", ch.code)
				} else {
					result["error"] = "checkout approve rejected: result=" + fmt.Sprint(result["approveResult"])
				}
				writeResult(result)
				fmt.Fprintln(os.Stderr, result["error"])
				return 1
			}
		}

		activeStripeVer := stripeVer
		enableSave := "never"
		if resolveProtocolMode() == "b" {
			activeStripeVer = stripeVerB
			enableSave = "auto"
		}
		pollBudgetSec := pollBudgetSeconds()
		pollQueries := pollMaxQueries()
		pollDeadline := time.Now().Add(time.Duration(pollBudgetSec) * time.Second)
		pollSawJSON := false
		pollSawFailure := false
		pollState := ""
		for i := range pollQueries {
			if hasRealPixMaterial(qr) || pollSawFailure {
				break
			}
			if time.Now().After(pollDeadline) {
				break
			}
			// Match local_pix_api.stripe_elements_session_params / poll.
			params := url.Values{
				"key":             {pk},
				"_stripe_version": {activeStripeVer},
				"elements_session_client[client_betas][0]":                        {"custom_checkout_server_updates_1"},
				"elements_session_client[client_betas][1]":                        {"custom_checkout_manual_approval_1"},
				"elements_session_client[elements_init_source]":                   {"custom_checkout"},
				"elements_session_client[referrer_host]":                          {"chatgpt.com"},
				"elements_session_client[stripe_js_id]":                           {ids.clientSessionID},
				"elements_session_client[locale]":                                 {ch.elementsLocale},
				"elements_session_client[is_aggregation_expected]":                {"false"},
				"elements_options_client[saved_payment_method][enable_save]":      {enableSave},
				"elements_options_client[saved_payment_method][enable_redisplay]": {enableSave},
			}
			if ids.elementsSessionID != "" {
				params.Set("elements_session_client[session_id]", ids.elementsSessionID)
			}
			t0 = time.Now()
			status, raw, err = doReq(client, fhttp.MethodGet, "https://api.stripe.com/v1/payment_pages/"+csID+"?"+params.Encode(), stripeH, nil)
			rec(fmt.Sprintf("stripe.poll_1_%d", i+1), status, status >= 200 && status < 300, string(raw), err, time.Since(t0).Milliseconds())
			if err == nil && status < 300 {
				var poll map[string]any
				if json.Unmarshal(raw, &poll) == nil {
					pollSawJSON = true
					qr = mergeQR(qr, sanitizeQR(qrFromObj(poll)))
					enrichDeclineFromObj(result, poll)
					if sa, ok := poll["submission_attempt"].(map[string]any); ok {
						pollState = strings.ToLower(firstString(sa, "state"))
					}
					pollSawFailure = hasGenericDeclineResult(result) || pollState == "failed"
				}
			}
			if hasRealPixMaterial(qr) || pollSawFailure {
				break
			}
			// Remaining budget-aware sleep; never exceed configured total.
			remain := time.Until(pollDeadline)
			if remain <= 0 {
				break
			}
			sleepFor := 500 * time.Millisecond
			if remain < sleepFor {
				sleepFor = remain
			}
			time.Sleep(sleepFor)
		}
		if !hasRealPixMaterial(qr) && !pollSawFailure && !pollSawJSON {
			// Only fall back to the heavy hosted page when payment_pages produced no usable JSON.
			if fullpageFallbackEnabled() {
				t0 = time.Now()
				status, raw, err = doReq(client, fhttp.MethodGet, "https://checkout.stripe.com/c/pay/"+csID, pageH, nil)
				rec("stripe.fullpage_fallback_1", status, status >= 200 && status < 400, "", err, time.Since(t0).Milliseconds())
				if err == nil {
					qr = mergeQR(qr, sanitizeQR(qrFromText(string(raw))))
				}
			} else {
				rec("stripe.fullpage_fallback_skipped", 0, true, "UPI_FULLPAGE_FALLBACK_ENABLED=0", nil, 0)
			}
		}
		result["hardStopStage"] = "stripe.poll_first"
	}

	result["pixPayload"] = qr.pixPayload
	result["pixInstructionUrl"] = qr.instruction
	result["pixQrPngUrl"] = qr.png
	result["pixQrSvgUrl"] = qr.svg
	result["amount"] = amt
	result["currency"] = cur
	result["paymentMethodTypes"] = methods
	url := firstNonEmpty(qr.instruction, qr.png, qr.svg, qr.pixPayload)
	result["url"] = url
	result["long_url"] = url
	result["billing_name"] = name
	result["checkout_email"] = billingEmail
	result["hardStopped"] = hasRealPixMaterial(qr)
	// Failure diagnostics are enriched from the current payment_pages JSON only.
	result["ok"] = hasRealPixMaterial(qr)
	if !result["ok"].(bool) {
		if result["error"] == nil || result["error"] == "" {
			qrMissing := qrLabel + "_qr_missing"
			if dc, _ := result["declineCode"].(string); dc != "" {
				result["error"] = qrMissing + ":" + dc
			} else if msg, _ := result["lastSetupErrorMessage"].(string); msg != "" {
				result["error"] = msg
			} else {
				result["error"] = qrMissing
			}
		}
	} else {
		// production success: drop probe-only noise that Python slot result filters similarly
		result["hardStopStage"] = ch.code + "_link_once"
		if ch.code == "upi" {
			result["message"] = "UPI redirect/QR material ready"
		} else {
			result["message"] = "PIX QR/instruction ready"
		}
	}
	result["steps"] = steps
	result["elapsedMs"] = time.Now().UnixMilli() - startedMs
	writeResult(result)
	if result["ok"].(bool) {
		if !slotMode {
			fmt.Println("=== PIX SUCCESS ===")
			fmt.Println("instruction:", qr.instruction)
			fmt.Println("payload_prefix:", truncate(qr.pixPayload, 80))
			fmt.Println("png:", truncate(qr.png, 120))
		}
		return 0
	}
	if !slotMode {
		fmt.Fprintln(os.Stderr, "pix_qr_missing")
	}
	return 1
}

type qrFields struct{ pixPayload, instruction, png, svg string }

func mergeQR(a, b qrFields) qrFields {
	if a.pixPayload == "" {
		a.pixPayload = b.pixPayload
	}
	if a.instruction == "" {
		a.instruction = b.instruction
	}
	if a.png == "" {
		a.png = b.png
	}
	if a.svg == "" {
		a.svg = b.svg
	}
	return a
}

func qrFromObj(obj map[string]any) qrFields {
	qr := findQR(obj)
	out := qrFields{
		pixPayload:  firstNonEmpty(strMap(qr, "data"), strMap(qr, "payload"), strMap(qr, "emv"), findKey(obj, "pix_payload", "pixPayload")),
		instruction: firstNonEmpty(pixSuccessURL(strMap(qr, "hosted_instructions_url")), pixSuccessURL(strMap(qr, "instructions_url")), pixSuccessURL(strMap(qr, "redirect_url")), pixSuccessURL(strMap(qr, "url")), findPixURL(obj)),
		png:         firstNonEmpty(strMap(qr, "image_url_png"), strMap(qr, "png"), findKey(obj, "image_url_png", "qr_code_png")),
		svg:         firstNonEmpty(strMap(qr, "image_url_svg"), strMap(qr, "svg"), findKey(obj, "image_url_svg", "qr_code_svg")),
	}
	return out
}

func qrFromText(text string) qrFields {
	decoded := html.UnescapeString(strings.ReplaceAll(text, "\\/", "/"))
	urls := extractHTTPURLs(decoded)
	out := qrFields{}
	for _, u := range urls {
		low := strings.ToLower(u)
		if out.instruction == "" && looksLikePixSuccessURL(u) {
			out.instruction = u
		}
		if out.png == "" && strings.Contains(low, "png") && (strings.Contains(low, "pix") || strings.Contains(low, "upi")) {
			out.png = u
		}
		if out.svg == "" && strings.Contains(low, "svg") && (strings.Contains(low, "pix") || strings.Contains(low, "upi")) {
			out.svg = u
		}
	}
	if m := regexp.MustCompile(`\b000201[A-Z0-9a-z./:+\-]{20,}`).FindString(decoded); m != "" {
		out.pixPayload = m
	}
	return out
}

func findQR(obj any) map[string]any {
	switch t := obj.(type) {
	case map[string]any:
		// PIX nested display block
		if d, ok := t["pix_display_qr_code"].(map[string]any); ok {
			return d
		}
		// UPI setup_intent / payment_page next_action blocks
		for _, key := range []string{
			"upi_handle_redirect_or_display_qr_code",
			"display_upi_qr_code",
			"upi_display_qr_code",
		} {
			if d, ok := t[key].(map[string]any); ok {
				// Flatten nested qr_code if present.
				if qr, ok := d["qr_code"].(map[string]any); ok {
					out := map[string]any{}
					for k, v := range d {
						out[k] = v
					}
					if _, ok := out["image_url_png"]; !ok {
						out["image_url_png"] = qr["image_url_png"]
					}
					if _, ok := out["image_url_svg"]; !ok {
						out["image_url_svg"] = qr["image_url_svg"]
					}
					if _, ok := out["data"]; !ok {
						out["data"] = qr["data"]
					}
					return out
				}
				return d
			}
		}
		if na, ok := t["next_action"].(map[string]any); ok {
			if f := findQR(na); len(f) > 0 {
				return f
			}
		}
		for _, v := range t {
			if f := findQR(v); len(f) > 0 {
				return f
			}
		}
	case []any:
		for _, v := range t {
			if f := findQR(v); len(f) > 0 {
				return f
			}
		}
	}
	return map[string]any{}
}

func findKey(obj any, keys ...string) string {
	want := map[string]bool{}
	for _, k := range keys {
		want[k] = true
	}
	var walk func(any) string
	walk = func(v any) string {
		switch t := v.(type) {
		case map[string]any:
			for k, val := range t {
				if want[k] && val != nil && fmt.Sprint(val) != "" {
					return fmt.Sprint(val)
				}
			}
			for _, val := range t {
				if s := walk(val); s != "" {
					return s
				}
			}
		case []any:
			for _, val := range t {
				if s := walk(val); s != "" {
					return s
				}
			}
		}
		return ""
	}
	return walk(obj)
}

func findPixURL(obj any) string {
	var walk func(any) string
	walk = func(v any) string {
		switch t := v.(type) {
		case map[string]any:
			for k, val := range t {
				if s, ok := val.(string); ok {
					if strings.Contains(strings.ToLower(k), "url") || k == "hosted_instructions_url" {
						if u := pixSuccessURL(s); u != "" {
							return u
						}
					}
				}
			}
			for _, val := range t {
				if s := walk(val); s != "" {
					return s
				}
			}
		case []any:
			for _, val := range t {
				if s := walk(val); s != "" {
					return s
				}
			}
		case string:
			return pixSuccessURL(t)
		}
		return ""
	}
	return walk(obj)
}

func extractHTTPURLs(text string) []string {
	re := regexp.MustCompile(`https?://[^\s"'<>)\]]+`)
	found := re.FindAllString(text, -1)
	out := make([]string, 0, len(found))
	seen := map[string]bool{}
	for _, u := range found {
		u = strings.TrimRight(u, ".,;)")
		if !seen[u] {
			seen[u] = true
			out = append(out, u)
		}
	}
	return out
}

func looksLikePixSuccessURL(u string) bool {
	if !strings.HasPrefix(u, "http") {
		return false
	}
	p, err := url.Parse(u)
	if err != nil {
		return false
	}
	host := strings.ToLower(p.Hostname())
	low := strings.ToLower(u)
	// Match Python denylist in local_pix_api._looks_like_pix_success_url
	switch host {
	case "js.stripe.com", "m.stripe.network", "api.stripe.com", "r.stripe.com",
		"q.stripe.com", "checkout.stripe.com", "pay.openai.com", "chatgpt.com", "www.chatgpt.com":
		return false
	}
	if host == "payments.stripe.com" || host == "pay.stripe.com" || host == "qr.stripe.com" {
		// Bare Stripe origins are not payment material. Require a QR/instruction/UPI path.
		path := strings.ToLower(p.Path)
		if host == "qr.stripe.com" && path != "" && path != "/" {
			return true
		}
		if strings.Contains(path, "/qr/") || strings.Contains(path, "instruction") || strings.Contains(path, "pix") || strings.Contains(path, "upi") {
			return true
		}
		if strings.Contains(low, "/qr/instructions/") || strings.Contains(low, "/upi/instructions") {
			return true
		}
		return false
	}
	if strings.Contains(low, "/qr/instructions/") {
		return true
	}
	if host == "pm-redirects.stripe.com" && strings.Contains(p.Path, "/redirect/complete") {
		return true
	}
	if (host == "stripe.com" || strings.HasSuffix(host, ".stripe.com") || host == "stripe.network" || strings.HasSuffix(host, ".stripe.network")) && strings.Contains(low, "pix") {
		if strings.Contains(low, "icon-pm-pix") || strings.Contains(low, "fingerprinted/img") {
			return false
		}
		return true
	}
	return false
}

func sanitizeQR(q qrFields) qrFields {
	if q.instruction != "" && !looksLikePixSuccessURL(q.instruction) {
		q.instruction = ""
	}
	lowPng := strings.ToLower(q.png)
	if strings.Contains(lowPng, "icon-pm-pix") || strings.Contains(lowPng, "js.stripe.com") {
		q.png = ""
	}
	lowSvg := strings.ToLower(q.svg)
	if strings.Contains(lowSvg, "icon-pm-pix") || strings.Contains(lowSvg, "js.stripe.com") {
		q.svg = ""
	}
	return q
}

func hasRealPixMaterial(q qrFields) bool {
	if strings.HasPrefix(q.pixPayload, "000201") {
		return true
	}
	if q.instruction != "" && looksLikePixSuccessURL(q.instruction) {
		return true
	}
	if q.png != "" && !strings.Contains(strings.ToLower(q.png), "icon-pm-pix") {
		return true
	}
	if q.svg != "" && !strings.Contains(strings.ToLower(q.svg), "icon-pm-pix") && !strings.Contains(strings.ToLower(q.svg), "js.stripe.com") {
		return true
	}
	return false
}

func mapKeys(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func pixSuccessURL(v string) string {
	v = strings.TrimSpace(v)
	if looksLikePixSuccessURL(v) {
		return v
	}
	for _, u := range extractHTTPURLs(v) {
		if looksLikePixSuccessURL(u) {
			return u
		}
	}
	return ""
}

func confirmReturnURL(csID string, initData map[string]any) string {
	hosted := firstString(initData, "stripe_hosted_url")
	if strings.HasPrefix(hosted, "https://checkout.stripe.com") {
		hosted = "https://pay.openai.com" + hosted[len("https://checkout.stripe.com"):]
	}
	if hosted == "" {
		hosted = "https://pay.openai.com/c/pay/" + csID
	}
	p, err := url.Parse(hosted)
	if err != nil {
		return "https://pay.openai.com/c/pay/" + csID + "?redirect_pm_type=pix&ui_mode=custom"
	}
	q := p.Query()
	q.Del("success_return_url")
	q.Set("redirect_pm_type", "pix")
	q.Set("lid", newUUID())
	q.Set("ui_mode", "custom")
	p.RawQuery = q.Encode()
	if p.Host == "checkout.stripe.com" {
		p.Host = "pay.openai.com"
	}
	return p.String()
}

func stripeFailMessage(data map[string]any) string {
	if errObj, ok := data["error"].(map[string]any); ok {
		return firstNonEmpty(fmt.Sprint(errObj["message"]), fmt.Sprint(errObj["code"]), fmt.Sprint(errObj["decline_code"]))
	}
	// nested intent errors
	if s := findKey(data, "decline_code", "message"); s != "" && (strings.Contains(strings.ToLower(s), "declin") || strings.Contains(strings.ToLower(s), "fail")) {
		return s
	}
	return ""
}

func updateStripeIDs(ids *stripeIDs, init map[string]any) {
	if c := firstString(init, "config_id"); c != "" {
		ids.configID = c
		if ids.checkoutConfigID == "" {
			ids.checkoutConfigID = c
		}
	}
	if c := firstString(init, "init_checksum"); c != "" {
		ids.initChecksum = c
	}
	if es, ok := init["elements_session"].(map[string]any); ok {
		if id := firstString(es, "id", "session_id"); id != "" {
			// Prefer real ids from init when present; elements/sessions may overwrite later.
			if ids.elementsSessionID == "" || strings.HasPrefix(ids.elementsSessionID, "elements_session_") {
				ids.elementsSessionID = id
			}
		}
		if cfg := firstString(es, "config_id"); cfg != "" && ids.elementsConfigID == "" {
			ids.elementsConfigID = cfg
		}
	}
}

func inspectInit(initData map[string]any) (currency string, amount any, methods []string) {
	currency = "brl"
	cands := []any{}
	if eo, ok := initData["elements_options"].(map[string]any); ok {
		cands = append(cands, eo["amount"])
	}
	if inv, ok := initData["invoice"].(map[string]any); ok {
		cands = append(cands, inv["amount_due"], inv["total"])
	}
	if ts, ok := initData["total_summary"].(map[string]any); ok {
		cands = append(cands, ts["due"])
	}
	if di, ok := initData["deferred_intent"].(map[string]any); ok {
		cands = append(cands, di["amount"])
		if c, ok := di["currency"].(string); ok && c != "" {
			currency = strings.ToLower(c)
		}
	}
	if c := firstString(initData, "currency"); c != "" {
		currency = strings.ToLower(c)
	}
	for _, c := range cands {
		if c != nil {
			amount = c
			break
		}
	}
	seen := map[string]bool{}
	var walk func(any)
	walk = func(v any) {
		switch t := v.(type) {
		case map[string]any:
			if arr, ok := t["payment_method_types"].([]any); ok {
				for _, x := range arr {
					s := strings.ToLower(fmt.Sprint(x))
					if s != "" && !seen[s] {
						seen[s] = true
						methods = append(methods, s)
					}
				}
			}
			for _, x := range t {
				walk(x)
			}
		case []any:
			for _, x := range t {
				walk(x)
			}
		}
	}
	walk(initData)
	return
}

func doReq(client tls_client.HttpClient, method, rawURL string, headers fhttp.Header, body []byte) (int, []byte, error) {
	var rdr io.Reader
	if body != nil {
		rdr = strings.NewReader(string(body))
	}
	req, err := fhttp.NewRequest(method, rawURL, rdr)
	if err != nil {
		return 0, nil, err
	}
	req.Header = headers
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	return resp.StatusCode, raw, err
}

func writeResult(result map[string]any) {
	if captureSlotResult {
		capturedSlotResult = result
		return
	}
	if emitSlotJSON {
		// Python sidecar expects exactly one JSON object on stdout.
		raw, _ := json.Marshal(result)
		if path := strings.TrimSpace(os.Getenv("PIX_RESULT_PATH")); path != "" {
			_ = os.WriteFile(path, raw, 0o644)
		}
		fmt.Println(string(raw))
		return
	}
	raw, _ := json.MarshalIndent(result, "", "  ")
	path := strings.TrimSpace(os.Getenv("PIX_RESULT_PATH"))
	if path == "" {
		path = "/tmp/pix_go_extract_result.json"
	}
	_ = os.WriteFile(path, raw, 0o644)
	_ = os.WriteFile("pix_go_extract_result.json", raw, 0o644)
	// always keep last path pointer
	_ = os.WriteFile("/tmp/pix_go_extract_result.json", raw, 0o644)
	fmt.Println("wrote", path)
}

type pythonBillingPayload struct {
	Name          string `json:"name"`
	Email         string `json:"email"`
	TaxID         string `json:"tax_id"`
	Line1         string `json:"line1"`
	Line2         string `json:"line2"`
	City          string `json:"city"`
	State         string `json:"state"`
	PostalCode    string `json:"postal_code"`
	Country       string `json:"country"`
	PoolAddressID any    `json:"pool_address_id"`
	PoolCpfID     any    `json:"pool_cpf_id"`
}

// loadBillingFromPythonEnv reads PIX_BILLING_JSON injected by local_pix_api.
// When present and complete, Go must not invent its own address/CPF.
func loadBillingFromPythonEnv() (addr billingAddress, cpf, name, email string, ok bool) {
	raw := strings.TrimSpace(os.Getenv("PIX_BILLING_JSON"))
	if raw == "" {
		return billingAddress{}, "", "", "", false
	}
	var payload pythonBillingPayload
	if err := json.Unmarshal([]byte(raw), &payload); err != nil {
		return billingAddress{}, "", "", "", false
	}
	line1 := strings.TrimSpace(payload.Line1)
	city := strings.TrimSpace(payload.City)
	state := strings.ToUpper(strings.TrimSpace(payload.State))
	postal := strings.TrimSpace(payload.PostalCode)
	tax := strings.TrimSpace(payload.TaxID)
	ch := resolveChannel()
	if line1 == "" || city == "" || state == "" || postal == "" {
		return billingAddress{}, "", "", "", false
	}
	if ch.requireTaxID {
		if tax == "" {
			return billingAddress{}, "", "", "", false
		}
		cpf = formatCPF(tax)
		if cpf == "" {
			// accept already-formatted or digit-only values that formatCPF rejected only on punctuation
			digits := regexp.MustCompile(`\D`).ReplaceAllString(tax, "")
			if len(digits) == 11 {
				cpf = formatCPF(digits)
			}
		}
		if cpf == "" {
			return billingAddress{}, "", "", "", false
		}
	} else {
		// UPI: no CPF/tax_id required
		cpf = ""
	}
	addr = billingAddress{
		line1:      line1,
		line2:      strings.TrimSpace(payload.Line2),
		city:       city,
		state:      state,
		postalCode: postal,
	}
	name = strings.TrimSpace(payload.Name)
	email = strings.TrimSpace(payload.Email)
	return addr, cpf, name, email, true
}

func randomBillingAddress() billingAddress {
	if len(brBillingAddresses) == 0 {
		return billingAddress{"Avenida Paulista, 1578", "Bela Vista", "São Paulo", "SP", "01310-200"}
	}
	return brBillingAddresses[mrand.IntN(len(brBillingAddresses))]
}

func randomBillingName() string {
	return brFirstNames[mrand.IntN(len(brFirstNames))] + " " + brLastNames[mrand.IntN(len(brLastNames))]
}

func looksLikeBRPersonName(n string) bool {
	n = strings.TrimSpace(n)
	if n == "" || !strings.Contains(n, " ") {
		return false
	}
	// reject common non-BR anglo first tokens seen on trial accounts
	low := strings.ToLower(strings.Fields(n)[0])
	anglo := map[string]bool{"ivy": true, "john": true, "mike": true, "james": true, "emily": true, "sarah": true, "david": true}
	if anglo[low] {
		return false
	}
	return true
}

func cpfLast4(cpf string) string {
	d := regexp.MustCompile(`\D`).ReplaceAllString(cpf, "")
	if len(d) < 4 {
		return d
	}
	return d[len(d)-4:]
}

func enrichDeclineFromObj(result map[string]any, poll map[string]any) {
	if result == nil || poll == nil {
		return
	}
	if sa, ok := poll["submission_attempt"].(map[string]any); ok {
		result["pollSubmissionState"] = firstString(sa, "state")
		if errObj, ok := sa["error"].(map[string]any); ok {
			result["checkoutErrorCode"] = firstString(errObj, "code")
			result["checkoutDeclineCode"] = firstString(errObj, "decline_code")
			if pe, ok := errObj["payment_error"].(map[string]any); ok {
				result["paymentErrorCode"] = firstString(pe, "code")
				result["declineCode"] = firstString(pe, "decline_code")
			}
		}
	}
	if si, ok := poll["setup_intent"].(map[string]any); ok {
		result["setupIntentStatus"] = firstString(si, "status")
		if le, ok := si["last_setup_error"].(map[string]any); ok {
			result["lastSetupErrorCode"] = firstString(le, "code")
			if result["declineCode"] == nil || result["declineCode"] == "" {
				result["declineCode"] = firstString(le, "decline_code")
			}
			result["lastSetupErrorType"] = firstString(le, "type")
			result["lastSetupErrorMessage"] = firstString(le, "message")
		}
	}
}

func jsonSuccessFalse(raw []byte) bool {
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		return false
	}
	value, ok := data["success"]
	return ok && value == false
}

func firstString(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok && v != nil {
			s := strings.TrimSpace(fmt.Sprint(v))
			if s != "" && s != "<nil>" {
				return s
			}
		}
	}
	return ""
}

func strMap(m map[string]any, k string) string {
	if m == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprint(m[k]))
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" && v != "<nil>" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func containsStr(arr []string, want string) bool {
	for _, a := range arr {
		if a == want {
			return true
		}
	}
	return false
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func redactPreview(s string) string {
	s = strings.ReplaceAll(s, "\n", " ")
	s = regexp.MustCompile(`eyJ[a-zA-Z0-9_\-\.]+`).ReplaceAllString(s, "<jwt>")
	s = regexp.MustCompile(`\b\d{3}\.\d{3}\.\d{3}-\d{2}\b`).ReplaceAllString(s, "<cpf>")
	return truncate(s, 240)
}

func openaiErrorDetail(body string) string {
	body = strings.TrimSpace(body)
	if body == "" {
		return ""
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(body), &payload); err == nil {
		// OpenAI shape: {"detail":"..."} or {"error":{"message":"...","code":"..."}}
		if d := firstString(payload, "detail", "message"); d != "" {
			return d
		}
		if errObj, ok := payload["error"].(map[string]any); ok {
			msg := firstString(errObj, "message", "detail")
			code := firstString(errObj, "code", "type")
			switch {
			case msg != "" && code != "":
				return code + ": " + msg
			case msg != "":
				return msg
			case code != "":
				return code
			}
		}
	}
	// Stripe shape: {"error":{"message":"...","code":"..."}}
	preview := redactPreview(body)
	// Prefer short non-HTML text
	if strings.Contains(strings.ToLower(preview), "<html") || strings.Contains(strings.ToLower(preview), "<!doctype") {
		return "html_blocked_or_waf"
	}
	return preview
}

func stageHTTPError(stage string, status int, body string, err error) string {
	parts := []string{stage}
	if err != nil {
		parts = append(parts, "err="+truncate(err.Error(), 160))
	}
	if status > 0 {
		parts = append(parts, fmt.Sprintf("http %d", status))
	}
	detail := openaiErrorDetail(body)
	if detail != "" {
		parts = append(parts, detail)
	} else if status == 0 && err == nil {
		parts = append(parts, "empty_response")
	}
	return strings.Join(parts, ": ")
}

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func stripeBrowserID() string {
	// UUIDv4 + 8 hex chars (Python shape)
	return newUUID() + randHex(8)
}

func randHex(n int) string {
	b := make([]byte, (n+1)/2)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)[:n]
}

func generateCPF() string {
	for range 64 {
		d := make([]int, 9)
		for i := range d {
			d[i] = mrand.IntN(10)
		}
		// avoid all same
		same := true
		for i := 1; i < 9; i++ {
			if d[i] != d[0] {
				same = false
				break
			}
		}
		if same {
			continue
		}
		s1 := 0
		for i, w := 0, 10; i < 9; i, w = i+1, w-1 {
			s1 += d[i] * w
		}
		d1 := (s1 * 10) % 11
		if d1 == 10 {
			d1 = 0
		}
		s2 := 0
		for i, w := 0, 11; i < 9; i, w = i+1, w-1 {
			s2 += d[i] * w
		}
		s2 += d1 * 2
		d2 := (s2 * 10) % 11
		if d2 == 10 {
			d2 = 0
		}
		out := ""
		for _, x := range d {
			out += strconv.Itoa(x)
		}
		return out + strconv.Itoa(d1) + strconv.Itoa(d2)
	}
	return "52998224725"
}

func formatCPF(digits string) string {
	digits = regexp.MustCompile(`\D`).ReplaceAllString(digits, "")
	if len(digits) != 11 {
		return digits
	}
	return digits[:3] + "." + digits[3:6] + "." + digits[6:9] + "-" + digits[9:]
}

func randomBillingEmail() string {
	firsts := []string{"lucas", "gabriel", "rafael", "mateus", "bruno", "pedro", "ana", "maria", "julia", "camila"}
	lasts := []string{"silva", "santos", "oliveira", "souza", "rodrigues", "ferreira", "alves", "pereira"}
	domains := []string{"gmail.com", "outlook.com", "hotmail.com", "yahoo.com.br", "icloud.com"}
	seps := []string{".", "_", ""}
	num := mrand.IntN(9000) + 10
	return firsts[mrand.IntN(len(firsts))] + seps[mrand.IntN(len(seps))] + lasts[mrand.IntN(len(lasts))] + strconv.Itoa(num) + "@" + domains[mrand.IntN(len(domains))]
}

func jwtProfileName(token string) string {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return ""
	}
	raw := parts[1]
	if m := len(raw) % 4; m != 0 {
		raw += strings.Repeat("=", 4-m)
	}
	b, err := base64.URLEncoding.DecodeString(raw)
	if err != nil {
		b, err = base64.RawURLEncoding.DecodeString(parts[1])
		if err != nil {
			return ""
		}
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return ""
	}
	if p, ok := m["https://api.openai.com/profile"].(map[string]any); ok {
		if n := strings.TrimSpace(fmt.Sprint(p["name"])); n != "" && n != "<nil>" {
			return n
		}
	}
	return ""
}

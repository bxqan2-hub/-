"""Unified client-environment profile for protocol-based registration.

Every HTTP session, Sentinel proof, and V8 runtime MUST read its
environment fields from a single ``ProtocolEnvironmentProfile`` instance
so that no two layers contradict each other (e.g. Firefox TLS + Chrome UA).

Design rules
------------
* One deployment picks one profile and sticks to it for an entire
  registration flow.  Profiles are NOT rotated per-account inside a
  single worker.
* The ``impersonate`` target MUST belong to the same browser family as
  ``user_agent``.  A startup assertion enforces this before any network
  traffic leaves the process.
* Profiles are immutable (frozen dataclass); each variant is a factory
  classmethod so callers never need to hand-assemble the fields.
* A **fingerprint pool** supplies multiple profiles for batch
  registration workers that each need a distinct device appearance.
  Rotation is round-robin across a curated list of internally-consistent
  profiles, NOT random field mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar


PROTOCOL_CHROME_VERSION = "146"
PROTOCOL_CHROME_IMPERSONATE = f"chrome{PROTOCOL_CHROME_VERSION}"


# ---------------------------------------------------------------------------
# Family helpers
# ---------------------------------------------------------------------------

def _browser_family(ua: str) -> str:
    """Return ``chrome``, ``firefox``, or ``unknown`` for a User-Agent."""
    lowered = ua.lower()
    if "chrome" in lowered and "firefox" not in lowered:
        return "chrome"
    if "firefox" in lowered:
        return "firefox"
    return "unknown"


def _impersonate_family(target: str) -> str:
    """Map curl_cffi impersonation target to a browser family."""
    lowered = str(target or "").lower()
    if lowered.startswith("chrome") or lowered.startswith("edge"):
        return "chrome"
    if lowered.startswith("firefox"):
        return "firefox"
    if lowered.startswith("safari"):
        return "safari"
    return "unknown"


# ---------------------------------------------------------------------------
# Profile definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtocolEnvironmentProfile:
    """All client-environment fields used during a protocol registration.

    Every field consumed by HTTP headers, Sentinel proofs, or the Node V8
    sandbox must come from ONE instance of this class.  Do NOT duplicate
    constants in callers.
    """

    # Human-readable label shown in diagnostic logs only.
    name: str

    # --- TLS / HTTP layer ------------------------------------------------
    # curl_cffi impersonation target (e.g. "chrome124", "firefox133").
    impersonate: str

    # Explicit User-Agent sent in every HTTP request.  Must belong to the
    # same browser family as *impersonate*.
    user_agent: str

    # --- Locale -----------------------------------------------------------
    # ``Accept-Language`` request header value.
    accept_language: str

    # ``navigator.language`` (primary, e.g. "en-US").
    language: str

    # ``navigator.languages`` as a tuple (e.g. ("en-US", "en")).
    languages: tuple[str, ...]

    # IANA timezone name (e.g. "America/New_York", "Asia/Tokyo").
    # Used by Python proof generation AND the Node V8 sandbox.
    timezone: str

    # --- Display ----------------------------------------------------------
    screen_width: int
    screen_height: int

    # --- Hardware ---------------------------------------------------------
    # ``navigator.hardwareConcurrency`` — MUST be a curated constant, never
    # ``os.cpu_count()`` from the server host.
    hardware_concurrency: int

    # --- Sentinel ---------------------------------------------------------
    # URL of the Sentinel SDK (cached locally).
    sdk_url: str

    # --- Cookie policy -----------------------------------------------------
    # If True, the V8 sandbox is told the environment has no cookie support.
    # This should match the HTTP session's actual cookie capability.
    no_cookie: bool = False

    # ======================================================================
    # Validation
    # ======================================================================

    _VALID_IMPERSONATE_TARGETS: ClassVar[set[str]] = {
        "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
        "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
        "chrome124", "chrome126", "chrome127", "chrome128", "chrome129",
        "chrome130", "chrome131", "chrome133a", "chrome136", "chrome142",
        "chrome145", "chrome146",
        "firefox102", "firefox110", "firefox117", "firefox128",
        "firefox133", "firefox135", "firefox144",
        "safari15_5", "safari17_0", "safari18_0",
        "edge99", "edge101",
    }

    def validate(self) -> None:
        """Raise ``ValueError`` if the profile is internally inconsistent."""
        if self.impersonate not in self._VALID_IMPERSONATE_TARGETS:
            raise ValueError(
                f"Profile {self.name!r}: impersonate target "
                f"{self.impersonate!r} is not a known curl_cffi target. "
                f"Add it to _VALID_IMPERSONATE_TARGETS if it is supported "
                f"by your curl_cffi version."
            )

        ua_family = _browser_family(self.user_agent)
        imp_family = _impersonate_family(self.impersonate)
        if ua_family != imp_family:
            raise ValueError(
                f"Profile {self.name!r}: User-Agent family {ua_family!r} "
                f"does not match impersonation target family {imp_family!r} "
                f"(impersonate={self.impersonate!r}, "
                f"user_agent={self.user_agent[:80]!r}). "
                f"Choose matching values or add a new profile variant."
            )

        if not (640 <= self.screen_width <= 7680):
            raise ValueError(
                f"Profile {self.name!r}: screen_width={self.screen_width} "
                f"is out of plausible range [640, 7680]."
            )
        if not (480 <= self.screen_height <= 4320):
            raise ValueError(
                f"Profile {self.name!r}: screen_height={self.screen_height} "
                f"is out of plausible range [480, 4320]."
            )

        if not (1 <= self.hardware_concurrency <= 128):
            raise ValueError(
                f"Profile {self.name!r}: hardware_concurrency="
                f"{self.hardware_concurrency} is out of plausible range "
                f"[1, 128]."
            )

        if not self.timezone or "/" not in self.timezone:
            raise ValueError(
                f"Profile {self.name!r}: timezone {self.timezone!r} "
                f"must be an IANA timezone name (e.g. America/New_York)."
            )

    # ======================================================================
    # Factory methods — curated, internally-consistent profiles
    # ======================================================================

    @classmethod
    def desktop_us_en_chrome_v1(cls) -> ProtocolEnvironmentProfile:
        """Windows desktop, current Chrome, English (US), 1920×1080, 8 cores."""
        return cls(
            name="desktop-us-en-chrome-v1",
            impersonate=PROTOCOL_CHROME_IMPERSONATE,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
            ),
            accept_language="en-US,en;q=0.9",
            language="en-US",
            languages=("en-US", "en"),
            timezone="America/New_York",
            screen_width=1920,
            screen_height=1080,
            hardware_concurrency=8,
            sdk_url="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
            no_cookie=False,
        )

    @classmethod
    def desktop_us_en_chrome_v2(cls) -> ProtocolEnvironmentProfile:
        """Windows desktop, current Chrome, English (US), 2560×1440, 16 cores."""
        return cls(
            name="desktop-us-en-chrome-v2",
            impersonate=PROTOCOL_CHROME_IMPERSONATE,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
            ),
            accept_language="en-US,en;q=0.9",
            language="en-US",
            languages=("en-US", "en"),
            timezone="America/Chicago",
            screen_width=2560,
            screen_height=1440,
            hardware_concurrency=16,
            sdk_url="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
            no_cookie=False,
        )

    @classmethod
    def desktop_us_en_chrome_v3(cls) -> ProtocolEnvironmentProfile:
        """macOS desktop, current Chrome, English (US), 1680×1050, 10 cores."""
        return cls(
            name="desktop-us-en-chrome-v3",
            impersonate=PROTOCOL_CHROME_IMPERSONATE,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
            ),
            accept_language="en-US,en;q=0.9",
            language="en-US",
            languages=("en-US", "en"),
            timezone="America/Los_Angeles",
            screen_width=1680,
            screen_height=1050,
            hardware_concurrency=10,
            sdk_url="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
            no_cookie=False,
        )

    @classmethod
    def desktop_us_en_chrome_v4(cls) -> ProtocolEnvironmentProfile:
        """Windows laptop, current Chrome, English (US), 1366×768, 4 cores."""
        return cls(
            name="desktop-us-en-chrome-v4",
            impersonate=PROTOCOL_CHROME_IMPERSONATE,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
            ),
            accept_language="en-US,en;q=0.9",
            language="en-US",
            languages=("en-US", "en"),
            timezone="America/Denver",
            screen_width=1366,
            screen_height=768,
            hardware_concurrency=4,
            sdk_url="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
            no_cookie=False,
        )

    @classmethod
    def desktop_us_en_chrome_v5(cls) -> ProtocolEnvironmentProfile:
        """Windows desktop, current Chrome, English (US), 1440×900, 6 cores."""
        return cls(
            name="desktop-us-en-chrome-v5",
            impersonate=PROTOCOL_CHROME_IMPERSONATE,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
            ),
            accept_language="en-US,en;q=0.9",
            language="en-US",
            languages=("en-US", "en"),
            timezone="America/New_York",
            screen_width=1440,
            screen_height=900,
            hardware_concurrency=6,
            sdk_url="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
            no_cookie=False,
        )

    @classmethod
    def desktop_us_en_chrome_v6(cls) -> ProtocolEnvironmentProfile:
        """Linux desktop, current Chrome, English (US), 1920×1080, 12 cores."""
        return cls(
            name="desktop-us-en-chrome-v6",
            impersonate=PROTOCOL_CHROME_IMPERSONATE,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0 Safari/537.36"
            ),
            accept_language="en-US,en;q=0.9",
            language="en-US",
            languages=("en-US", "en"),
            timezone="America/Chicago",
            screen_width=1920,
            screen_height=1080,
            hardware_concurrency=12,
            sdk_url="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
            no_cookie=False,
        )

    @classmethod
    def desktop_us_en_firefox_v1(cls) -> ProtocolEnvironmentProfile:
        """Windows 10 desktop, Firefox 144, English (US), 1920×1080, 8 cores.

        The ``firefox144`` curl_cffi impersonation target provides a Firefox
        TLS/HTTP2 fingerprint that Cloudflare does NOT challenge on ChatGPT's
        edge, unlike the Chrome impersonation (HTTP 403).  The UA string is kept
        consistent with the impersonated Firefox version.
        """
        return cls(
            name="desktop-us-en-firefox-v1",
            impersonate="firefox144",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) "
                "Gecko/20100101 Firefox/144.0"
            ),
            accept_language="en-US,en;q=0.9",
            language="en-US",
            languages=("en-US", "en"),
            timezone="America/New_York",
            screen_width=1920,
            screen_height=1080,
            hardware_concurrency=8,
            sdk_url="https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
            no_cookie=False,
        )

    @classmethod
    def all_us_en_desktop_variants(cls) -> list[ProtocolEnvironmentProfile]:
        """Return every US-English desktop profile for fingerprint rotation.

        Firefox variants come first: curl_cffi's Chrome impersonation is flagged
        by Cloudflare on ChatGPT's edge (HTTP 403 challenge) while the Firefox
        TLS/HTTP2 fingerprint passes, so the pool prefers Firefox.
        """
        return [
            cls.desktop_us_en_firefox_v1(),
            cls.desktop_us_en_chrome_v1(),
            cls.desktop_us_en_chrome_v2(),
            cls.desktop_us_en_chrome_v3(),
            cls.desktop_us_en_chrome_v4(),
            cls.desktop_us_en_chrome_v5(),
            cls.desktop_us_en_chrome_v6(),
        ]

    @classmethod
    def default(cls) -> ProtocolEnvironmentProfile:
        """The recommended default for new deployments.

        Uses the Firefox TLS/HTTP2 fingerprint which passes Cloudflare's
        ChatGPT edge (Chrome impersonation is challenged with HTTP 403).
        """
        return cls.desktop_us_en_firefox_v1()


# ---------------------------------------------------------------------------
# Fingerprint pool — round-robin across consistent profiles
# ---------------------------------------------------------------------------


@dataclass
class FingerprintPool:
    """Round-robin iterator over curated, internally-consistent profiles.

    Usage::

        pool = FingerprintPool.from_us_en_desktop()
        worker = ChatGPTProtocolRegister(profile=next(pool), ...)
    """

    profiles: list[ProtocolEnvironmentProfile]
    _index: int = field(default=0, init=False)

    def __iter__(self):
        return self

    def __next__(self) -> ProtocolEnvironmentProfile:
        profile = self.profiles[self._index % len(self.profiles)]
        self._index += 1
        return profile

    @classmethod
    def from_us_en_desktop(cls) -> FingerprintPool:
        profiles = ProtocolEnvironmentProfile.all_us_en_desktop_variants()
        for p in profiles:
            p.validate()
        return cls(profiles=profiles)

    def reset(self) -> None:
        self._index = 0

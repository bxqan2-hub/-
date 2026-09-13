# -*- coding: utf-8 -*-
"""SMS-Activate handler API 接码客户端（HeroSMS / SMSBower）。

HeroSMS 兼容 SMS-Activate handler API：
https://hero-sms.com/stubs/handler_api.php
"""
import logging
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from curl_cffi.requests import Session as CurlSession

from config import browser as _browser_cfg
from config import codex as _cfg
from core.otp_utils import mask_otp

logger = logging.getLogger(__name__)

_MIN_CANCEL_DELAY = 125
_ACTIVATION_LOCK = threading.Lock()
_ACTIVATION_META: dict[str, dict] = {}
_SCHEDULED_CANCELS: set[str] = set()
_PROVIDER_LOCK = threading.RLock()


@contextmanager
def provider_context(provider: str | None = None):
    """在单次 API 查询中临时切换平台，结束后恢复全局配置。"""
    if not provider:
        yield
        return
    value = str(provider).strip().lower()
    if value not in {"herosms", "smsbower"}:
        raise SmsProviderConfigurationError(f"不支持的接码平台：{provider}")
    with _PROVIDER_LOCK:
        had = hasattr(_cfg, "SMS_PROVIDER")
        previous = getattr(_cfg, "SMS_PROVIDER", "herosms")
        setattr(_cfg, "SMS_PROVIDER", value)
        try:
            yield
        finally:
            if had:
                setattr(_cfg, "SMS_PROVIDER", previous)
            else:
                delattr(_cfg, "SMS_PROVIDER")


def configured_excluded_countries() -> set[str]:
    raw = str(getattr(_cfg, "SMS_EXCLUDED_COUNTRIES", "4") or "")
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def configured_priority_countries() -> list[str]:
    raw = str(getattr(_cfg, "SMS_PRIORITY_COUNTRIES", "56,54,33") or "")
    result = []
    for item in raw.replace(";", ",").split(","):
        country_id = item.strip()
        if country_id and country_id not in result:
            result.append(country_id)
    return result


def _prioritize_offers(offers: list[dict]) -> list[dict]:
    priority = {country_id: index for index, country_id in enumerate(configured_priority_countries())}
    return sorted(
        offers,
        key=lambda offer: (
            0 if str(offer.get("id") or "") in priority else 1,
            priority.get(str(offer.get("id") or ""), len(priority)),
            offer.get("price") if offer.get("price") is not None else float("inf"),
            str(offer.get("name") or ""),
            str(offer.get("id") or ""),
        ),
    )


class SmsProviderError(RuntimeError):
    """HeroSMS 通用错误。"""


class SmsProviderConfigurationError(SmsProviderError):
    """HeroSMS 配置缺失或格式错误。"""


class SmsNoNumbersError(SmsProviderError):
    """HeroSMS 当前没有符合条件的号码。"""


class SmsNoBalanceError(SmsProviderError):
    """HeroSMS 账户余额不足。"""


class SmsCodeTimeout(SmsProviderError):
    """等待短信验证码超时。"""


def _provider_name() -> str:
    value = str(getattr(_cfg, "SMS_PROVIDER", "herosms") or "herosms").strip().lower()
    if value in {"smsbower", "sms-bower", "sms_bower"}:
        return "smsbower"
    return "herosms"


def _provider_label() -> str:
    return "SMSBower" if _provider_name() == "smsbower" else "HeroSMS"


def _api_base() -> str:
    if _provider_name() == "smsbower":
        return str(getattr(_cfg, "SMSBOWER_API_BASE", "") or "").strip()
    return str(getattr(_cfg, "SMS_API_BASE", "") or "").strip()


def _api_key() -> str:
    if _provider_name() == "smsbower":
        return str(getattr(_cfg, "SMSBOWER_API_KEY", "") or "").strip()
    return str(getattr(_cfg, "SMS_API_KEY", "") or "").strip()


def _http() -> CurlSession:
    local_proxy = _cfg.resolve_local_proxy()
    session = CurlSession(impersonate=_browser_cfg.IMPERSONATE, trust_env=False)
    session.proxies = {"http": local_proxy, "https": local_proxy}
    session.timeout = _cfg.SMS_REQUEST_TIMEOUT
    return session


def validate_configuration() -> str:
    """校验当前接码平台静态配置，不发起网络请求。"""
    provider = _provider_name()
    label = _provider_label()
    base_key = "SMSBOWER_API_BASE" if provider == "smsbower" else "SMS_API_BASE"
    api_key_name = "SMSBOWER_API_KEY" if provider == "smsbower" else "SMS_API_KEY"
    required = {
        base_key: _api_base(),
        api_key_name: _api_key(),
        "SMS_SERVICE": str(getattr(_cfg, "SMS_SERVICE", "") or "").strip(),
        "SMS_COUNTRY": str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise SmsProviderConfigurationError(
            f"{label} 配置不完整：{', '.join(missing)}；请在本站配置页补齐后重试 Codex OAuth"
        )
    country_strategy = required["SMS_COUNTRY"].lower()
    if country_strategy != "auto" and not country_strategy.isdigit():
        raise SmsProviderConfigurationError(
            f"{label} 的 SMS_COUNTRY 只能填写 auto 或数字国家 ID"
        )
    if country_strategy != "auto" and country_strategy in configured_excluded_countries():
        raise SmsProviderConfigurationError(
            f"{label} 国家 ID {country_strategy} 已在 SMS_EXCLUDED_COUNTRIES 中永久排除"
        )
    price_limit = str(getattr(_cfg, "SMS_MAX_PRICE", "") or "").strip()
    if country_strategy == "auto" and not price_limit:
        raise SmsProviderConfigurationError("自动选国家时必须填写 SMS_MAX_PRICE 金额上限")
    if price_limit:
        try:
            if Decimal(price_limit) <= 0:
                raise SmsProviderConfigurationError("SMS_MAX_PRICE 金额上限必须大于 0")
        except InvalidOperation as exc:
            raise SmsProviderConfigurationError("SMS_MAX_PRICE 金额上限必须是有效数字") from exc
    return provider


def _request(http: CurlSession, params: dict) -> str:
    # provider_context 在 WebUI 查询时持有同一把锁，避免并发注册读到临时平台。
    with _PROVIDER_LOCK:
        label = _provider_label()
        request_params = {"api_key": _api_key(), **params}
        response = http.get(_api_base(), params=request_params)
    text = (response.text or "").strip()
    if response.status_code != 200:
        raise SmsProviderError(f"{label} HTTP {response.status_code}: {text[:200]}")

    error_code = text.split(":", 1)[0]
    if error_code in {"BAD_KEY", "BAD_ACTION", "BAD_SERVICE", "BAD_STATUS", "WRONG_SERVICE", "WRONG_COUNTRY"}:
        raise SmsProviderError(f"{label} 请求失败：{text}")
    if error_code == "NO_BALANCE":
        raise SmsNoBalanceError(f"{label} 余额不足（NO_BALANCE），请充值后重试")
    if error_code == "NO_NUMBERS":
        raise SmsNoNumbersError(f"{label} 暂无符合当前国家、服务和价格条件的号码（NO_NUMBERS）")
    if error_code == "NO_ACTIVATION":
        raise SmsProviderError(f"{label} 激活 ID 不存在（NO_ACTIVATION）")
    if error_code in {"BANNED", "ERROR_SQL", "SERVICE_UNAVAILABLE_REGION"}:
        raise SmsProviderError(f"{label} 服务异常：{text}")
    return text


def _request_json(http: CurlSession, params: dict):
    text = _request(http, params)
    try:
        import json

        return json.loads(text)
    except Exception as exc:
        raise SmsProviderError(f"{_provider_label()} 返回的 JSON 格式异常：{text[:200]}") from exc


def _phone_digits(value: str) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _remember_activation(activation_id: str, country: str) -> None:
    with _ACTIVATION_LOCK:
        _ACTIVATION_META[activation_id] = {
            "country": str(country or "").strip(),
            "acquired_at": time.time(),
        }


def activation_country(activation_id: str) -> str:
    with _ACTIVATION_LOCK:
        return str((_ACTIVATION_META.get(str(activation_id or "")) or {}).get("country") or "")


def _forget_activation(activation_id: str) -> None:
    with _ACTIVATION_LOCK:
        _ACTIVATION_META.pop(str(activation_id or ""), None)
        _SCHEDULED_CANCELS.discard(str(activation_id or ""))


def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
    max_price: str | float | None = None,
    excluded_countries: set[str] | list[str] | tuple[str, ...] | None = None,
) -> tuple[str, str]:
    """通过 getNumber 获取一个 HeroSMS 号码。"""
    validate_configuration()
    own_http = http is None
    http = http or _http()
    try:
        service_code = str(service or _cfg.SMS_SERVICE).strip()
        country_strategy = str(country if country is not None else _cfg.SMS_COUNTRY).strip()
        price_limit = (
            str(getattr(_cfg, "SMS_MAX_PRICE", "") or "").strip()
            if max_price is None
            else str(max_price or "").strip()
        )
        if _provider_name() == "smsbower" and getattr(_cfg, "SMSBOWER_PROVIDER_ID", "") and country_strategy.lower() == "auto":
            raise SmsProviderConfigurationError("指定 SMSBower 供应商时请选择固定国家")
        offers = []
        if country_strategy.lower() == "auto":
            offers = list_affordable_countries(
                service=service_code,
                max_price=price_limit,
                http=http,
            )
            excluded = configured_excluded_countries()
            excluded.update(str(item).strip() for item in (excluded_countries or []) if str(item).strip())
            offers = _prioritize_offers(
                [offer for offer in offers if str(offer.get("id") or "") not in excluded]
            )
            if not offers:
                raise SmsNoNumbersError(
                    f"{_provider_label()} 没有价格不超过 {price_limit or '不限'}、有库存且本轮尚未失败的 {service_code} 国家"
                )
        else:
            offers = [{"id": country_strategy, "name": country_strategy, "price": None, "count": None}]

        text = ""
        selected_offer = None
        for offer in offers:
            selected_offer = offer
            country_strategy = str(offer["id"])
            if country is None and str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip().lower() == "auto":
                logger.info(
                    f"[SMS:{_provider_label()}] 自动选国家：id=%s, name=%s, price=%s, stock=%s, maxPrice=%s",
                    offer["id"], offer["name"], offer["price"], offer["count"], price_limit,
                )
            params = {
                "action": "getNumber",
                "service": service_code,
                "country": country_strategy,
            }
            if price_limit:
                params["maxPrice"] = price_limit
            supplier_id = str(getattr(_cfg, "SMSBOWER_PROVIDER_ID", "") or "").strip() if _provider_name() == "smsbower" else ""
            if supplier_id:
                if not supplier_id.isdigit() or country_strategy == "auto":
                    raise SmsProviderConfigurationError("指定 SMSBower 供应商时请选择固定国家和数字供应商 ID")
                params["providerIds"] = supplier_id
            try:
                text = _request(http, params)
                break
            except SmsNoNumbersError:
                logger.info(f"[SMS:{_provider_label()}] 国家 id=%s 库存已变化，继续尝试下一个国家", country_strategy)
                selected_offer = None
        if selected_offer is None:
            raise SmsNoNumbersError(f"{_provider_label()} 当前候选国家均没有可取号码")
        parts = text.split(":", 2)
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise SmsProviderError(f"{_provider_label()} getNumber 响应格式异常：{text[:200]}")
        activation_id = parts[1].strip()
        phone = _phone_digits(parts[2])
        if not activation_id or not phone:
            raise SmsProviderError(f"{_provider_label()} getNumber 响应缺少激活 ID 或号码：{text[:200]}")
        _remember_activation(activation_id, country_strategy)
        logger.info("[SMS:%s] 取号成功：activation_id=%s, phone=***%s", _provider_label(), activation_id, phone[-4:])
        return activation_id, phone
    finally:
        if own_http:
            http.close()


def get_countries(http: CurlSession | None = None) -> list[dict]:
    """读取并规范化 HeroSMS 国家列表。"""
    validate_configuration()
    own_http = http is None
    http = http or _http()
    try:
        data = _request_json(http, {"action": "getCountries"})
        if not isinstance(data, dict):
            raise SmsProviderError(f"{_provider_label()} getCountries 响应不是对象")
        countries = []
        for raw_id, raw_item in data.items():
            item = raw_item if isinstance(raw_item, dict) else {}
            country_id = str(item.get("id") or raw_id).strip()
            if not country_id:
                continue
            name = str(
                item.get("chn")
                or item.get("name")
                or item.get("eng")
                or item.get("rus")
                or f"国家 {country_id}"
            ).strip()
            countries.append({
                "id": country_id,
                "name": name,
                "eng": str(item.get("eng") or "").strip(),
                "iso": str(item.get("iso") or item.get("iso2") or "").strip().upper(),
            })
        return countries
    finally:
        if own_http:
            http.close()


def get_prices(
    service: str | None = None,
    country: str | None = None,
    http: CurlSession | None = None,
) -> dict:
    """读取 HeroSMS getPrices 原始价格对象。"""
    validate_configuration()
    own_http = http is None
    http = http or _http()
    try:
        params = {"action": "getPrices", "service": str(service or _cfg.SMS_SERVICE).strip()}
        if country:
            params["country"] = str(country).strip()
        data = _request_json(http, params)
        if not isinstance(data, dict):
            raise SmsProviderError(f"{_provider_label()} getPrices 响应不是对象")
        return data
    finally:
        if own_http:
            http.close()


def list_affordable_countries(
    service: str | None = None,
    max_price: str | float | None = None,
    http: CurlSession | None = None,
) -> list[dict]:
    """列出指定服务有库存且价格不超过上限的国家。"""
    validate_configuration()
    service_code = str(service or _cfg.SMS_SERVICE).strip()
    price_limit = str(max_price if max_price is not None else getattr(_cfg, "SMS_MAX_PRICE", "") or "").strip()
    try:
        limit = Decimal(price_limit) if price_limit else None
    except InvalidOperation as exc:
        raise SmsProviderConfigurationError("金额上限必须是有效数字") from exc
    if limit is not None and limit < 0:
        raise SmsProviderConfigurationError("金额上限不能小于 0")

    own_http = http is None
    http = http or _http()
    try:
        prices = get_prices(service=service_code, http=http)
        try:
            country_names = {item["id"]: item for item in get_countries(http=http)}
        except SmsProviderError as exc:
            logger.warning(f"[SMS:{_provider_label()}] 国家名称读取失败，将仅显示国家 ID：%s", exc)
            country_names = {}

        results = []
        for raw_country_id, raw_services in prices.items():
            country_id = str(raw_country_id).strip()
            services = raw_services if isinstance(raw_services, dict) else {}
            raw_offer = services.get(service_code)
            if raw_offer is None and "cost" in services:
                raw_offer = services
            offer = raw_offer if isinstance(raw_offer, dict) else {}
            try:
                cost = Decimal(str(offer.get("cost", offer.get("price", ""))))
            except InvalidOperation:
                continue
            try:
                count = int(offer.get("count", offer.get("quantity", 0)) or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0 or (limit is not None and cost > limit):
                continue
            country = country_names.get(country_id, {})
            results.append({
                "id": country_id,
                "name": country.get("name") or f"国家 {country_id}",
                "iso": country.get("iso") or "",
                "price": float(cost),
                "count": count,
                "service": service_code,
            })
        results.sort(key=lambda item: (item["price"], item["name"], item["id"]))
        return results
    finally:
        if own_http:
            http.close()


def list_price_tiers(
    service: str | None = None,
    country: str | None = None,
    max_price: str | float | None = None,
    http: CurlSession | None = None,
) -> dict:
    """展示全部单服务报价；预算只标注档位，不隐藏更高价格。

    SMSBower V3 返回国家 → 服务 → 供应商 → price/count/provider_id。
    标准 getPrices 是服务汇总，getTopCountriesByService 只含推荐子集。
    两者都不替代完整 V3 档位。V3 未公布金/银/铜等级，rank 留空。
    """
    provider = validate_configuration()
    service_code = str(service or _cfg.SMS_SERVICE).strip()
    country_filter = str(country or "").strip()
    if country_filter and not country_filter.isdigit():
        raise SmsProviderConfigurationError("请选择数字国家 ID")
    price_limit = str(max_price if max_price is not None else getattr(_cfg, "SMS_MAX_PRICE", "") or "").strip()
    try:
        limit = Decimal(price_limit) if price_limit else None
    except InvalidOperation as exc:
        raise SmsProviderConfigurationError("金额上限必须是有效数字") from exc
    if limit is not None and (not limit.is_finite() or limit <= 0):
        raise SmsProviderConfigurationError("金额上限必须是大于 0 的有限数字")
    own_http = http is None
    http = http or _http()
    try:
        grouped = []
        if provider == "smsbower":
            params = {"action": "getPricesV3", "service": service_code}
            if country_filter:
                params["country"] = country_filter
            raw = _request_json(http, params)
            if not isinstance(raw, dict):
                raise SmsProviderError("SMSBower getPricesV3 响应不是对象")
            names = {item["id"]: item for item in get_countries(http=http)}
            for country_id, services in raw.items():
                country_id = str(country_id)
                if country_filter and country_id != country_filter:
                    continue
                if not country_id.isdigit() or not isinstance(services, dict):
                    continue
                providers = services.get(service_code, {})
                if not isinstance(providers, dict):
                    continue
                tiers = []
                for key, item in providers.items():
                    if not isinstance(item, dict):
                        continue
                    try:
                        price = Decimal(str(item.get("price", "")))
                        count = int(item.get("count", 0))
                    except (InvalidOperation, ValueError, TypeError, OverflowError):
                        continue
                    supplier_id = str(item.get("provider_id") or key)
                    if not price.is_finite() or price < 0 or count <= 0 or not supplier_id.isdigit():
                        continue
                    tiers.append({"provider_id": supplier_id, "price": float(price), "count": count,
                                  "rank": None, "within_budget": limit is None or price <= limit})
                # 价格上限是购买条件，超出预算的档位不进入前端列表，避免出现灰色“买不了”行。
                if limit is not None:
                    tiers = [tier for tier in tiers if tier["within_budget"]]
                if tiers:
                    meta = names.get(country_id, {})
                    grouped.append({"id": country_id, "name": meta.get("name") or f"国家 {country_id}",
                                    "iso": meta.get("iso") or "", "eng": meta.get("eng") or "", "tiers": tiers})
        else:
            # HeroSMS 保持原 endpoint 和服务汇总口径，不冒充供应商档位。
            for item in list_affordable_countries(service=service_code, max_price="", http=http):
                if country_filter and item["id"] != country_filter:
                    continue
                if limit is not None and Decimal(str(item["price"])) > limit:
                    continue
                grouped.append({"id": item["id"], "name": item["name"], "iso": item["iso"], "tiers": [{
                    "provider_id": "", "price": item["price"], "count": item["count"], "rank": None,
                    "within_budget": limit is None or Decimal(str(item["price"])) <= limit,
                }]})
        offers = []
        for item in grouped:
            item["tiers"].sort(key=lambda tier: (tier["price"], tier["provider_id"]))
            affordable = [tier for tier in item["tiers"] if tier["within_budget"]]
            if affordable:
                offers.append({"id": item["id"], "name": item["name"], "iso": item["iso"],
                               "price": affordable[0]["price"], "count": sum(tier["count"] for tier in affordable),
                               "service": service_code})
        grouped.sort(key=lambda item: (item["tiers"][0]["price"], item["id"]))
        offers.sort(key=lambda item: (item["price"], item["id"]))
        return {"countries": grouped, "offers": offers, "currency": "USD" if provider == "smsbower" else "",
                "rank_notice": "供应商等级（黄金/银器/铜器）未由此 API 返回" if provider == "smsbower" else "HeroSMS 当前接口提供服务汇总报价"}
    finally:
        if own_http:
            http.close()


def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    previous_code: str | None = None,
) -> str:
    """轮询 getStatus，直到 HeroSMS 返回新鲜的 ``STATUS_OK`` 验证码。

    ``previous_code`` 用于同一号码请求第二条短信的场景。HeroSMS 在新短信尚未
    到达时可能短暂返回上一条 ``STATUS_OK``；此时继续轮询，避免把旧验证码再次
    提交给 OpenAI。
    """
    own_http = http is None
    http = http or _http()
    total_wait = _cfg.SMS_CODE_WAIT if max_wait is None else max_wait
    interval = _cfg.SMS_POLL_INTERVAL if poll_interval is None else poll_interval
    deadline = time.time() + total_wait
    try:
        round_no = 0
        while time.time() < deadline:
            try:
                from core.registration_service import check_stop_requested

                check_stop_requested()
            except ImportError:
                pass

            round_no += 1
            text = _request(http, {"action": "getStatus", "id": activation_id})
            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                if not code:
                    raise SmsProviderError(f"{_provider_label()} 返回 STATUS_OK，但验证码为空")
                if previous_code and code == str(previous_code).strip():
                    logger.info(
                        "[SMS:%s] 第 %s 轮仍是上一条验证码，继续等待新短信",
                        _provider_label(),
                        round_no,
                    )
                    if interval > 0:
                        time.sleep(interval)
                    continue
                logger.info("[SMS:%s] 第 %s 轮收到验证码：%s", _provider_label(), round_no, mask_otp(code))
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError(f"{_provider_label()} 激活已取消（STATUS_CANCEL）")
            if not text.startswith("STATUS_WAIT"):
                raise SmsProviderError(f"{_provider_label()} getStatus 非预期响应：{text[:200]}")

            remaining = max(0, int(deadline - time.time()))
            logger.info(
                "[SMS:%s] 第 %s 轮状态=%s，%ss 后重试（剩余 %ss）",
                _provider_label(),
                round_no,
                text,
                interval,
                remaining,
            )
            if interval > 0:
                time.sleep(interval)

        raise SmsCodeTimeout(f"等待 {_provider_label()} 短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


def request_another_code(activation_id: str, http: CurlSession | None = None) -> str:
    """保留当前号码并通知 HeroSMS 等待下一条短信。"""
    result = set_status(activation_id, 3, http=http)
    logger.info(f"[SMS:{_provider_label()}] 已请求当前号码继续接收下一条短信 activation_id=%s", activation_id)
    return result


def set_status(activation_id: str, status: int, http: CurlSession | None = None) -> str:
    """设置 HeroSMS 激活状态；平台支持 3（重发）、6（完成）、8（取消）。"""
    if status == 1:
        logger.debug(f"[SMS:{_provider_label()}] 忽略兼容状态 1：activation_id=%s", activation_id)
        return "NO_ACTION"
    if status not in {3, 6, 8}:
        raise SmsProviderError(f"{_provider_label()} 不支持的激活状态：{status}")

    own_http = http is None
    http = http or _http()
    try:
        return _request(
            http,
            {"action": "setStatus", "status": str(status), "id": activation_id},
        )
    finally:
        if own_http:
            http.close()


def complete(activation_id: str, http: CurlSession | None = None) -> None:
    """标记激活完成；失败仅记录，避免覆盖已验证成功的主流程。"""
    try:
        set_status(activation_id, 6, http=http)
        logger.info(f"[SMS:{_provider_label()}] 已完成 activation_id=%s", activation_id)
    except Exception as exc:
        logger.warning(f"[SMS:{_provider_label()}] 标记完成失败（不影响结果）：%s", exc)
    finally:
        _forget_activation(activation_id)


def _cancel_after_delay(activation_id: str, delay: float) -> None:
    try:
        if delay > 0:
            time.sleep(delay)
        for attempt in range(1, 4):
            try:
                set_status(activation_id, 8)
                logger.info(f"[SMS:{_provider_label()}] 延迟取消成功 activation_id=%s", activation_id)
                return
            except Exception as exc:
                if "EARLY_CANCEL_DENIED" in str(exc) and attempt < 3:
                    logger.info(f"[SMS:{_provider_label()}] 平台仍限制取消，5 秒后重试 activation_id=%s", activation_id)
                    time.sleep(5)
                    continue
                logger.warning(
                    f"[SMS:{_provider_label()}] 延迟取消失败，需到平台检查 activation_id=%s: %s",
                    activation_id,
                    exc,
                )
                return
    finally:
        _forget_activation(activation_id)


def cancel(
    activation_id: str,
    http: CurlSession | None = None,
    background: bool = True,
) -> None:
    """达到 HeroSMS 最短激活时长后取消；默认后台等待，避免阻塞换号。"""
    activation_id = str(activation_id or "").strip()
    with _ACTIVATION_LOCK:
        metadata = dict(_ACTIVATION_META.get(activation_id) or {})
        already_scheduled = activation_id in _SCHEDULED_CANCELS
    acquired_at = float(metadata.get("acquired_at") or 0)
    remaining = max(0.0, _MIN_CANCEL_DELAY - (time.time() - acquired_at)) if acquired_at else 0.0
    if remaining > 0 and background:
        if already_scheduled:
            logger.info(f"[SMS:{_provider_label()}] 取消任务已存在 activation_id=%s", activation_id)
            return
        with _ACTIVATION_LOCK:
            _SCHEDULED_CANCELS.add(activation_id)
        logger.info(
            f"[SMS:{_provider_label()}] 已安排 %.1f 秒后取消 activation_id=%s（平台最短激活时间 120 秒）",
            remaining,
            activation_id,
        )
        threading.Thread(
            target=_cancel_after_delay,
            args=(activation_id, remaining),
            name=f"herosms-cancel-{activation_id}",
            daemon=True,
        ).start()
        return
    if remaining > 0:
        logger.info(f"[SMS:{_provider_label()}] 等待 %.1f 秒后取消 activation_id=%s", remaining, activation_id)
        time.sleep(remaining)
    try:
        set_status(activation_id, 8, http=http)
        logger.info(f"[SMS:{_provider_label()}] 已取消 activation_id=%s", activation_id)
    except Exception as exc:
        logger.warning(
            f"[SMS:{_provider_label()}] 取消失败（不影响主流程，需到平台检查）：activation_id=%s, %s",
            activation_id,
            exc,
        )
    finally:
        _forget_activation(activation_id)

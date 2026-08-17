# -*- coding: utf-8 -*-
"""HeroSMS 接码平台客户端。

HeroSMS 兼容 SMS-Activate handler API：
https://hero-sms.com/stubs/handler_api.php
"""
import logging
import threading
import time
from decimal import Decimal, InvalidOperation

from curl_cffi.requests import Session as CurlSession

from config import IMPERSONATE
from config import codex as _cfg

logger = logging.getLogger(__name__)

_MIN_CANCEL_DELAY = 125
_ACTIVATION_LOCK = threading.Lock()
_ACTIVATION_META: dict[str, dict] = {}
_SCHEDULED_CANCELS: set[str] = set()


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


def _http() -> CurlSession:
    session = CurlSession(impersonate=IMPERSONATE)
    local_proxy = str(getattr(_cfg, "CODEX_LOCAL_PROXY", "") or "").strip()
    if local_proxy:
        session.proxies = {"http": local_proxy, "https": local_proxy}
    session.timeout = _cfg.SMS_REQUEST_TIMEOUT
    return session


def validate_configuration() -> str:
    """校验 HeroSMS 静态配置，不发起网络请求。"""
    required = {
        "SMS_API_BASE": str(getattr(_cfg, "SMS_API_BASE", "") or "").strip(),
        "SMS_API_KEY": str(getattr(_cfg, "SMS_API_KEY", "") or "").strip(),
        "SMS_SERVICE": str(getattr(_cfg, "SMS_SERVICE", "") or "").strip(),
        "SMS_COUNTRY": str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise SmsProviderConfigurationError(
            f"HeroSMS 配置不完整：{', '.join(missing)}；请在本站配置页补齐后重试 Codex OAuth"
        )
    country_strategy = required["SMS_COUNTRY"].lower()
    if country_strategy != "auto" and not country_strategy.isdigit():
        raise SmsProviderConfigurationError(
            "HeroSMS 的 SMS_COUNTRY 只能填写 auto 或数字国家 ID"
        )
    if country_strategy != "auto" and country_strategy in configured_excluded_countries():
        raise SmsProviderConfigurationError(
            f"HeroSMS 国家 ID {country_strategy} 已在 SMS_EXCLUDED_COUNTRIES 中永久排除"
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
    return "herosms"


def _request(http: CurlSession, params: dict) -> str:
    request_params = {"api_key": _cfg.SMS_API_KEY, **params}
    response = http.get(_cfg.SMS_API_BASE, params=request_params)
    text = (response.text or "").strip()
    if response.status_code != 200:
        raise SmsProviderError(f"HeroSMS HTTP {response.status_code}: {text[:200]}")

    error_code = text.split(":", 1)[0]
    if error_code in {"BAD_KEY", "BAD_ACTION", "BAD_SERVICE", "BAD_STATUS", "WRONG_SERVICE", "WRONG_COUNTRY"}:
        raise SmsProviderError(f"HeroSMS 请求失败：{text}")
    if error_code == "NO_BALANCE":
        raise SmsNoBalanceError("HeroSMS 余额不足（NO_BALANCE），请充值后重试")
    if error_code == "NO_NUMBERS":
        raise SmsNoNumbersError("HeroSMS 暂无符合当前国家、服务和价格条件的号码（NO_NUMBERS）")
    if error_code == "NO_ACTIVATION":
        raise SmsProviderError("HeroSMS 激活 ID 不存在（NO_ACTIVATION）")
    if error_code in {"BANNED", "ERROR_SQL", "SERVICE_UNAVAILABLE_REGION"}:
        raise SmsProviderError(f"HeroSMS 服务异常：{text}")
    return text


def _request_json(http: CurlSession, params: dict):
    text = _request(http, params)
    try:
        import json

        return json.loads(text)
    except Exception as exc:
        raise SmsProviderError(f"HeroSMS 返回的 JSON 格式异常：{text[:200]}") from exc


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
                    f"HeroSMS 没有价格不超过 {price_limit or '不限'}、有库存且本轮尚未失败的 {service_code} 国家"
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
                    "[SMS:HeroSMS] 自动选国家：id=%s, name=%s, price=%s, stock=%s, maxPrice=%s",
                    offer["id"], offer["name"], offer["price"], offer["count"], price_limit,
                )
            params = {
                "action": "getNumber",
                "service": service_code,
                "country": country_strategy,
            }
            if price_limit:
                params["maxPrice"] = price_limit
            try:
                text = _request(http, params)
                break
            except SmsNoNumbersError:
                logger.info("[SMS:HeroSMS] 国家 id=%s 库存已变化，继续尝试下一个国家", country_strategy)
                selected_offer = None
        if selected_offer is None:
            raise SmsNoNumbersError("HeroSMS 当前候选国家均没有可取号码")
        parts = text.split(":", 2)
        if len(parts) != 3 or parts[0] != "ACCESS_NUMBER":
            raise SmsProviderError(f"HeroSMS getNumber 响应格式异常：{text[:200]}")
        activation_id = parts[1].strip()
        phone = _phone_digits(parts[2])
        if not activation_id or not phone:
            raise SmsProviderError(f"HeroSMS getNumber 响应缺少激活 ID 或号码：{text[:200]}")
        _remember_activation(activation_id, country_strategy)
        logger.info("[SMS:HeroSMS] 取号成功：activation_id=%s, phone=+%s", activation_id, phone)
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
            raise SmsProviderError("HeroSMS getCountries 响应不是对象")
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
            raise SmsProviderError("HeroSMS getPrices 响应不是对象")
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
            logger.warning("[SMS:HeroSMS] 国家名称读取失败，将仅显示国家 ID：%s", exc)
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


def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
) -> str:
    """轮询 getStatus，直到 HeroSMS 返回 STATUS_OK。"""
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
                    raise SmsProviderError("HeroSMS 返回 STATUS_OK，但验证码为空")
                logger.info("[SMS:HeroSMS] 第 %s 轮收到验证码：%s", round_no, code)
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError("HeroSMS 激活已取消（STATUS_CANCEL）")
            if not text.startswith("STATUS_WAIT"):
                raise SmsProviderError(f"HeroSMS getStatus 非预期响应：{text[:200]}")

            remaining = max(0, int(deadline - time.time()))
            logger.info(
                "[SMS:HeroSMS] 第 %s 轮状态=%s，%ss 后重试（剩余 %ss）",
                round_no,
                text,
                interval,
                remaining,
            )
            if interval > 0:
                time.sleep(interval)

        raise SmsCodeTimeout(f"等待 HeroSMS 短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


def set_status(activation_id: str, status: int, http: CurlSession | None = None) -> str:
    """设置 HeroSMS 激活状态；平台支持 3（重发）、6（完成）、8（取消）。"""
    if status == 1:
        logger.debug("[SMS:HeroSMS] 忽略兼容状态 1：activation_id=%s", activation_id)
        return "NO_ACTION"
    if status not in {3, 6, 8}:
        raise SmsProviderError(f"HeroSMS 不支持的激活状态：{status}")

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
        logger.info("[SMS:HeroSMS] 已完成 activation_id=%s", activation_id)
    except Exception as exc:
        logger.warning("[SMS:HeroSMS] 标记完成失败（不影响结果）：%s", exc)
    finally:
        _forget_activation(activation_id)


def _cancel_after_delay(activation_id: str, delay: float) -> None:
    try:
        if delay > 0:
            time.sleep(delay)
        for attempt in range(1, 4):
            try:
                set_status(activation_id, 8)
                logger.info("[SMS:HeroSMS] 延迟取消成功 activation_id=%s", activation_id)
                return
            except Exception as exc:
                if "EARLY_CANCEL_DENIED" in str(exc) and attempt < 3:
                    logger.info("[SMS:HeroSMS] 平台仍限制取消，5 秒后重试 activation_id=%s", activation_id)
                    time.sleep(5)
                    continue
                logger.warning(
                    "[SMS:HeroSMS] 延迟取消失败，需到平台检查 activation_id=%s: %s",
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
            logger.info("[SMS:HeroSMS] 取消任务已存在 activation_id=%s", activation_id)
            return
        with _ACTIVATION_LOCK:
            _SCHEDULED_CANCELS.add(activation_id)
        logger.info(
            "[SMS:HeroSMS] 已安排 %.1f 秒后取消 activation_id=%s（平台最短激活时间 120 秒）",
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
        logger.info("[SMS:HeroSMS] 等待 %.1f 秒后取消 activation_id=%s", remaining, activation_id)
        time.sleep(remaining)
    try:
        set_status(activation_id, 8, http=http)
        logger.info("[SMS:HeroSMS] 已取消 activation_id=%s", activation_id)
    except Exception as exc:
        logger.warning(
            "[SMS:HeroSMS] 取消失败（不影响主流程，需到平台检查）：activation_id=%s, %s",
            activation_id,
            exc,
        )
    finally:
        _forget_activation(activation_id)

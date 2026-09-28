#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toss_api.py — 토스증권 Open API 클라이언트 (개인용).
인증(OAuth2 client_credentials, 토큰 캐시) + 공통 요청 헬퍼.
실계좌 연동이므로 주문 관련 함수는 항상 신중하게 사용할 것.

2026-09-28 추가 (openapi.json v1.2.17 기준):
  - _request: gzip 응답 해제, 429 시 Retry-After 만큼 1회 대기 후 재시도, 그룹별 호출 간격 제한
  - TossApiError: HTTP 에러의 code/message/requestId/data 파싱 (RuntimeError 하위 → 기존 except 그대로 동작)
  - 일반 주문: create_order / modify_order / cancel_order / get_orders / get_order
  - 시장 정보: get_us_market_calendar / get_exchange_rate / get_commissions
"""
import os
import re
import json
import gzip
import time
import urllib.request
import urllib.parse
import urllib.error
from decimal import Decimal, ROUND_DOWN, InvalidOperation

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE_PATH = os.path.expanduser("~/.toss_token.json")

# ── 호출 간격 제한 (스펙: ORDER 10/s, 계좌계열 1/s, 시세 5/s). 테스트에서 False 로 끌 수 있음.
THROTTLE_ENABLED = True
_MIN_INTERVAL = {"ORDER": 0.1, "ACCOUNT": 1.0, "QUOTE": 0.2}
_last_call = {}


def _client_id():
    return os.environ["TOSS_CLIENT_ID"]


def _client_secret():
    return os.environ["TOSS_CLIENT_SECRET"]


def _load_cached_token():
    if not os.path.exists(TOKEN_CACHE_PATH):
        return None
    try:
        with open(TOKEN_CACHE_PATH) as f:
            data = json.load(f)
        if data.get("expires_at", 0) > time.time() + 60:  # 60초 여유
            return data["access_token"]
    except Exception:
        pass
    return None


def _save_token(access_token, expires_in):
    data = {"access_token": access_token, "expires_at": time.time() + expires_in}
    with open(TOKEN_CACHE_PATH, "w") as f:
        json.dump(data, f)
    os.chmod(TOKEN_CACHE_PATH, 0o600)


def get_access_token(force_refresh=False):
    """캐시된 토큰이 유효하면 재사용 (클라이언트당 유효 토큰 1개 -> 재발급 시 이전 토큰 즉시 무효화됨)."""
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached

    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/oauth2/token", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    _save_token(data["access_token"], data["expires_in"])
    return data["access_token"]


class TossApiError(RuntimeError):
    """HTTP 4xx/5xx 응답. 문자열 표현은 기존과 동일("HTTP {code} {path}: {body}").
    .status(int) .code(str|None, 예: 'insufficient-buying-power') .message .request_id .data
    """

    def __init__(self, status, path, body):
        super().__init__(f"HTTP {status} {path}: {body}")
        self.status = int(status)
        self.path = path
        self.body = body
        self.code = None
        self.message = ""
        self.request_id = None
        self.data = None
        try:
            err = (json.loads(body) or {}).get("error") or {}
            self.code = err.get("code")
            self.message = err.get("message") or ""
            self.request_id = err.get("requestId")
            self.data = err.get("data")
        except Exception:
            pass


def _decode_body(raw, content_encoding=None):
    """gzip 응답(Content-Encoding: gzip 또는 매직바이트 1f 8b) 해제 후 UTF-8 문자열로."""
    if not raw:
        return ""
    if (content_encoding or "").lower().strip() == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def _rate_group(method, path):
    if path.startswith("/oauth2/"):
        return None
    if path.startswith("/api/v1/orders"):
        return "ORDER" if method == "POST" else "ACCOUNT"
    if path.startswith("/api/v1/conditional-orders"):
        return "ORDER" if method in ("POST", "DELETE") else "ACCOUNT"
    if path.startswith(("/api/v1/accounts", "/api/v1/holdings", "/api/v1/buying-power",
                        "/api/v1/sellable-quantity", "/api/v1/commissions")):
        return "ACCOUNT"
    return "QUOTE"


def _throttle(method, path):
    if not THROTTLE_ENABLED:
        return
    group = _rate_group(method, path)
    if group is None:
        return
    interval = _MIN_INTERVAL.get(group, 0.2)
    now = time.monotonic()
    wait = _last_call.get(group, 0.0) + interval - now
    if wait > 0:
        time.sleep(wait)
    _last_call[group] = time.monotonic()


def _request(method, path, params=None, json_body=None, account_seq=None, retry_on_401=True,
             _retry_429=True):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    if account_seq is not None:
        headers["X-Tossinvest-Account"] = str(account_seq)
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    _throttle(method, path)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = _decode_body(resp.read(), resp.headers.get("Content-Encoding"))
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body = _decode_body(e.read(), e.headers.get("Content-Encoding") if e.headers else None)
        if e.code == 401 and retry_on_401:
            get_access_token(force_refresh=True)
            return _request(method, path, params, json_body, account_seq, retry_on_401=False,
                            _retry_429=_retry_429)
        if e.code == 429 and _retry_429:
            # 429 는 서버가 처리하지 않은 요청 → 재시도 안전. Retry-After(초) 만큼 쉬고 1회만 재시도.
            try:
                retry_after = float((e.headers.get("Retry-After") if e.headers else None) or 1)
            except ValueError:
                retry_after = 1.0
            time.sleep(min(max(retry_after, 0.5), 5.0))
            return _request(method, path, params, json_body, account_seq, retry_on_401=retry_on_401,
                            _retry_429=False)
        raise TossApiError(e.code, path, body) from None


# ── 조회 (계좌 무관) ──────────────────────────────────────────────────────
def get_prices(symbols):
    return _request("GET", "/api/v1/prices", params={"symbols": ",".join(symbols)})["result"]


def get_orderbook(symbol):
    return _request("GET", "/api/v1/orderbook", params={"symbol": symbol})["result"]


def get_stocks(symbols):
    return _request("GET", "/api/v1/stocks", params={"symbols": ",".join(symbols)})["result"]


# ── 계좌 ──────────────────────────────────────────────────────────────────
def get_accounts():
    return _request("GET", "/api/v1/accounts")["result"]


def get_holdings(account_seq, symbol=None):
    params = {"symbol": symbol} if symbol else None
    return _request("GET", "/api/v1/holdings", params=params, account_seq=account_seq)["result"]


def get_buying_power(account_seq, currency="KRW"):
    return _request("GET", "/api/v1/buying-power", params={"currency": currency},
                     account_seq=account_seq)["result"]


def get_sellable_quantity(account_seq, symbol):
    return _request("GET", "/api/v1/sellable-quantity", params={"symbol": symbol},
                     account_seq=account_seq)["result"]


# ── 시장 정보 ──────────────────────────────────────────────────────────────
def get_kr_market_calendar(date=None):
    params = {"date": date} if date else None
    return _request("GET", "/api/v1/market-calendar/KR", params=params)["result"]


# ── 조건주문 (매매 예약) ──────────────────────────────────────────────────
def create_conditional_order(account_seq, symbol, quantity, expire_date, first,
                              second=None, cond_type="SINGLE", order_type="LIMIT",
                              client_order_id=None, confirm_high_value=False):
    """
    first/second: {"orderSide": "BUY"|"SELL", "triggerPrice": "가격", "orderPrice": "가격(LIMIT일 때만)"}
    cond_type: SINGLE(단일 조건) | OCO(둘 중 하나, 매도만) | OTO(첫 체결 후 둘째 감시)
    order_type: LIMIT(지정가, 대부분) | MARKET(시장가, orderPrice 생략)
    """
    body = {
        "symbol": symbol, "type": cond_type, "quantity": str(quantity),
        "orderType": order_type, "expireDate": expire_date, "first": first,
        "confirmHighValueOrder": confirm_high_value,
    }
    if second is not None:
        body["second"] = second
    if client_order_id:
        body["clientOrderId"] = client_order_id
    return _request("POST", "/api/v1/conditional-orders", json_body=body,
                     account_seq=account_seq)["result"]


def reserve_order(account_seq, symbol, side, trigger_price, quantity, expire_date,
                   order_price=None, client_order_id=None):
    """
    간단한 단일 조건 매매예약 ("이 가격 되면 사/팔아줘").
    order_price 생략 시 시장가(MARKET), 지정 시 지정가(LIMIT, trigger_price와 동일하게 두는 게 보통).
    """
    order_type = "MARKET" if order_price is None else "LIMIT"
    first = {"orderSide": side, "triggerPrice": str(trigger_price)}
    if order_price is not None:
        first["orderPrice"] = str(order_price)
    return create_conditional_order(account_seq, symbol, quantity, expire_date, first,
                                     cond_type="SINGLE", order_type=order_type,
                                     client_order_id=client_order_id)


def get_conditional_orders(account_seq, status, symbol=None, cursor=None, limit=20):
    params = {"status": status, "limit": limit}
    if symbol:
        params["symbol"] = symbol
    if cursor:
        params["cursor"] = cursor
    return _request("GET", "/api/v1/conditional-orders", params=params,
                     account_seq=account_seq)["result"]


def get_conditional_order(account_seq, conditional_order_id):
    return _request("GET", f"/api/v1/conditional-orders/{conditional_order_id}",
                     account_seq=account_seq)["result"]


def cancel_conditional_order(account_seq, conditional_order_id):
    return _request("DELETE", f"/api/v1/conditional-orders/{conditional_order_id}",
                     account_seq=account_seq)


def modify_conditional_order(account_seq, conditional_order_id, symbol_unused, quantity,
                              expire_date, first, second=None, cond_type="SINGLE",
                              order_type="LIMIT", confirm_high_value=False):
    body = {
        "type": cond_type, "quantity": str(quantity), "orderType": order_type,
        "expireDate": expire_date, "first": first,
        "confirmHighValueOrder": confirm_high_value,
    }
    if second is not None:
        body["second"] = second
    return _request("POST", f"/api/v1/conditional-orders/{conditional_order_id}/modify",
                     json_body=body, account_seq=account_seq)["result"]


# ── 숫자 → API decimal 문자열 헬퍼 (스펙: pattern ^\d+(\.\d+)?$, 지수표기 불가) ────────
_CLIENT_ORDER_ID_RE = re.compile(r"^[a-zA-Z0-9\-_]{1,36}$")
_KR_SYMBOL_RE = re.compile(r"^[0-9A-Z]{6}$")


def is_kr_symbol(symbol):
    """KRX 6자리 종목코드(숫자 또는 영문·숫자 조합, 예: 005930 / 0101N0) 여부. 그 외는 US 티커로 간주."""
    s = str(symbol)
    return bool(_KR_SYMBOL_RE.match(s)) and any(ch.isdigit() for ch in s)


def to_decimal_str(value, scale=None, name="value"):
    """숫자/문자열 → 스펙 decimal 문자열. scale 지정 시 그 자리 이하 절삭(ROUND_DOWN, 서버 절삭 규칙과 동일).
    예: 10 → '10', 10.0 → '10', 0.5 → '0.5', 1000.0 → '1000', 1e-7(scale 6) → '0'
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name}: 숫자가 필요함 (받은 값: {value!r})")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name}: 숫자가 아님 ({value!r})") from None
    if not d.is_finite() or d < 0:
        raise ValueError(f"{name}: 0 이상의 유한한 숫자여야 함 ({value!r})")
    if scale is not None:
        d = d.quantize(Decimal(1).scaleb(-int(scale)), rounding=ROUND_DOWN)
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def price_to_str(symbol, price):
    """주문 가격 문자열. KR: 정수(원). US: $1 미만 소수 4자리, $1 이상 2자리 (이하 절삭 — 스펙 동일)."""
    d = Decimal(str(price))
    if is_kr_symbol(symbol):
        return to_decimal_str(d, 0, "price")
    return to_decimal_str(d, 4 if d < 1 else 2, "price")


def _check_client_order_id(client_order_id):
    if client_order_id is None:
        return None
    cid = str(client_order_id)
    if not _CLIENT_ORDER_ID_RE.match(cid):
        raise ValueError("client_order_id: 영숫자/-/_ 만, 최대 36자 (멱등키, 10분 유효)")
    return cid


# ── 일반 주문 (실계좌) ─────────────────────────────────────────────────────
ORDER_SIDES = ("BUY", "SELL")
ORDER_TYPES = ("LIMIT", "MARKET")
TIME_IN_FORCES = ("DAY", "CLS", "OPG")


def create_order(account_seq, symbol, side, order_type, quantity=None, price=None,
                 order_amount=None, time_in_force="DAY", client_order_id=None,
                 confirm_high_value=False):
    """실계좌 주문 — 실행 시 실제 돈이 움직임.

    POST /api/v1/orders (헤더 X-Tossinvest-Account=account_seq). 스펙 v1.2.17 OrderCreateRequest:
      수량 기반: {symbol, side, orderType, timeInForce(DAY|CLS|OPG, 기본 DAY), quantity, price(LIMIT 필수),
                  confirmHighValueOrder, clientOrderId?}
      금액 기반: {symbol, side, orderType=MARKET, orderAmount(USD), confirmHighValueOrder, clientOrderId?}
                 (US MARKET 전용, timeInForce 필드 없음, 정규장 시작~종료 1시간 전만 접수)
    - quantity / order_amount 중 정확히 하나. 숫자는 모두 decimal 문자열로 전송.
    - 소수점 quantity 는 US MARKET+SELL 만 허용(6자리) → 그 외는 여기서 ValueError.
    - price: LIMIT 필수 / MARKET 전달 불가. US 는 $1 미만 4자리, 이상 2자리로 절삭.
    - 1억원 이상 주문은 confirm_high_value=True 필요 (400 confirm-high-value-required).
    - 주요 에러(TossApiError.code): 409 opposite-pending-order-exists / request-in-progress,
      422 insufficient-buying-power / order-hours-closed / amount-order-outside-regular-hours /
      fractional-quantity-outside-regular-hours / stock-restricted / price-out-of-range /
      idempotency-key-conflict, 400 invalid-request.
    반환: {"orderId": ..., "clientOrderId": ...|None}
    """
    side = str(side).upper()
    order_type = str(order_type).upper()
    tif = str(time_in_force or "DAY").upper()
    if side not in ORDER_SIDES:
        raise ValueError(f"side 는 {ORDER_SIDES} 중 하나 ({side!r})")
    if order_type not in ORDER_TYPES:
        raise ValueError(f"order_type 은 {ORDER_TYPES} 중 하나 ({order_type!r})")
    if tif not in TIME_IN_FORCES:
        raise ValueError(f"time_in_force 는 {TIME_IN_FORCES} 중 하나 ({tif!r})")
    if (quantity is None) == (order_amount is None):
        raise ValueError("quantity 또는 order_amount 중 정확히 하나만 지정")
    if order_amount is not None and order_type != "MARKET":
        raise ValueError("금액 주문(order_amount)은 US MARKET 주문에만 가능")
    if order_type == "LIMIT" and price is None:
        raise ValueError("LIMIT 주문은 price 필수")
    if order_type == "MARKET" and price is not None:
        raise ValueError("MARKET 주문은 price 전달 불가")
    if tif == "CLS" and order_type != "LIMIT":
        raise ValueError("CLS(장마감 주문)는 US LIMIT 주문만 지원")
    cid = _check_client_order_id(client_order_id)

    body = {"symbol": str(symbol), "side": side, "orderType": order_type}
    if order_amount is not None:
        amt = to_decimal_str(order_amount, 6, "order_amount")
        if Decimal(amt) <= 0:
            raise ValueError("order_amount 는 0 보다 커야 함")
        body["orderAmount"] = amt
    else:
        qty = to_decimal_str(quantity, 6, "quantity")
        if Decimal(qty) <= 0:
            raise ValueError("quantity 는 0 보다 커야 함")
        fractional = "." in qty
        if fractional and not (order_type == "MARKET" and side == "SELL" and not is_kr_symbol(symbol)):
            raise ValueError("소수점 수량은 US 시장가 매도(MARKET+SELL)만 가능. 소수점 매수는 order_amount 사용")
        body["timeInForce"] = tif
        body["quantity"] = qty
        if order_type == "LIMIT":
            body["price"] = price_to_str(symbol, price)
    body["confirmHighValueOrder"] = bool(confirm_high_value)
    if cid:
        body["clientOrderId"] = cid
    return _request("POST", "/api/v1/orders", json_body=body, account_seq=account_seq)["result"]


def modify_order(account_seq, order_id, quantity=None, price=None, order_type=None,
                 symbol=None, confirm_high_value=False):
    """실계좌 주문 — 실행 시 실제 돈이 움직임.

    POST /api/v1/orders/{orderId}/modify. 스펙 OrderModifyRequest:
      {orderType(필수 LIMIT|MARKET), quantity?(KR 필수·정수, US 전달 불가), price?(LIMIT 필수), confirmHighValueOrder}
    - order_type 생략 시 price 가 있으면 LIMIT, 없으면 MARKET.
    - US 주문은 가격 정정만 가능 (quantity 주면 400 us-modify-quantity-not-supported).
    - symbol 을 주면 가격을 그 시장의 호가 자릿수로 절삭해서 전송.
    반환: {"orderId": 새 주문 식별자} (원주문 orderId 와 다름)
    """
    if order_type is None:
        order_type = "LIMIT" if price is not None else "MARKET"
    order_type = str(order_type).upper()
    if order_type not in ORDER_TYPES:
        raise ValueError(f"order_type 은 {ORDER_TYPES} 중 하나 ({order_type!r})")
    if order_type == "LIMIT" and price is None:
        raise ValueError("LIMIT 정정은 price 필수")
    if order_type == "MARKET" and price is not None:
        raise ValueError("MARKET 정정은 price 전달 불가")
    body = {"orderType": order_type}
    if quantity is not None:
        qty = to_decimal_str(quantity, None, "quantity")
        if "." in qty or Decimal(qty) <= 0:
            raise ValueError("정정 수량은 양의 정수만 가능 (KR 전용)")
        body["quantity"] = qty
    if price is not None:
        body["price"] = price_to_str(symbol, price) if symbol else to_decimal_str(price, None, "price")
    body["confirmHighValueOrder"] = bool(confirm_high_value)
    return _request("POST", f"/api/v1/orders/{order_id}/modify", json_body=body,
                     account_seq=account_seq)["result"]


def cancel_order(account_seq, order_id):
    """실계좌 주문 — 실행 시 실제 돈이 움직임.

    POST /api/v1/orders/{orderId}/cancel (본문 없음/빈 객체). 이미 체결된 주문은 취소 불가(409).
    반환: {"orderId": 취소 처리로 새로 발급된 주문 식별자}
    """
    return _request("POST", f"/api/v1/orders/{order_id}/cancel", json_body={},
                     account_seq=account_seq)["result"]


def get_orders(account_seq, status="OPEN", symbol=None, cursor=None, limit=20,
               date_from=None, date_to=None):
    """주문 목록 (읽기 전용). status: OPEN(전량 반환, limit/cursor 무시) | CLOSED(limit 기본 20·최대 100, cursor).
    date_from/date_to: YYYY-MM-DD (KST, orderedAt 기준). 반환: {orders: [Order], nextCursor, hasNext}
    Order: {orderId, symbol, side, orderType, timeInForce, status, price, quantity, orderAmount, currency,
            orderedAt, canceledAt, execution:{filledQuantity, averageFilledPrice, filledAmount, commission, tax,
            filledAt, settlementDate}}
    """
    status = str(status).upper()
    if status not in ("OPEN", "CLOSED"):
        raise ValueError("status 는 OPEN | CLOSED")
    params = {"status": status, "limit": int(limit)}
    if symbol:
        params["symbol"] = symbol
    if cursor:
        params["cursor"] = cursor
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    return _request("GET", "/api/v1/orders", params=params, account_seq=account_seq)["result"]


def get_order(account_seq, order_id):
    """주문 상세 (읽기 전용). 반환: Order (get_orders 참조)."""
    return _request("GET", f"/api/v1/orders/{order_id}", account_seq=account_seq)["result"]


# ── 시장 정보 (US 장운영 / 환율 / 수수료) ─────────────────────────────────
def get_us_market_calendar(date=None):
    """GET /api/v1/market-calendar/US. date: YYYY-MM-DD(미국 현지 날짜, 생략 시 오늘).
    반환: {today, previousBusinessDay, nextBusinessDay} 각 {date, dayMarket, preMarket, regularMarket, afterMarket}
    세션은 {startTime, endTime} (KST ISO, +09:00) 또는 휴장 시 null.
    """
    params = {"date": date} if date else None
    return _request("GET", "/api/v1/market-calendar/US", params=params)["result"]


def get_exchange_rate(base_currency="USD", quote_currency="KRW", date_time=None):
    """GET /api/v1/exchange-rate (gzip 응답). baseCurrency/quoteCurrency 필수(KRW|USD), dateTime 선택.
    반환: {baseCurrency, quoteCurrency, rate(매수 환율, 1 base = ? quote), midRate(매매기준율), basisPoint,
           rateChangeType(UP|EQUAL|DOWN), validFrom, validUntil}  — 참고용 표시 환율(1분 갱신), 거래 환율과 다를 수 있음.
    """
    params = {"baseCurrency": base_currency, "quoteCurrency": quote_currency}
    if date_time:
        params["dateTime"] = date_time
    return _request("GET", "/api/v1/exchange-rate", params=params)["result"]


def get_commissions(account_seq):
    """GET /api/v1/commissions (계좌 헤더 필요). 반환: [{marketCountry: KR|US, commissionRate(소수비율, 0.00015=0.015%),
    startDate, endDate}]"""
    return _request("GET", "/api/v1/commissions", account_seq=account_seq)["result"]


if __name__ == "__main__":
    print("=== 토큰 발급 테스트 ===")
    token = get_access_token()
    print(f"토큰 발급 성공 (길이 {len(token)}자)")

    print("\n=== 계좌 조회 ===")
    accounts = get_accounts()
    print(accounts)

    print("\n=== 현재가 조회 (삼성전자, SK하이닉스) ===")
    prices = get_prices(["005930", "000660"])
    print(prices)

    if accounts:
        seq = accounts[0]["accountSeq"]
        print(f"\n=== 매수가능금액 조회 (accountSeq={seq}) ===")
        print(get_buying_power(seq, "KRW"))

        print(f"\n=== 보유종목 조회 (accountSeq={seq}) ===")
        print(get_holdings(seq))

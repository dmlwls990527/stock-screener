#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toss_api.py — 토스증권 Open API 클라이언트 (개인용).
인증(OAuth2 client_credentials, 토큰 캐시) + 공통 요청 헬퍼.
실계좌 연동이므로 주문 관련 함수는 항상 신중하게 사용할 것.
"""
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE_PATH = os.path.expanduser("~/.toss_token.json")


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


def _request(method, path, params=None, json_body=None, account_seq=None, retry_on_401=True):
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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 401 and retry_on_401:
            get_access_token(force_refresh=True)
            return _request(method, path, params, json_body, account_seq, retry_on_401=False)
        raise RuntimeError(f"HTTP {e.code} {path}: {body}") from None


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

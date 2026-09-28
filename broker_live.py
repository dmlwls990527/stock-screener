#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
broker_live.py — 토스증권 실계좌 브로커 어댑터 (auto_buy 의 LIVE 모드용).

PaperBroker(broker_paper.py) 와 같은 메서드 이름/시그니처를 제공해서 auto_buy 가 mode 값만 바꿔 같은 코드 경로를 타게 한다.
    quote(symbol) -> float                     현재가 (USD 종목이면 USD)
    quotes(symbols) -> {symbol: float}
    buying_power(currency="USD") -> float      현금 매수가능금액
    holdings() -> {symbol: {qty, avg_price, ...}}
    place_order(symbol, side, order_type="MARKET", quantity=None, price=None, amount=None,
                time_in_force="DAY", client_order_id=None, confirm_high_value=False) -> dict
    open_orders() -> [dict]
    cancel(order_id) -> dict
    session_now(now=None) -> "regularMarket" | "preMarket" | "afterMarket" | "dayMarket" | None
    tick() -> dict (실계좌는 서버가 체결/만료를 처리하므로 조회만)
    summary() -> dict

잠금 (실제 돈이 움직이므로 3중):
    1) 생성자 allow_live=True 명시            — 아니면 RuntimeError('live 주문 차단 ...')
    2) 환경변수 AUTO_BUY_LIVE_OK=1             — place_order / cancel 호출 시 검사
    3) auto_buy.py 쪽의 config mode=="live" + --live 플래그 + 'LIVE' 타이핑 확인
조회 메서드(quote/holdings/buying_power/open_orders/session_now)는 읽기 전용이라 2) 없이도 동작한다.

세션 판정은 GET /api/v1/market-calendar/US (KST ISO 시각) 기준. 헬퍼 함수(session_windows,
current_us_session, session_day)는 순수 함수라 broker_paper / 테스트에서 가짜 캘린더로 재사용 가능.
"""
import os
import time
from datetime import datetime, timezone, timedelta

import toss_api

KST = timezone(timedelta(hours=9))
SESSION_NAMES = ("regularMarket", "preMarket", "afterMarket", "dayMarket")
LIVE_ENV_FLAG = "AUTO_BUY_LIVE_OK"
_CALENDAR_TTL_SEC = 600  # 장운영 정보 캐시 (MARKET_INFO 호출 절약)


# ── 순수 헬퍼 ────────────────────────────────────────────────────────────
def parse_kst(ts):
    """'2026-03-25T22:30:00+09:00' / '...T09:31:15.000+09:00' → aware datetime. tz 없는 문자열은 KST 로 간주."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


def now_kst():
    return datetime.now(KST)


def session_windows(calendar):
    """캘린더(result) → [{"date", "session", "start", "end"}] (전일/당일/익일 × 4세션 중 null 아닌 것)."""
    out = []
    if not calendar:
        return out
    for day_key in ("previousBusinessDay", "today", "nextBusinessDay"):
        day = calendar.get(day_key) or {}
        for name in SESSION_NAMES:
            s = day.get(name)
            if not s:
                continue
            out.append({"date": day.get("date"), "day_key": day_key, "session": name,
                        "start": parse_kst(s["startTime"]), "end": parse_kst(s["endTime"])})
    return out


def session_day(calendar, now=None):
    """now 가 속한 (영업일 dict, 세션명). 어느 세션에도 속하지 않으면 (None, None)."""
    now = now or now_kst()
    for w in session_windows(calendar):
        if w["start"] <= now < w["end"]:
            return (calendar.get(w["day_key"]) or {}), w["session"]
    return None, None


def current_us_session(calendar, now=None):
    """now(KST) 시점의 세션명 또는 None(휴장/장외)."""
    return session_day(calendar, now)[1]


def regular_session_bounds(calendar, now=None):
    """now 가 속한 영업일의 정규장 (start, end). DAY 주문 만료·금액주문 마감(1시간 전) 계산용. 없으면 None."""
    day, _ = session_day(calendar, now)
    reg = (day or {}).get("regularMarket")
    if not reg:
        return None
    return parse_kst(reg["startTime"]), parse_kst(reg["endTime"])


def _f(x, default=None):
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize_order(o):
    """스펙 Order → 공용 dict (paper 브로커의 open_orders 항목과 같은 키)."""
    ex = o.get("execution") or {}
    return {
        "order_id": o.get("orderId"),
        "client_order_id": o.get("clientOrderId"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "order_type": o.get("orderType"),
        "time_in_force": o.get("timeInForce"),
        "status": o.get("status"),
        "price": _f(o.get("price")),
        "quantity": _f(o.get("quantity")),
        "amount": _f(o.get("orderAmount")),
        "currency": o.get("currency"),
        "ordered_at": o.get("orderedAt"),
        "canceled_at": o.get("canceledAt"),
        "filled_qty": _f(ex.get("filledQuantity"), 0.0),
        "avg_fill_price": _f(ex.get("averageFilledPrice")),
        "filled_amount": _f(ex.get("filledAmount")),
        "commission": _f(ex.get("commission")),
        "filled_at": ex.get("filledAt"),
        "raw": o,
    }


# ── 실계좌 브로커 ─────────────────────────────────────────────────────────
class LiveBroker:
    """실계좌 — 실행 시 실제 돈이 움직임. allow_live=True 없이는 생성 자체가 안 됨."""

    mode = "live"

    def __init__(self, account_seq, allow_live=False):
        if allow_live is not True:
            raise RuntimeError("live 주문 차단: LiveBroker(account_seq, allow_live=True) 로만 생성 가능")
        if account_seq is None:
            raise RuntimeError("live 주문 차단: account_seq 가 없음 (config live.account_seq 확인)")
        self.account_seq = int(account_seq)
        self._calendar = None
        self._calendar_at = 0.0

    # -- 잠금 ------------------------------------------------------------
    @staticmethod
    def live_env_ok():
        return os.environ.get(LIVE_ENV_FLAG) == "1"

    def _require_live_env(self, what):
        if not self.live_env_ok():
            raise RuntimeError(f"live 주문 차단: {what} 는 환경변수 {LIVE_ENV_FLAG}=1 이 있어야 실행됨")

    # -- 시세 / 세션 (읽기 전용) ------------------------------------------
    def quote(self, symbol):
        """현재가(float). 종목 통화 그대로(US 티커 → USD)."""
        rows = toss_api.get_prices([symbol]) or []
        for r in rows:
            if r.get("symbol", "").upper() == str(symbol).upper() and r.get("lastPrice") is not None:
                return float(r["lastPrice"])
        raise LookupError(f"현재가 없음: {symbol}")

    def quotes(self, symbols):
        """여러 종목 현재가 {symbol: float}. 시세 없는 종목은 빠짐."""
        symbols = list(dict.fromkeys(str(s).upper() for s in symbols))
        out = {}
        for i in range(0, len(symbols), 20):  # 한 번에 너무 많이 넣지 않도록 20개씩
            for r in toss_api.get_prices(symbols[i:i + 20]) or []:
                if r.get("lastPrice") is not None:
                    out[r["symbol"].upper()] = float(r["lastPrice"])
        return out

    def calendar(self, refresh=False):
        """US 장운영 정보 (10분 캐시)."""
        if refresh or self._calendar is None or time.monotonic() - self._calendar_at > _CALENDAR_TTL_SEC:
            self._calendar = toss_api.get_us_market_calendar()
            self._calendar_at = time.monotonic()
        return self._calendar

    def session_now(self, now=None):
        """현재 세션명 (regularMarket / preMarket / afterMarket / dayMarket) 또는 None."""
        return current_us_session(self.calendar(), now)

    def regular_bounds(self, now=None):
        return regular_session_bounds(self.calendar(), now)

    # -- 계좌 (읽기 전용) --------------------------------------------------
    def buying_power(self, currency="USD"):
        r = toss_api.get_buying_power(self.account_seq, currency)
        return _f(r.get("cashBuyingPower"), 0.0)

    def holdings(self):
        """{symbol: {qty, avg_price, currency, name, last_price, market_value, pnl, pnl_rate, market}}"""
        r = toss_api.get_holdings(self.account_seq) or {}
        out = {}
        for it in r.get("items") or []:
            mv = it.get("marketValue") or {}
            pl = it.get("profitLoss") or {}
            out[it["symbol"]] = {
                "qty": _f(it.get("quantity"), 0.0),
                "avg_price": _f(it.get("averagePurchasePrice")),
                "currency": it.get("currency"),
                "name": it.get("name"),
                "market": it.get("marketCountry"),
                "last_price": _f(it.get("lastPrice")),
                "market_value": _f(mv.get("amount")),
                "pnl": _f(pl.get("amount")),
                "pnl_rate": _f(pl.get("rate")),
            }
        return out

    def open_orders(self):
        r = toss_api.get_orders(self.account_seq, status="OPEN") or {}
        return [normalize_order(o) for o in r.get("orders") or []]

    def closed_orders(self, limit=50, date_from=None, date_to=None):
        r = toss_api.get_orders(self.account_seq, status="CLOSED", limit=limit,
                                date_from=date_from, date_to=date_to) or {}
        return [normalize_order(o) for o in r.get("orders") or []]

    def get_order(self, order_id):
        return normalize_order(toss_api.get_order(self.account_seq, order_id))

    # -- 주문 (실제 돈) ----------------------------------------------------
    def place_order(self, symbol, side, order_type="MARKET", quantity=None, price=None, amount=None,
                    time_in_force="DAY", client_order_id=None, confirm_high_value=False):
        """실계좌 주문 — 실행 시 실제 돈이 움직임.

        amount(USD) 를 주면 금액 주문(US MARKET 전용, 소수점 체결), 아니면 quantity 주문.
        반환 dict:
          접수됨  {"status": "SUBMITTED", "order_id", "client_order_id", ...echo}
          거절됨  {"status": "REJECTED", "reason": <스펙 error.code>, "message", "http", ...echo}
                  (400/409/422 — 예: order-hours-closed, insufficient-buying-power, opposite-pending-order-exists)
        401/429/5xx/네트워크 오류는 예외로 올림.
        """
        self._require_live_env("place_order")
        echo = {
            "symbol": str(symbol).upper(), "side": str(side).upper(), "order_type": str(order_type).upper(),
            "quantity": quantity, "price": price, "amount": amount, "time_in_force": time_in_force,
            "client_order_id": client_order_id, "ts": now_kst().isoformat(timespec="seconds"), "mode": "live",
        }
        try:
            res = toss_api.create_order(
                self.account_seq, echo["symbol"], echo["side"], echo["order_type"],
                quantity=quantity, price=price, order_amount=amount, time_in_force=time_in_force,
                client_order_id=client_order_id, confirm_high_value=confirm_high_value,
            )
        except toss_api.TossApiError as e:
            if e.status in (400, 409, 422):
                return {**echo, "status": "REJECTED", "reason": e.code or f"http-{e.status}",
                        "message": e.message, "http": e.status, "request_id": e.request_id, "data": e.data}
            raise
        return {**echo, "status": "SUBMITTED", "order_id": res.get("orderId"),
                "client_order_id": res.get("clientOrderId") or client_order_id, "raw": res}

    def cancel(self, order_id):
        """실계좌 주문 취소 — 실제 주문이 취소됨. 반환 {"status": "CANCEL_REQUESTED"|"REJECTED", ...}"""
        self._require_live_env("cancel")
        try:
            res = toss_api.cancel_order(self.account_seq, order_id)
        except toss_api.TossApiError as e:
            if e.status in (400, 404, 409, 422):
                return {"status": "REJECTED", "order_id": order_id, "reason": e.code or f"http-{e.status}",
                        "message": e.message, "http": e.status}
            raise
        return {"status": "CANCEL_REQUESTED", "order_id": order_id, "new_order_id": res.get("orderId"), "raw": res}

    def modify(self, order_id, price=None, quantity=None, symbol=None):
        """실계좌 주문 정정 — 실제 주문이 바뀜 (US 는 가격만)."""
        self._require_live_env("modify")
        try:
            res = toss_api.modify_order(self.account_seq, order_id, quantity=quantity, price=price, symbol=symbol)
        except toss_api.TossApiError as e:
            if e.status in (400, 404, 409, 422):
                return {"status": "REJECTED", "order_id": order_id, "reason": e.code or f"http-{e.status}",
                        "message": e.message, "http": e.status}
            raise
        return {"status": "MODIFY_REQUESTED", "order_id": order_id, "new_order_id": res.get("orderId"), "raw": res}

    # -- PaperBroker 와 인터페이스 맞추기 ----------------------------------
    def tick(self):
        """실계좌는 체결/만료를 서버가 처리. 미체결 주문만 조회해서 돌려줌."""
        oo = self.open_orders()
        return {"mode": "live", "ts": now_kst().isoformat(timespec="seconds"), "open_orders": len(oo), "orders": oo}

    def summary(self):
        cash_usd = self.buying_power("USD")
        cash_krw = self.buying_power("KRW")
        pos = self.holdings()
        pos_value_usd = sum((p.get("market_value") or 0.0) for p in pos.values() if p.get("currency") == "USD")
        return {
            "mode": "live", "account_seq": self.account_seq, "ts": now_kst().isoformat(timespec="seconds"),
            "session": self.session_now(),
            "cash": {"USD": cash_usd, "KRW": cash_krw},
            "positions": pos, "positions_value_usd": pos_value_usd,
            "equity_usd": cash_usd + pos_value_usd,
            "open_orders": self.open_orders(),
            "live_env_ok": self.live_env_ok(),
        }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
broker_paper.py — 모의투자(페이퍼) 브로커.

토스증권 Open API 에는 모의투자 기능이 없어서, 실제 시세(quote_fn)와 실제 장 세션
(session_fn)을 받아 토스 주문 제약을 흉내 내는 가상 계좌를 JSON 파일로 관리한다.
LiveBroker(broker_live.py) 와 메서드 이름/시그니처를 맞춰 auto_buy.py 가 같은 코드로
paper / live 를 오간다.

흉내 내는 토스 규칙 (openapi v1.2.17 기준):
  - MARKET 주문: 정규장(regularMarket)에서만 → 아니면 'order-hours-closed'
  - LIMIT 주문: dayMarket/preMarket/regularMarket/afterMarket 모두 허용, 장외면 거절
  - 금액주문(orderAmount): US MARKET BUY 전용, 정규장 시작~종료 1시간 전까지만
  - 소수점 수량: MARKET SELL 만 허용. BUY 소수점은 금액주문으로만
  - 호가 단위: $1 미만 0.0001, $1 이상 0.01
  - 같은 종목 반대방향 미체결 존재 → 'opposite-pending-order-exists' (409)
  - 같은 날 같은 clientOrderId 재사용 → 'duplicate-client-order-id'
  - 현금 부족 → 'insufficient-buying-power' (auto_fx 면 KRW 에서 환전 시도)
  - DAY 주문은 주문 당시 달력(calendar)의 정규장 종료 시각에 만료

체결 가격:
  - MARKET BUY  = 시세 × (1 + slippage_bps/1e4), MARKET SELL = 시세 × (1 − slippage_bps/1e4)
  - LIMIT       = 시세가 지정가에 닿으면(BUY: quote ≤ limit, SELL: quote ≥ limit)
                  BUY 는 min(지정가, 시세), SELL 은 max(지정가, 시세) 로 체결 (지정가보다 유리하면 시세)
  - 수수료      = 체결금액 × commission_pct%  (USD 현금에서 차감)

손익 회계 (토스 방식): 매수 수수료는 평균단가에 포함하고, 매도 수수료는 실현손익에서 뺀다.
  → 총자산 변화 = 실현손익 + 평가손익  (항등식. summary()['net_pnl'] 과 ['equity_change'] 가 같다)

상태 파일은 임시파일 + os.replace 로 원자적으로 기록한다.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
STATE_VERSION = 1
SESSIONS = ("dayMarket", "preMarket", "regularMarket", "afterMarket")

# 토스 스펙(openapi v1.2.17) 의 422 코드와 같은 문자열을 쓴다 (paper/live 로그·리포트가 같은 코드로 보이도록)
CODE_AMOUNT_OUTSIDE = "amount-order-outside-regular-hours"
CODE_FRACTIONAL_OUTSIDE = "fractional-quantity-outside-regular-hours"


class StateFileError(RuntimeError):
    """상태 JSON 이 깨져서 읽을 수 없음 (백업 후 reset 필요)."""

# 기본 페이퍼 설정 (auto_buy_config.json 의 "paper" 섹션과 같은 키)
DEFAULT_CFG = {
    "initial_cash_usd": 10000.0,
    "initial_cash_krw": 0.0,
    "slippage_bps": 5,
    "commission_pct": 0.1,
    "fx_spread_pct_market": 0.05,   # 09:00~15:30 KST 환전 스프레드
    "fx_spread_pct_off": 0.5,       # 그 외 시간
    "auto_fx": False,               # USD 부족 시 KRW 자동 환전 (페이퍼 전용 가정)
    "fx_rate": 1400.0,              # fx_fn 이 없을 때 쓰는 기본 환율 (KRW per USD)
}


# ── 시각 / 달력 유틸 ─────────────────────────────────────────────────────
def now_kst():
    return datetime.now(KST)


def parse_ts(s):
    """'2026-09-28T09:00:00.000+09:00' 같은 토스 ISO 문자열 → aware datetime."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=KST)
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def fmt_ts(dt):
    return dt.astimezone(KST).isoformat(timespec="seconds")


def synthetic_calendar(now):
    """달력 API 가 없을 때 쓰는 미국장 하루 달력(서머타임 기준, KST).
    09:00 이전이면 전날 거래일 달력으로 본다(정규장 종료 05:00, 애프터 08:50)."""
    now = now.astimezone(KST)
    base = now.date() if now.hour >= 9 else (now - timedelta(days=1)).date()
    d0 = datetime(base.year, base.month, base.day, tzinfo=KST)
    d1 = d0 + timedelta(days=1)

    def w(a, b):
        return {"startTime": fmt_ts(a), "endTime": fmt_ts(b)}

    return {
        "date": base.isoformat(),
        "dayMarket": w(d0 + timedelta(hours=9), d0 + timedelta(hours=17)),
        "preMarket": w(d0 + timedelta(hours=17), d0 + timedelta(hours=22, minutes=30)),
        "regularMarket": w(d0 + timedelta(hours=22, minutes=30), d1 + timedelta(hours=5)),
        "afterMarket": w(d1 + timedelta(hours=5), d1 + timedelta(hours=8, minutes=50)),
    }


def _calendar_days(cal):
    """토스 market-calendar 응답(result) 또는 하루치 dict → 하루치 dict 리스트."""
    if not cal:
        return []
    if "date" in cal or any(k in cal for k in SESSIONS):
        return [cal]
    days = []
    for k in ("today", "previousBusinessDay", "nextBusinessDay"):
        if isinstance(cal.get(k), dict):
            days.append(cal[k])
    return days


def session_from_calendar(cal, now):
    """(세션명 | None, 해당 하루치 달력 dict | None). 세션 시간대에 now 가 들어가면 그 세션."""
    for day in _calendar_days(cal):
        for name in SESSIONS:
            win = day.get(name)
            if not win:
                continue
            s, e = parse_ts(win.get("startTime")), parse_ts(win.get("endTime"))
            if s and e and s <= now < e:
                return name, day
    return None, None


def price_tick(price):
    return 0.0001 if price < 1 else 0.01


def round_qty(q):
    return float(round(q, 6))


def _floor6(x):
    return math.floor(x * 1e6 + 1e-9) / 1e6


# ── 브로커 ────────────────────────────────────────────────────────────────
class PaperBroker:
    """LiveBroker 와 같은 메서드: quote, buying_power, holdings, place_order,
    open_orders, cancel, session_now. 추가: tick, summary, to_frames, reset, meta.

    quote_fn(symbol) -> float(USD)
    session_fn()     -> 'dayMarket'|'preMarket'|'regularMarket'|'afterMarket'|None
    calendar_fn()    -> 토스 market-calendar/US 의 result dict (선택. 없으면 합성 달력)
    now_fn()         -> aware datetime (선택. 테스트용)
    fx_fn()          -> KRW per USD (선택. 없으면 cfg.fx_rate)
    """

    def __init__(self, state_path, quote_fn, session_fn, cfg=None,
                 calendar_fn=None, now_fn=None, fx_fn=None):
        if isinstance(cfg, dict) and isinstance(cfg.get("paper"), dict):
            cfg = cfg["paper"]
        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(cfg or {})
        self.state_path = state_path
        self.quote_fn = quote_fn
        self.session_fn = session_fn
        self.calendar_fn = calendar_fn
        self.now_fn = now_fn or now_kst
        self.fx_fn = fx_fn
        self.state = self._load_or_init()

    # ── 상태 파일 ──
    def _init_state(self):
        return {
            "version": STATE_VERSION,
            "created": fmt_ts(self._now()),
            "cash": {"USD": float(self.cfg["initial_cash_usd"]),
                     "KRW": float(self.cfg["initial_cash_krw"])},
            "positions": {},
            "open_orders": [],
            "fills": [],
            "rejected": [],
            "closed_orders": [],      # 만료/취소된 주문
            "fx_events": [],          # auto_fx 환전 기록
            "equity_history": [],
            "meta": {"last_run_asof": None, "last_run_ts": None, "order_seq": 0},
        }

    def _load_or_init(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    st = json.load(f)
                if not isinstance(st, dict) or not isinstance(st.get("cash"), dict):
                    raise ValueError("cash 항목이 없음")
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
                raise StateFileError(f"상태 파일 손상: {self.state_path} ({e}) → 백업 후 reset 필요") from None
            base = self._init_state()
            for k, v in base.items():          # 옛 파일에 없는 키 보충
                st.setdefault(k, v)
            for k, v in base["meta"].items():
                st["meta"].setdefault(k, v)
            return st
        st = self._init_state()
        self._write(st)
        return st

    def _write(self, st):
        d = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(d, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def save(self):
        self._write(self.state)

    def reset(self):
        """페이퍼 상태를 초기 현금으로 되돌린다 (되돌릴 수 없음)."""
        self.state = self._init_state()
        self.save()
        return self.state

    # ── meta ──
    def get_meta(self, key, default=None):
        return self.state["meta"].get(key, default)

    def set_meta(self, **kw):
        self.state["meta"].update(kw)
        self.save()

    # ── 시각 / 세션 ──
    def _now(self):
        n = self.now_fn()
        return n if n.tzinfo else n.replace(tzinfo=KST)

    def session_now(self):
        return self.session_fn()

    def _session_and_day(self, now):
        name = self.session_fn()
        day = None
        if self.calendar_fn is not None:
            try:
                cal = self.calendar_fn()
            except Exception:
                cal = None
            _, day = session_from_calendar(cal, now)
            if day is None and cal:
                days = _calendar_days(cal)
                day = days[0] if days else None
        if day is None:
            day = synthetic_calendar(now)
        return name, day

    def _fx_rate(self):
        if self.fx_fn is not None:
            try:
                r = float(self.fx_fn())
                if r > 0:
                    return r
            except Exception:
                pass
        return float(self.cfg.get("fx_rate") or DEFAULT_CFG["fx_rate"])

    def _fx_spread_pct(self, now):
        t = now.astimezone(KST)
        hm = t.hour * 60 + t.minute
        if 9 * 60 <= hm < 15 * 60 + 30:
            return float(self.cfg["fx_spread_pct_market"])
        return float(self.cfg["fx_spread_pct_off"])

    # ── 조회 ──
    def quote(self, symbol):
        return float(self.quote_fn(symbol.upper()))

    def _safe_quote(self, symbol):
        try:
            q = self.quote(symbol)
            if q is None or not (q > 0):
                return None
            return q
        except Exception:
            return None

    def _reserved_usd(self):
        return sum(float(o.get("reservedUsd") or 0.0) for o in self.state["open_orders"]
                   if o["side"] == "BUY")

    def buying_power(self, currency="USD"):
        cur = currency.upper()
        if cur == "USD":
            return max(0.0, self.state["cash"]["USD"] - self._reserved_usd())
        if cur == "KRW":
            return float(self.state["cash"]["KRW"])
        raise ValueError(f"unknown currency {currency}")

    def holdings(self):
        return {s: dict(p) for s, p in self.state["positions"].items() if p.get("qty", 0) > 0}

    def open_orders(self):
        return [dict(o) for o in self.state["open_orders"]]

    def _open_sell_qty(self, symbol):
        return sum(float(o["quantity"] or 0) for o in self.state["open_orders"]
                   if o["symbol"] == symbol and o["side"] == "SELL")

    # ── 주문 ──
    def _next_order_id(self):
        self.state["meta"]["order_seq"] = int(self.state["meta"].get("order_seq", 0)) + 1
        return f"P{self.state['meta']['order_seq']:06d}"

    def _reject(self, code, reason, now, **fields):
        rec = {"ts": fmt_ts(now), "status": "REJECTED", "code": code, "reason": reason}
        rec.update(fields)
        self.state["rejected"].append(rec)
        self.save()
        return dict(rec)

    def _client_id_used_today(self, client_order_id, now):
        if not client_order_id:
            return False
        today = now.astimezone(KST).date().isoformat()
        pools = (self.state["open_orders"], self.state["closed_orders"])
        for o in pools:
            for x in o:
                if x.get("clientOrderId") == client_order_id and \
                        str(x.get("placedAt", ""))[:10] == today:
                    return True
        for f in self.state["fills"]:
            if f.get("clientOrderId") == client_order_id and str(f.get("ts", ""))[:10] == today:
                return True
        return False

    def _ensure_usd(self, need_usd, now):
        """USD 현금이 need_usd 미만이면 auto_fx 일 때 KRW 에서 부족분 환전.
        반환: (ok, fx_event|None)."""
        cash = self.state["cash"]
        avail = cash["USD"] - self._reserved_usd()
        if avail + 1e-9 >= need_usd:
            return True, None
        if not self.cfg.get("auto_fx"):
            return False, None
        shortfall = need_usd - avail
        rate = self._fx_rate()
        spread = self._fx_spread_pct(now)
        eff_rate = rate * (1 + spread / 100.0)
        krw_needed = shortfall * eff_rate
        if cash["KRW"] + 1e-6 < krw_needed:
            return False, None
        cash["KRW"] -= krw_needed
        cash["USD"] += shortfall
        ev = {"ts": fmt_ts(now), "usd": round(shortfall, 6), "krw": round(krw_needed, 2),
              "rate": rate, "spread_pct": spread, "effective_rate": round(eff_rate, 4)}
        self.state["fx_events"].append(ev)
        return True, ev

    def place_order(self, symbol, side, order_type="MARKET", quantity=None, price=None,
                    order_amount=None, time_in_force="DAY", client_order_id=None,
                    amount=None, **_ignored):
        """토스 POST /orders 를 흉내 낸 모의 주문. 거절 시 예외 대신
        {'status':'REJECTED','code':...} 를 돌려주고 state.rejected 에 남긴다.
        amount 는 order_amount 의 별칭 (LiveBroker.place_order 와 같은 이름)."""
        if order_amount is None and amount is not None:
            order_amount = amount
        now = self._now()
        symbol = str(symbol).upper()
        side = str(side).upper()
        order_type = str(order_type).upper()
        tif = str(time_in_force or "DAY").upper()
        base = {"symbol": symbol, "side": side, "orderType": order_type,
                "quantity": quantity, "price": price, "orderAmount": order_amount,
                "timeInForce": tif, "clientOrderId": client_order_id}

        if side not in ("BUY", "SELL") or order_type not in ("MARKET", "LIMIT"):
            return self._reject("invalid-request", f"side/orderType 오류 {side}/{order_type}", now, **base)
        if client_order_id and len(str(client_order_id)) > 36:
            return self._reject("invalid-request", "clientOrderId 36자 초과", now, **base)
        if self._client_id_used_today(client_order_id, now):
            return self._reject("duplicate-client-order-id",
                                f"같은 날 같은 clientOrderId 재사용: {client_order_id}", now, **base)
        opposite = "SELL" if side == "BUY" else "BUY"
        if any(o["symbol"] == symbol and o["side"] == opposite for o in self.state["open_orders"]):
            return self._reject("opposite-pending-order-exists",
                                f"{symbol} 반대방향({opposite}) 미체결 주문 존재", now, **base)

        session, day = self._session_and_day(now)
        if order_type == "MARKET" and session != "regularMarket":
            return self._reject("order-hours-closed",
                                f"MARKET 주문은 정규장에서만 가능 (현재 세션: {session})", now, **base)
        if order_type == "LIMIT" and session not in SESSIONS:
            return self._reject("order-hours-closed", "장외 시간 (LIMIT 도 불가)", now, **base)

        # 수량 / 금액 검증
        if order_amount is not None:
            if side != "BUY" or order_type != "MARKET":
                return self._reject("invalid-request", "금액주문은 MARKET BUY 만 가능", now, **base)
            if quantity is not None:
                return self._reject("invalid-request", "quantity 와 orderAmount 동시 지정 불가", now, **base)
            order_amount = float(order_amount)
            if order_amount <= 0:
                return self._reject("invalid-request", "orderAmount 는 양수", now, **base)
            reg_end = parse_ts((day.get("regularMarket") or {}).get("endTime"))
            if reg_end and now >= reg_end - timedelta(hours=1):
                return self._reject(CODE_AMOUNT_OUTSIDE,
                                    "금액주문은 정규장 시작~종료 1시간 전까지만 가능", now, **base)
        else:
            if quantity is None:
                return self._reject("invalid-request", "quantity 또는 orderAmount 필요", now, **base)
            quantity = float(quantity)
            if quantity <= 0:
                return self._reject("invalid-request", "quantity 는 양수", now, **base)
            fractional = abs(quantity - round(quantity)) > 1e-9
            if fractional and not (side == "SELL" and order_type == "MARKET"):
                return self._reject(CODE_FRACTIONAL_OUTSIDE,
                                    "소수점 수량은 정규장 MARKET SELL 만 가능 (BUY 는 금액주문 사용)", now, **base)
            quantity = round_qty(quantity)

        if order_type == "LIMIT":
            if price is None or float(price) <= 0:
                return self._reject("invalid-request", "LIMIT 은 price 필요", now, **base)
            price = float(price)
            tick = price_tick(price)
            if abs(price / tick - round(price / tick)) > 1e-6:
                return self._reject("invalid-price-tick",
                                    f"호가 단위 위반 (tick={tick}): {price}", now, **base)
        else:
            price = None

        quote = self._safe_quote(symbol)
        if quote is None:
            return self._reject("quote-unavailable", f"{symbol} 시세 조회 실패", now, **base)

        comm_pct = float(self.cfg["commission_pct"]) / 100.0
        slip = float(self.cfg["slippage_bps"]) / 1e4

        if side == "SELL":
            held = float(self.state["positions"].get(symbol, {}).get("qty", 0.0))
            sellable = held - self._open_sell_qty(symbol)
            if quantity > sellable + 1e-9:
                return self._reject("insufficient-sellable-quantity",
                                    f"{symbol} 매도가능 {sellable:.6f} < 주문 {quantity}", now, **base)

        order = {
            "orderId": None, "clientOrderId": client_order_id, "symbol": symbol, "side": side,
            "orderType": order_type, "quantity": quantity, "orderAmount": order_amount,
            "price": price, "timeInForce": tif, "status": "OPEN",
            "placedAt": fmt_ts(now), "session": session, "calendarDate": day.get("date"),
            "expiresAt": None, "reservedUsd": 0.0, "quoteAtPlace": quote,
        }

        # 매수 자금 확인 (+auto_fx)
        fx_event = None
        if side == "BUY":
            if order_type == "MARKET":
                fill_px = quote * (1 + slip)
                if order_amount is not None:
                    est_qty = _floor6(order_amount / fill_px)
                    if est_qty <= 0:
                        return self._reject("invalid-request", "금액이 너무 작아 수량 0", now, **base)
                    gross = est_qty * fill_px
                else:
                    gross = quantity * fill_px
            else:
                gross = quantity * price
            need = gross * (1 + comm_pct)
            ok, fx_event = self._ensure_usd(need, now)
            if not ok:
                avail = self.buying_power("USD")
                return self._reject("insufficient-buying-power",
                                    f"필요 ${need:,.2f} > 가용 ${avail:,.2f}"
                                    + (" (auto_fx: KRW 부족)" if self.cfg.get("auto_fx") else ""),
                                    now, **base)
            order["reservedUsd"] = need
        if fx_event:
            order["fx"] = fx_event

        order["orderId"] = self._next_order_id()

        # 체결 판단
        if order_type == "MARKET":
            fill_px = quote * (1 + slip) if side == "BUY" else quote * (1 - slip)
            fill = self._fill(order, fill_px, quote, now, session, note="market")
            self.save()
            return self._order_result(order, fill)

        # LIMIT: 즉시 체결 가능하면 min/max(지정가, 시세) 로 체결, 아니면 대기
        marketable = (quote <= price) if side == "BUY" else (quote >= price)
        if marketable:
            fill = self._fill(order, self._limit_fill_price(side, price, quote), quote, now, session,
                              note="limit-immediate")
            self.save()
            return self._order_result(order, fill)

        if tif == "DAY":
            reg_end = parse_ts((day.get("regularMarket") or {}).get("endTime"))
            aft_end = parse_ts((day.get("afterMarket") or {}).get("endTime"))
            exp = reg_end
            if exp is None or now >= exp:
                exp = aft_end if (aft_end and now < aft_end) else (now + timedelta(hours=4))
            order["expiresAt"] = fmt_ts(exp)
        else:
            order["expiresAt"] = fmt_ts(now + timedelta(days=30))   # GTC 류: 30일 후 만료로 근사
        self.state["open_orders"].append(order)
        self.save()
        return self._order_result(order, None)

    def _order_result(self, order, fill):
        r = dict(order)
        if fill:
            r["status"] = "FILLED"
            r["fill"] = fill
        return r

    @staticmethod
    def _limit_fill_price(side, limit, quote):
        """지정가 체결가: BUY 는 min(지정가, 시세), SELL 은 max(지정가, 시세)."""
        return min(limit, quote) if side == "BUY" else max(limit, quote)

    def _fill(self, order, fill_px, quote, now, session, note=""):
        symbol, side = order["symbol"], order["side"]
        comm_pct = float(self.cfg["commission_pct"]) / 100.0
        if order.get("orderAmount") is not None and order.get("quantity") is None:
            qty = _floor6(float(order["orderAmount"]) / fill_px)
        else:
            qty = float(order["quantity"])
        qty = round_qty(qty)
        gross = qty * fill_px
        commission = gross * comm_pct
        cash = self.state["cash"]
        pos = self.state["positions"]
        realized = 0.0
        if side == "BUY":
            cash["USD"] -= gross + commission
            p = pos.get(symbol) or {"qty": 0.0, "avg_price": 0.0,
                                    "opened": now.astimezone(KST).date().isoformat()}
            new_qty = p["qty"] + qty
            # 토스 방식: 매수 수수료를 매입단가에 포함 → 총자산 변화 = 실현 + 평가 가 정확히 맞는다
            p["avg_price"] = (p["qty"] * p["avg_price"] + gross + commission) / new_qty if new_qty > 0 else 0.0
            p["qty"] = round_qty(new_qty)
            p["last_price"] = quote
            p["last_price_ts"] = fmt_ts(now)
            pos[symbol] = p
        else:
            p = pos.get(symbol) or {"qty": 0.0, "avg_price": 0.0}
            realized = (fill_px - p["avg_price"]) * qty - commission
            cash["USD"] += gross - commission
            p["qty"] = round_qty(p["qty"] - qty)
            p["last_price"] = quote
            p["last_price_ts"] = fmt_ts(now)
            if p["qty"] <= 1e-9:
                pos.pop(symbol, None)
            else:
                pos[symbol] = p
        order["status"] = "FILLED"
        order["reservedUsd"] = 0.0
        fill = {
            "ts": fmt_ts(now), "orderId": order["orderId"], "clientOrderId": order.get("clientOrderId"),
            "symbol": symbol, "side": side, "orderType": order["orderType"],
            "qty": qty, "fillPrice": round(fill_px, 6), "quotePrice": quote,
            "grossValue": round(gross, 6), "commission": round(commission, 6),
            "netCash": round((-(gross + commission)) if side == "BUY" else (gross - commission), 6),
            "realizedPnl": round(realized, 6), "session": session, "note": note,
            "orderAmount": order.get("orderAmount"), "limitPrice": order.get("price"),
        }
        if order.get("fx"):
            fill["fx"] = order["fx"]
        self.state["fills"].append(fill)
        return fill

    def cancel(self, order_id):
        now = self._now()
        for i, o in enumerate(self.state["open_orders"]):
            if o["orderId"] == order_id:
                o = self.state["open_orders"].pop(i)
                o["status"] = "CANCELLED"
                o["closedAt"] = fmt_ts(now)
                o["reservedUsd"] = 0.0
                self.state["closed_orders"].append(o)
                self.save()
                return dict(o)
        raise KeyError(f"open order not found: {order_id}")

    # ── tick: 미체결 재검사 + 만료 + 평가 ──
    def tick(self, mark=True):
        """미체결 LIMIT 주문을 현재 시세와 다시 비교(체결/만료)하고 자산을 평가해 기록.
        반환: 이번 tick 에서 일어난 이벤트 리스트."""
        now = self._now()
        session, _day = self._session_and_day(now)
        events = []
        remaining = []
        for o in self.state["open_orders"]:
            exp = parse_ts(o.get("expiresAt"))
            if exp and now >= exp:
                o["status"] = "EXPIRED"
                o["closedAt"] = fmt_ts(now)
                o["reservedUsd"] = 0.0
                self.state["closed_orders"].append(o)
                events.append({"event": "EXPIRED", "order": dict(o)})
                continue
            if session not in SESSIONS:
                remaining.append(o)
                continue
            quote = self._safe_quote(o["symbol"])
            if quote is None:
                remaining.append(o)
                continue
            hit = (quote <= o["price"]) if o["side"] == "BUY" else (quote >= o["price"])
            if hit:
                fill = self._fill(o, self._limit_fill_price(o["side"], o["price"], quote), quote, now,
                                  session, note="limit-tick")
                events.append({"event": "FILLED", "order": dict(o), "fill": fill})
            else:
                remaining.append(o)
        self.state["open_orders"] = remaining
        if mark:
            snap = self.mark_to_market(now, append=True)
            events.append({"event": "MARK", "snapshot": snap})
        self.save()
        return events

    def valuation(self, now=None):
        """현재 시세로 평가 (기록하지 않음). 시세 실패 시 마지막 가격 사용."""
        now = now or self._now()
        pos_value = 0.0
        unrealized = 0.0
        detail = {}
        for sym, p in self.state["positions"].items():
            q = self._safe_quote(sym)
            if q is None:
                q = p.get("last_price") or p.get("avg_price") or 0.0
                stale = True
            else:
                p["last_price"] = q
                p["last_price_ts"] = fmt_ts(now)
                stale = False
            val = p["qty"] * q
            pos_value += val
            unrealized += (q - p["avg_price"]) * p["qty"]
            detail[sym] = {"qty": p["qty"], "avg_price": p["avg_price"], "price": q,
                           "value": val, "unrealized": (q - p["avg_price"]) * p["qty"],
                           "pnl_pct": ((q / p["avg_price"] - 1) * 100) if p["avg_price"] else 0.0,
                           "opened": p.get("opened"), "stale": stale}
        fx = self._fx_rate()
        cash_usd = self.state["cash"]["USD"]
        cash_krw = self.state["cash"]["KRW"]
        equity = cash_usd + cash_krw / fx + pos_value
        return {"ts": fmt_ts(now), "equity_usd": equity, "cash_usd": cash_usd,
                "cash_krw": cash_krw, "positions_value": pos_value,
                "unrealized": unrealized, "fx_rate": fx, "positions": detail}

    def mark_to_market(self, now=None, append=True):
        v = self.valuation(now)
        snap = {k: (round(v[k], 6) if isinstance(v[k], float) else v[k])
                for k in ("ts", "equity_usd", "cash_usd", "cash_krw", "positions_value")}
        if append:
            self.state["equity_history"].append(snap)
        return snap

    @staticmethod
    def max_drawdown_from(series):
        """[equity...] → 최대낙폭 (0.25 = -25%)."""
        peak = None
        mdd = 0.0
        for e in series:
            if e is None:
                continue
            if peak is None or e > peak:
                peak = e
            if peak and peak > 0:
                dd = (peak - e) / peak
                if dd > mdd:
                    mdd = dd
        return mdd

    def max_drawdown(self):
        return self.max_drawdown_from([h["equity_usd"] for h in self.state["equity_history"]])

    # ── 요약 / 프레임 ──
    def summary(self):
        """realized_pnl 은 매도 수수료 차감 후, unrealized_pnl 은 매수 수수료 포함 평단 기준.
        net_pnl = realized + unrealized 이고, KRW 환전 손실이 없으면 equity_change 와 같다(항등식)."""
        v = self.valuation()
        fills = self.state["fills"]
        realized = sum(f.get("realizedPnl", 0.0) for f in fills)
        commissions = sum(f.get("commission", 0.0) for f in fills)
        buy_comm = sum(f.get("commission", 0.0) for f in fills if f["side"] == "BUY")
        sell_comm = commissions - buy_comm
        bought = sum(f["grossValue"] for f in fills if f["side"] == "BUY")
        sold = sum(f["grossValue"] for f in fills if f["side"] == "SELL")
        initial = float(self.cfg["initial_cash_usd"]) + float(self.cfg["initial_cash_krw"]) / v["fx_rate"]
        fx_cost = sum((float(e.get("krw", 0)) / v["fx_rate"]) - float(e.get("usd", 0))
                      for e in self.state.get("fx_events", []))      # 환전 스프레드로 새어나간 USD 환산액
        return {
            "ts": v["ts"], "session": self.session_now(),
            "cash_usd": v["cash_usd"], "cash_krw": v["cash_krw"],
            "buying_power_usd": self.buying_power("USD"),
            "positions_count": len(self.state["positions"]),
            "positions_value": v["positions_value"], "equity_usd": v["equity_usd"],
            "initial_equity_usd": initial,
            "equity_change": v["equity_usd"] - initial,
            "return_pct": ((v["equity_usd"] / initial - 1) * 100) if initial else 0.0,
            "unrealized_pnl": v["unrealized"], "realized_pnl": realized,
            "net_pnl": realized + v["unrealized"],
            "commissions": commissions, "buy_commissions": buy_comm, "sell_commissions": sell_comm,
            "fx_cost_usd": fx_cost,
            "total_bought": bought, "total_sold": sold,
            "open_orders": len(self.state["open_orders"]),
            "fills": len(fills), "rejected": len(self.state["rejected"]),
            "max_drawdown_pct": self.max_drawdown() * 100,
            "last_run_asof": self.get_meta("last_run_asof"),
            "last_run_ts": self.get_meta("last_run_ts"),
            "created": self.state.get("created"),
            "positions": v["positions"],
        }

    def to_frames(self):
        """(positions_df, fills_df, equity_df) — 영문 snake_case 컬럼."""
        import pandas as pd
        v = self.valuation()
        rows = []
        for sym, d in v["positions"].items():
            rows.append({"symbol": sym, "qty": d["qty"], "avg_price": d["avg_price"],
                         "price": d["price"], "value": d["value"], "unrealized": d["unrealized"],
                         "pnl_pct": d["pnl_pct"], "opened": d["opened"], "stale": d["stale"]})
        pos_cols = ["symbol", "qty", "avg_price", "price", "value", "unrealized", "pnl_pct", "opened", "stale"]
        positions = pd.DataFrame(rows, columns=pos_cols)
        fill_cols = ["ts", "orderId", "clientOrderId", "symbol", "side", "orderType", "qty",
                     "fillPrice", "quotePrice", "grossValue", "commission", "realizedPnl",
                     "session", "note"]
        fills = pd.DataFrame(self.state["fills"], columns=fill_cols)
        eq_cols = ["ts", "equity_usd", "cash_usd", "cash_krw", "positions_value"]
        equity = pd.DataFrame(self.state["equity_history"], columns=eq_cols)
        if len(equity):
            peak = equity["equity_usd"].cummax()
            equity["drawdown_pct"] = ((equity["equity_usd"] / peak - 1) * 100).round(3)
        else:
            equity["drawdown_pct"] = []
        return positions, fills, equity

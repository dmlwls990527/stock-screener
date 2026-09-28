#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rules.py — 후보 선정 + 주문 크기 규칙 (auto_buy.py 와 replay.py 가 같이 쓴다).

I/O 는 load_watchlist(엑셀 읽기)만 하고, select_candidates / size_orders 는 순수 함수다.

cfg 는 auto_buy_config.json 전체 dict (source / sizing / paper 섹션을 읽는다).

size_orders 의 반환값 OrderPlan 은 list 의 하위 클래스 — 그대로 순회하면 주문 의도
(dict) 들이고, .skipped 에 건너뛴 후보와 사유, .cash_before / .cash_after 가 붙어 있다.

주문 의도 dict:
  {symbol, name, side:'BUY', order_type:'MARKET'|'LIMIT', quantity|None, order_amount|None,
   price|None, quote, est_usd, score, reason, client_order_id|None}

현금 버퍼: 브로커(broker_paper.place_order)가 요구하는 금액과 같은 식을 쓴다.
  수량주문 필요금액 = qty × 시세 × (1+슬리피지) × (1+수수료)  →  buffer = (1+c)(1+s) − 1
  (금액주문·LIMIT 은 슬리피지가 없어 이 버퍼가 약간 보수적이지만, 계획에서 OK 인 주문이
   브로커에서 insufficient-buying-power 로 거절되는 일은 없다)
"""
import math

import pandas as pd

COL_TICKER = "티커"
COL_NAME = "종목명"
COL_SECTOR = "섹터"
COL_WARN = "주의"

REASON_NO_QUOTE = "시세 없음"
REASON_PENDING = "미체결 매수 대기"
REASON_HELD = "이미 보유 (skip_if_held)"

_DEFAULT_SOURCE = {"sheet": "주도주", "sort_by": "주도주점수", "top_n": 5,
                   "exclude_if_주의": True, "exclude_sectors": [], "exclude_tickers": []}
_DEFAULT_SIZING = {"per_stock_usd": 1000, "max_positions": 10, "weekly_cap_usd": 3000,
                   "skip_if_held": True, "order_type": "MARKET", "use_amount_orders": True,
                   "limit_offset_pct": 0.5, "min_order_usd": 50}


def _section(cfg, name, defaults):
    """cfg 가 dict 이든 속성 객체이든 섹션을 dict 로 꺼낸다 (기본값 병합)."""
    sec = None
    if isinstance(cfg, dict):
        sec = cfg.get(name)
    else:
        sec = getattr(cfg, name, None)
    out = dict(defaults)
    if isinstance(sec, dict):
        out.update(sec)
    elif sec is not None:
        for k in defaults:
            if hasattr(sec, k):
                out[k] = getattr(sec, k)
    return out


def _int(sec, section, key):
    """설정값을 정수로. 아니면 원인이 보이는 ValueError (트레이스백 대신 한 줄 메시지용)."""
    v = sec.get(key)
    try:
        if isinstance(v, bool):
            raise ValueError
        f = float(v)
        if f != int(f):
            raise ValueError
        return int(f)
    except (TypeError, ValueError):
        raise ValueError(f"설정 {section}.{key} 는 정수여야 함: {v!r}") from None


def _float(sec, section, key):
    v = sec.get(key)
    try:
        if isinstance(v, bool):
            raise ValueError
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"설정 {section}.{key} 는 숫자여야 함: {v!r}") from None


def _is_blank(v):
    if v is None:
        return True
    try:
        if isinstance(v, float) and math.isnan(v):
            return True
    except Exception:
        pass
    return str(v).strip() == "" or str(v).strip().lower() == "nan"


# ── 워치리스트 ─────────────────────────────────────────────────────────────
def load_watchlist(path, sheet="주도주"):
    """엑셀 → (DataFrame, 기준일 'YYYY-MM-DD'). 기준일은 '설명' 시트의 항목=='기준일' 행.
    파일 없음 → FileNotFoundError, 시트 없음 → ValueError (호출 쪽에서 한 줄 메시지로 처리)."""
    with pd.ExcelFile(path) as xl:
        if sheet not in xl.sheet_names:
            raise ValueError(f"시트 '{sheet}' 가 없음 (있는 시트: {', '.join(xl.sheet_names)})")
        df = xl.parse(sheet)
        asof = None
        if "설명" in xl.sheet_names:
            desc = xl.parse("설명")
            if "항목" in desc.columns and "값" in desc.columns:
                hit = desc[desc["항목"].astype(str).str.strip() == "기준일"]
                if len(hit):
                    v = hit["값"].iloc[0]
                    asof = v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v).strip()[:10]
    return df, asof


# ── 후보 선정 ─────────────────────────────────────────────────────────────
def select_candidates(df, cfg, return_dropped=False):
    """정렬(sort_by desc) → 티커 없는 행 제외 → 주의 제외 → 섹터/티커 제외 → top_n.
    반환: [{symbol, name, score, reason, sector, rank}] (return_dropped=True 면 (cands, dropped))."""
    src = _section(cfg, "source", _DEFAULT_SOURCE)
    sort_by = src["sort_by"]
    top_n = _int(src, "source", "top_n")
    excl_sec = {str(s).strip() for s in (src.get("exclude_sectors") or [])}
    excl_tic = {str(t).strip().upper() for t in (src.get("exclude_tickers") or [])}
    excl_warn = bool(src.get("exclude_if_주의", True))

    if df is None or len(df) == 0 or COL_TICKER not in df.columns:
        return ([], []) if return_dropped else []
    d = df.copy()
    if sort_by in d.columns:
        d[sort_by] = pd.to_numeric(d[sort_by], errors="coerce")
        d = d.sort_values(sort_by, ascending=False, na_position="last", kind="mergesort")
    d = d.reset_index(drop=True)

    cands, dropped = [], []
    for i, row in d.iterrows():
        raw_sym = row[COL_TICKER]
        if _is_blank(raw_sym):                       # 빈 티커 행은 top_n 자리를 먹지 않게 먼저 뺀다
            dropped.append({"symbol": "", "name": "", "sector": "", "score": None, "rank": i + 1,
                            "reason": "티커 없음"})
            continue
        sym = str(raw_sym).strip().upper()
        name = str(row[COL_NAME]) if COL_NAME in d.columns and not _is_blank(row.get(COL_NAME)) else sym
        sector = str(row[COL_SECTOR]) if COL_SECTOR in d.columns and not _is_blank(row.get(COL_SECTOR)) else ""
        score = row[sort_by] if sort_by in d.columns else None
        score = None if _is_blank(score) else float(score)
        warn = row.get(COL_WARN) if COL_WARN in d.columns else None
        base = {"symbol": sym, "name": name, "sector": sector, "score": score, "rank": i + 1}
        if excl_warn and not _is_blank(warn):
            dropped.append({**base, "reason": f"주의: {str(warn).strip()}"})
            continue
        if sector in excl_sec:
            dropped.append({**base, "reason": f"제외 섹터: {sector}"})
            continue
        if sym in excl_tic:
            dropped.append({**base, "reason": "제외 티커"})
            continue
        if len(cands) >= top_n:
            dropped.append({**base, "reason": f"top_n({top_n}) 초과"})
            continue
        sc = f"{score:.3f}" if score is not None else "-"
        cands.append({**base, "reason": f"{sort_by} {sc} (정렬 {i + 1}위)"})
    return (cands, dropped) if return_dropped else cands


# ── 주문 크기 ─────────────────────────────────────────────────────────────
class OrderPlan(list):
    """주문 의도 리스트 + .skipped / .cash_before / .cash_after."""

    def __init__(self, orders=(), skipped=None, cash_before=0.0, cash_after=0.0):
        super().__init__(orders)
        self.skipped = list(skipped or [])
        self.cash_before = float(cash_before)
        self.cash_after = float(cash_after)

    @property
    def total_usd(self):
        return sum(o.get("est_usd", 0.0) for o in self)

    def as_dict(self):
        return {"orders": list(self), "skipped": list(self.skipped),
                "cash_before": self.cash_before, "cash_after": self.cash_after,
                "total_usd": self.total_usd}


def _round_tick(price):
    tick = 0.0001 if price < 1 else 0.01
    return round(round(price / tick) * tick, 4 if tick < 0.01 else 2)


def cash_buffer_pct(cfg):
    """브로커와 같은 식: (1+수수료)(1+슬리피지) − 1."""
    paper = _section(cfg, "paper", {"commission_pct": 0.1, "slippage_bps": 5})
    comm = float(paper.get("commission_pct", 0.1)) / 100.0
    slip = float(paper.get("slippage_bps", 5)) / 1e4
    return (1 + comm) * (1 + slip) - 1


_DONE_STATUSES = {"FILLED", "CANCELLED", "CANCELED", "EXPIRED", "REJECTED"}


def _pending_buys(open_orders):
    for o in open_orders or []:
        if not isinstance(o, dict):
            continue
        if str(o.get("side", "")).upper() != "BUY":
            continue
        if str(o.get("status", "OPEN")).upper() in _DONE_STATUSES:
            continue
        if _is_blank(o.get("symbol")):
            continue
        yield o


def pending_buy_symbols(open_orders):
    """브로커 open_orders() (paper: symbol/side/status, live: normalize_order 와 같은 키) → 매수 대기 심볼."""
    return {str(o["symbol"]).strip().upper() for o in _pending_buys(open_orders)}


def pending_buy_usd(open_orders):
    """미체결 매수 주문이 잡아둔 금액(USD) — 주간 한도(weekly_cap_usd)에서 미리 뺀다.
    금액주문이면 orderAmount/amount, 아니면 quantity×price. 둘 다 없으면 0."""
    total = 0.0
    for o in _pending_buys(open_orders):
        amt = o.get("orderAmount", o.get("amount"))
        try:
            if amt is not None and float(amt) > 0:
                total += float(amt)
                continue
            q, p = o.get("quantity"), o.get("price")
            if q is not None and p is not None:
                total += float(q) * float(p)
        except (TypeError, ValueError):
            continue
    return total


def size_orders(candidates, cash_usd, holdings, cfg, quotes, client_id_prefix=None, open_orders=None):
    """후보 → 주문 의도. 규칙 순서:
      미체결 매수 대기 → skip_if_held → max_positions(보유+미체결+신규, 새 종목만 소비)
      → weekly_cap_usd(미체결 매수 예약금은 이미 쓴 것으로) → per_stock_usd(현금 한도 내)
      → MARKET 이면 금액주문(use_amount_orders) 또는 qty=floor(금액/시세)
      → LIMIT 이면 price = 시세×(1+limit_offset_pct%) 호가 반올림, qty=floor(금액/price)
      → min_order_usd 미만이면 건너뜀.
    quotes: {symbol: price}. holdings: {symbol: {qty, avg_price}}. open_orders: 브로커 open_orders() 결과.
    수수료·슬리피지 버퍼는 브로커와 같은 식 (1+c)(1+s)−1 로 현금에서 미리 뺀다."""
    sz = _section(cfg, "sizing", _DEFAULT_SIZING)
    per_stock = _float(sz, "sizing", "per_stock_usd")
    max_pos = _int(sz, "sizing", "max_positions")
    weekly_cap = _float(sz, "sizing", "weekly_cap_usd")
    skip_if_held = bool(sz["skip_if_held"])
    order_type = str(sz["order_type"]).upper()
    use_amount = bool(sz["use_amount_orders"])
    limit_off = _float(sz, "sizing", "limit_offset_pct") / 100.0
    min_order = _float(sz, "sizing", "min_order_usd")
    buffer_pct = cash_buffer_pct(cfg)

    held = {str(s).upper() for s, p in (holdings or {}).items() if float((p or {}).get("qty", 0)) > 0}
    pending = pending_buy_symbols(open_orders)
    pending_usd = pending_buy_usd(open_orders)
    quotes = {str(k).upper(): v for k, v in (quotes or {}).items()}
    cash = float(cash_usd or 0.0)
    cap_left = weekly_cap - pending_usd        # 미체결 매수가 잡아둔 금액은 이번 주 한도에서 이미 쓴 것으로
    n_pos = len(held | pending)
    n_new = 0
    orders, skipped = [], []

    for c in candidates:
        sym = str(c["symbol"]).upper()
        base = {k: c.get(k) for k in ("symbol", "name", "score", "sector", "rank")}
        base["symbol"] = sym
        if sym in pending:
            skipped.append({**base, "reason": f"{REASON_PENDING} (미체결 매수 주문 있음)"})
            continue
        if skip_if_held and sym in held:
            skipped.append({**base, "reason": REASON_HELD})
            continue
        is_new = sym not in held            # 보유 종목 추가 매수(skip_if_held=false)는 자리를 안 먹는다
        if is_new and n_pos >= max_pos:
            skipped.append({**base, "reason": f"max_positions({max_pos}) 도달 (보유 {len(held)} + 미체결 "
                                              f"{len(pending - held)} + 신규 {n_new})"})
            continue
        if cap_left < min_order:
            skipped.append({**base, "reason": f"weekly_cap_usd 소진 (남은 ${cap_left:,.2f}"
                                              + (f", 미체결 예약 ${pending_usd:,.2f}" if pending_usd else "") + ")"})
            continue
        q = quotes.get(sym)
        try:
            q = float(q) if q is not None else None
        except Exception:
            q = None
        if q is None or not (q > 0):
            skipped.append({**base, "reason": REASON_NO_QUOTE})
            continue
        spendable = cash / (1 + buffer_pct)
        budget = min(per_stock, cap_left, spendable)
        if budget < min_order:
            skipped.append({**base, "reason": f"예산 ${budget:,.2f} < min_order_usd ${min_order:,.2f} (현금 ${cash:,.2f}, 주간한도잔여 ${cap_left:,.2f})"})
            continue

        intent = {**base, "side": "BUY", "order_type": order_type, "quantity": None,
                  "order_amount": None, "price": None, "quote": q, "est_usd": 0.0,
                  "reason": c.get("reason", ""), "client_order_id": None}
        if order_type == "MARKET":
            if use_amount:
                amt = math.floor(budget * 100) / 100.0
                intent["order_amount"] = amt
                intent["est_usd"] = amt
                intent["est_qty"] = math.floor(amt / q * 1e6) / 1e6
            else:
                qty = math.floor(budget / q)
                if qty < 1:
                    skipped.append({**base, "reason": f"1주 가격 ${q:,.2f} > 예산 ${budget:,.2f} (금액주문 꺼짐)"})
                    continue
                intent["quantity"] = qty
                intent["est_usd"] = qty * q
        elif order_type == "LIMIT":
            px = _round_tick(q * (1 + limit_off))
            qty = math.floor(budget / px)
            if qty < 1:
                skipped.append({**base, "reason": f"지정가 ${px:,.2f} > 예산 ${budget:,.2f}"})
                continue
            intent["price"] = px
            intent["quantity"] = qty
            intent["est_usd"] = qty * px
        else:
            skipped.append({**base, "reason": f"알 수 없는 order_type {order_type}"})
            continue
        if intent["est_usd"] < min_order:
            skipped.append({**base, "reason": f"주문금액 ${intent['est_usd']:,.2f} < min_order_usd"})
            continue
        if client_id_prefix:
            intent["client_order_id"] = f"{client_id_prefix}-{sym}"[:36]
        cost = intent["est_usd"] * (1 + buffer_pct)          # 브로커 필요금액과 같은 식
        cash -= cost
        cap_left -= intent["est_usd"]
        if is_new:
            n_pos += 1
            n_new += 1
        orders.append(intent)

    return OrderPlan(orders, skipped, cash_before=float(cash_usd or 0.0), cash_after=cash)

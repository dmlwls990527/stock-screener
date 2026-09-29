#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strategy.py — 자동매수 v2 규칙 (2026-09-28 사용자 확정). auto_buy.py 와 replay.py 가 같이 쓴다.
I/O 없는 순수 함수만 둔다. 설정은 auto_buy_config.json 의 sizing / rebalance / exit 섹션.

규칙
  대상      : 주도주 시트 상위 source.top_n(50)종 — rules.select_candidates 결과 그대로
  목표비중  : w_i = 주도주점수_i / Σ(목록 안 점수).  목표금액_i = w_i × 총자산(현금 + 보유 평가액)
  신규 매수 : 목록에 있고 안 가진 종목 → 목표금액만큼 금액주문(소수점).
              현금이 모자라면 신규 종목끼리 비중대로 똑같이 줄인다.
  보유 갱신 : rebalance.mode
                A = 안 건드림
                B = 추가매수만 (기본) — 목표 − 보유 ≥ 목표의 topup_threshold_pct(30)% 이고
                    ≥ min_order_usd 면 차이만큼 더 산다. 줄이지는 않는다.
                C = 양방향 — B 에 더해 보유 − 목표 ≥ 목표의 30% 면 초과분을 판다.
              현금 우선순위: 신규 종목 먼저, 남으면 추가매수. 둘 다 점수 순.
              목록에서 빠진 보유 종목은 추가매수하지 않는다 (매도 규칙을 기다린다).
  매도      : ① 추적손절 — 보유 후 최고가(high_water) 대비 trailing_stop_pct(15)% 이하로 떨어짐
              ② 게이트 탈락 — 주도주 시트(게이트 통과 전체, top_n 아님)에서 gate_absent_weeks(2)회 연속 빠짐
              먼저 걸린 쪽이 pending_exit 로 표시되고, 다음 정규장 진입 시각에 전량 시장가로 판다.
  재매수    : 쿨다운 없음. 같은 실행 안에서 판 종목만 다시 사지 않는다.

exit_state — 브로커 meta 'exit_state' 에 저장 (paper: 상태 JSON, live: paper/live_meta.json)
  {SYM: {"high_water": float, "absent_weeks": int, "pending_exit": {"reason": str, "ts": str} | None}}
  high_water 초기값은 평균단가(매수 수수료 포함). 이후 run / exits / tick 에서 본 시세의 최댓값.
"""
import math
import re

from rules import (OrderPlan, REASON_NO_QUOTE, REASON_PENDING, _section, _float, _int,
                   cash_buffer_pct, pending_buy_symbols)

DEFAULT_REBALANCE = {"mode": "B", "topup_threshold_pct": 30, "every_weeks": 1}
DEFAULT_EXIT = {"enabled": True, "trailing_stop_pct": 15, "gate_absent_weeks": 2, "stop_type": "trailing"}
DEFAULT_SIZING = {"max_positions": 50, "min_order_usd": 20}

REASON_TRAIL = "추적손절"          # 보유 후 최고가 대비
REASON_FIXED = "손절"              # 매입가(평균단가) 대비
REASON_GATE = "게이트탈락"
REASON_TRIM = "비중초과"
REASON_SOLD_NOW = "이번 실행에서 매도됨 → 재매수 안 함"


def floor2(x):
    return math.floor(float(x) * 100 + 1e-9) / 100.0


def floor6(x):
    return math.floor(float(x) * 1e6 + 1e-9) / 1e6


def client_id(prefix, sym):
    """토스 clientOrderId 규칙(^[a-zA-Z0-9_-]+$, 36자)에 맞춘다. BRK.B 같은 점 티커 대비."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{prefix}-{sym}")[:36]


def exit_cfg(cfg):
    ex = _section(cfg, "exit", DEFAULT_EXIT)
    st = str(ex.get("stop_type", "trailing")).lower()
    if st not in ("trailing", "fixed"):
        raise ValueError(f"설정 exit.stop_type 은 trailing(최고가 대비) 또는 fixed(매입가 대비): {st!r}")
    return {"enabled": bool(ex.get("enabled", True)),
            "trailing_stop_pct": _float(ex, "exit", "trailing_stop_pct"),
            "gate_absent_weeks": max(1, _int(ex, "exit", "gate_absent_weeks")),
            "stop_type": st}


def every_weeks(cfg):
    """리밸런스 주기(주). 1 = 매주. 기준일(금요일 워치리스트)이 이만큼 지나야 다음 리밸런스."""
    v = _section(cfg, "rebalance", DEFAULT_REBALANCE).get("every_weeks", 1)
    try:
        n = int(v or 1)
    except (TypeError, ValueError):
        raise ValueError(f"설정 rebalance.every_weeks 는 정수여야 함: {v!r}") from None
    return max(1, n)


def rebalance_due(cfg, asof, last_rebalance_asof):
    """(이번 기준일이 리밸런스 주인가, 아니면 그 사유). 공휴일로 기준일이 며칠 당겨져도 인정(−3일)."""
    n = every_weeks(cfg)
    if n <= 1 or not last_rebalance_asof:
        return True, None
    from datetime import date
    d = (date.fromisoformat(str(asof)[:10]) - date.fromisoformat(str(last_rebalance_asof)[:10])).days
    if d >= n * 7 - 3:
        return True, None
    return False, f"리밸런스 주 아님 (마지막 리밸런스 기준일 {last_rebalance_asof}, {n}주마다, {d}일 경과)"


def rebalance_mode(cfg):
    m = str(_section(cfg, "rebalance", DEFAULT_REBALANCE).get("mode", "B")).upper()
    if m not in ("A", "B", "C"):
        raise ValueError(f"설정 rebalance.mode 는 A/B/C 중 하나: {m!r}")
    return m


# ── 목표비중 ───────────────────────────────────────────────────────────────
def weighting(cfg):
    w = str(_section(cfg, "sizing", {"weighting": "score"}).get("weighting", "score")).lower()
    if w not in ("score", "equal", "rank"):
        raise ValueError(f"설정 sizing.weighting 은 score / equal / rank: {w!r}")
    return w


def target_weights(cands, method="score"):
    """{SYM: 비중} (합 1). method=equal 이면 균등.
    score: 점수가 없거나 0 이하인 종목은 목록 최저 양수점수의 10% 로 둔다. 점수가 하나도 없으면 균등."""
    if not cands:
        return {}
    if method == "equal":
        return {str(c["symbol"]).upper(): 1.0 / len(cands) for c in cands}
    if method == "rank":        # 1위 n, 2위 n−1 … n위 1 (목록 순서 = 순위)
        n = len(cands)
        tot = n * (n + 1) / 2
        return {str(c["symbol"]).upper(): (n - i) / tot for i, c in enumerate(cands)}
    scores = []
    for c in cands:
        try:
            s = float(c.get("score"))
        except (TypeError, ValueError):
            s = None
        scores.append(s if (s is not None and s == s and s > 0) else None)
    valid = [s for s in scores if s is not None]
    if not valid:
        w = 1.0 / len(cands)
        return {str(c["symbol"]).upper(): w for c in cands}
    floor_score = min(valid) * 0.1
    vals = [s if s is not None else floor_score for s in scores]
    tot = sum(vals)
    return {str(c["symbol"]).upper(): v / tot for c, v in zip(cands, vals)}


def position_values(holdings, quotes):
    """{SYM: 평가액}. 시세가 없으면 마지막 가격 → 평균단가 순으로 대신 쓴다."""
    out = {}
    for sym, p in (holdings or {}).items():
        q = (quotes or {}).get(sym)
        if not q:
            q = p.get("last_price") or p.get("avg_price") or 0.0
        out[str(sym).upper()] = float(p.get("qty", 0)) * float(q)
    return out


# ── 매도 조건 상태 ──────────────────────────────────────────────────────────
def sync_exit_state(es, holdings):
    """보유 종목만 남기고, 새 보유 종목은 high_water=평균단가로 시작. es 를 제자리에서 고치고 돌려준다."""
    held = {str(s).upper(): p for s, p in (holdings or {}).items() if float((p or {}).get("qty", 0)) > 0}
    for sym in list(es):
        if sym not in held:
            es.pop(sym)
    for sym, p in held.items():
        st = es.setdefault(sym, {"high_water": None, "absent_weeks": 0, "pending_exit": None})
        st.setdefault("absent_weeks", 0)
        st.setdefault("pending_exit", None)
        ap0 = float(p.get("avg_price") or 0.0)
        if ap0 > 0:
            st["entry"] = ap0            # 고정 손절 기준 (추가매수하면 평균단가를 따라감)
        if not st.get("high_water"):
            ap = float(p.get("avg_price") or 0.0)
            st["high_water"] = ap if ap > 0 else None
    return es


def mark_high_water(es, quotes):
    """본 시세로 최고가 갱신. 시세가 없으면 그대로 둔다 (0 이나 None 으로 덮지 않음)."""
    for sym, st in es.items():
        q = (quotes or {}).get(sym)
        if q and q > 0:
            hw = st.get("high_water") or q
            st["high_water"] = max(float(hw), float(q))


def stop_price(st, pct, stop_type="trailing"):
    """손절가. trailing = 최고가×(1−pct%), fixed = 매입가(평균단가)×(1−pct%). pct 0 이하/100 이상이면 None."""
    if not pct or pct <= 0 or pct >= 100:
        return None
    base = st.get("entry") if stop_type == "fixed" else st.get("high_water")
    return float(base) * (1 - pct / 100.0) if base else None


def check_trailing(es, quotes, pct, ts, stop_type="trailing"):
    """시세 ≤ 최고가×(1−pct%) 이면 pending_exit 표시. 새로 걸린 종목 리스트를 돌려준다.
    pct 가 0 이하(또는 100 이상)면 추적손절 끔."""
    hits = []
    if not pct or pct <= 0 or pct >= 100:
        return hits
    for sym, st in es.items():
        if st.get("pending_exit"):
            continue
        q = (quotes or {}).get(sym)
        sp = stop_price(st, pct, stop_type)
        if not q or sp is None:
            continue
        if q <= sp:
            if stop_type == "fixed":
                why = f"{REASON_FIXED} −{pct:g}% (매입가 {st['entry']:.2f} → {q:.2f}, 손절가 {sp:.2f})"
            else:
                why = f"{REASON_TRAIL} −{pct:g}% (최고 {st['high_water']:.2f} → {q:.2f}, 손절가 {sp:.2f})"
            st["pending_exit"] = {"reason": why, "ts": ts}
            hits.append(sym)
    return hits


def update_gate_absence(es, listed, weeks, ts):
    """주간 run 에서만 부른다. listed = 주도주 시트 전체 티커(게이트 통과 목록).
    있으면 0 으로, 없으면 +1. weeks 회 연속이면 pending_exit. 새로 걸린 종목 리스트."""
    listed = {str(s).upper() for s in (listed or ())}
    hits = []
    for sym, st in es.items():
        if sym in listed:
            st["absent_weeks"] = 0
            continue
        st["absent_weeks"] = int(st.get("absent_weeks") or 0) + 1
        if st["absent_weeks"] >= weeks and not st.get("pending_exit"):
            st["pending_exit"] = {"reason": f"{REASON_GATE} {st['absent_weeks']}주 연속", "ts": ts}
            hits.append(sym)
    return hits


def pending_exits(es, holdings):
    return [s for s, st in es.items() if st.get("pending_exit") and s in (holdings or {})]


# ── 주문 계획 ──────────────────────────────────────────────────────────────
def _base(c):
    return {"symbol": str(c["symbol"]).upper(), "name": c.get("name"), "score": c.get("score"),
            "sector": c.get("sector"), "rank": c.get("rank")}


def plan_trims(cands, holdings, quotes, cash_usd, cfg, asof, exclude=()):
    """mode C 전용: 보유 − 목표 ≥ 목표×임계% 인 종목의 초과분 시장가 매도 의도."""
    if rebalance_mode(cfg) != "C":
        return []
    reb = _section(cfg, "rebalance", DEFAULT_REBALANCE)
    sz = _section(cfg, "sizing", DEFAULT_SIZING)
    thr = _float(reb, "rebalance", "topup_threshold_pct") / 100.0
    min_order = _float(sz, "sizing", "min_order_usd")
    w = target_weights(cands, weighting(cfg))
    vals = position_values(holdings, quotes)
    equity = float(cash_usd or 0.0) + sum(vals.values())
    out = []
    for c in cands:
        sym = str(c["symbol"]).upper()
        if sym not in holdings or sym in exclude:
            continue
        q = (quotes or {}).get(sym)
        if not q:
            continue
        target = w[sym] * equity
        excess = vals[sym] - target
        if excess < thr * target or excess < min_order:
            continue
        qty = floor6(min(excess / q, float(holdings[sym]["qty"])))
        if qty <= 0:
            continue
        out.append({**_base(c), "side": "SELL", "order_type": "MARKET", "quantity": qty,
                    "order_amount": None, "price": None, "quote": q, "est_usd": qty * q,
                    "kind": "trim", "target_weight": w[sym], "target_usd": target,
                    "reason": f"{REASON_TRIM}: 목표 ${target:,.2f} 보유 ${vals[sym]:,.2f} "
                              f"(+{excess / target * 100:.0f}%) → ${qty * q:,.2f} 매도",
                    "client_order_id": client_id(f"at-{asof}", sym)})
    return out


def plan_buys(cands, holdings, quotes, cash_usd, cfg, asof, open_orders=None, exclude=()):
    """신규(점수 비중, 현금 부족 시 비례 축소) → 추가매수(mode B/C). OrderPlan 을 돌려준다.
    plan.weights / plan.targets / plan.values / plan.equity / plan.scale 이 붙는다."""
    sz = _section(cfg, "sizing", DEFAULT_SIZING)
    reb = _section(cfg, "rebalance", DEFAULT_REBALANCE)
    mode = rebalance_mode(cfg)
    thr = _float(reb, "rebalance", "topup_threshold_pct") / 100.0
    min_order = _float(sz, "sizing", "min_order_usd")
    max_pos = _int(sz, "sizing", "max_positions")
    buf = cash_buffer_pct(cfg)
    exclude = {str(s).upper() for s in (exclude or ())}

    holdings = {str(s).upper(): p for s, p in (holdings or {}).items() if float((p or {}).get("qty", 0)) > 0}
    quotes = {str(k).upper(): float(v) for k, v in (quotes or {}).items() if v}
    held = set(holdings)
    pending = pending_buy_symbols(open_orders)
    w = target_weights(cands, weighting(cfg))
    vals = position_values(holdings, quotes)
    cash = float(cash_usd or 0.0)
    equity = cash + sum(vals.values())
    targets = {s: w[s] * equity for s in w}
    spendable = cash / (1 + buf)
    orders, skipped = [], []

    def intent(c, q, amt, kind, reason):
        sym = str(c["symbol"]).upper()
        return {**_base(c), "side": "BUY", "order_type": "MARKET", "quantity": None,
                "order_amount": amt, "price": None, "quote": q, "est_usd": amt,
                "est_qty": floor6(amt / q), "kind": kind, "target_weight": w[sym],
                "target_usd": targets[sym], "reason": reason,
                "client_order_id": client_id(f"ab-{asof}", sym)}

    # ① 신규 진입
    new = []
    n_pos = len(held | pending)
    for c in cands:
        sym = str(c["symbol"]).upper()
        if sym in held:
            continue
        if sym in exclude:
            skipped.append({**_base(c), "reason": REASON_SOLD_NOW})
            continue
        if sym in pending:
            skipped.append({**_base(c), "reason": f"{REASON_PENDING} (미체결 매수 주문 있음)"})
            continue
        q = quotes.get(sym)
        if not q:
            skipped.append({**_base(c), "reason": REASON_NO_QUOTE})
            continue
        if n_pos >= max_pos:
            skipped.append({**_base(c), "reason": f"max_positions({max_pos}) 도달"})
            continue
        new.append((c, q))
        n_pos += 1
    need = sum(targets[str(c["symbol"]).upper()] for c, _ in new)
    scale = min(1.0, spendable / need) if need > 0 else 0.0
    for c, q in new:
        sym = str(c["symbol"]).upper()
        amt = floor2(targets[sym] * scale)
        if amt < min_order:
            skipped.append({**_base(c), "reason": f"배정액 ${amt:,.2f} < min_order_usd ${min_order:,.2f}"
                                                  + (f" (현금 부족으로 {scale * 100:.1f}% 축소)" if scale < 0.99 else "")})
            continue
        amt = min(amt, floor2(spendable))
        if amt < min_order:
            skipped.append({**_base(c), "reason": "현금 소진"})
            continue
        r = f"신규 목표비중 {w[sym] * 100:.2f}% 목표 ${targets[sym]:,.2f}"
        if scale < 0.99:                                   # 수수료·슬리피지 버퍼(≈0.15%)만큼은 표시 안 함
            r += f" → 현금 부족으로 {scale * 100:.1f}% 축소"
        orders.append(intent(c, q, amt, "new", r))
        spendable -= amt

    # ② 추가매수 (B/C)
    if mode in ("B", "C"):
        for c in cands:
            sym = str(c["symbol"]).upper()
            if sym not in held or sym in exclude or sym in pending:
                continue
            q = quotes.get(sym)
            if not q:
                skipped.append({**_base(c), "reason": f"{REASON_NO_QUOTE} (추가매수 판단 불가)"})
                continue
            target, have = targets[sym], vals.get(sym, 0.0)
            gap = target - have
            if gap < thr * target or gap < min_order:
                continue
            if spendable < min_order:
                skipped.append({**_base(c), "reason": f"추가매수 대기: 현금 소진 (부족분 ${gap:,.2f})"})
                continue
            amt = floor2(min(gap, spendable))
            orders.append(intent(c, q, amt, "topup",
                                 f"추가매수: 목표 ${target:,.2f} 보유 ${have:,.2f} "
                                 f"(−{gap / target * 100:.0f}%)" + (" 일부만" if amt < floor2(gap) else "")))
            spendable -= amt

    cash_after = cash - sum(o["est_usd"] for o in orders) * (1 + buf)
    plan = OrderPlan(orders, skipped, cash_before=cash, cash_after=cash_after)
    plan.weights, plan.targets, plan.values = w, targets, vals
    plan.equity, plan.scale, plan.mode = equity, scale, mode
    return plan


def is_v2(cfg):
    sz = cfg.get("sizing") if isinstance(cfg, dict) else None
    return str((sz or {}).get("method", "equal")).lower() == "score_weight"


def rule_label(cfg):
    """메뉴·리포트·로그에 찍는 한 줄 규칙 설명."""
    src = cfg.get("source") or {}
    if not is_v2(cfg):
        sz = cfg.get("sizing") or {}
        return (f"v1 균등 — 상위 {src.get('top_n')}종 종목당 ${sz.get('per_stock_usd')} "
                f"주간한도 ${sz.get('weekly_cap_usd')}")
    ex = exit_cfg(cfg)
    m = rebalance_mode(cfg)
    mname = {"A": "보유 유지", "B": "추가매수만", "C": "양방향 리밸런스"}[m]
    n = every_weeks(cfg)
    mname += " 매주" if n <= 1 else f" {n}주마다"
    if not ex["enabled"]:
        sell = "매도규칙 꺼짐"
    else:
        kind = "손절(매입가 대비)" if ex["stop_type"] == "fixed" else "추적손절"
        stop = (f"{kind} −{ex['trailing_stop_pct']:g}%" if 0 < ex["trailing_stop_pct"] < 100 else "손절 없음")
        gate = ("리밸런스 때 목록 밖이면 매도" if ex["gate_absent_weeks"] <= 1
                else f"게이트탈락 {ex['gate_absent_weeks']}회")
        sell = f"{stop} · {gate}"
    off = (cfg.get("schedule") or {}).get("entry_offset_min", 45)
    wname = {"equal": "균등가중", "rank": "순위가중", "score": "점수가중"}[weighting(cfg)]
    return f"v2 — 상위 {src.get('top_n')}종 {wname} · {mname}({m}) · {sell} · 정규장 +{off}분 시장가"

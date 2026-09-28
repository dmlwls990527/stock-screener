#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_buy.py — 주도주 워치리스트(leader_watchlist_latest.xlsx) → 자동 매수.

기본은 PAPER(모의) 모드: 실제 토스 시세·장 세션으로 가상 계좌(paper/paper_state.json)에
주문을 넣는다. 실계좌(LIVE)는 3중 잠금을 모두 풀어야만 동작한다:
  ① auto_buy_config.json 의 "mode": "live"
  ② 실행 플래그 --live
  ③ 환경변수 AUTO_BUY_LIVE_OK=1   (+ 터미널이면 'LIVE' 직접 타이핑)
셋 중 하나라도 빠지면 broker_live 를 import 조차 하지 않는다.

사용:
  ./.venv/bin/python auto_buy.py            # 번호 선택 메뉴
  ./.venv/bin/python auto_buy.py plan       # 주문 계획만 (상태 변경 없음)
  ./.venv/bin/python auto_buy.py run        # 주문 실행 (같은 기준일 재실행 거부, --force 로 강제)
  ./.venv/bin/python auto_buy.py tick       # 미체결 재검사 + 만료 + 자산 평가 기록
  ./.venv/bin/python auto_buy.py status
  ./.venv/bin/python auto_buy.py report     # paper_report_latest.xlsx
  ./.venv/bin/python auto_buy.py replay --start 2025-01-06
  ./.venv/bin/python auto_buy.py reset --yes
로그: logs/auto_buy_YYYYMMDD.log (매수/건너뜀/거절 사유 전부)
"""
import argparse
import copy
import json
import logging
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import rules                                     # noqa: E402
from broker_paper import (PaperBroker, KST, now_kst, session_from_calendar,   # noqa: E402
                          fmt_ts, StateFileError)

DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "auto_buy_config.json")
DEFAULT_CONFIG = {
    "mode": "paper",
    "source": {
        "file": "/data/frame/leader_watchlist_latest.xlsx",
        "sheet": "주도주",
        "sort_by": "주도주점수",
        "top_n": 5,
        "exclude_if_주의": True,
        "exclude_sectors": [],
        "exclude_tickers": [],
    },
    "sizing": {
        "per_stock_usd": 1000,
        "max_positions": 10,
        "weekly_cap_usd": 3000,
        "skip_if_held": True,
        "order_type": "MARKET",
        "use_amount_orders": True,
        "limit_offset_pct": 0.5,
        "min_order_usd": 50,
    },
    "exit": {"enabled": False, "rule": "drop_from_list", "weeks_absent": 2},
    "paper": {
        "initial_cash_usd": 10000,
        "initial_cash_krw": 0,
        "slippage_bps": 5,
        "commission_pct": 0.1,
        "fx_spread_pct_market": 0.05,
        "fx_spread_pct_off": 0.5,
        "auto_fx": False,
        "state_path": "/data/frame/paper/paper_state.json",
    },
    "schedule": {"place_at_session": "regularMarket"},
    "live": {"account_seq": None},
    # replay 전용: 분기 재무를 분기말(END_DATE) + N일 뒤에야 안 것으로 취급 (10-Q 제출 지연 흉내)
    "replay": {"financial_lag_days": 45},
}

log = logging.getLogger("auto_buy")


# ── 설정 ──────────────────────────────────────────────────────────────────
def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path=None):
    """설정 파일이 없으면 기본값으로 만든다. 있으면 기본값 위에 덮어쓴다(빠진 키 보충)."""
    path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        save_config(path, DEFAULT_CONFIG)
        log.info("설정 파일 생성: %s", path)
        return copy.deepcopy(DEFAULT_CONFIG), path
    with open(path, encoding="utf-8") as f:
        user = json.load(f)
    return deep_merge(DEFAULT_CONFIG, user), path


def save_config(path, cfg):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def mode_label(cfg):
    return "[LIVE 실계좌]" if str(cfg.get("mode", "paper")).lower() == "live" else "[PAPER 모의]"


def paper_dir(cfg):
    sp = cfg["paper"].get("state_path") or os.path.join(BASE_DIR, "paper", "paper_state.json")
    d = os.path.dirname(os.path.abspath(sp))
    os.makedirs(d, exist_ok=True)
    return d


# ── 로깅 ──────────────────────────────────────────────────────────────────
def setup_logging(log_dir=None):
    log_dir = log_dir or os.environ.get("AUTO_BUY_LOG_DIR") or os.path.join(BASE_DIR, "logs")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, f"auto_buy_{datetime.now(KST):%Y%m%d}.log"),
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as e:                       # 로그 디렉토리 못 만들어도 실행은 계속
        print(f"[warn] 로그 파일 생성 실패: {e}", file=sys.stderr)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(sh)
    return log


# ── 토스 시세 / 달력 (읽기 전용 호출만) ───────────────────────────────────
class TossQuotes:
    """get_prices 배치 호출 + 캐시. quote_fn(symbol) 로도, quotes(symbols) 로도 쓴다.
    시세 호출 제한(5/s)을 지키려고 묶음 사이에 0.25s 쉰다."""

    def __init__(self, ttl_sec=60, chunk=20):
        self.ttl = ttl_sec
        self.chunk = chunk
        self.cache = {}
        self.stamp = {}

    def prefetch(self, symbols):
        import toss_api
        need = [s.upper() for s in dict.fromkeys(symbols)
                if time.time() - self.stamp.get(s.upper(), 0) > self.ttl]
        for i in range(0, len(need), self.chunk):
            part = need[i:i + self.chunk]
            if i:
                time.sleep(0.25)
            try:
                rows = toss_api.get_prices(part)
            except Exception as e:
                log.warning("시세 조회 실패 %s: %s", part, e)
                continue
            for r in rows or []:
                try:
                    self.cache[str(r["symbol"]).upper()] = float(r["lastPrice"])
                    self.stamp[str(r["symbol"]).upper()] = time.time()
                except Exception:
                    pass
        return {s.upper(): self.cache.get(s.upper()) for s in symbols}

    def quotes(self, symbols):
        return self.prefetch(symbols)

    def __call__(self, symbol):
        s = symbol.upper()
        if time.time() - self.stamp.get(s, 0) > self.ttl:
            self.prefetch([s])
        if s not in self.cache:
            raise RuntimeError(f"시세 없음: {s}")
        return self.cache[s]


class TossSession:
    """GET /market-calendar/US 로 현재 세션 판정 (10분 캐시).
    toss_api.get_us_market_calendar 가 있으면 그것을, 없으면 _request 직접 호출."""

    def __init__(self, ttl_sec=600):
        self.ttl = ttl_sec
        self._cal = None
        self._stamp = 0

    def calendar(self):
        if self._cal is not None and time.time() - self._stamp < self.ttl:
            return self._cal
        import toss_api
        if hasattr(toss_api, "get_us_market_calendar"):
            cal = toss_api.get_us_market_calendar()
        else:
            cal = toss_api._request("GET", "/api/v1/market-calendar/US")
        if isinstance(cal, dict) and "result" in cal and isinstance(cal["result"], dict):
            cal = cal["result"]
        self._cal, self._stamp = cal, time.time()
        return cal

    def __call__(self):
        name, _ = session_from_calendar(self.calendar(), now_kst())
        return name


def toss_fx_rate():
    """toss_api.get_exchange_rate 가 있으면 KRW/USD 를 뽑아본다. 실패하면 None(설정값 사용)."""
    try:
        import toss_api
        if not hasattr(toss_api, "get_exchange_rate"):
            return None
        r = toss_api.get_exchange_rate()
        if isinstance(r, dict) and "result" in r:
            r = r["result"]
        items = r if isinstance(r, list) else [r]
        for it in items:
            if not isinstance(it, dict):
                continue
            cur = str(it.get("currency") or it.get("currencyCode") or "USD").upper()
            if cur != "USD":
                continue
            for k in ("rate", "exchangeRate", "basePrice", "price", "baseRate", "value"):
                if it.get(k) is not None:
                    return float(it[k])
    except Exception as e:
        log.warning("환율 조회 실패(설정값 사용): %s", e)
    return None


# ── 브로커 선택 ───────────────────────────────────────────────────────────
def make_paper_broker(cfg, quote_fn=None, session_fn=None, calendar_fn=None,
                      now_fn=None, fx_fn=None):
    if quote_fn is None:
        quote_fn = TossQuotes()
    if session_fn is None:
        ts = TossSession()
        session_fn = ts
        calendar_fn = calendar_fn or ts.calendar
    if fx_fn is None:
        fx_fn = toss_fx_rate
    state_path = cfg["paper"].get("state_path") or os.path.join(BASE_DIR, "paper", "paper_state.json")
    os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
    return PaperBroker(state_path, quote_fn, session_fn, cfg["paper"],
                       calendar_fn=calendar_fn, now_fn=now_fn, fx_fn=fx_fn)


def live_lock_status(cfg, args):
    """(live 여부, 거부 사유). 셋 다 맞아야 live: config mode, --live, (run 이면) env."""
    mode_live = str(cfg.get("mode", "paper")).lower() == "live"
    flag_live = bool(getattr(args, "live", False))
    if not mode_live and not flag_live:
        return False, None
    if mode_live and not flag_live:
        return False, "설정이 mode=live 인데 --live 플래그가 없음 → 차단 (페이퍼로 돌리려면 mode 를 paper 로)"
    if flag_live and not mode_live:
        return False, "--live 플래그가 있는데 설정 mode 가 live 가 아님 → 차단"
    if getattr(args, "cmd", "") == "run":
        if os.environ.get("AUTO_BUY_LIVE_OK") != "1":
            return False, "환경변수 AUTO_BUY_LIVE_OK=1 이 없음 → 실계좌 주문 차단"
    return True, None


def make_broker(cfg, args, inj):
    """inj: 테스트 주입용 {quote_fn, session_fn, calendar_fn, now_fn, fx_fn}."""
    is_live, why = live_lock_status(cfg, args)
    if why:
        raise PermissionError(why)
    if not is_live:
        return make_paper_broker(cfg, **inj), False
    if getattr(args, "cmd", "") == "run" and sys.stdin.isatty():
        typed = input("실계좌 주문입니다. 계속하려면 LIVE 를 입력: ")
        if typed.strip() != "LIVE":
            raise PermissionError("확인 문자열 불일치 → 실계좌 주문 차단")
    # ↓ 3중 잠금을 전부 통과한 경우에만 여기 도달 (broker_live 는 여기서만 import)
    from broker_live import LiveBroker
    seq = cfg.get("live", {}).get("account_seq")
    if seq is None:
        raise PermissionError("live.account_seq 가 설정에 없음")
    return LiveBroker(seq, allow_live=True), True


def _coerce_quotes(got):
    """{symbol: 값} → 양수 float 만 남긴다. 문자열/NaN/0/음수는 경고만 하고 그 종목만 뺀다."""
    out = {}
    for s, v in (got or {}).items():
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            log.warning("시세 값 오류 %s: %r (건너뜀)", s, v)
            continue
        if f != f or f <= 0:
            log.warning("시세 값 이상 %s: %r (건너뜀)", s, v)
            continue
        out[str(s).upper()] = f
    return out


def fetch_quotes(broker, symbols):
    """브로커 종류에 상관없이 {symbol: price}. 시세 실패·이상 종목은 빠진다."""
    symbols = [s.upper() for s in dict.fromkeys(symbols)]
    if not symbols:
        return {}
    qf = getattr(broker, "quote_fn", None)
    if qf is not None and hasattr(qf, "quotes"):
        return _coerce_quotes(qf.quotes(symbols))
    if hasattr(broker, "quotes"):
        return _coerce_quotes(broker.quotes(symbols))
    out = {}
    for i, s in enumerate(symbols):
        if i and not isinstance(broker, PaperBroker):
            time.sleep(0.21)                      # STOCK 5/s
        try:
            out[s] = float(broker.quote(s))
        except Exception as e:
            log.warning("시세 실패 %s: %s", s, e)
    return out


def broker_meta_get(broker, key):
    if hasattr(broker, "get_meta"):
        return broker.get_meta(key)
    p = os.path.join(BASE_DIR, "paper", "live_meta.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get(key)
    except Exception:
        return None


def broker_meta_set(broker, **kw):
    if hasattr(broker, "set_meta"):
        broker.set_meta(**kw)
        return
    p = os.path.join(BASE_DIR, "paper", "live_meta.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d = {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        pass
    d.update(kw)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


# ── 계획 ──────────────────────────────────────────────────────────────────
def build_plan(cfg, broker):
    """워치리스트 → 후보 → 주문 의도. 워치리스트/설정 문제는 트레이스백 대신 한 줄 로그 + None."""
    src = cfg["source"]
    try:
        df, asof = rules.load_watchlist(src["file"], src.get("sheet", "주도주"))
    except FileNotFoundError:
        log.error("워치리스트 파일 없음: %s", src["file"])
        return None
    except ValueError as e:
        log.error("워치리스트 읽기 실패 %s: %s", src["file"], e)
        return None
    if not asof:
        asof = datetime.fromtimestamp(os.path.getmtime(src["file"]), KST).strftime("%Y-%m-%d")
        log.warning("설명 시트에 기준일이 없어 파일 수정일(%s)을 기준일로 씀", asof)
    try:
        cands, dropped = rules.select_candidates(df, cfg, return_dropped=True)
    except ValueError as e:
        log.error("후보 선정 실패: %s", e)
        return None
    log.info("워치리스트 %s 시트 %d행, 기준일 %s → 후보 %d, 제외 %d",
             src.get("sheet"), len(df), asof, len(cands), len(dropped))
    for d in dropped:
        if not d["reason"].startswith("top_n"):
            log.info("  제외 %-6s %s", d["symbol"] or "(빈 티커)", d["reason"])
    holdings = broker.holdings()
    try:
        open_orders = broker.open_orders()            # 미체결 매수도 보유처럼 취급 (중복 주문 방지)
    except Exception as e:
        log.warning("미체결 조회 실패(없는 것으로 계산): %s", e)
        open_orders = []
    quotes = fetch_quotes(broker, [c["symbol"] for c in cands])
    cash = float(broker.buying_power("USD"))
    try:
        plan = rules.size_orders(cands, cash, holdings, cfg, quotes, client_id_prefix=f"ab-{asof}",
                                 open_orders=open_orders)
    except ValueError as e:
        log.error("주문 크기 계산 실패: %s", e)
        return None
    session = None
    try:
        session = broker.session_now()
    except Exception as e:
        log.warning("세션 조회 실패: %s", e)
    return {"asof": asof, "candidates": cands, "dropped": dropped, "holdings": holdings,
            "open_orders": open_orders, "quotes": quotes, "cash_usd": cash, "plan": plan,
            "session": session, "ts": fmt_ts(now_kst())}


def print_plan(cfg, info):
    plan = info["plan"]
    lines = []
    lines.append(f"기준일 {info['asof']}  현재세션 {info['session'] or '장외'}  "
                 f"가용 USD ${info['cash_usd']:,.2f}  보유 {len(info['holdings'])}종목  "
                 f"미체결 {len(info.get('open_orders') or [])}건")
    lines.append(f"후보 {len(info['candidates'])}: " +
                 ", ".join(f"{c['symbol']}({c['score']:.3f})" if c['score'] is not None else c['symbol']
                           for c in info["candidates"]))
    if plan:
        lines.append(f"{'티커':<7}{'종목명':<28}{'유형':<8}{'수량/금액':>14}{'시세':>10}{'예상$':>11}  사유")
        for o in plan:
            amt = f"${o['order_amount']:,.2f}" if o.get("order_amount") is not None else f"{o['quantity']}주"
            if o.get("price"):
                amt += f"@{o['price']}"
            lines.append(f"{o['symbol']:<7}{str(o['name'])[:26]:<28}{o['order_type']:<8}"
                         f"{amt:>14}{o['quote']:>10,.2f}{o['est_usd']:>11,.2f}  {o['reason']}")
    else:
        lines.append("주문할 종목 없음")
    for s in plan.skipped:
        lines.append(f"  건너뜀 {s['symbol']:<6} {s['reason']}")
    ok = plan.cash_after >= 0
    lines.append(f"현금 확인: 가용 ${plan.cash_before:,.2f} − 주문 ${plan.total_usd:,.2f}(+수수료·슬리피지 버퍼) "
                 f"= 잔여 ${plan.cash_after:,.2f} → {'OK' if ok else '부족'}")
    sess = cfg["schedule"].get("place_at_session", "regularMarket")
    if cfg["sizing"].get("order_type", "MARKET").upper() == "MARKET":
        if info["session"] != "regularMarket":
            lines.append(f"주의: MARKET 주문은 정규장(regularMarket)에서만 접수됨 — 지금은 '{info['session'] or '장외'}' "
                         f"(설정 place_at_session={sess})")
    for ln in lines:
        log.info(ln)


def write_last_plan(cfg, info):
    out = os.path.join(paper_dir(cfg), "last_plan.json")
    data = {"ts": info["ts"], "asof": info["asof"], "mode": cfg.get("mode"),
            "session": info["session"], "cash_usd": info["cash_usd"],
            "candidates": info["candidates"], "dropped": info["dropped"],
            "orders": list(info["plan"]), "skipped": info["plan"].skipped,
            "cash_after": info["plan"].cash_after, "total_usd": info["plan"].total_usd,
            "holdings": info["holdings"], "open_orders": info.get("open_orders", []),
            "quotes": info["quotes"]}
    with open(out + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    os.replace(out + ".tmp", out)
    return out


def _place(broker, o):
    """주문 의도 → broker.place_order. 금액주문 kwarg 이름이 브로커마다 다를 수 있어
    (PaperBroker: order_amount / LiveBroker: amount) 시그니처를 보고 맞춘다."""
    import inspect
    kw = {"symbol": o["symbol"], "side": "BUY", "order_type": o["order_type"],
          "quantity": o.get("quantity"), "price": o.get("price"), "time_in_force": "DAY",
          "client_order_id": o.get("client_order_id")}
    try:
        params = inspect.signature(broker.place_order).parameters
    except (TypeError, ValueError):
        params = {}
    if "order_amount" in params or not params:
        kw["order_amount"] = o.get("order_amount")
    elif "amount" in params:
        kw["amount"] = o.get("order_amount")
    else:
        kw["order_amount"] = o.get("order_amount")
    return broker.place_order(**kw)


# ── 서브커맨드 ────────────────────────────────────────────────────────────
def cmd_plan(cfg, args, inj):
    broker, is_live = make_broker(cfg, args, inj)
    info = build_plan(cfg, broker)
    if info is None:
        return 1
    print_plan(cfg, info)
    p = write_last_plan(cfg, info)
    log.info("계획 저장: %s", p)
    return 0


def _empty_plan_verdict(info):
    """주문이 0건일 때 이 기준일을 '실행 완료'로 기록해도 되는지.
    반환 (record: bool, rc: int, message).
      - 후보 자체가 0개(시트 비었거나 전부 제외) → 기록 안 함, rc 1 (크론 로그에 실패로 남겨 재시도 유도)
      - 시세 조회 실패로 건너뛴 후보가 있음    → 기록 안 함, rc 1 (다음 실행에서 재시도)
      - 미체결 매수 대기 중                   → 기록 안 함, rc 0 (체결/만료 뒤 다시 실행하면 됨)
      - 그 외(이미 보유 / max_positions / 주간한도 / 예산 부족) → 정당한 0건 → 기록, rc 0"""
    plan = info["plan"]
    reasons = [str(s.get("reason", "")) for s in plan.skipped]
    if not info["candidates"]:
        return False, 1, "후보가 0개 (워치리스트가 비었거나 전부 제외) → 기준일 기록 안 함"
    if any(r.startswith(rules.REASON_NO_QUOTE) for r in reasons):
        return False, 1, "시세 조회 실패로 건너뛴 후보가 있음 → 기준일 기록 안 함 (다음 실행에서 재시도)"
    if any(r.startswith(rules.REASON_PENDING) for r in reasons):
        return False, 0, "미체결 매수 주문 대기 중 → 기준일 기록 안 함 (tick 으로 체결/만료 확인 후 재실행)"
    return True, 0, "주문 없음 (보유/한도/예산 사유) → 기준일 실행 완료로 기록"


def cmd_run(cfg, args, inj):
    broker, is_live = make_broker(cfg, args, inj)
    tag = "LIVE 실계좌" if is_live else "PAPER 모의"
    log.info("===== run [%s] %s =====", tag, fmt_ts(now_kst()))
    info = build_plan(cfg, broker)
    if info is None:
        return 1
    print_plan(cfg, info)
    write_last_plan(cfg, info)
    asof = info["asof"]
    last = broker_meta_get(broker, "last_run_asof")
    if last == asof and not getattr(args, "force", False):
        log.info("[SKIP] 기준일 %s 는 이미 실행됨 (last_run_asof). 다시 하려면 --force", asof)
        return 0
    plan = info["plan"]
    if not plan:
        record, rc, msg = _empty_plan_verdict(info)
        (log.info if rc == 0 else log.warning)("%s (기준일 %s)", msg, asof)
        if record:
            broker_meta_set(broker, last_run_asof=asof, last_run_ts=fmt_ts(now_kst()))
        return rc
    if any(str(s.get("reason", "")).startswith(rules.REASON_NO_QUOTE) for s in plan.skipped):
        log.warning("일부 후보는 시세가 없어 이번 실행에서 빠짐: %s",
                    [s["symbol"] for s in plan.skipped if str(s.get("reason", "")).startswith(rules.REASON_NO_QUOTE)])
    if is_live:
        log.warning("!!! 실계좌 주문 %d건 전송 시작 — 실제 돈이 움직입니다 !!!", len(plan))
    filled, pending, rejected = 0, 0, 0
    for i, o in enumerate(plan):
        if i and is_live:
            time.sleep(0.2)
        try:
            res = _place(broker, o)
        except Exception as e:
            res = {"status": "REJECTED", "code": "exception", "reason": str(e)}
        res = res or {}
        st = str(res.get("status", "OPEN")).upper()
        oid = res.get("orderId") or res.get("order_id")
        if st == "REJECTED":
            rejected += 1
            # PaperBroker: code/reason, LiveBroker: reason(=코드)/message
            code = res.get("code") or res.get("reason")
            text = res.get("reason") if "code" in res else res.get("message")
            log.info("  거절 %-6s %s: %s", o["symbol"], code, text)
        elif st == "FILLED":
            filled += 1
            f = res.get("fill") or {}
            log.info("  체결 %-6s %s주 @%.4f 금액 $%.2f 수수료 $%.2f (id %s)",
                     o["symbol"], f.get("qty"), f.get("fillPrice") or 0, f.get("grossValue") or 0,
                     f.get("commission") or 0, oid)
        else:
            pending += 1
            log.info("  접수 %-6s %s (id %s, 만료 %s)", o["symbol"], st, oid, res.get("expiresAt"))
    if hasattr(broker, "tick"):
        broker.tick()
    if is_live and pending > 0 and filled == 0:
        # 실계좌는 체결 여부를 바로 알 수 없다(SUBMITTED). 접수 자체를 실행으로 기록해 이중 주문을 막는다.
        filled = pending
        pending = 0
    if filled > 0:
        broker_meta_set(broker, last_run_asof=asof, last_run_ts=fmt_ts(now_kst()))
        log.info("완료: 체결 %d, 미체결 접수 %d, 거절 %d → 기준일 %s 기록", filled, pending, rejected, asof)
        _print_summary(broker)
        return 0
    if pending > 0:
        # 지정가가 하나도 즉시 체결되지 않음. 기준일을 기록하지 않아 만료되면 다음 run 이 다시 시도한다.
        # (대기 중에는 rules 가 '미체결 매수 대기' 로 같은 종목을 다시 내지 않는다)
        log.info("완료: 미체결 접수 %d, 거절 %d → 체결 0건이라 기준일 기록 안 함 (정규장 중 tick 으로 체결 확인)",
                 pending, rejected)
        _print_summary(broker)
        return 0
    log.info("완료: 전부 거절 (%d건) → 기준일 기록 안 함, 정규장에 다시 실행 가능", rejected)
    return 1


def cmd_tick(cfg, args, inj):
    broker, is_live = make_broker(cfg, args, inj)
    if not hasattr(broker, "tick"):
        log.info("LIVE 브로커는 tick 대상이 아님 (미체결은 토스 서버가 관리). 상태만 출력.")
        _print_summary(broker)
        return 0
    fetch_quotes(broker, list(broker.holdings()) + [o["symbol"] for o in broker.open_orders()])
    events = broker.tick()
    for ev in events:
        if ev["event"] == "FILLED":
            f = ev["fill"]
            log.info("  tick 체결 %-6s %s %s주 @%.4f", f["symbol"], f["side"], f["qty"], f["fillPrice"])
        elif ev["event"] == "EXPIRED":
            o = ev["order"]
            log.info("  tick 만료 %-6s %s %s @%s (id %s)", o["symbol"], o["side"],
                     o.get("quantity"), o.get("price"), o["orderId"])
        elif ev["event"] == "MARK":
            s = ev["snapshot"]
            log.info("  평가 %s 총자산 $%.2f (현금 $%.2f, 주식 $%.2f)", s["ts"], s["equity_usd"],
                     s["cash_usd"], s["positions_value"])
    return 0


def _print_summary(broker):
    if not hasattr(broker, "summary"):
        try:
            log.info("보유: %s", broker.holdings())
            log.info("가용 USD: %s", broker.buying_power("USD"))
            log.info("미체결: %s", broker.open_orders())
        except Exception as e:
            log.warning("LIVE 상태 조회 실패: %s", e)
        return
    fetch_quotes(broker, list(broker.holdings()))
    s = broker.summary()
    log.info("--- 상태 %s (세션 %s) ---", s["ts"], s["session"] or "장외")
    log.info("현금 USD $%.2f (가용 $%.2f)  KRW %.0f", s["cash_usd"], s["buying_power_usd"], s["cash_krw"])
    log.info("보유 %d종목 평가 $%.2f  총자산 $%.2f  수익률 %+.2f%%  최대낙폭 %.2f%%",
             s["positions_count"], s["positions_value"], s["equity_usd"], s["return_pct"],
             s["max_drawdown_pct"])
    log.info("순손익 $%+.2f = 실현 $%+.2f (매도수수료 차감) + 평가 $%+.2f (매수수수료 포함 평단)  "
             "누적수수료 $%.2f (참고)  체결 %d  거절 %d  미체결 %d",
             s["net_pnl"], s["realized_pnl"], s["unrealized_pnl"], s["commissions"], s["fills"],
             s["rejected"], s["open_orders"])
    if s["positions"]:
        log.info(f"{'티커':<7}{'수량':>12}{'평단':>11}{'현재가':>11}{'평가$':>12}{'손익$':>11}{'손익%':>8}  매수일")
        for sym, p in sorted(s["positions"].items()):
            log.info(f"{sym:<7}{p['qty']:>12.6f}{p['avg_price']:>11.4f}{p['price']:>11.4f}"
                     f"{p['value']:>12.2f}{p['unrealized']:>+11.2f}{p['pnl_pct']:>+8.2f}  {p['opened']}"
                     + ("  (시세 stale)" if p.get("stale") else ""))
    for o in broker.open_orders():
        log.info("  미체결 %s %s %s %s주 @%s 만료 %s (id %s)", o["symbol"], o["side"], o["orderType"],
                 o.get("quantity"), o.get("price"), o.get("expiresAt"), o["orderId"])
    log.info("마지막 실행 기준일 %s (%s)", s["last_run_asof"], s["last_run_ts"])


def cmd_status(cfg, args, inj):
    broker, is_live = make_broker(cfg, args, inj)
    log.info("모드 %s", mode_label(cfg))
    _print_summary(broker)
    return 0


def _flatten(d, prefix=""):
    rows = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            rows.extend(_flatten(v, key + "."))
        else:
            rows.append((key, json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v))
    return rows


def cmd_report(cfg, args, inj):
    import pandas as pd
    broker = make_paper_broker(cfg, **inj)          # 리포트는 페이퍼 상태 전용
    fetch_quotes(broker, list(broker.holdings()))
    s = broker.summary()
    positions, fills, equity = broker.to_frames()
    out = getattr(args, "out", None) or os.path.join(BASE_DIR, "paper_report_latest.xlsx")

    summary_rows = [
        ("기준시각", s["ts"]), ("모드", "PAPER 모의"), ("현재세션", s["session"] or "장외"),
        ("초기자산(USD)", round(s["initial_equity_usd"], 2)), ("총자산(USD)", round(s["equity_usd"], 2)),
        ("수익률%", round(s["return_pct"], 3)), ("현금USD", round(s["cash_usd"], 2)),
        ("현금KRW", round(s["cash_krw"], 0)), ("주식평가액(USD)", round(s["positions_value"], 2)),
        ("보유종목수", s["positions_count"]),
        ("순손익(USD) = 실현 + 평가 = 총자산 − 초기자산", round(s["net_pnl"], 2)),
        ("실현손익(USD, 매도수수료 차감 후)", round(s["realized_pnl"], 2)),
        ("평가손익(USD, 매수수수료 포함 평단 기준)", round(s["unrealized_pnl"], 2)),
        ("누적수수료(USD, 참고 — 위 손익에 이미 반영됨)", round(s["commissions"], 2)),
        ("총매수금액(USD)", round(s["total_bought"], 2)), ("총매도금액(USD)", round(s["total_sold"], 2)),
        ("최대낙폭%", round(s["max_drawdown_pct"], 3)), ("미체결주문", s["open_orders"]),
        ("체결건수", s["fills"]), ("거절건수", s["rejected"]),
        ("마지막실행기준일", s["last_run_asof"] or "-"), ("마지막실행시각", s["last_run_ts"] or "-"),
        ("계좌생성", s["created"]),
    ]
    summary_df = pd.DataFrame(summary_rows, columns=["항목", "값"])
    pos_df = positions.rename(columns={
        "symbol": "티커", "qty": "수량", "avg_price": "평균단가", "price": "현재가", "value": "평가금액",
        "unrealized": "평가손익", "pnl_pct": "수익률%", "opened": "매수일", "stale": "시세지연"})
    fills_df = fills.rename(columns={
        "ts": "체결시각", "orderId": "주문ID", "clientOrderId": "클라이언트주문ID", "symbol": "티커",
        "side": "매매", "orderType": "주문유형", "qty": "수량", "fillPrice": "체결가", "quotePrice": "시세",
        "grossValue": "체결금액", "commission": "수수료", "realizedPnl": "실현손익", "session": "세션",
        "note": "비고"})
    eq_df = equity.rename(columns={
        "ts": "시각", "equity_usd": "총자산", "cash_usd": "현금USD", "cash_krw": "현금KRW",
        "positions_value": "주식평가액", "drawdown_pct": "고점대비낙폭%"})
    cfg_df = pd.DataFrame(_flatten(cfg), columns=["설정키", "값"])
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        summary_df.to_excel(xw, sheet_name="요약", index=False)
        pos_df.to_excel(xw, sheet_name="보유", index=False)
        fills_df.to_excel(xw, sheet_name="체결내역", index=False)
        eq_df.to_excel(xw, sheet_name="자산추이", index=False)
        cfg_df.to_excel(xw, sheet_name="설정", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, width + 2))
    log.info("리포트 저장: %s (요약/보유/체결내역/자산추이/설정)", out)
    return 0


def cmd_replay(cfg, args, inj):
    try:
        import replay
    except ImportError as e:
        log.error("replay.py 를 찾을 수 없습니다 (%s). 과거 시뮬레이션 모듈이 아직 없습니다.", e)
        return 2
    kwargs = {"cfg": cfg}
    if getattr(args, "start", None):
        kwargs["start"] = args.start
    if getattr(args, "end", None):
        kwargs["end"] = args.end
    if getattr(args, "cadence", None):
        kwargs["cadence"] = args.cadence
    if getattr(args, "refresh", False):
        kwargs["refresh"] = True
    if getattr(args, "out", None):
        kwargs["out"] = args.out
    log.info("replay 시작 %s (기준일마다 스크리닝 10~20s, 캐시 paper/replay_cache/ 에 있으면 즉시)",
             {k: v for k, v in kwargs.items() if k != "cfg"})
    res = replay.replay(**kwargs)          # 페이퍼 상태(paper_state.json)는 건드리지 않음 — replay_state.json 별도
    if res is not None:
        log.info("replay 요약:\n%s", res.to_string(index=False) if hasattr(res, "to_string") else res)
    return 0


def cmd_reset(cfg, args, inj):
    sp = cfg["paper"].get("state_path")
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            log.error("--yes 없이 비대화형으로 reset 불가")
            return 2
        a = input(f"페이퍼 상태({sp})를 초기화합니다. 계속? [y/N] ")
        if a.strip().lower() != "y":
            log.info("취소")
            return 0
    broker = make_paper_broker(cfg, **inj)
    broker.reset()
    log.info("페이퍼 상태 초기화 완료: USD %.2f / KRW %.0f (%s)",
             broker.state["cash"]["USD"], broker.state["cash"]["KRW"], sp)
    return 0


def cmd_config(cfg, args, inj):
    log.info("설정 파일: %s", getattr(args, "config", None) or DEFAULT_CONFIG_PATH)
    log.info(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


COMMANDS = {"plan": cmd_plan, "run": cmd_run, "tick": cmd_tick, "status": cmd_status,
            "report": cmd_report, "replay": cmd_replay, "reset": cmd_reset, "config": cmd_config}


# ── 메뉴 ──────────────────────────────────────────────────────────────────
def menu(cfg_path, inj):
    while True:
        cfg, _ = load_config(cfg_path)
        label = mode_label(cfg)
        print()
        print("=" * 56)
        print(f"  auto_buy  {label}   설정: {cfg_path}")
        print("=" * 56)
        print("  1. plan    이번 주 주문 계획 보기 (상태 변경 없음)")
        print("  2. run     주문 실행 (페이퍼 / live 는 플래그·환경변수 필요)")
        print("  3. tick    미체결 재검사 + 만료 + 자산 평가 기록")
        print("  4. status  현금 / 보유 / 미체결 / 자산·최대낙폭")
        print("  5. report  엑셀 리포트 (paper_report_latest.xlsx)")
        print("  6. replay  과거 시뮬레이션 (replay.py)")
        print("  7. reset   페이퍼 상태 초기화")
        print("  8. config  설정 보기")
        print("  0. 종료")
        try:
            sel = input("번호 선택: ").strip()
        except EOFError:
            return 0
        argv_map = {"1": ["plan"], "2": ["run"], "3": ["tick"], "4": ["status"], "5": ["report"],
                    "6": ["replay"], "7": ["reset"], "8": ["config"]}
        if sel == "0" or sel == "":
            return 0
        if sel not in argv_map:
            print("잘못된 번호")
            continue
        sub = argv_map[sel]
        if sel == "6":
            s = input("시작일 (기본 2025-01-06): ").strip()
            e = input("종료일 (기본 DB 최신): ").strip()
            c = input("주기 weekly/monthly (기본 weekly): ").strip()
            if s:
                sub += ["--start", s]
            if e:
                sub += ["--end", e]
            if c:
                sub += ["--cadence", c]
        rc = main(sub + ["--config", cfg_path], **inj)
        print(f"(exit {rc})")


# ── 진입점 ────────────────────────────────────────────────────────────────
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="설정 JSON 경로 (기본 auto_buy_config.json)")
    p = argparse.ArgumentParser(description="주도주 자동 매수 (paper 기본 / live 3중 잠금)",
                                parents=[common])
    sp = p.add_subparsers(dest="cmd")
    pl = sp.add_parser("plan", parents=[common], help="주문 계획만 출력 (상태 변경 없음)")
    pl.add_argument("--live", action="store_true", help="실계좌 잔고 기준으로 계획 (config mode=live 필요)")
    r = sp.add_parser("run", parents=[common], help="주문 실행")
    r.add_argument("--live", action="store_true", help="실계좌 (config mode=live + AUTO_BUY_LIVE_OK=1 필요)")
    r.add_argument("--force", action="store_true", help="같은 기준일 재실행 허용")
    for name in ("tick", "status"):
        x = sp.add_parser(name, parents=[common])
        x.add_argument("--live", action="store_true", help="실계좌 조회 (config mode=live 필요)")
    rp = sp.add_parser("report", parents=[common], help="paper_report_latest.xlsx 작성")
    rp.add_argument("--out", default=None)
    rr = sp.add_parser("replay", parents=[common], help="과거 시뮬레이션 (replay.py)")
    rr.add_argument("--start", default=None)
    rr.add_argument("--end", default=None)
    rr.add_argument("--cadence", default=None, choices=["weekly", "monthly"])
    rr.add_argument("--refresh", action="store_true", help="스크리닝 캐시 무시하고 다시 계산")
    rr.add_argument("--out", default=None, help="결과 엑셀 경로 (기본 paper_replay_latest.xlsx)")
    rs = sp.add_parser("reset", parents=[common], help="페이퍼 상태 초기화")
    rs.add_argument("--yes", action="store_true")
    sp.add_parser("config", parents=[common], help="설정 출력")
    return p


def main(argv=None, quote_fn=None, session_fn=None, calendar_fn=None, now_fn=None,
         fx_fn=None, log_dir=None):
    """CLI 진입점. 테스트에서는 quote_fn/session_fn 등을 주입해 토스 호출 없이 돌린다."""
    setup_logging(log_dir)
    inj = {"quote_fn": quote_fn, "session_fn": session_fn, "calendar_fn": calendar_fn,
           "now_fn": now_fn, "fx_fn": fx_fn}
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg_path = getattr(args, "config", None) or DEFAULT_CONFIG_PATH
    if not args.cmd:
        return menu(cfg_path, inj)
    try:
        cfg, cfg_path = load_config(cfg_path)
    except Exception as e:
        log.error("설정 로드 실패 %s: %s", cfg_path, e)
        return 2
    args.config = cfg_path
    log.info("%s %s %s", mode_label(cfg), args.cmd, " ".join(argv[1:]))
    try:
        return int(COMMANDS[args.cmd](cfg, args, inj) or 0)
    except PermissionError as e:
        log.error("차단: %s", e)
        return 3
    except StateFileError as e:
        log.error("%s", e)
        return 1
    except Exception as e:
        log.exception("실패: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

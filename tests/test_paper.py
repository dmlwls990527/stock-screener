#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_paper.py — 페이퍼 브로커 / 규칙 / auto_buy 잠금 검증 (plain unittest).

실행:  cd /data/frame && ./.venv/bin/python -m unittest tests.test_paper -v
토스 API 는 한 번도 부르지 않는다 (시세·세션·환율 전부 Fake 주입, toss_api._request 는 raise 로 패치).
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import broker_paper                                            # noqa: E402
from broker_paper import PaperBroker, KST, synthetic_calendar  # noqa: E402
import rules                                                   # noqa: E402
import auto_buy                                                # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────
class FakeQuote:
    def __init__(self, prices):
        self.prices = {k.upper(): float(v) for k, v in prices.items()}
        self.calls = 0

    def __call__(self, symbol):
        self.calls += 1
        if symbol.upper() not in self.prices:
            raise RuntimeError(f"no quote {symbol}")
        return self.prices[symbol.upper()]

    def quotes(self, symbols):
        return {s.upper(): self.prices.get(s.upper()) for s in symbols}

    def set(self, symbol, price):
        self.prices[symbol.upper()] = float(price)


class FakeSession:
    """session name + 고정 달력 + 조절 가능한 현재시각."""

    def __init__(self, name="regularMarket", now=None):
        self.name = name
        self.now = now or datetime(2026, 9, 28, 23, 0, tzinfo=KST)   # 월 23:00 KST = 정규장
        self.cal_day = synthetic_calendar(self.now)

    def __call__(self):
        return self.name

    def calendar(self):
        return {"today": self.cal_day}

    def now_fn(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


PAPER_CFG = {"initial_cash_usd": 10000, "initial_cash_krw": 0, "slippage_bps": 5,
             "commission_pct": 0.1, "fx_spread_pct_market": 0.05, "fx_spread_pct_off": 0.5,
             "auto_fx": False, "fx_rate": 1400.0}


def make_broker(tmp, quotes=None, session=None, **cfg_over):
    cfg = dict(PAPER_CFG)
    cfg.update(cfg_over)
    q = quotes or FakeQuote({"AAPL": 100.0, "MU": 50.0, "XYZ": 0.5})
    s = session or FakeSession()
    b = PaperBroker(os.path.join(tmp, "state.json"), q, s, cfg,
                    calendar_fn=s.calendar, now_fn=s.now_fn)
    return b, q, s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paper_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── 브로커 체결 수학 ───────────────────────────────────────────────────────
class TestFills(Base):
    def test_market_buy_slippage_commission(self):
        b, q, s = make_broker(self.tmp)
        r = b.place_order("AAPL", "BUY", "MARKET", quantity=10)
        self.assertEqual(r["status"], "FILLED")
        f = r["fill"]
        self.assertAlmostEqual(f["fillPrice"], 100.05, places=6)          # 100 × (1+5bps)
        self.assertAlmostEqual(f["grossValue"], 1000.5, places=6)
        self.assertAlmostEqual(f["commission"], 1.0005, places=6)          # 0.1%
        self.assertAlmostEqual(b.state["cash"]["USD"], 10000 - 1000.5 - 1.0005, places=6)
        p = b.holdings()["AAPL"]
        self.assertEqual(p["qty"], 10)
        self.assertAlmostEqual(p["avg_price"], (1000.5 + 1.0005) / 10, places=6)   # 매수수수료 포함 평단(토스 방식)
        self.assertEqual(len(b.state["fills"]), 1)

    def test_market_sell_slippage_realized(self):
        b, q, s = make_broker(self.tmp)
        b.place_order("AAPL", "BUY", "MARKET", quantity=10)
        avg = b.holdings()["AAPL"]["avg_price"]
        q.set("AAPL", 110)
        r = b.place_order("AAPL", "SELL", "MARKET", quantity=4.5)          # MARKET SELL 소수점 허용
        self.assertEqual(r["status"], "FILLED")
        f = r["fill"]
        self.assertAlmostEqual(f["fillPrice"], 110 * (1 - 0.0005), places=6)
        exp_real = (f["fillPrice"] - avg) * 4.5 - f["commission"]
        self.assertAlmostEqual(f["realizedPnl"], exp_real, places=4)   # 저장 시 6자리 반올림
        self.assertAlmostEqual(b.holdings()["AAPL"]["qty"], 5.5, places=6)
        # 보유 초과 매도 거절
        r2 = b.place_order("AAPL", "SELL", "MARKET", quantity=6)
        self.assertEqual(r2["code"], "insufficient-sellable-quantity")

    def test_pnl_identity_equity_change_equals_realized_plus_unrealized(self):
        """리뷰 예시: 슬리피지 0, 수수료 1%: 10주@100 매수(수수료 10) → 10주@110 매도(수수료 11).
        현금 10,079 → 총자산 손익 +79 = 실현손익(수수료 차감 후) + 평가손익. 누적수수료는 따로 빼지 않는다."""
        b, q, s = make_broker(self.tmp, slippage_bps=0, commission_pct=1.0)
        b.place_order("AAPL", "BUY", "MARKET", quantity=10)
        self.assertAlmostEqual(b.holdings()["AAPL"]["avg_price"], 101.0, places=9)   # (1000+10)/10
        q.set("AAPL", 110)
        sm = b.summary()
        self.assertAlmostEqual(sm["unrealized_pnl"], 90.0, places=6)                # (110−101)×10
        self.assertAlmostEqual(sm["equity_change"], sm["net_pnl"], places=6)        # 항등식 (보유 중)
        r = b.place_order("AAPL", "SELL", "MARKET", quantity=10)
        self.assertAlmostEqual(r["fill"]["realizedPnl"], 79.0, places=6)
        self.assertAlmostEqual(b.state["cash"]["USD"], 10079.0, places=6)
        sm = b.summary()
        self.assertAlmostEqual(sm["realized_pnl"], 79.0, places=6)
        self.assertAlmostEqual(sm["unrealized_pnl"], 0.0, places=6)
        self.assertAlmostEqual(sm["net_pnl"], 79.0, places=6)
        self.assertAlmostEqual(sm["equity_change"], 79.0, places=6)
        self.assertAlmostEqual(sm["commissions"], 21.0, places=6)
        self.assertAlmostEqual(sm["buy_commissions"], 10.0, places=6)
        self.assertAlmostEqual(sm["sell_commissions"], 11.0, places=6)
        # 여러 종목·부분 매도·가격 변동을 섞어도 항등식 유지
        b.place_order("MU", "BUY", "MARKET", quantity=20)
        b.place_order("AAPL", "BUY", "MARKET", quantity=3)
        q.set("MU", 45)
        q.set("AAPL", 120)
        b.place_order("MU", "SELL", "MARKET", quantity=7.5)
        sm = b.summary()
        self.assertAlmostEqual(sm["equity_change"], sm["realized_pnl"] + sm["unrealized_pnl"], places=6)

    def test_amount_order_fractional_qty(self):
        b, q, s = make_broker(self.tmp)
        r = b.place_order("AAPL", "BUY", "MARKET", order_amount=1000)
        self.assertEqual(r["status"], "FILLED")
        f = r["fill"]
        qty = f["qty"]
        self.assertAlmostEqual(qty, 9.995002, places=6)                    # floor6(1000/100.05)
        self.assertEqual(round(qty, 6), qty)
        self.assertLessEqual(f["grossValue"], 1000.0 + 1e-6)
        self.assertGreater(f["grossValue"], 999.9)

    def test_amount_order_not_allowed_near_close_or_off_regular(self):
        s = FakeSession("regularMarket")
        s.now = broker_paper.parse_ts(s.cal_day["regularMarket"]["endTime"]) - timedelta(minutes=30)
        b, q, _ = make_broker(self.tmp, session=s)
        r = b.place_order("AAPL", "BUY", "MARKET", order_amount=500)
        self.assertEqual(r["code"], "amount-order-outside-regular-hours")          # 토스 스펙 코드 그대로
        # BUY 소수점 수량은 금액주문으로만
        r2 = b.place_order("AAPL", "BUY", "MARKET", quantity=1.5)
        self.assertEqual(r2["code"], "fractional-quantity-outside-regular-hours")
        # 코드 문자열이 toss_api.create_order 문서의 스펙 코드와 같은지 (paper/live 로그 비교용)
        import toss_api
        doc = toss_api.create_order.__doc__ or ""
        self.assertIn(r["code"], doc)
        self.assertIn(r2["code"], doc)

    def test_market_rejected_outside_regular(self):
        for name in ("preMarket", "afterMarket", "dayMarket", None):
            b, q, s = make_broker(os.path.join(self.tmp, str(name)), session=FakeSession(name))
            r = b.place_order("AAPL", "BUY", "MARKET", quantity=1)
            self.assertEqual(r["code"], "order-hours-closed", name)
            self.assertEqual(b.holdings(), {})
            self.assertEqual(b.state["cash"]["USD"], 10000)
        b, q, s = make_broker(os.path.join(self.tmp, "closed"), session=FakeSession(None))
        r = b.place_order("AAPL", "BUY", "LIMIT", quantity=1, price=99)
        self.assertEqual(r["code"], "order-hours-closed")

    def test_insufficient_cash_rejected(self):
        b, q, s = make_broker(self.tmp, initial_cash_usd=500)
        r = b.place_order("AAPL", "BUY", "MARKET", quantity=10)
        self.assertEqual(r["status"], "REJECTED")
        self.assertEqual(r["code"], "insufficient-buying-power")
        self.assertEqual(b.state["cash"]["USD"], 500)
        self.assertEqual(b.holdings(), {})
        self.assertEqual(len(b.state["rejected"]), 1)
        self.assertEqual(len(b.state["fills"]), 0)

    def test_auto_fx_conversion_math(self):
        # 23:00 KST → 장외 스프레드 0.5%
        b, q, s = make_broker(self.tmp, initial_cash_usd=0, initial_cash_krw=2_000_000,
                              auto_fx=True, fx_rate=1400.0)
        r = b.place_order("AAPL", "BUY", "MARKET", quantity=1)
        self.assertEqual(r["status"], "FILLED", r)
        need = 100.05 * 1.001                                   # 체결금액 + 수수료
        krw = need * 1400 * 1.005
        self.assertAlmostEqual(b.state["cash"]["KRW"], 2_000_000 - krw, places=4)
        self.assertAlmostEqual(b.state["cash"]["USD"], 0.0, places=6)
        self.assertEqual(len(b.state["fx_events"]), 1)
        self.assertAlmostEqual(b.state["fx_events"][0]["spread_pct"], 0.5)
        # 장중(09:00~15:30) 스프레드 0.05%
        self.assertAlmostEqual(b._fx_spread_pct(datetime(2026, 9, 28, 10, 0, tzinfo=KST)), 0.05)
        self.assertAlmostEqual(b._fx_spread_pct(datetime(2026, 9, 28, 15, 30, tzinfo=KST)), 0.5)
        # KRW 도 부족하면 거절
        r2 = b.place_order("AAPL", "BUY", "MARKET", quantity=100)
        self.assertEqual(r2["code"], "insufficient-buying-power")
        # auto_fx 꺼져 있으면 KRW 가 있어도 거절
        b2, _, _ = make_broker(self.tmp + "_b", initial_cash_usd=0, initial_cash_krw=5_000_000,
                               auto_fx=False)
        r3 = b2.place_order("AAPL", "BUY", "MARKET", quantity=1)
        self.assertEqual(r3["code"], "insufficient-buying-power")
        shutil.rmtree(self.tmp + "_b", ignore_errors=True)

    def test_limit_pending_then_fill_on_tick(self):
        b, q, s = make_broker(self.tmp)
        r = b.place_order("AAPL", "BUY", "LIMIT", quantity=5, price=99.0)
        self.assertEqual(r["status"], "OPEN")
        self.assertEqual(len(b.open_orders()), 1)
        self.assertAlmostEqual(b.buying_power("USD"), 10000 - 5 * 99 * 1.001, places=6)   # 예약
        ev = b.tick()
        self.assertEqual([e["event"] for e in ev], ["MARK"])                 # 아직 미체결
        q.set("AAPL", 98.5)
        ev = b.tick()
        kinds = [e["event"] for e in ev]
        self.assertIn("FILLED", kinds)
        self.assertEqual(b.open_orders(), [])
        p = b.holdings()["AAPL"]
        self.assertEqual(p["qty"], 5)
        # 지정가 99 인데 시세 98.5 → min(지정가, 시세)=98.5 로 체결 (지정가보다 유리한 시세면 시세)
        self.assertAlmostEqual(b.state["fills"][-1]["fillPrice"], 98.5)
        self.assertAlmostEqual(p["avg_price"], 98.5 * 1.001)
        self.assertAlmostEqual(b.state["cash"]["USD"], 10000 - 5 * 98.5 * 1.001, places=6)
        self.assertEqual(len(b.state["equity_history"]), 2)
        # 즉시 체결 가능한 지정가(quote ≤ limit)는 바로 min(지정가, 시세) 로 체결 → 시세 50
        r2 = b.place_order("MU", "BUY", "LIMIT", quantity=2, price=51.0)
        self.assertEqual(r2["status"], "FILLED")
        self.assertAlmostEqual(r2["fill"]["fillPrice"], 50.0)
        # SELL 지정가는 max(지정가, 시세)
        q.set("MU", 60.0)
        r2s = b.place_order("MU", "SELL", "LIMIT", quantity=1, price=55.0)
        self.assertEqual(r2s["status"], "FILLED")
        self.assertAlmostEqual(r2s["fill"]["fillPrice"], 60.0)
        # 호가 단위 위반
        r3 = b.place_order("MU", "BUY", "LIMIT", quantity=1, price=49.005)
        self.assertEqual(r3["code"], "invalid-price-tick")
        r4 = b.place_order("XYZ", "BUY", "LIMIT", quantity=1, price=0.4999)
        self.assertEqual(r4["status"], "OPEN")

    def test_day_order_expires_at_regular_end(self):
        b, q, s = make_broker(self.tmp)
        r = b.place_order("AAPL", "BUY", "LIMIT", quantity=5, price=99.0)
        self.assertEqual(r["expiresAt"], s.cal_day["regularMarket"]["endTime"].replace(".000", ""))
        s.advance(hours=5)                                # 04:00 아직 정규장
        b.tick()
        self.assertEqual(len(b.open_orders()), 1)
        s.advance(hours=1, minutes=1)                     # 05:01 만료
        s.name = "afterMarket"
        ev = b.tick()
        self.assertIn("EXPIRED", [e["event"] for e in ev])
        self.assertEqual(b.open_orders(), [])
        self.assertEqual(b.state["closed_orders"][0]["status"], "EXPIRED")
        self.assertAlmostEqual(b.buying_power("USD"), 10000)     # 예약 해제

    def test_duplicate_client_order_id_same_day(self):
        b, q, s = make_broker(self.tmp)
        r1 = b.place_order("AAPL", "BUY", "MARKET", quantity=1, client_order_id="cid-1")
        self.assertEqual(r1["status"], "FILLED")
        r2 = b.place_order("AAPL", "BUY", "MARKET", quantity=1, client_order_id="cid-1")
        self.assertEqual(r2["code"], "duplicate-client-order-id")
        self.assertEqual(len(b.state["fills"]), 1)
        s.advance(days=1)                                  # 다음 날은 재사용 가능
        s.cal_day = synthetic_calendar(s.now)
        r3 = b.place_order("AAPL", "BUY", "MARKET", quantity=1, client_order_id="cid-1")
        self.assertEqual(r3["status"], "FILLED")

    def test_opposite_pending_rejected(self):
        b, q, s = make_broker(self.tmp)
        b.place_order("AAPL", "BUY", "MARKET", quantity=10)
        r = b.place_order("AAPL", "SELL", "LIMIT", quantity=5, price=120.0)   # 대기
        self.assertEqual(r["status"], "OPEN")
        r2 = b.place_order("AAPL", "BUY", "MARKET", quantity=1)
        self.assertEqual(r2["code"], "opposite-pending-order-exists")
        b.cancel(r["orderId"])
        r3 = b.place_order("AAPL", "BUY", "LIMIT", quantity=1, price=90.0)     # 대기 매수
        self.assertEqual(r3["status"], "OPEN")
        r4 = b.place_order("AAPL", "SELL", "MARKET", quantity=1)
        self.assertEqual(r4["code"], "opposite-pending-order-exists")
        self.assertEqual(len(b.state["closed_orders"]), 1)
        self.assertEqual(b.state["closed_orders"][0]["status"], "CANCELLED")

    def test_summary_frames_and_drawdown(self):
        b, q, s = make_broker(self.tmp)
        b.place_order("AAPL", "BUY", "MARKET", quantity=50)
        b.tick()
        q.set("AAPL", 80)
        b.tick()
        q.set("AAPL", 90)
        b.tick()
        sm = b.summary()
        self.assertEqual(sm["positions_count"], 1)
        self.assertGreater(sm["max_drawdown_pct"], 5)
        self.assertLess(sm["equity_usd"], 10000)
        pos, fills, eq = b.to_frames()
        self.assertEqual(len(pos), 1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(eq), 3)
        self.assertIn("drawdown_pct", eq.columns)
        self.assertAlmostEqual(PaperBroker.max_drawdown_from([100, 120, 60, 90]), 0.5)


class TestAtomicWrite(Base):
    def test_no_tmp_left_and_failure_keeps_old_state(self):
        b, q, s = make_broker(self.tmp)
        b.place_order("AAPL", "BUY", "MARKET", quantity=1)
        path = b.state_path
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(path + ".tmp"))
        before = open(path, encoding="utf-8").read()
        json.loads(before)
        orig = broker_paper.json.dump

        def boom(*a, **k):
            raise IOError("disk full")
        broker_paper.json.dump = boom
        try:
            b.state["cash"]["USD"] = -1
            with self.assertRaises(IOError):
                b.save()
        finally:
            broker_paper.json.dump = orig
        after = open(path, encoding="utf-8").read()
        self.assertEqual(before, after)                     # 원본 손상 없음
        self.assertEqual(json.loads(after)["cash"]["USD"], json.loads(before)["cash"]["USD"])
        # 다시 열면 그대로 복원
        b2 = PaperBroker(path, q, s, PAPER_CFG, calendar_fn=s.calendar, now_fn=s.now_fn)
        self.assertEqual(b2.holdings()["AAPL"]["qty"], 1)


# ── rules ────────────────────────────────────────────────────────────────
def watch_df():
    import pandas as pd
    return pd.DataFrame({
        "순위": [1, 2, 3, 4, 5, 6, 7],
        "티커": ["DELL", "HPE", "VLO", "MU", "ILMN", "P", "TWLO"],
        "종목명": ["Dell", "HPE", "Valero", "Micron", "Illumina", "Everpure", "Twilio"],
        "섹터": ["IT", "IT", "에너지", "IT", "헬스케어", "IT", "IT"],
        "주의": [None, "", None, "시클리컬 지금 이익 최고", float("nan"), None, None],
        "주도주점수": [1.086, 1.067, 0.991, 1.5, 0.970, 0.948, 0.905],
    })


def cfg_with(**over):
    cfg = json.loads(json.dumps(auto_buy.DEFAULT_CONFIG))
    for k, v in over.items():
        sec, key = k.split("__")
        cfg[sec][key] = v
    return cfg


class TestRules(unittest.TestCase):
    def test_select_candidates(self):
        df = watch_df()
        c = rules.select_candidates(df, cfg_with(source__top_n=3))
        self.assertEqual([x["symbol"] for x in c], ["DELL", "HPE", "VLO"])   # MU 주의 제외, 정렬 desc
        c, dropped = rules.select_candidates(df, cfg_with(source__top_n=3, source__exclude_if_주의=False),
                                             return_dropped=True)
        self.assertEqual([x["symbol"] for x in c], ["MU", "DELL", "HPE"])
        self.assertTrue(any(d["reason"].startswith("top_n") for d in dropped))
        c = rules.select_candidates(df, cfg_with(source__top_n=3, source__exclude_sectors=["IT"]))
        self.assertEqual([x["symbol"] for x in c], ["VLO", "ILMN"])
        c = rules.select_candidates(df, cfg_with(source__top_n=3, source__exclude_tickers=["dell"]))
        self.assertEqual([x["symbol"] for x in c], ["HPE", "VLO", "ILMN"])
        self.assertEqual(rules.select_candidates(df.iloc[0:0], cfg_with()), [])

    def test_size_orders_rules(self):
        cands = [{"symbol": s, "name": s, "score": 1.0, "reason": "r"} for s in
                 ["DELL", "HPE", "VLO", "ILMN", "P"]]
        quotes = {"DELL": 150.0, "HPE": 25.0, "VLO": 180.0, "ILMN": 90.0, "P": 3000.0}
        # skip_if_held
        plan = rules.size_orders(cands, 10000, {"DELL": {"qty": 3, "avg_price": 100}},
                                 cfg_with(sizing__per_stock_usd=1000, sizing__weekly_cap_usd=3000,
                                          sizing__max_positions=10), quotes)
        self.assertEqual([o["symbol"] for o in plan], ["HPE", "VLO", "ILMN"])            # cap 3000
        self.assertTrue(any("skip_if_held" in s["reason"] for s in plan.skipped if s["symbol"] == "DELL"))
        self.assertTrue(any("weekly_cap" in s["reason"] for s in plan.skipped if s["symbol"] == "P"))
        self.assertAlmostEqual(plan.total_usd, 3000.0)
        self.assertEqual(plan[0]["order_amount"], 1000.0)                                  # 금액주문
        self.assertIsNone(plan[0]["quantity"])
        # max_positions: 보유 2 + 신규 → 3 이면 1개만
        plan = rules.size_orders(cands, 10000, {"AAA": {"qty": 1, "avg_price": 1}, "BBB": {"qty": 1, "avg_price": 1}},
                                 cfg_with(sizing__max_positions=3, sizing__weekly_cap_usd=9999), quotes)
        self.assertEqual(len(plan), 1)
        self.assertTrue(any("max_positions" in s["reason"] for s in plan.skipped))
        # 현금 부족: 1500 → 1000 + 나머지 ~498 → 두 번째는 예산 축소, 세 번째 min_order 미만
        plan = rules.size_orders(cands, 1500, {}, cfg_with(sizing__weekly_cap_usd=9999, sizing__min_order_usd=50), quotes)
        self.assertEqual(len(plan), 2)
        self.assertLess(plan[1]["order_amount"], 500)
        self.assertGreaterEqual(plan.cash_after, 0)
        # qty 주문(금액주문 끔): P 는 1주 3000 > 1000 예산 → 건너뜀
        plan = rules.size_orders(cands, 100000, {}, cfg_with(sizing__use_amount_orders=False,
                                                              sizing__weekly_cap_usd=99999,
                                                              sizing__per_stock_usd=1000), quotes)
        syms = [o["symbol"] for o in plan]
        self.assertNotIn("P", syms)
        self.assertEqual(plan[0]["quantity"], 6)                                           # floor(1000/150)
        self.assertAlmostEqual(plan[0]["est_usd"], 900.0)
        # LIMIT: 시세×1.005 호가 반올림
        plan = rules.size_orders(cands[:1], 100000, {}, cfg_with(sizing__order_type="LIMIT",
                                                                  sizing__limit_offset_pct=0.5), quotes)
        self.assertEqual(plan[0]["order_type"], "LIMIT")
        self.assertAlmostEqual(plan[0]["price"], 150.75)
        self.assertEqual(plan[0]["quantity"], 6)
        # 시세 없음
        plan = rules.size_orders(cands[:1], 100000, {}, cfg_with(), {})
        self.assertEqual(len(plan), 0)
        self.assertIn("시세 없음", plan.skipped[0]["reason"])
        # client id prefix
        plan = rules.size_orders(cands[:1], 100000, {}, cfg_with(), quotes, client_id_prefix="ab-2026-09-25")
        self.assertEqual(plan[0]["client_order_id"], "ab-2026-09-25-DELL")

    def test_pending_buy_counts_as_held_and_toward_max_positions(self):
        """미체결(OPEN) 매수 주문은 보유처럼 취급: 같은 종목 재주문 금지 + max_positions 자리 소비."""
        cands = [{"symbol": s, "name": s, "score": 1.0, "reason": "r"} for s in ["AAPL", "MSFT", "NVDA"]]
        quotes = {"AAPL": 100.0, "MSFT": 400.0, "NVDA": 120.0}
        open_orders = [{"symbol": "AAPL", "side": "BUY", "status": "OPEN", "orderType": "LIMIT"}]
        plan = rules.size_orders(cands, 10000, {}, cfg_with(sizing__max_positions=2, sizing__weekly_cap_usd=9999),
                                 quotes, open_orders=open_orders)
        self.assertEqual([o["symbol"] for o in plan], ["MSFT"])
        by = {s["symbol"]: s["reason"] for s in plan.skipped}
        self.assertTrue(by["AAPL"].startswith(rules.REASON_PENDING))
        self.assertIn("max_positions(2)", by["NVDA"])
        self.assertIn("미체결 1", by["NVDA"])
        # 완료된 주문(FILLED/CANCELLED/EXPIRED) 이나 SELL 은 대기로 치지 않음. live 브로커 키(order_id 등)도 무해
        done = [{"symbol": "AAPL", "side": "BUY", "status": "EXPIRED"},
                {"symbol": "MSFT", "side": "SELL", "status": "OPEN"},
                {"symbol": "NVDA", "side": "BUY", "status": "CANCELLED", "order_id": "x"}]
        self.assertEqual(rules.pending_buy_symbols(done), set())
        self.assertEqual(rules.pending_buy_symbols([{"symbol": "nvda", "side": "buy"}]), {"NVDA"})

    def test_skip_if_held_false_add_on_buy_does_not_consume_max_positions(self):
        """skip_if_held=false 로 보유 종목 추가 매수 → 이미 max_positions 에 도달해 있어도 허용되고
        신규 종목 자리를 하나 잃지도 않는다."""
        held = {"AAPL": {"qty": 1, "avg_price": 100}, "MSFT": {"qty": 1, "avg_price": 100}}
        quotes = {"AAPL": 100.0, "MSFT": 400.0, "NVDA": 120.0}
        cands = [{"symbol": s, "name": s, "score": 1.0, "reason": "r"} for s in ["AAPL", "NVDA"]]
        plan = rules.size_orders(cands, 10000, held, cfg_with(sizing__max_positions=2, sizing__skip_if_held=False,
                                                              sizing__weekly_cap_usd=9999), quotes)
        self.assertEqual([o["symbol"] for o in plan], ["AAPL"])            # (a) 보유 종목 추가 매수 허용
        self.assertIn("max_positions(2)", plan.skipped[0]["reason"])       # NVDA 는 진짜 신규라 차단
        plan = rules.size_orders(cands, 10000, held, cfg_with(sizing__max_positions=3, sizing__skip_if_held=False,
                                                              sizing__weekly_cap_usd=9999), quotes)
        self.assertEqual([o["symbol"] for o in plan], ["AAPL", "NVDA"])    # (b) 추가 매수가 자리를 안 먹음

    def test_cash_buffer_matches_broker_need(self):
        """rules 의 현금 버퍼 (1+c)(1+s)−1 == 브로커 필요금액 식 → 계획 OK 인 마지막 주문이 브로커에서
        insufficient-buying-power 로 거절되는 일이 없다 (리뷰 예: 현금 $681.02, 시세 $2, 수량주문)."""
        cfg = cfg_with(sizing__use_amount_orders=False, sizing__per_stock_usd=100000,
                       sizing__weekly_cap_usd=999999, sizing__min_order_usd=1)
        c, s = 0.001, 0.0005
        self.assertAlmostEqual(rules.cash_buffer_pct(cfg), (1 + c) * (1 + s) - 1, places=12)
        cand = [{"symbol": "AAPL", "name": "AAPL", "score": 1.0, "reason": "r"}]
        import itertools
        bad = []
        for cash_c, q_c in itertools.product(range(50000, 400001, 1373), range(200, 5001, 137)):
            cash, q = cash_c / 100.0, q_c / 100.0
            plan = rules.size_orders(cand, cash, {}, cfg, {"AAPL": q})
            if not plan:
                continue
            need = plan[0]["quantity"] * q * (1 + s) * (1 + c)
            if need > cash + 1e-9:
                bad.append((cash, q, plan[0]["quantity"], need))
            self.assertGreaterEqual(plan.cash_after, -1e-9)
        self.assertEqual(bad, [])
        # 실제 브로커로 리뷰 예시 재현
        b = PaperBroker(os.path.join(tempfile.mkdtemp(prefix="paper_buf_"), "s.json"), FakeQuote({"AAPL": 2.0}),
                        FakeSession(), dict(PAPER_CFG, initial_cash_usd=681.02),
                        calendar_fn=FakeSession().calendar, now_fn=FakeSession().now_fn)
        plan = rules.size_orders(cand, b.buying_power("USD"), {}, cfg, {"AAPL": 2.0})
        r = b.place_order("AAPL", "BUY", "MARKET", quantity=plan[0]["quantity"])
        self.assertEqual(r["status"], "FILLED", r)
        shutil.rmtree(os.path.dirname(b.state_path), ignore_errors=True)

    def test_blank_ticker_rows_do_not_consume_top_n(self):
        import pandas as pd
        df = watch_df()
        df.loc[len(df)] = [8, None, "빈티커", "IT", None, 5.0]                # 점수 최고지만 티커 없음
        df.loc[len(df)] = [9, float("nan"), "NaN티커", "IT", None, 4.0]
        df.loc[len(df)] = [10, "  ", "공백티커", "IT", None, 3.5]
        cands, dropped = rules.select_candidates(df, cfg_with(source__top_n=3), return_dropped=True)
        self.assertEqual([c["symbol"] for c in cands], ["DELL", "HPE", "VLO"])
        self.assertEqual(sum(1 for d in dropped if d["reason"] == "티커 없음"), 3)
        self.assertNotIn("NAN", [c["symbol"] for c in cands])

    def test_config_int_validation_message(self):
        with self.assertRaises(ValueError) as cm:
            rules.select_candidates(watch_df(), cfg_with(source__top_n="abc"))
        self.assertIn("source.top_n", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            rules.size_orders([], 100, {}, cfg_with(sizing__max_positions="x"), {})
        self.assertIn("sizing.max_positions", str(cm.exception))
        self.assertEqual(len(rules.select_candidates(watch_df(), cfg_with(source__top_n=2.0))), 2)  # 2.0 은 허용


# ── auto_buy 통합 (토스 호출 0건) ─────────────────────────────────────────
class TestAutoBuy(Base):
    @classmethod
    def setUpClass(cls):
        cls.logdir = tempfile.mkdtemp(prefix="paper_log_")
        auto_buy.setup_logging(cls.logdir)
        import toss_api
        cls.toss = toss_api
        cls.saved = {}

        def _boom(*a, **k):
            raise AssertionError("toss_api 호출 금지 (paper 모드에서 실호출 발생)")
        for name in ("_request", "create_order", "modify_order", "cancel_order",
                     "create_conditional_order", "reserve_order", "get_prices", "get_access_token"):
            cls.saved[name] = getattr(toss_api, name, None)
            setattr(toss_api, name, _boom)

    @classmethod
    def tearDownClass(cls):
        for name, fn in cls.saved.items():
            if fn is None:
                delattr(cls.toss, name)
            else:
                setattr(cls.toss, name, fn)
        shutil.rmtree(cls.logdir, ignore_errors=True)

    def setUp(self):
        super().setUp()
        import pandas as pd
        self.xlsx = os.path.join(self.tmp, "wl.xlsx")
        with pd.ExcelWriter(self.xlsx, engine="openpyxl") as xw:
            watch_df().to_excel(xw, sheet_name="주도주", index=False)
            pd.DataFrame({"항목": ["기준일", "유니버스"], "값": ["2026-09-25", "7종"]}).to_excel(
                xw, sheet_name="설명", index=False)
        self.state = os.path.join(self.tmp, "paper", "state.json")
        self.cfg_path = os.path.join(self.tmp, "cfg.json")
        self.q = FakeQuote({"DELL": 150.0, "HPE": 25.0, "VLO": 180.0, "MU": 1000.0, "ILMN": 90.0,
                            "P": 42.0, "TWLO": 120.0})
        self.s = FakeSession("regularMarket")
        self.inj = dict(quote_fn=self.q, session_fn=self.s, calendar_fn=self.s.calendar,
                        now_fn=self.s.now_fn, fx_fn=lambda: 1400.0)
        sys.modules.pop("broker_live", None)

    def tearDown(self):
        sys.modules.pop("broker_live", None)
        os.environ.pop("AUTO_BUY_LIVE_OK", None)
        super().tearDown()

    def write_cfg(self, mode="paper", **over):
        cfg = cfg_with(**over)
        cfg["mode"] = mode
        cfg["source"]["file"] = self.xlsx
        cfg["paper"]["state_path"] = self.state
        cfg["live"]["account_seq"] = 1
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        return self.cfg_path

    def run_ab(self, *argv):
        return auto_buy.main(list(argv) + ["--config", self.cfg_path], **self.inj)

    def state_json(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def test_plan_writes_last_plan_without_state_change(self):
        self.write_cfg()
        rc = self.run_ab("plan")
        self.assertEqual(rc, 0)
        lp = json.load(open(os.path.join(self.tmp, "paper", "last_plan.json"), encoding="utf-8"))
        self.assertEqual(lp["asof"], "2026-09-25")
        self.assertEqual([o["symbol"] for o in lp["orders"]], ["DELL", "HPE", "VLO"])   # cap 3000 / 1000
        self.assertEqual(self.state_json()["fills"], [])
        self.assertIsNone(self.state_json()["meta"]["last_run_asof"])

    def test_run_fills_then_run_twice_guard(self):
        self.write_cfg()
        rc = self.run_ab("run")
        self.assertEqual(rc, 0)
        st = self.state_json()
        self.assertEqual(len(st["fills"]), 3)
        self.assertEqual(st["meta"]["last_run_asof"], "2026-09-25")
        self.assertIsNotNone(st["meta"]["last_run_ts"])
        self.assertAlmostEqual(st["cash"]["USD"], 10000 - sum(f["grossValue"] + f["commission"] for f in st["fills"]), places=4)
        rc = self.run_ab("run")                       # 같은 기준일 → 거부
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.state_json()["fills"]), 3)
        rc = self.run_ab("run", "--force")            # 강제 → 이미 보유라 skip, 나머지 후보(ILMN,P)는 cap
        st = self.state_json()
        self.assertEqual(len(st["fills"]), 5)
        self.assertEqual(sorted(f["symbol"] for f in st["fills"]), ["DELL", "HPE", "ILMN", "P", "VLO"])
        # 상태·리포트
        self.assertEqual(self.run_ab("status"), 0)
        self.assertEqual(self.run_ab("tick"), 0)
        out = os.path.join(self.tmp, "rep.xlsx")
        self.assertEqual(self.run_ab("report", "--out", out), 0)
        import pandas as pd
        with pd.ExcelFile(out) as xl:
            self.assertEqual(xl.sheet_names, ["요약", "보유", "체결내역", "자산추이", "설정"])
            self.assertEqual(len(xl.parse("보유")), 5)
            summ = dict(zip(*[xl.parse("요약")[c] for c in ("항목", "값")]))
        self.assertIn("순손익(USD) = 실현 + 평가 = 총자산 − 초기자산", summ)
        self.assertAlmostEqual(float(summ["순손익(USD) = 실현 + 평가 = 총자산 − 초기자산"]),
                               float(summ["총자산(USD)"]) - float(summ["초기자산(USD)"]), places=1)
        self.assertEqual(self.run_ab("reset", "--yes"), 0)
        self.assertEqual(self.state_json()["fills"], [])
        self.assertEqual(self.state_json()["cash"]["USD"], 10000)

    def test_run_outside_regular_all_rejected_no_fill(self):
        self.write_cfg()
        self.s.name = "preMarket"
        rc = self.run_ab("run")
        self.assertEqual(rc, 1)                        # 전부 거절 → 실패 코드, 기준일 기록 안 함
        st = self.state_json()
        self.assertEqual(st["fills"], [])
        self.assertEqual(len(st["rejected"]), 3)
        self.assertTrue(all(r["code"] == "order-hours-closed" for r in st["rejected"]))
        self.assertIsNone(st["meta"]["last_run_asof"])
        self.assertEqual(st["cash"]["USD"], 10000)

    def test_live_broker_never_touched_unless_fully_unlocked(self):
        touched = {"init": 0}
        fake = types.ModuleType("broker_live")

        class LiveBroker:
            def __init__(self, *a, **k):
                touched["init"] += 1
                raise AssertionError("LiveBroker instantiated")
        fake.LiveBroker = LiveBroker

        # ① paper 모드: broker_live import 없음, 실호출 없음
        self.write_cfg("paper")
        sys.modules["broker_live"] = fake
        self.assertEqual(self.run_ab("run"), 0)
        self.assertEqual(touched["init"], 0)
        self.assertEqual(self.run_ab("status"), 0)
        self.assertEqual(self.run_ab("plan"), 0)
        # ② paper 모드 + --live → 차단
        self.assertEqual(self.run_ab("run", "--live"), 3)
        self.assertEqual(touched["init"], 0)
        # ③ live 모드, --live 없음 → 차단 (paper 로 조용히 떨어지지도 않음)
        self.write_cfg("live")
        fills_before = len(self.state_json()["fills"])
        self.assertEqual(self.run_ab("run"), 3)
        self.assertEqual(self.run_ab("status"), 3)
        self.assertEqual(len(self.state_json()["fills"]), fills_before)
        self.assertEqual(touched["init"], 0)
        # ④ live + --live, 환경변수 없음 → 차단
        os.environ.pop("AUTO_BUY_LIVE_OK", None)
        self.assertEqual(self.run_ab("run", "--live"), 3)
        self.assertEqual(touched["init"], 0)
        # ⑤ 셋 다 충족(stdin 비대화형) → 비로소 LiveBroker 생성 시도 (가짜가 raise → rc 1)
        os.environ["AUTO_BUY_LIVE_OK"] = "1"
        import io
        real_stdin = sys.stdin
        sys.stdin = io.StringIO()                      # 비대화형 강제 (터미널에서 돌려도 input() 안 뜸)
        try:
            rc = self.run_ab("run", "--live")
        finally:
            sys.stdin = real_stdin
        self.assertEqual(touched["init"], 1)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.state_json()["fills"]), fills_before)

    def test_paper_run_does_not_import_real_broker_live(self):
        self.write_cfg("paper")
        self.assertNotIn("broker_live", sys.modules)
        self.assertEqual(self.run_ab("run"), 0)
        self.assertNotIn("broker_live", sys.modules)

    # ── 리뷰 반영: 빈 계획 / 시세 장애 / 입력 오류 / LIMIT 대기 ──
    def test_quote_outage_does_not_record_asof_and_retry_works(self):
        """시세가 전부 실패(get_prices 503 등) → 후보 전부 '시세 없음' → rc 1, last_run_asof 기록 안 함.
        시세가 돌아온 다음 실행은 [SKIP] 없이 정상 매수."""
        self.write_cfg()
        saved = dict(self.q.prices)
        self.q.prices.clear()                                   # quotes() 가 전부 None → 503 과 같은 효과
        rc = self.run_ab("run")
        self.assertEqual(rc, 1)
        st = self.state_json()
        self.assertEqual(st["fills"], [])
        self.assertIsNone(st["meta"]["last_run_asof"])
        self.q.prices.update(saved)
        self.assertEqual(self.run_ab("run"), 0)
        st = self.state_json()
        self.assertEqual(len(st["fills"]), 3)
        self.assertEqual(st["meta"]["last_run_asof"], "2026-09-25")

    def _write_xlsx(self, df, asof):
        import pandas as pd
        with pd.ExcelWriter(self.xlsx, engine="openpyxl") as xw:
            df.to_excel(xw, sheet_name="주도주", index=False)
            pd.DataFrame({"항목": ["기준일"], "값": [asof]}).to_excel(xw, sheet_name="설명", index=False)

    def test_empty_sheet_or_top_n_zero_does_not_record_asof(self):
        self.write_cfg()
        self._write_xlsx(watch_df().iloc[0:0], "2026-10-09")               # 헤더만
        self.assertEqual(self.run_ab("run"), 1)
        self.assertIsNone(self.state_json()["meta"]["last_run_asof"])
        self._write_xlsx(watch_df(), "2026-10-09")
        self.write_cfg(source__top_n=0)
        self.assertEqual(self.run_ab("run"), 1)
        self.assertIsNone(self.state_json()["meta"]["last_run_asof"])
        # 정당한 0건(전부 이미 보유) 은 기록한다
        self.write_cfg(sizing__weekly_cap_usd=9999)
        self.assertEqual(self.run_ab("run"), 0)
        self.assertEqual(len(self.state_json()["fills"]), 5)
        self.assertEqual(self.state_json()["meta"]["last_run_asof"], "2026-10-09")
        self._write_xlsx(watch_df(), "2026-10-16")                          # 다음 주: 후보 5 전부 보유 중
        self.assertEqual(self.run_ab("run"), 0)
        self.assertEqual(len(self.state_json()["fills"]), 5)
        self.assertEqual(self.state_json()["meta"]["last_run_asof"], "2026-10-16")

    def test_bad_quote_value_skips_only_that_symbol(self):
        self.write_cfg()
        self.q.prices["DELL"] = "abc"                                      # 배치 시세 소스가 이상값
        self.assertEqual(self.run_ab("plan"), 0)
        lp = json.load(open(os.path.join(self.tmp, "paper", "last_plan.json"), encoding="utf-8"))
        self.assertEqual([o["symbol"] for o in lp["orders"]], ["HPE", "VLO", "ILMN"])
        self.assertTrue(any(s["symbol"] == "DELL" and "시세 없음" in s["reason"] for s in lp["skipped"]))
        self.assertEqual(auto_buy._coerce_quotes({"A": "1.5", "B": float("nan"), "C": 0, "D": -2, "E": None, "F": "x"}),
                         {"A": 1.5})

    def test_operator_mistakes_give_rc1_without_state_change(self):
        # (a) 워치리스트 파일 없음
        self.write_cfg()
        cfg = json.load(open(self.cfg_path, encoding="utf-8"))
        cfg["source"]["file"] = os.path.join(self.tmp, "없음.xlsx")
        json.dump(cfg, open(self.cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
        self.assertEqual(self.run_ab("run"), 1)
        # (b) 시트 이름 없음
        self.write_cfg(source__sheet="없는시트")
        self.assertEqual(self.run_ab("plan"), 1)
        # (c) top_n 이 정수가 아님
        self.write_cfg(source__top_n="abc")
        self.assertEqual(self.run_ab("run"), 1)
        st = self.state_json()
        self.assertEqual(st["fills"], [])
        self.assertIsNone(st["meta"]["last_run_asof"])
        # (d) 상태 파일 손상 → StateFileError 한 줄, rc 1
        self.write_cfg()
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.assertEqual(self.run_ab("status"), 1)
        with self.assertRaises(broker_paper.StateFileError):
            PaperBroker(self.state, self.q, self.s, PAPER_CFG)
        self.assertTrue(os.path.exists(self.state))                         # 손상 파일을 지우지 않음

    def test_limit_pending_only_does_not_record_asof(self):
        """LIMIT 설정: 즉시 체결 안 된 지정가만 남으면 기준일을 기록하지 않는다.
        대기 중 재실행은 같은 종목을 '미체결 매수 대기' 로 건너뛰고(중복 주문 없음), 기록도 안 한다.
        tick 으로 체결된 뒤의 재실행은 skip_if_held 로 0건 → 그때 기록."""
        self.write_cfg(sizing__order_type="LIMIT", sizing__limit_offset_pct=-5.0,   # 시세보다 5% 낮은 지정가
                       source__top_n=3)
        self.assertEqual(self.run_ab("run"), 0)
        st = self.state_json()
        self.assertEqual(st["fills"], [])
        self.assertEqual(len(st["open_orders"]), 3)
        self.assertIsNone(st["meta"]["last_run_asof"])
        self.assertEqual(self.run_ab("run"), 0)                            # 대기 중 재실행
        st = self.state_json()
        self.assertEqual(len(st["open_orders"]), 3)                        # 중복 접수 없음
        self.assertIsNone(st["meta"]["last_run_asof"])
        lp = json.load(open(os.path.join(self.tmp, "paper", "last_plan.json"), encoding="utf-8"))
        self.assertEqual(lp["orders"], [])
        self.assertTrue(all(s["reason"].startswith(rules.REASON_PENDING) for s in lp["skipped"]))
        # 미체결 예약금은 주간 한도에서 빠진다 (top_n 을 늘려 재실행해도 한도 초과 주문이 안 나감)
        self.write_cfg(sizing__order_type="LIMIT", sizing__limit_offset_pct=-5.0, source__top_n=5,
                       sizing__weekly_cap_usd=2900)
        self.assertEqual(self.run_ab("plan"), 0)
        lp = json.load(open(os.path.join(self.tmp, "paper", "last_plan.json"), encoding="utf-8"))
        self.assertEqual(lp["orders"], [])
        self.assertTrue(any("미체결 예약" in s["reason"] or "예산" in s["reason"] or "지정가" in s["reason"]
                            for s in lp["skipped"] if s["symbol"] in ("ILMN", "P")))
        self.write_cfg(sizing__order_type="LIMIT", sizing__limit_offset_pct=-5.0, source__top_n=3)
        for sym in ("DELL", "HPE", "VLO"):                                 # 시세 하락 → tick 에서 체결
            self.q.set(sym, self.q.prices[sym] * 0.9)
        self.assertEqual(self.run_ab("tick"), 0)
        st = self.state_json()
        self.assertEqual(len(st["fills"]), 3)
        self.assertEqual(st["open_orders"], [])
        self.assertEqual(self.run_ab("run"), 0)                            # 이제 전부 보유 → 정당한 0건 → 기록
        self.assertEqual(self.state_json()["meta"]["last_run_asof"], "2026-09-25")


if __name__ == "__main__":
    unittest.main(verbosity=2)

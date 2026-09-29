#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_strategy.py — v2 규칙 (strategy.py + auto_buy v2 흐름 + replay v2) 검증. 토스 API 호출 없음.
  실행: cd /data/frame && ./.venv/bin/python -m unittest tests.test_strategy -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import strategy                                                # noqa: E402
import rules                                                   # noqa: E402
import auto_buy                                                # noqa: E402
import replay                                                  # noqa: E402
from broker_paper import KST, synthetic_calendar               # noqa: E402
from tests.test_paper import FakeQuote, FakeSession            # noqa: E402
from tests.test_replay import bdays, watchlist                 # noqa: E402


def v2cfg(mode="B", **paper):
    cfg = json.loads(json.dumps(auto_buy.DEFAULT_CONFIG))
    cfg["source"].update({"top_n": 50, "exclude_if_주의": False})
    cfg["sizing"].update({"method": "score_weight", "max_positions": 50, "min_order_usd": 20})
    cfg["rebalance"] = {"mode": mode, "topup_threshold_pct": 30}
    cfg["exit"].update({"enabled": True, "trailing_stop_pct": 15, "gate_absent_weeks": 2})
    cfg["paper"].update({"slippage_bps": 5, "commission_pct": 0.1})
    cfg["paper"].update(paper)
    return cfg


def cands(pairs):
    return [{"symbol": s, "name": s, "score": sc, "sector": "", "rank": i + 1} for i, (s, sc) in enumerate(pairs)]


class TestWeights(unittest.TestCase):
    def test_weights_sum_to_one_and_proportional(self):
        w = strategy.target_weights(cands([("A", 2.0), ("B", 1.0), ("C", 1.0)]))
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertAlmostEqual(w["A"], 0.5)
        self.assertAlmostEqual(w["B"], 0.25)

    def test_missing_or_negative_score_gets_floor(self):
        w = strategy.target_weights(cands([("A", 1.0), ("B", None), ("C", -0.3)]))
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertAlmostEqual(w["B"] / w["A"], 0.1)
        self.assertAlmostEqual(w["C"] / w["A"], 0.1)

    def test_client_id_sanitized(self):
        self.assertEqual(strategy.client_id("ab-2026-09-25", "BRK.B"), "ab-2026-09-25-BRK_B")


class TestPlanBuys(unittest.TestCase):
    def setUp(self):
        self.cfg = v2cfg()
        self.buf = rules.cash_buffer_pct(self.cfg)
        self.c = cands([("A", 3.0), ("B", 2.0), ("C", 1.0)])
        self.q = {"A": 100.0, "B": 50.0, "C": 10.0}

    def test_first_week_all_cash_scaled_by_weight(self):
        plan = strategy.plan_buys(self.c, {}, self.q, 6000.0, self.cfg, "2026-09-25")
        amts = {o["symbol"]: o["order_amount"] for o in plan}
        self.assertEqual(set(amts), {"A", "B", "C"})
        self.assertLessEqual(sum(amts.values()) * (1 + self.buf), 6000.0 + 1e-6)
        self.assertAlmostEqual(amts["A"] / amts["B"], 1.5, places=3)
        self.assertAlmostEqual(amts["B"] / amts["C"], 2.0, places=3)
        self.assertLess(plan.scale, 1.0)                     # 수수료·슬리피지 버퍼만큼 축소
        self.assertGreater(plan.scale, 0.99)
        self.assertGreaterEqual(plan.cash_after, -1e-6)

    def test_cash_short_new_entrant_scaled_and_held_counts_in_equity(self):
        held = {"A": {"qty": 30.0, "avg_price": 100.0}, "B": {"qty": 40.0, "avg_price": 50.0}}   # $3000 + $2000
        plan = strategy.plan_buys(self.c, held, self.q, 600.0, self.cfg, "x")
        self.assertAlmostEqual(plan.equity, 5600.0)
        new = [o for o in plan if o["kind"] == "new"]
        self.assertEqual([o["symbol"] for o in new], ["C"])
        target_c = 5600.0 / 6
        self.assertLess(new[0]["order_amount"], target_c)           # 현금 600 < 목표 933 → 축소
        self.assertAlmostEqual(new[0]["order_amount"], strategy.floor2(600 / (1 + self.buf)), places=2)

    def test_min_order_skip(self):
        plan = strategy.plan_buys(cands([("A", 100.0), ("B", 0.1)]), {}, {"A": 10.0, "B": 10.0}, 1000.0,
                                  self.cfg, "x")
        self.assertEqual([o["symbol"] for o in plan], ["A"])
        self.assertTrue(any("min_order_usd" in s["reason"] for s in plan.skipped))

    def _topup_case(self, held_value_c, mode="B"):
        cfg = v2cfg(mode)
        # 목표를 쉽게 맞추려고 C 만 목록에 두고 현금 충분
        c = cands([("C", 1.0)])
        equity_target = 1000.0
        cash = equity_target - held_value_c
        held = {"C": {"qty": held_value_c / 10.0, "avg_price": 10.0}}
        return strategy.plan_buys(c, held, {"C": 10.0}, cash, cfg, "x")

    def test_topup_threshold_30pct(self):
        # 목표 1000. 보유 710 → 부족 29% → 추가매수 없음. 보유 690 → 부족 31% → 추가매수
        self.assertEqual(len(self._topup_case(710.0)), 0)
        p = self._topup_case(690.0)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["kind"], "topup")
        self.assertAlmostEqual(p[0]["order_amount"], strategy.floor2(min(310.0, 310.0 / (1 + self.buf))), places=2)

    def test_mode_a_never_topups_mode_c_trims(self):
        self.assertEqual(len(self._topup_case(500.0, mode="A")), 0)
        self.assertEqual(len(self._topup_case(500.0, mode="B")), 1)
        cfg = v2cfg("C")
        c = cands([("A", 1.0), ("B", 1.0)])
        held = {"A": {"qty": 80.0, "avg_price": 10.0}, "B": {"qty": 20.0, "avg_price": 10.0}}   # 800 / 200, 목표 500/500
        trims = strategy.plan_trims(c, held, {"A": 10.0, "B": 10.0}, 0.0, cfg, "x")
        self.assertEqual([(t["symbol"], round(t["quantity"], 6)) for t in trims], [("A", 30.0)])
        self.assertEqual(strategy.plan_trims(c, held, {"A": 10.0, "B": 10.0}, 0.0, v2cfg("B"), "x"), [])

    def test_sold_now_not_rebought_and_pending_buy_skipped(self):
        plan = strategy.plan_buys(self.c, {}, self.q, 6000.0, self.cfg, "x", exclude={"A"},
                                  open_orders=[{"symbol": "B", "side": "BUY", "status": "OPEN"}])
        self.assertEqual([o["symbol"] for o in plan], ["C"])
        reasons = {s["symbol"]: s["reason"] for s in plan.skipped}
        self.assertEqual(reasons["A"], strategy.REASON_SOLD_NOW)
        self.assertTrue(reasons["B"].startswith(rules.REASON_PENDING))


class TestExitState(unittest.TestCase):
    def test_high_water_and_trailing_trigger_at_exactly_15pct(self):
        es = strategy.sync_exit_state({}, {"A": {"qty": 1.0, "avg_price": 100.0}})
        self.assertEqual(es["A"]["high_water"], 100.0)
        strategy.mark_high_water(es, {"A": 120.0})
        strategy.mark_high_water(es, {"A": 110.0})
        self.assertEqual(es["A"]["high_water"], 120.0)
        self.assertEqual(strategy.check_trailing(es, {"A": 102.01}, 15, "t"), [])
        self.assertEqual(strategy.check_trailing(es, {"A": 102.0}, 15, "t"), ["A"])     # 120×0.85 = 102 (≤)
        self.assertTrue(es["A"]["pending_exit"]["reason"].startswith(strategy.REASON_TRAIL))

    def test_missing_quote_does_not_reset_high_water(self):
        es = strategy.sync_exit_state({}, {"A": {"qty": 1.0, "avg_price": 100.0}})
        strategy.mark_high_water(es, {"A": 130.0})
        strategy.mark_high_water(es, {})
        strategy.mark_high_water(es, {"A": None})
        self.assertEqual(es["A"]["high_water"], 130.0)

    def test_gate_absence_counts_resets_and_triggers(self):
        es = strategy.sync_exit_state({}, {"A": {"qty": 1, "avg_price": 1}, "B": {"qty": 1, "avg_price": 1}})
        self.assertEqual(strategy.update_gate_absence(es, {"B"}, 2, "w1"), [])
        self.assertEqual(es["A"]["absent_weeks"], 1)
        strategy.update_gate_absence(es, {"A", "B"}, 2, "w2")                 # 다시 들어오면 0
        self.assertEqual(es["A"]["absent_weeks"], 0)
        strategy.update_gate_absence(es, {"B"}, 2, "w3")
        self.assertEqual(strategy.update_gate_absence(es, {"B"}, 2, "w4"), ["A"])
        self.assertTrue(es["A"]["pending_exit"]["reason"].startswith(strategy.REASON_GATE))

    def test_sync_drops_sold_positions(self):
        es = strategy.sync_exit_state({}, {"A": {"qty": 1, "avg_price": 1}})
        strategy.sync_exit_state(es, {})
        self.assertEqual(es, {})


# ── auto_buy v2 흐름 (페이퍼, 가짜 시세·세션) ───────────────────────────────
class TestAutoBuyV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logdir = tempfile.mkdtemp(prefix="v2_log_")
        auto_buy.setup_logging(cls.logdir)
        import toss_api
        cls.toss, cls.saved = toss_api, {}

        def _boom(*a, **k):
            raise AssertionError("toss_api 호출 금지")
        for name in ("_request", "create_order", "modify_order", "cancel_order", "create_conditional_order",
                     "reserve_order", "get_prices", "get_access_token", "get_exchange_rate"):
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
        self.tmp = tempfile.mkdtemp(prefix="v2_test_")
        self.xlsx = os.path.join(self.tmp, "wl.xlsx")
        self.state = os.path.join(self.tmp, "paper", "state.json")
        self.cfg_path = os.path.join(self.tmp, "cfg.json")
        self.q = FakeQuote({"AAA": 100.0, "BBB": 50.0, "CCC": 20.0, "DDD": 10.0})
        self.s = FakeSession("regularMarket", now=datetime(2026, 9, 28, 23, 20, tzinfo=KST))
        self.inj = dict(quote_fn=self.q, session_fn=self.s, calendar_fn=self.s.calendar,
                        now_fn=self.s.now_fn, fx_fn=lambda: 1400.0)
        self.write_wl(["AAA", "BBB", "CCC"], "2026-09-25")
        cfg = v2cfg(initial_cash_usd=0, initial_cash_krw=10_000_000, convert_at_start=True, fx_rate=1400.0)
        cfg["source"]["file"] = self.xlsx
        cfg["paper"]["state_path"] = self.state
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_wl(self, tickers, asof, scores=None):
        scores = scores or {"AAA": 3.0, "BBB": 2.0, "CCC": 1.0, "DDD": 1.5}
        df = pd.DataFrame({"순위": range(1, len(tickers) + 1), "티커": tickers, "종목명": tickers,
                           "섹터": "IT", "주의": None, "주도주점수": [scores[t] for t in tickers]})
        with pd.ExcelWriter(self.xlsx, engine="openpyxl") as xw:
            df.to_excel(xw, sheet_name="주도주", index=False)
            pd.DataFrame({"항목": ["기준일"], "값": [asof]}).to_excel(xw, sheet_name="설명", index=False)

    def ab(self, *argv):
        return auto_buy.main(list(argv) + ["--config", self.cfg_path], **self.inj)

    def st(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def test_convert_at_start_math(self):
        self.assertEqual(self.ab("status"), 0)
        st = self.st()
        self.assertAlmostEqual(st["cash"]["USD"], strategy.floor2(10_000_000 / (1400.0 * 1.0005)), places=2)
        self.assertEqual(st["cash"]["KRW"], 0.0)
        self.assertEqual(st["meta"]["initial_krw"], 10_000_000)

    def test_entry_window_gating(self):
        self.s.now = datetime(2026, 9, 28, 23, 14, tzinfo=KST)          # 22:30 + 45분 = 23:15 전
        self.assertEqual(self.ab("run"), 3)
        self.assertEqual(self.st()["fills"], [])
        self.s.now = datetime(2026, 9, 29, 4, 1, tzinfo=KST)            # 05:00 − 1h 이후
        self.assertEqual(self.ab("run"), 3)
        self.s.now = datetime(2026, 9, 28, 23, 15, tzinfo=KST)
        self.assertEqual(self.ab("run"), 0)
        self.assertEqual(len(self.st()["fills"]), 3)
        self.assertEqual(self.ab("run"), 3)                             # 같은 기준일 재실행 차단

    def test_three_week_walk(self):
        self.assertEqual(self.ab("run"), 0)                             # 1주차: 3종목 전부 신규
        st = self.st()
        usd0 = st["meta"]["initial_equity_usd"]
        buys = {f["symbol"]: f["grossValue"] for f in st["fills"]}
        self.assertAlmostEqual(buys["AAA"] / buys["CCC"], 3.0, places=2)
        self.assertLessEqual(sum(buys.values()), usd0)
        self.assertGreaterEqual(st["cash"]["USD"], 0.0)

        # 화 06:10 tick: BBB 가 −20% → 추적손절 대기
        self.q.set("BBB", 40.0)
        self.s.now = datetime(2026, 9, 29, 6, 10, tzinfo=KST)
        self.s.name = "afterMarket"
        self.assertEqual(self.ab("tick"), 0)
        es = self.st()["meta"]["exit_state"]
        self.assertTrue(es["BBB"]["pending_exit"]["reason"].startswith(strategy.REASON_TRAIL))
        self.assertIsNone(es["AAA"]["pending_exit"])

        # 화 23:15 exits: BBB 만 매도, 매수 없음
        self.s.cal_day = synthetic_calendar(
            datetime(2026, 9, 29, 23, 15, tzinfo=KST))
        self.s.now = datetime(2026, 9, 29, 23, 15, tzinfo=KST)
        self.s.name = "regularMarket"
        n_fills = len(self.st()["fills"])
        self.assertEqual(self.ab("exits"), 0)
        st = self.st()
        new = st["fills"][n_fills:]
        self.assertEqual([(f["symbol"], f["side"]) for f in new], [("BBB", "SELL")])
        self.assertNotIn("BBB", st["positions"])
        self.assertEqual(self.ab("exits"), 0)                           # 대기 없음 → 할 일 없음

        # 2주차: 목록에서 AAA·CCC 빠지고 DDD 신규 → 탈락 1주, DDD 매수
        self.write_wl(["DDD"], "2026-10-02")
        self.s.cal_day = synthetic_calendar(datetime(2026, 10, 5, 23, 20, tzinfo=KST))
        self.s.now = datetime(2026, 10, 5, 23, 20, tzinfo=KST)
        self.assertEqual(self.ab("run"), 0)
        st = self.st()
        es = st["meta"]["exit_state"]
        self.assertEqual(es["AAA"]["absent_weeks"], 1)
        self.assertIn("DDD", st["positions"])
        self.assertIn("AAA", st["positions"])
        # 같은 기준일로 --force 해도 탈락 주수는 한 번만
        self.assertEqual(self.ab("run", "--force"), 0)
        self.assertEqual(self.st()["meta"]["exit_state"]["AAA"]["absent_weeks"], 1)

        # 3주차: 여전히 빠짐 → AAA·CCC 게이트탈락 매도 → 판 현금으로 DDD 추가매수(B)
        self.write_wl(["DDD"], "2026-10-09")
        self.s.cal_day = synthetic_calendar(datetime(2026, 10, 12, 23, 20, tzinfo=KST))
        self.s.now = datetime(2026, 10, 12, 23, 20, tzinfo=KST)
        n_fills = len(self.st()["fills"])
        self.assertEqual(self.ab("run"), 0)
        st = self.st()
        new = [(f["symbol"], f["side"]) for f in st["fills"][n_fills:]]
        self.assertEqual(new[:2], [("AAA", "SELL"), ("CCC", "SELL")])          # 매도 먼저
        self.assertIn(("DDD", "BUY"), new[2:])                                  # 그 현금으로 추가매수
        self.assertEqual(set(st["positions"]), {"DDD"})
        self.assertGreaterEqual(st["cash"]["USD"], 0.0)
        # 리포트 v2 컬럼
        self.assertEqual(self.ab("report", "--out", os.path.join(self.tmp, "r.xlsx")), 0)
        pos = pd.read_excel(os.path.join(self.tmp, "r.xlsx"), sheet_name="보유")
        for col in ("목표비중%", "보유비중%", "최고가", "손절가", "탈락주수", "매도대기"):
            self.assertIn(col, pos.columns)

    def test_ignore_hours_refused_in_live(self):
        cfg = json.load(open(self.cfg_path, encoding="utf-8"))
        cfg["mode"] = "live"
        cfg["live"]["account_seq"] = 1
        json.dump(cfg, open(self.cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
        os.environ.pop("AUTO_BUY_LIVE_OK", None)
        self.assertEqual(self.ab("run", "--live", "--ignore-hours"), 3)       # 환경변수 없음 → 차단
        self.assertEqual(self.ab("exits", "--live"), 3)
        self.assertFalse(os.path.exists(self.state) and self.st()["fills"])


# ── replay v2: 종가 추적손절 → 다음날 시가 매도 ────────────────────────────
class TestReplayV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="replay_v2_")
        self.state = os.path.join(self.tmp, "st.json")
        self.days = bdays("2024-12-30", "2025-01-31")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trailing_close_then_next_open_sell(self):
        o = pd.DataFrame(index=self.days)
        c = pd.DataFrame(index=self.days)
        # AAA: 1/6 시가 100 → 1/8 종가 120 (최고) → 1/10 종가 100 (−16.7%) → 1/13 시가 99 에 매도
        path_c = {"2025-01-06": 110, "2025-01-07": 115, "2025-01-08": 120, "2025-01-09": 110, "2025-01-10": 100}
        o["AAA"] = [99.0 if d == "2025-01-13" else 100.0 for d in self.days]
        c["AAA"] = [path_c.get(d, 100.0) for d in self.days]
        o["BBB"], c["BBB"] = 50.0, 50.0
        panel = {"OPEN": o, "CLOSE": c}
        rows = [("AAA", "에이", "", 1.0), ("BBB", "비", "", 1.0)]

        def screen(asof):
            return {"watchlist": watchlist(rows), "universe": ["AAA", "BBB"], "cached": True}

        cfg = v2cfg("B", initial_cash_usd=10000)
        res = replay.simulate(["2025-01-06"], self.days, screen, panel, cfg, self.state,
                              end="2025-01-15", progress=False)
        sells = [t for t in res["trades"] if t["매매"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["티커"], "AAA")
        self.assertEqual(sells[0]["체결일"], "2025-01-13")
        self.assertAlmostEqual(sells[0]["체결가"], 99.0 * (1 - 5 / 1e4), places=6)
        self.assertTrue(sells[0]["비고"].startswith(strategy.REASON_TRAIL))
        self.assertEqual(res["counters"]["stop"], 1)

    def test_compare_variants_distinct(self):
        v = replay.variant_configs(v2cfg())
        self.assertEqual(list(v), ["v1_equal", "v2_A", "v2_B", "v2_C"])
        self.assertFalse(strategy.is_v2(v["v1_equal"]))
        self.assertEqual([v[k]["rebalance"]["mode"] for k in ("v2_A", "v2_B", "v2_C")], ["A", "B", "C"])




# ── 정기 리밸런싱 (N주마다) ────────────────────────────────────────────────
class TestPeriodicRebalance(unittest.TestCase):
    def test_rebalance_due(self):
        cfg = v2cfg("C")
        cfg["rebalance"]["every_weeks"] = 4
        self.assertEqual(strategy.rebalance_due(cfg, "2026-09-25", None), (True, None))
        self.assertFalse(strategy.rebalance_due(cfg, "2026-10-16", "2026-09-25")[0])     # 3주
        self.assertTrue(strategy.rebalance_due(cfg, "2026-10-23", "2026-09-25")[0])      # 4주
        self.assertTrue(strategy.rebalance_due(cfg, "2026-10-21", "2026-09-25")[0])      # 휴장으로 이틀 당겨짐
        cfg["rebalance"]["every_weeks"] = 1
        self.assertTrue(strategy.rebalance_due(cfg, "2026-09-26", "2026-09-25")[0])

    def test_trailing_off_when_pct_zero(self):
        es = strategy.sync_exit_state({}, {"A": {"qty": 1.0, "avg_price": 100.0}})
        self.assertEqual(strategy.check_trailing(es, {"A": 1.0}, 0, "t"), [])
        self.assertIsNone(es["A"]["pending_exit"])

    def test_rebal_variant_set(self):
        v = replay.variant_configs(v2cfg(), "rebal")
        self.assertEqual(strategy.every_weeks(v["R4"]), 4)
        self.assertEqual(v["R4"]["rebalance"]["mode"], "C")
        self.assertEqual(v["R4"]["exit"]["gate_absent_weeks"], 1)
        self.assertEqual(v["R4_top20"]["source"]["top_n"], 20)
        self.assertEqual(v["R4_stop15"]["exit"]["trailing_stop_pct"], 15)
        self.assertEqual(v["v2_B_weekly"]["rebalance"]["mode"], "B")
        self.assertIn("4주마다", strategy.rule_label(v["R4"]))


class TestAutoBuyEveryWeeks(TestAutoBuyV2):
    # 부모의 시나리오 테스트는 매주 설정 전제라 여기서는 돌리지 않는다 (setUp·헬퍼만 재사용)
    test_three_week_walk = test_entry_window_gating = test_convert_at_start_math = None
    test_ignore_hours_refused_in_live = None

    def setUp(self):
        super().setUp()
        cfg = json.load(open(self.cfg_path, encoding="utf-8"))
        cfg["rebalance"].update({"mode": "C", "every_weeks": 2})
        cfg["exit"]["gate_absent_weeks"] = 1
        json.dump(cfg, open(self.cfg_path, "w", encoding="utf-8"), ensure_ascii=False)

    def _week(self, day, asof, tickers):
        self.write_wl(tickers, asof)
        self.s.cal_day = synthetic_calendar(datetime(2026, 10, day, 23, 20, tzinfo=KST))
        self.s.now = datetime(2026, 10, day, 23, 20, tzinfo=KST)
        return self.ab("run")

    def test_skips_off_weeks_then_sells_dropped_and_rebalances(self):
        self.assertEqual(self._week(5, "2026-10-02", ["AAA", "BBB", "CCC"]), 0)      # 첫 리밸런스
        n = len(self.st()["fills"])
        self.assertEqual(self._week(12, "2026-10-09", ["DDD"]), 3)                    # 1주 뒤 → 건너뜀
        self.assertEqual(len(self.st()["fills"]), n)
        self.assertEqual(self._week(19, "2026-10-16", ["AAA", "DDD"]), 0)             # 2주 뒤 → 리밸런스
        st = self.st()
        new = [(f["symbol"], f["side"]) for f in st["fills"][n:]]
        self.assertIn(("BBB", "SELL"), new)                                           # 목록 밖 → 매도
        self.assertIn(("CCC", "SELL"), new)
        self.assertIn(("DDD", "BUY"), new)
        self.assertEqual(set(st["positions"]), {"AAA", "DDD"})
        self.assertEqual(st["meta"]["last_rebalance_asof"], "2026-10-16")


if __name__ == "__main__":
    unittest.main()

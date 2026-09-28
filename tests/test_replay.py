#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_replay.py — replay.py 의 DB 무관 부분 검증 (가짜 screen_fn + 합성 가격 패널).
  실행: cd /data/frame && ./.venv/bin/python -m unittest tests.test_replay -v
검증: 리밸런스 달력, 기준일/체결일 선택, 분할 감지·소급조정, 시가×슬리피지 체결·수수료,
      종가 평가 행, skip_if_held, 이탈 매도(weeks_absent), 현금소진 시점, 벤치마크 수식,
      엑셀 시트 구성, leader_screener(DB) 를 import 하지 않음.
"""
import math
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import replay                                   # noqa: E402


def bdays(a, b):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(a, b)]


def make_panel(days, prices):
    """prices: {sym: (open0, close0, step)} → 하루 step 씩 오르는 합성 시가/종가."""
    o = pd.DataFrame(index=days)
    c = pd.DataFrame(index=days)
    for sym, (o0, c0, step) in prices.items():
        o[sym] = [o0 + i * step for i in range(len(days))]
        c[sym] = [c0 + i * step for i in range(len(days))]
    return {"OPEN": o, "CLOSE": c}


def watchlist(rows):
    """rows: [(티커, 종목명, 주의, 점수)]"""
    return pd.DataFrame([{"순위": i + 1, "티커": t, "종목명": n, "섹터": "IT", "유형": "", "주의": w,
                          "주도주점수": s} for i, (t, n, w, s) in enumerate(rows)])


def base_cfg(**over):
    cfg = {
        "mode": "paper",
        "source": {"sheet": "주도주", "sort_by": "주도주점수", "top_n": 5, "exclude_if_주의": True,
                   "exclude_sectors": [], "exclude_tickers": []},
        "sizing": {"per_stock_usd": 1000, "max_positions": 10, "weekly_cap_usd": 3000,
                   "skip_if_held": True, "order_type": "MARKET", "use_amount_orders": True,
                   "limit_offset_pct": 0.5, "min_order_usd": 50},
        "exit": {"enabled": False, "rule": "drop_from_list", "weeks_absent": 2},
        "paper": {"initial_cash_usd": 10000, "initial_cash_krw": 0, "slippage_bps": 5,
                  "commission_pct": 0.1, "auto_fx": False},
    }
    for k, v in over.items():
        cfg[k] = {**cfg[k], **v} if isinstance(v, dict) else v
    return cfg


def floor6(x):
    return math.floor(x * 1e6 + 1e-9) / 1e6


class TestCalendar(unittest.TestCase):
    def test_rebalance_dates_weekly_and_monthly(self):
        self.assertEqual(replay.rebalance_dates("2025-01-06", "2025-02-10", "weekly"),
                         ["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27", "2025-02-03", "2025-02-10"])
        self.assertEqual(replay.rebalance_dates("2025-01-08", "2025-01-20", "weekly")[0], "2025-01-13")
        self.assertEqual(replay.rebalance_dates("2025-01-06", "2025-03-31", "monthly"),
                         ["2025-01-06", "2025-02-03", "2025-03-03"])
        self.assertEqual(replay.rebalance_dates("2026-03-02", "2026-09-25", "monthly"),
                         ["2026-03-02", "2026-04-06", "2026-05-04", "2026-06-01", "2026-07-06",
                          "2026-08-03", "2026-09-07"])
        self.assertEqual(replay.rebalance_dates("2025-01-07", "2025-01-12", "weekly"), [])
        with self.assertRaises(ValueError):
            replay.rebalance_dates("2025-01-06", "2025-02-10", "daily")

    def test_pick_asof_and_next_trading_day(self):
        days = [d for d in bdays("2025-01-01", "2025-01-31") if d != "2025-01-20"]   # 1/20 휴장
        self.assertEqual(replay.pick_asof("2025-01-13", days), "2025-01-10")   # 직전 금요일
        self.assertEqual(replay.pick_asof("2025-01-06", days), "2025-01-03")
        self.assertEqual(replay.pick_asof("2024-12-30", days), None)
        self.assertEqual(replay.next_trading_day("2025-01-13", days), "2025-01-13")
        self.assertEqual(replay.next_trading_day("2025-01-20", days), "2025-01-21")   # 휴장 → 화요일
        self.assertEqual(replay.next_trading_day("2025-02-03", days), None)


class TestSplits(unittest.TestCase):
    """분할은 daily_marcap_us.STOCKS(발행주식수) 가 같은 배수로 변한 경우에만 확정한다."""

    def _rows(self):
        days = bdays("2024-12-02", "2025-01-24")
        rows, stocks = [], []
        for i, d in enumerate(days):
            vol = 1_000_000
            # CCC: 1/20 부터 2:1 분할 (200 → 100), STOCKS 도 같은 날 ×2
            p = 200.0 if d < "2025-01-20" else 100.0
            rows.append({"CODE": "CCC", "D": d, "OPEN": p, "CLOSE": p + 1, "VOLUME": vol})
            stocks.append({"CODE": "CCC", "D": d, "STOCKS": 1_000_000 if d < "2025-01-20" else 2_000_000})
            # DDD: 1/20 에 -45% 급락 (정수비 아님 → 미조정)
            q = 100.0 if d < "2025-01-20" else 55.0
            rows.append({"CODE": "DDD", "D": d, "OPEN": q, "CLOSE": q + 0.5, "VOLUME": vol})
            stocks.append({"CODE": "DDD", "D": d, "STOCKS": 5_000_000})
            # MRNA 형(정확히 ×2 로 만든 버전): 시가가 전일종가의 정확히 2배(=1:2 역분할 후보), STOCKS 불변,
            # 거래량 36배 → 가격 이벤트로 분류, 미조정. (실제 MRNA 의 1.84배는 ±3% 규칙에서 후보조차 아님)
            rows.append({"CODE": "EEE", "D": d, "OPEN": 63.0 if d < "2025-01-20" else 126.0,
                         "CLOSE": 63.0 if d < "2025-01-20" else 130.0,
                         "VOLUME": 36_000_000 if d == "2025-01-20" else vol})
            stocks.append({"CODE": "EEE", "D": d, "STOCKS": 400_000_000})
            # AVB 형(정확히 ÷3): 전일종가 182 → 시가 60.667 (3:1 후보), STOCKS 불변, 거래량 12배 → 가격 이벤트
            a = 184.0 if d < "2025-01-20" else 182.0 / 3
            rows.append({"CODE": "FFF", "D": d, "OPEN": a, "CLOSE": a - 2, "VOLUME": 12_000_000 if d == "2025-01-20" else vol})
            stocks.append({"CODE": "FFF", "D": d, "STOCKS": 142_000_000})
            # APH 형: 1/20 부터 2:1 분할인데 STOCKS 는 하루 늦게(1/21) ×2 되고 1/22 에 잠깐 옛값으로 흔들림
            g = 160.0 if d < "2025-01-20" else 80.0
            rows.append({"CODE": "GGG", "D": d, "OPEN": g, "CLOSE": g + 0.5, "VOLUME": vol})
            st = 1_200_000 if d < "2025-01-21" or d == "2025-01-22" else 2_400_000
            stocks.append({"CODE": "GGG", "D": d, "STOCKS": st})
            # HHH: 정수비(2:1) 인데 STOCKS 데이터가 아예 없음 → 미조정(stocks-missing)
            h = 50.0 if d < "2025-01-20" else 25.0
            rows.append({"CODE": "HHH", "D": d, "OPEN": h, "CLOSE": h + 0.1, "VOLUME": vol})
            # III: 정수비 2:1 이지만 STOCKS 가 ×1.5 만 변함(±3% 밖) → stocks-unconfirmed
            v = 90.0 if d < "2025-01-20" else 45.0
            rows.append({"CODE": "III", "D": d, "OPEN": v, "CLOSE": v + 0.2, "VOLUME": vol})
            stocks.append({"CODE": "III", "D": d, "STOCKS": 1_000_000 if d < "2025-01-20" else 1_500_000})
        return pd.DataFrame(rows), pd.DataFrame(stocks)

    def test_split_confirmed_by_stocks_only(self):
        rows, stocks = self._rows()
        px, events = replay.adjust_splits(rows, stocks)
        by = {e["CODE"]: e for e in events}
        self.assertEqual(sorted(by), ["CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III"])
        self.assertTrue(by["CCC"]["applied"])
        self.assertEqual((by["CCC"]["D"], by["CCC"]["factor"], by["CCC"]["reason"]), ("2025-01-20", 2.0, "split"))
        self.assertAlmostEqual(by["CCC"]["stocks_ratio"], 2.0)
        self.assertTrue(by["GGG"]["applied"])                               # 하루 늦은 STOCKS + 흔들림도 확정
        self.assertEqual(by["GGG"]["factor"], 2.0)
        self.assertFalse(by["DDD"]["applied"])
        self.assertEqual(by["DDD"]["reason"], "not-integer-ratio")
        self.assertFalse(by["EEE"]["applied"])                              # MRNA 형: 옛 규칙이면 ×2 소급조정됐을 것
        self.assertEqual(by["EEE"]["reason"], "price-event")
        self.assertGreaterEqual(by["EEE"]["vol_ratio"], 10)
        self.assertFalse(by["FFF"]["applied"])                              # AVB 형
        self.assertEqual(by["FFF"]["reason"], "price-event")
        self.assertEqual(by["HHH"]["reason"], "stocks-missing")
        self.assertFalse(by["HHH"]["applied"])
        self.assertEqual(by["III"]["reason"], "stocks-unconfirmed")
        self.assertFalse(by["III"]["applied"])
        panel = replay.panel_from_long(px)
        self.assertAlmostEqual(panel["CLOSE"].at["2025-01-17", "CCC"], 100.5)     # 201/2
        self.assertAlmostEqual(panel["OPEN"].at["2025-01-20", "CCC"], 100.0)
        self.assertAlmostEqual(panel["CLOSE"].at["2025-01-17", "GGG"], 80.25)
        self.assertAlmostEqual(panel["CLOSE"].at["2025-01-17", "DDD"], 100.5)    # 그대로
        self.assertAlmostEqual(panel["CLOSE"].at["2025-01-17", "EEE"], 63.0)     # 그대로 (+177% 이벤트 보존)
        self.assertAlmostEqual(panel["CLOSE"].at["2025-01-17", "FFF"], 182.0)    # 그대로 (−65% 보존)
        self.assertAlmostEqual(panel["CLOSE"].at["2025-01-17", "HHH"], 50.1)

    def test_no_stocks_data_means_no_adjustment(self):
        rows, _ = self._rows()
        px, events = replay.adjust_splits(rows, None)
        self.assertTrue(all(not e["applied"] for e in events))
        self.assertEqual({e["reason"] for e in events if e["CODE"] != "DDD"}, {"stocks-missing"})
        panel = replay.panel_from_long(px)
        self.assertAlmostEqual(panel["CLOSE"].at["2025-01-17", "CCC"], 201.0)

    def test_snap_tolerance_is_3pct(self):
        # 1.84 배는 2 의 ±3% 밖 → STOCKS 가 ×2 여도 후보조차 아님 (not-integer-ratio)
        days = bdays("2024-12-02", "2025-01-24")
        rows = [{"CODE": "JJJ", "D": d, "OPEN": 100.0 if d < "2025-01-20" else 54.3,
                 "CLOSE": 100.0 if d < "2025-01-20" else 54.3, "VOLUME": 1} for d in days]
        stocks = [{"CODE": "JJJ", "D": d, "STOCKS": 1 if d < "2025-01-20" else 2} for d in days]
        _, events = replay.adjust_splits(pd.DataFrame(rows), pd.DataFrame(stocks))
        self.assertEqual(events[0]["reason"], "not-integer-ratio")
        self.assertEqual(replay.SPLIT_SNAP_TOL, 0.03)


class SimBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="replay_test_")
        self.state = os.path.join(self.tmp, "replay_state.json")
        self.days = bdays("2024-12-30", "2025-01-31")
        self.panel = make_panel(self.days, {"AAA": (100.0, 101.0, 1.0), "BBB": (50.0, 50.5, 0.5),
                                            "CCC": (20.0, 20.0, 0.0)})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def screen(self, lists):
        """lists: [(asof_최대, [(티커,이름,주의,점수)...])] — asof 가 첫 항목의 상한 이하면 그 목록."""
        def fn(asof):
            for upto, rows in lists:
                if asof <= upto:
                    return {"watchlist": watchlist(rows), "universe": ["AAA", "BBB", "CCC"],
                            "cached": True, "elapsed": 0}
            return {"watchlist": watchlist(lists[-1][1]), "universe": ["AAA", "BBB", "CCC"],
                    "cached": True, "elapsed": 0}
        return fn


class TestSimulate(SimBase):
    def test_fill_at_next_open_with_slippage_and_commission(self):
        cfg = base_cfg()
        rows = [("AAA", "에이", "", 0.9), ("BBB", "비", "", 0.8), ("CCC", "씨", "지금 이익 최고", 0.7)]
        res = replay.simulate(["2025-01-06"], self.days, self.screen([("9999", rows)]), self.panel, cfg,
                              self.state, end="2025-01-10", progress=False)
        trades = res["trades"]
        self.assertEqual([t["티커"] for t in trades], ["AAA", "BBB"])          # CCC 는 주의 제외
        self.assertEqual(trades[0]["기준일"], "2025-01-03")
        self.assertEqual(trades[0]["체결일"], "2025-01-06")
        open_aaa = self.panel["OPEN"].at["2025-01-06", "AAA"]
        fill_px = open_aaa * (1 + 5 / 1e4)
        qty = floor6(1000 / fill_px)
        self.assertAlmostEqual(trades[0]["체결가"], fill_px, places=6)
        self.assertAlmostEqual(trades[0]["수량"], qty, places=6)
        self.assertAlmostEqual(trades[0]["수수료"], qty * fill_px * 0.001, places=4)
        broker = res["broker"]
        gross = sum(t["체결금액"] for t in trades)
        comm = sum(t["수수료"] for t in trades)
        self.assertAlmostEqual(broker.state["cash"]["USD"], 10000 - gross - comm, places=4)
        # 종가 평가: 체결일부터 end 까지 거래일마다 1행
        eq = res["equity"]
        self.assertEqual(eq["거래일"].tolist(), ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"])
        pos = broker.holdings()
        expect = broker.state["cash"]["USD"] + sum(
            p["qty"] * self.panel["CLOSE"].at["2025-01-10", s] for s, p in pos.items())
        self.assertAlmostEqual(eq["equity_usd"].iloc[-1], expect, places=4)
        # 벤치마크: 유니버스 3종 동일가중, 시가 진입 → 종가
        rel = [self.panel["CLOSE"].at["2025-01-10", s] / self.panel["OPEN"].at["2025-01-06", s]
               for s in ("AAA", "BBB", "CCC")]
        self.assertAlmostEqual(res["bench"]["ew_return_pct"], (np.mean(rel) - 1) * 100, places=6)
        self.assertAlmostEqual(res["bench"]["median_return_pct"], (np.median(rel) - 1) * 100, places=6)
        self.assertEqual(res["bench"]["n"], 3)
        self.assertIsNone(res["cash_out_date"])
        # 벤치마크②: 전략과 같은 현금 스케줄 — 1/6 에 전략 매수총액(gross)만큼 3종 동일가중 시가 투입, 나머지 현금
        sc = res["bench"]["schedule_curve"]
        self.assertEqual(sc.index.tolist(), eq["거래일"].tolist())
        exp_final = 10000 - gross + sum((gross / 3) / self.panel["OPEN"].at["2025-01-06", s]
                                        * self.panel["CLOSE"].at["2025-01-10", s] for s in ("AAA", "BBB", "CCC"))
        self.assertAlmostEqual(float(sc.iloc[-1]), exp_final, places=4)
        self.assertAlmostEqual(res["bench"]["schedule_invested"], gross, places=6)
        self.assertAlmostEqual(res["bench"]["schedule_return_pct"], (exp_final / 10000 - 1) * 100, places=6)
        self.assertEqual(len(res["bench"]["flows"]), 1)
        self.assertEqual(res["bench"]["flows"][0][0], "2025-01-06")
        self.assertAlmostEqual(res["bench"]["flows"][0][1], gross, places=6)
        # 리밸런스별목록: 시트 3행 전부 결과가 채워짐
        outcomes = {r["티커"]: r["결과"] for r in res["rebal_rows"]}
        self.assertTrue(outcomes["AAA"].startswith("매수"))
        self.assertTrue(outcomes["CCC"].startswith("제외: 주의"))

    def test_schedule_benchmark_handles_sells(self):
        days = self.days
        panel = self.panel
        # 1/6 에 3000 투입, 1/13 에 1000 회수 → 회수 비율만큼 units 축소, 현금 증가
        sc, invested, n = replay.schedule_benchmark_curve(panel, ["AAA", "BBB", "CCC"],
                                                          [("2025-01-06", 3000.0), ("2025-01-13", -1000.0)],
                                                          10000.0, days[5:])
        self.assertEqual(n, 3)
        self.assertAlmostEqual(invested, 3000.0)
        units = {s: 1000.0 / panel["OPEN"].at["2025-01-06", s] for s in ("AAA", "BBB", "CCC")}
        val_13 = sum(u * panel["OPEN"].at["2025-01-13", s] for s, u in units.items())
        scale = 1 - 1000.0 / val_13
        exp = 8000.0 + sum(u * scale * panel["CLOSE"].at["2025-01-13", s] for s, u in units.items())
        self.assertAlmostEqual(float(sc.loc["2025-01-13"]), exp, places=4)

    def test_skip_if_held_on_second_rebalance_and_holiday_fill(self):
        cfg = base_cfg()
        rows = [("AAA", "에이", "", 0.9), ("BBB", "비", "", 0.8)]
        days = [d for d in self.days if d != "2025-01-13"]           # 1/13 휴장 → 1/14 체결
        panel = {k: v.drop(index="2025-01-13") for k, v in self.panel.items()}
        res = replay.simulate(["2025-01-06", "2025-01-13"], days, self.screen([("9999", rows)]), panel,
                              cfg, self.state, end="2025-01-17", progress=False)
        rs = res["rebal_summary"]
        self.assertEqual([r["매수건수"] for r in rs], [2, 0])
        self.assertEqual(rs[1]["체결일"], "2025-01-14")
        self.assertEqual(rs[1]["기준일"], "2025-01-10")
        skipped = [r["결과"] for r in res["rebal_rows"] if r["리밸런스일"] == "2025-01-13"]
        self.assertTrue(all("이미 보유" in s for s in skipped))
        self.assertNotIn("2025-01-13", res["equity"]["거래일"].tolist())

    def test_exit_rule_sells_after_weeks_absent(self):
        rows_full = [("AAA", "에이", "", 0.9), ("BBB", "비", "", 0.8)]
        rows_aaa = [("AAA", "에이", "", 0.9)]
        lists = [("2025-01-05", rows_full), ("9999", rows_aaa)]     # 1/10 기준일부터 BBB 가 목록에서 빠짐
        dates = ["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27"]
        for weeks, sell_day in ((1, "2025-01-13"), (2, "2025-01-20")):
            cfg = base_cfg(exit={"enabled": True, "rule": "drop_from_list", "weeks_absent": weeks})
            res = replay.simulate(dates, self.days, self.screen(lists), self.panel, cfg, self.state,
                                  end="2025-01-31", progress=False)
            sells = [t for t in res["trades"] if t["매매"] == "SELL"]
            self.assertEqual(len(sells), 1, weeks)
            self.assertEqual(sells[0]["티커"], "BBB")
            self.assertEqual(sells[0]["체결일"], sell_day)
            bought = [t for t in res["trades"] if t["매매"] == "BUY" and t["티커"] == "BBB"][0]
            self.assertAlmostEqual(sells[0]["수량"], bought["수량"], places=6)
            open_px = self.panel["OPEN"].at[sell_day, "BBB"]
            self.assertAlmostEqual(sells[0]["체결가"], open_px * (1 - 5 / 1e4), places=6)
            self.assertEqual(list(res["broker"].holdings()), ["AAA"])
            self.assertGreater(res["broker"].summary()["total_sold"], 0)
        # exit 꺼짐 → 매도 없음
        res = replay.simulate(dates, self.days, self.screen(lists), self.panel, base_cfg(), self.state,
                              end="2025-01-31", progress=False)
        self.assertEqual([t for t in res["trades"] if t["매매"] == "SELL"], [])

    def test_cash_out_date_and_weekly_cap(self):
        rows = [("AAA", "에이", "", 0.9), ("BBB", "비", "", 0.8), ("CCC", "씨", "", 0.7)]
        cfg = base_cfg(paper={"initial_cash_usd": 1040})
        res = replay.simulate(["2025-01-06", "2025-01-13"], self.days, self.screen([("9999", rows)]),
                              self.panel, cfg, self.state, end="2025-01-17", progress=False)
        self.assertEqual(res["cash_out_date"], "2025-01-06")
        self.assertEqual(len(res["trades"]), 1)
        cfg = base_cfg(sizing={"weekly_cap_usd": 1500})
        res = replay.simulate(["2025-01-06"], self.days, self.screen([("9999", rows)]), self.panel, cfg,
                              self.state, end="2025-01-10", progress=False)
        self.assertEqual([round(t["주문금액"]) for t in res["trades"]], [1000, 500])
        self.assertIsNone(res["cash_out_date"])
        self.assertTrue(any("weekly_cap" in r["결과"] for r in res["rebal_rows"]))

    def test_state_file_is_fresh_each_run(self):
        rows = [("AAA", "에이", "", 0.9)]
        for _ in range(2):
            res = replay.simulate(["2025-01-06"], self.days, self.screen([("9999", rows)]), self.panel,
                                  base_cfg(), self.state, end="2025-01-08", progress=False)
            self.assertEqual(len(res["broker"].state["fills"]), 1)
        self.assertTrue(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.state + ".tmp"))

    def test_write_report_sheets(self):
        rows = [("AAA", "에이", "", 0.9), ("BBB", "비", "", 0.8)]
        res = replay.simulate(["2025-01-06", "2025-01-13"], self.days, self.screen([("9999", rows)]),
                              self.panel, base_cfg(), self.state, end="2025-01-17", progress=False)
        out = os.path.join(self.tmp, "rep.xlsx")
        events = [{"CODE": "ZZZ", "D": "2025-01-08", "ratio": 2.01, "factor": 2.0, "applied": True,
                   "reason": "split", "stocks_ratio": 2.0, "vol_ratio": 1.2},
                  {"CODE": "MRNA", "D": "2025-01-09", "ratio": 0.543, "factor": None, "applied": False,
                   "reason": "price-event", "stocks_ratio": 1.0, "vol_ratio": 36.2},
                  {"CODE": "YYY", "D": "2025-01-09", "ratio": 2.6, "factor": None, "applied": False,
                   "reason": "not-integer-ratio", "stocks_ratio": None, "vol_ratio": None}]
        cfg = base_cfg()
        cfg["replay"] = {"financial_lag_days": 45}
        df = replay.write_report(res, cfg, out, "weekly", events)
        with pd.ExcelFile(out) as xl:
            sheet_names = list(xl.sheet_names)
            eq = xl.parse("자산추이")
            tr = xl.parse("거래내역")
        for s in ("요약", "자산추이", "거래내역", "리밸런스별목록", "리밸런스요약", "설정"):
            self.assertIn(s, sheet_names)
        items = dict(zip(df["항목"], df["값"]))
        self.assertEqual(items["리밸런스 횟수"], 2)
        self.assertEqual(items["주기"], "weekly")
        self.assertIn("ZZZ 2025-01-08 ×2", str(items["분할 소급조정(STOCKS 로 확인된 것만)"]))
        self.assertTrue(str(items["분할 후보 중 STOCKS 미확인(미조정)"]).startswith("1건"))
        self.assertIn("MRNA", str(items["분할 후보 중 STOCKS 미확인(미조정)"]))
        self.assertTrue(str(items["불연속 미조정(정수비 아님)"]).startswith("1건"))
        self.assertIn("45일", str(items["한계1"]))
        # 회전율 = (총매수+총매도)/2 ÷ 평균자산 → 매수만 있어도 0 이 아니다
        turn_key = [k for k in items if k.startswith("회전율")][0]
        self.assertIn("총매수+총매도", turn_key)
        self.assertGreater(float(items[turn_key]), 0)
        self.assertIn("벤치마크② 같은 현금 스케줄 동일가중 수익률%(리밸런스마다 전략과 같은 금액 투입, 총자산 기준)", items)
        self.assertIn("순손익(USD) = 실현 + 평가", items)
        self.assertIn("벤치마크(동일가중B&H)자산", eq.columns)
        self.assertIn("벤치마크(같은현금스케줄)자산", eq.columns)
        self.assertAlmostEqual(float(eq["벤치마크(같은현금스케줄)자산"].iloc[0]),
                               round(float(res["bench"]["schedule_curve"].iloc[0]), 2), places=2)
        self.assertEqual(eq["리밸런스"].astype(str).tolist().count("●"), 2)
        self.assertEqual(list(tr["티커"]), ["AAA", "BBB"])

    def test_no_db_screener_imported(self):
        self.assertNotIn("leader_screener", sys.modules)
        self.assertNotIn("factor_analysis", sys.modules)


if __name__ == "__main__":
    unittest.main()

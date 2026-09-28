#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_toss_api_orders.py — toss_api 주문 함수 + broker_live 잠금을 네트워크 없이 검증.
실행: cd /data/frame && ./.venv/bin/python -m unittest tests.test_toss_api_orders -v

절대 실제 주문 엔드포인트를 호출하지 않음: toss_api._request / urllib.request.urlopen / get_access_token 을 모두 가짜로 바꿈.
스펙(openapi.json v1.2.17) 필드명 그대로인지 확인한다.
"""
import io
import os
import gzip
import json
import unittest
import urllib.error
from datetime import datetime, timezone, timedelta
from email.message import Message
from unittest import mock

import toss_api
import broker_live

# 스펙 OrderCreateRequest / OrderModifyRequest 의 property 이름 (그대로 옮김)
SPEC_CREATE_QTY_FIELDS = {"clientOrderId", "symbol", "side", "orderType", "timeInForce", "quantity", "price",
                          "confirmHighValueOrder"}
SPEC_CREATE_AMT_FIELDS = {"clientOrderId", "symbol", "side", "orderType", "orderAmount", "confirmHighValueOrder"}
SPEC_MODIFY_FIELDS = {"orderType", "quantity", "price", "confirmHighValueOrder"}
KST = timezone(timedelta(hours=9))


class Capture:
    """toss_api._request 대체: 호출 인자를 기록하고 고정 응답을 돌려준다."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"orderId": "OID-1", "clientOrderId": None}

    def __call__(self, method, path, params=None, json_body=None, account_seq=None, retry_on_401=True, **kw):
        self.calls.append({"method": method, "path": path, "params": params, "json_body": json_body,
                           "account_seq": account_seq})
        return {"result": self.result}


class CreateOrderBodyTest(unittest.TestCase):
    def setUp(self):
        self.cap = Capture()
        self._p = mock.patch.object(toss_api, "_request", self.cap)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_quantity_limit_body_matches_spec(self):
        res = toss_api.create_order(1, "AAPL", "buy", "limit", quantity=3, price=123.456,
                                    client_order_id="ab-1_x")
        self.assertEqual(res["orderId"], "OID-1")
        c = self.cap.calls[0]
        self.assertEqual((c["method"], c["path"], c["account_seq"]), ("POST", "/api/v1/orders", 1))
        self.assertEqual(c["json_body"], {
            "symbol": "AAPL", "side": "BUY", "orderType": "LIMIT", "timeInForce": "DAY",
            "quantity": "3", "price": "123.45", "confirmHighValueOrder": False, "clientOrderId": "ab-1_x",
        })
        self.assertTrue(set(c["json_body"]) <= SPEC_CREATE_QTY_FIELDS)
        self.assertNotIn("orderSide", c["json_body"])  # 스펙 필드명은 side

    def test_amount_market_body_matches_spec(self):
        toss_api.create_order(1, "AAPL", "BUY", "MARKET", order_amount=100.5)
        body = self.cap.calls[0]["json_body"]
        self.assertEqual(body, {"symbol": "AAPL", "side": "BUY", "orderType": "MARKET",
                                "orderAmount": "100.5", "confirmHighValueOrder": False})
        self.assertTrue(set(body) <= SPEC_CREATE_AMT_FIELDS)
        self.assertNotIn("timeInForce", body)  # 금액 주문 스키마에는 timeInForce 없음
        self.assertNotIn("quantity", body)

    def test_decimal_strings_never_exponent_or_trailing_zero(self):
        toss_api.create_order(1, "MU", "BUY", "MARKET", quantity=1000.0)
        self.assertEqual(self.cap.calls[-1]["json_body"]["quantity"], "1000")
        toss_api.create_order(1, "MU", "BUY", "MARKET", order_amount=1e3)
        self.assertEqual(self.cap.calls[-1]["json_body"]["orderAmount"], "1000")
        toss_api.create_order(1, "MU", "SELL", "MARKET", quantity=0.1234567)  # 6자리 절삭
        self.assertEqual(self.cap.calls[-1]["json_body"]["quantity"], "0.123456")

    def test_us_price_tick_truncation(self):
        toss_api.create_order(1, "AAPL", "BUY", "LIMIT", quantity=1, price=0.12345678)
        self.assertEqual(self.cap.calls[-1]["json_body"]["price"], "0.1234")  # $1 미만 4자리
        toss_api.create_order(1, "AAPL", "BUY", "LIMIT", quantity=1, price=250.999)
        self.assertEqual(self.cap.calls[-1]["json_body"]["price"], "250.99")  # $1 이상 2자리 절삭
        toss_api.create_order(1, "005930", "BUY", "LIMIT", quantity=1, price=70000.9)
        self.assertEqual(self.cap.calls[-1]["json_body"]["price"], "70000")  # KR 정수

    def test_market_sell_fractional_allowed_but_buy_rejected(self):
        toss_api.create_order(1, "AAPL", "SELL", "MARKET", quantity=0.5)
        self.assertEqual(self.cap.calls[-1]["json_body"]["quantity"], "0.5")
        with self.assertRaises(ValueError):
            toss_api.create_order(1, "AAPL", "BUY", "MARKET", quantity=0.5)
        with self.assertRaises(ValueError):
            toss_api.create_order(1, "AAPL", "SELL", "LIMIT", quantity=0.5, price=10)
        self.assertEqual(len(self.cap.calls), 1)  # 거절된 건은 _request 를 타지 않음

    def test_client_side_validation(self):
        bad = [
            dict(quantity=1, order_amount=10),                       # 둘 다
            dict(),                                                  # 둘 다 없음
            dict(order_amount=10, order_type="LIMIT", price=1),      # 금액+LIMIT
            dict(quantity=1, order_type="LIMIT"),                    # LIMIT 가격 없음
            dict(quantity=1, order_type="MARKET", price=1),          # MARKET 가격
            dict(quantity=1, client_order_id="bad id!"),             # 멱등키 문자
            dict(quantity=1, client_order_id="x" * 37),              # 멱등키 길이
            dict(quantity=0),                                        # 0 수량
            dict(quantity=1, time_in_force="CLS"),                   # CLS 는 LIMIT 만
            dict(quantity=1, side="HOLD"),
        ]
        for kw in bad:
            kw = {"side": "BUY", "order_type": "MARKET", **kw}
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                toss_api.create_order(1, "AAPL", kw.pop("side"), kw.pop("order_type"), **kw)
        self.assertEqual(self.cap.calls, [])

    def test_modify_cancel_get_bodies(self):
        toss_api.modify_order(1, "OID", price=71000, symbol="005930")
        c = self.cap.calls[-1]
        self.assertEqual((c["method"], c["path"]), ("POST", "/api/v1/orders/OID/modify"))
        self.assertEqual(c["json_body"], {"orderType": "LIMIT", "price": "71000", "confirmHighValueOrder": False})
        self.assertTrue(set(c["json_body"]) <= SPEC_MODIFY_FIELDS)
        toss_api.modify_order(1, "OID", quantity=15, price=71000, symbol="005930")
        self.assertEqual(self.cap.calls[-1]["json_body"]["quantity"], "15")
        with self.assertRaises(ValueError):
            toss_api.modify_order(1, "OID", quantity=1.5, price=1)

        toss_api.cancel_order(1, "OID")
        c = self.cap.calls[-1]
        self.assertEqual((c["method"], c["path"], c["json_body"], c["account_seq"]),
                         ("POST", "/api/v1/orders/OID/cancel", {}, 1))

        toss_api.get_orders(1)
        c = self.cap.calls[-1]
        self.assertEqual((c["method"], c["path"], c["params"]), ("GET", "/api/v1/orders", {"status": "OPEN", "limit": 20}))
        toss_api.get_orders(1, status="CLOSED", symbol="AAPL", cursor="c1", limit=50, date_from="2026-09-01")
        self.assertEqual(self.cap.calls[-1]["params"],
                         {"status": "CLOSED", "limit": 50, "symbol": "AAPL", "cursor": "c1", "from": "2026-09-01"})
        toss_api.get_order(1, "OID")
        self.assertEqual(self.cap.calls[-1]["path"], "/api/v1/orders/OID")

    def test_market_info_params(self):
        toss_api.get_exchange_rate()
        c = self.cap.calls[-1]
        self.assertEqual((c["method"], c["path"], c["params"], c["account_seq"]),
                         ("GET", "/api/v1/exchange-rate", {"baseCurrency": "USD", "quoteCurrency": "KRW"}, None))
        toss_api.get_us_market_calendar("2026-09-28")
        self.assertEqual(self.cap.calls[-1]["params"], {"date": "2026-09-28"})
        toss_api.get_commissions(1)
        c = self.cap.calls[-1]
        self.assertEqual((c["path"], c["account_seq"]), ("/api/v1/commissions", 1))


class FakeResp:
    def __init__(self, body: bytes, headers=None):
        self._b = body
        self.headers = Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RequestWireTest(unittest.TestCase):
    """urlopen 레벨에서 헤더/본문/gzip 처리 검증 (토큰은 가짜, 네트워크 없음)."""

    def setUp(self):
        self.reqs = []
        self.responses = []
        self._throttle = toss_api.THROTTLE_ENABLED
        toss_api.THROTTLE_ENABLED = False
        self._p1 = mock.patch.object(toss_api, "get_access_token", lambda force_refresh=False: "FAKE-TOKEN")
        self._p2 = mock.patch.object(toss_api.urllib.request, "urlopen", self._urlopen)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        toss_api.THROTTLE_ENABLED = self._throttle

    def _urlopen(self, req, timeout=None):
        self.reqs.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def test_create_order_headers_and_wire_body(self):
        self.responses.append(FakeResp(json.dumps({"result": {"orderId": "X1", "clientOrderId": "c1"}}).encode()))
        res = toss_api.create_order(7, "AAPL", "BUY", "MARKET", order_amount=50, client_order_id="c1")
        self.assertEqual(res, {"orderId": "X1", "clientOrderId": "c1"})
        req = self.reqs[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://openapi.tossinvest.com/api/v1/orders")
        self.assertEqual(req.get_header("Authorization"), "Bearer FAKE-TOKEN")
        self.assertEqual(req.get_header("X-tossinvest-account"), "7")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data.decode()),
                         {"symbol": "AAPL", "side": "BUY", "orderType": "MARKET", "orderAmount": "50",
                          "confirmHighValueOrder": False, "clientOrderId": "c1"})

    def test_gzip_response_is_decoded(self):
        payload = {"result": {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1380.5"}}
        gz = gzip.compress(json.dumps(payload).encode())
        self.responses.append(FakeResp(gz, {"Content-Encoding": "gzip"}))
        self.assertEqual(toss_api.get_exchange_rate()["rate"], "1380.5")
        self.assertIn("baseCurrency=USD", self.reqs[0].full_url)
        # 헤더 없이 매직바이트만 있어도 해제
        self.responses.append(FakeResp(gz))
        self.assertEqual(toss_api.get_exchange_rate()["rate"], "1380.5")

    def test_http_error_parsed_into_toss_api_error(self):
        err_body = {"error": {"requestId": "R1", "code": "insufficient-buying-power", "message": "주문 가능 금액이 부족합니다."}}
        gz = gzip.compress(json.dumps(err_body, ensure_ascii=False).encode())
        hdrs = Message()
        hdrs["Content-Encoding"] = "gzip"
        self.responses.append(urllib.error.HTTPError("u", 422, "Unprocessable", hdrs, io.BytesIO(gz)))
        with self.assertRaises(toss_api.TossApiError) as cm:
            toss_api.create_order(1, "AAPL", "BUY", "MARKET", quantity=1)
        e = cm.exception
        self.assertIsInstance(e, RuntimeError)  # 기존 except RuntimeError 호환
        self.assertEqual((e.status, e.code, e.request_id), (422, "insufficient-buying-power", "R1"))
        self.assertTrue(str(e).startswith("HTTP 422 /api/v1/orders: "))

    def test_429_retried_once_after_retry_after(self):
        hdrs = Message()
        hdrs["Retry-After"] = "0"
        self.responses.append(urllib.error.HTTPError("u", 429, "Too Many", hdrs, io.BytesIO(b'{"error":{"code":"rate-limit"}}')))
        self.responses.append(FakeResp(json.dumps({"result": [{"symbol": "AAPL", "lastPrice": "1.5"}]}).encode()))
        with mock.patch.object(toss_api.time, "sleep", lambda s: None):
            self.assertEqual(toss_api.get_prices(["AAPL"])[0]["lastPrice"], "1.5")
        self.assertEqual(len(self.reqs), 2)


def _cal(day="2026-09-28"):
    """월요일(미국 현지 9/28) 캘린더 흉내: day 09:00–17:00, pre 17:00–22:30, regular 22:30–05:00+1, after 05:00–08:50+1 (KST)."""
    nxt = "2026-09-29"
    def d(x, t):
        return f"{x}T{t}+09:00"
    today = {"date": day,
             "dayMarket": {"startTime": d(day, "09:00:00"), "endTime": d(day, "17:00:00")},
             "preMarket": {"startTime": d(day, "17:00:00"), "endTime": d(day, "22:30:00")},
             "regularMarket": {"startTime": d(day, "22:30:00"), "endTime": d(nxt, "05:00:00")},
             "afterMarket": {"startTime": d(nxt, "05:00:00"), "endTime": d(nxt, "08:50:00")}}
    prev = {"date": "2026-09-25", "dayMarket": None, "preMarket": None,
            "regularMarket": {"startTime": d("2026-09-25", "22:30:00"), "endTime": d("2026-09-26", "05:00:00")},
            "afterMarket": {"startTime": d("2026-09-26", "05:00:00"), "endTime": d("2026-09-26", "08:50:00")}}
    return {"today": today, "previousBusinessDay": prev, "nextBusinessDay": {"date": nxt, "dayMarket": None,
            "preMarket": None, "regularMarket": None, "afterMarket": None}}


class SessionHelperTest(unittest.TestCase):
    def test_session_lookup(self):
        cal = _cal()
        f = broker_live.current_us_session
        self.assertEqual(f(cal, datetime(2026, 9, 28, 10, 0, tzinfo=KST)), "dayMarket")
        self.assertEqual(f(cal, datetime(2026, 9, 28, 20, 0, tzinfo=KST)), "preMarket")
        self.assertEqual(f(cal, datetime(2026, 9, 28, 22, 30, tzinfo=KST)), "regularMarket")
        self.assertEqual(f(cal, datetime(2026, 9, 29, 4, 59, tzinfo=KST)), "regularMarket")
        self.assertEqual(f(cal, datetime(2026, 9, 29, 5, 0, tzinfo=KST)), "afterMarket")
        self.assertIsNone(f(cal, datetime(2026, 9, 29, 8, 50, tzinfo=KST)))
        self.assertIsNone(f(cal, datetime(2026, 9, 27, 12, 0, tzinfo=KST)))  # 일요일 낮
        self.assertIsNone(f({"today": {"date": "x", "dayMarket": None, "preMarket": None,
                                        "regularMarket": None, "afterMarket": None}}, datetime.now(KST)))
        start, end = broker_live.regular_session_bounds(cal, datetime(2026, 9, 28, 18, 0, tzinfo=KST))
        self.assertEqual((start.hour, end.day, end.hour), (22, 29, 5))


class LiveBrokerGuardTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop(broker_live.LIVE_ENV_FLAG, None)

    def tearDown(self):
        if self._env is not None:
            os.environ[broker_live.LIVE_ENV_FLAG] = self._env
        else:
            os.environ.pop(broker_live.LIVE_ENV_FLAG, None)

    def test_constructor_requires_allow_live(self):
        with self.assertRaisesRegex(RuntimeError, "live 주문 차단"):
            broker_live.LiveBroker(1)
        with self.assertRaisesRegex(RuntimeError, "live 주문 차단"):
            broker_live.LiveBroker(1, allow_live="yes")
        with self.assertRaisesRegex(RuntimeError, "live 주문 차단"):
            broker_live.LiveBroker(None, allow_live=True)

    def test_place_order_blocked_without_env(self):
        def boom(*a, **k):
            raise AssertionError("실주문 API 가 호출됨")
        with mock.patch.object(toss_api, "create_order", boom), mock.patch.object(toss_api, "cancel_order", boom), \
                mock.patch.object(toss_api, "_request", boom):
            b = broker_live.LiveBroker(1, allow_live=True)
            with self.assertRaisesRegex(RuntimeError, "live 주문 차단"):
                b.place_order("AAPL", "BUY", "MARKET", amount=100)
            with self.assertRaisesRegex(RuntimeError, "live 주문 차단"):
                b.cancel("OID")
            os.environ[broker_live.LIVE_ENV_FLAG] = "0"
            with self.assertRaisesRegex(RuntimeError, "live 주문 차단"):
                b.place_order("AAPL", "BUY", "MARKET", amount=100)

    def test_place_order_with_env_uses_create_order_and_maps_rejection(self):
        os.environ[broker_live.LIVE_ENV_FLAG] = "1"
        calls = []
        def fake_create(account_seq, symbol, side, order_type, **kw):
            calls.append((account_seq, symbol, side, order_type, kw))
            return {"orderId": "OID-9", "clientOrderId": kw.get("client_order_id")}
        with mock.patch.object(toss_api, "create_order", fake_create), mock.patch.object(toss_api, "_request", None):
            b = broker_live.LiveBroker(3, allow_live=True)
            r = b.place_order("aapl", "buy", "market", amount=100, client_order_id="k1")
        self.assertEqual((r["status"], r["order_id"], r["client_order_id"], r["mode"]), ("SUBMITTED", "OID-9", "k1", "live"))
        self.assertEqual(calls[0][:4], (3, "AAPL", "BUY", "MARKET"))
        self.assertEqual(calls[0][4]["order_amount"], 100)

        def rejecting(*a, **k):
            raise toss_api.TossApiError(422, "/api/v1/orders",
                                        '{"error":{"code":"order-hours-closed","message":"closed","requestId":"R"}}')
        with mock.patch.object(toss_api, "create_order", rejecting):
            r = broker_live.LiveBroker(3, allow_live=True).place_order("AAPL", "BUY", "MARKET", quantity=1)
        self.assertEqual((r["status"], r["reason"], r["http"]), ("REJECTED", "order-hours-closed", 422))

        def server_error(*a, **k):
            raise toss_api.TossApiError(500, "/api/v1/orders", "{}")
        with mock.patch.object(toss_api, "create_order", server_error), self.assertRaises(toss_api.TossApiError):
            broker_live.LiveBroker(3, allow_live=True).place_order("AAPL", "BUY", "MARKET", quantity=1)

    def test_read_only_methods_do_not_need_env(self):
        with mock.patch.object(toss_api, "get_prices", lambda syms: [{"symbol": "AAPL", "lastPrice": "255.1", "currency": "USD"}]), \
                mock.patch.object(toss_api, "get_buying_power", lambda seq, cur: {"currency": cur, "cashBuyingPower": "12.5"}), \
                mock.patch.object(toss_api, "get_holdings", lambda seq, symbol=None: {"items": [
                    {"symbol": "MU", "name": "Micron", "marketCountry": "US", "currency": "USD", "quantity": "2.5",
                     "lastPrice": "100", "averagePurchasePrice": "90", "marketValue": {"amount": "250"},
                     "profitLoss": {"amount": "25", "rate": "0.11"}}]}), \
                mock.patch.object(toss_api, "get_orders", lambda seq, status="OPEN", **k: {"orders": [
                    {"orderId": "O1", "symbol": "MU", "side": "BUY", "orderType": "LIMIT", "timeInForce": "DAY",
                     "status": "PENDING", "price": "95", "quantity": "1", "orderAmount": None, "currency": "USD",
                     "orderedAt": "2026-09-28T22:31:00.000+09:00", "execution": {"filledQuantity": "0"}}], "nextCursor": None, "hasNext": False}), \
                mock.patch.object(toss_api, "get_us_market_calendar", lambda date=None: _cal()):
            b = broker_live.LiveBroker(1, allow_live=True)
            self.assertEqual(b.quote("aapl"), 255.1)
            self.assertEqual(b.buying_power("USD"), 12.5)
            h = b.holdings()
            self.assertEqual((h["MU"]["qty"], h["MU"]["avg_price"], h["MU"]["currency"]), (2.5, 90.0, "USD"))
            oo = b.open_orders()
            self.assertEqual((oo[0]["order_id"], oo[0]["status"], oo[0]["price"], oo[0]["filled_qty"]), ("O1", "PENDING", 95.0, 0.0))
            self.assertEqual(b.session_now(datetime(2026, 9, 28, 23, 0, tzinfo=KST)), "regularMarket")
            s = b.summary()
            self.assertEqual((s["mode"], s["cash"]["USD"], s["positions_value_usd"], s["live_env_ok"]), ("live", 12.5, 250.0, False))


if __name__ == "__main__":
    unittest.main()

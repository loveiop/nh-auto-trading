# 나무 자동매매 · 휴대폰 업로드용
# app.py와 requirements.txt만 같은 위치에 업로드하세요.
# API 연결값과 앱 비밀번호는 코드 대신 Secrets/환경변수에 설정합니다.
from __future__ import annotations

import sys
from types import ModuleType
import streamlit as st

# The embedded source is fixed application code, never user-provided input.
# A cached module keeps its engine and worker threads alive across UI reruns.
_RUNTIME_VERSION = "f9154eceda44b64d"
_RUNTIME_SOURCE = r'''
from __future__ import annotations


# ===== core =====

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo
import re

KST = ZoneInfo("Asia/Seoul")
STRATEGY_VERSION = "2026.09.10-r4"


def now():
    return datetime.now(KST)


def number(value):
    result = float(str(value).replace(",", "").strip())
    if not isfinite(result):
        raise ValueError("유효하지 않은 숫자")
    return result


def integer(value):
    result = number(value)
    if result != int(result):
        raise ValueError("정수 항목에 소수 수신")
    return int(result)


def rows(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(x, dict) for x in value):
        return value
    raise ValueError("API 응답 블록 형식 불일치")


def one(value):
    values = rows(value)
    if len(values) != 1:
        raise ValueError("API 단일 응답 블록 누락 또는 중복")
    return values[0]


def code_of(value):
    code = str(value).strip()
    if not re.fullmatch(r"[0-9A-Z]{6}", code) or not re.search(r"[0-9]", code):
        raise ValueError("주문·시세에는 6자리 국내주식 단축코드가 필요합니다")
    return code


def response_code(value, field="응답 종목코드"):
    """Normalize recognized short-code representations, never slice a long ID."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(field + " · 종목코드 자료형 확인 필요")
    code = str(value).strip(" \t\r\n\x00").upper()
    if re.fullmatch(r"[0-9]{1,6}", code):
        code = code.zfill(6)
    elif re.fullmatch(r"A[0-9A-Z]{6}", code):
        code = code[1:]
    try:
        return code_of(code)
    except ValueError:
        # Length/type identify the defect without dumping an account response.
        raise ValueError(f"{field} · {len(code)}자리 코드를 거래용 단축코드로 확인할 수 없습니다") from None


def market_time(dt, buying=False):
    # Holidays/delayed sessions cannot produce an entry without fresh KRX executions.
    start, end = (dtime(9, 10), dtime(14, 50)) if buying else (dtime(9), dtime(15, 20))
    return dt.weekday() < 5 and start <= dt.timetz().replace(tzinfo=None) < end


def event_time(value, received):
    raw = str(value).split(".")[0].replace(":", "").strip()
    if not re.fullmatch(r"\d{6}", raw):
        raise ValueError("체결시각 형식 불일치")
    return received.replace(hour=int(raw[:2]), minute=int(raw[2:4]),
                            second=int(raw[4:6]), microsecond=0)


@dataclass(frozen=True)
class Settings:
    capital: int = 1_000_000
    per_order: int = 250_000
    stop_loss: float = 0.8
    take_profit: float = 1.5
    max_positions: int = 3
    buy_score: int = 70
    daily_loss: int = 50_000
    max_buys: int = 6
    trailing: float = 0.6
    cooldown_seconds: int = 1200
    min_volume: int = 1_000_000
    min_turnover: int = 500_000_000
    max_spread_pct: float = 0.3
    freshness_seconds: float = 3.0
    cost_allowance_pct: float = 0.3  # Budget estimate, not a claim about applicable tax/fees.
    risk_per_trade_pct: float = 0.35  # Position-sizing budget; not a guaranteed loss cap.
    min_net_reward_risk: float = 1.0
    consecutive_loss_limit: int = 3
    impulse_min_pct: float = 0.3
    pullback_volume_ratio: float = 0.8
    max_chase_pct: float = 0.15
    max_vwap_distance_pct: float = 2.0

    def validate(self):
        for value in asdict(self).values():
            if not isfinite(float(value)) or value <= 0:
                raise ValueError("설정값은 0보다 큰 유한 숫자여야 합니다.")
        if self.per_order > self.capital:
            raise ValueError("1회 주문금액은 총 투자한도 이하여야 합니다.")
        if self.min_volume != 1_000_000:
            raise ValueError("거래량 100만 주 조건은 고정입니다.")
        if not 1 <= self.max_positions <= 3 or not 1 <= self.buy_score <= 100:
            raise ValueError("보유종목/점수 범위 오류")
        if self.stop_loss >= 100 or self.take_profit >= 100:
            raise ValueError("손절·익절 범위 오류")
        if self.risk_per_trade_pct > 1 or self.cost_allowance_pct >= self.take_profit:
            raise ValueError("거래당 위험은 투자한도의 1% 이하, 익절폭은 예상 비용보다 크게 설정해 주세요.")
        if self.net_reward_risk() < self.min_net_reward_risk:
            raise ValueError("예상 비용을 뺀 목표수익이 손절 위험보다 작습니다. 손절·익절 값을 다시 확인해 주세요.")
        if self.pullback_volume_ratio > 1 or self.max_chase_pct > 1 or self.max_vwap_distance_pct > 5:
            raise ValueError("눌림 거래량 비율은 1 이하, 추격 폭은 1% 이하, 평균가격 이격은 5% 이하로 설정해 주세요.")
        return self

    def net_reward_risk(self):
        # Exit conditions use the executable bid relative to the actual fill.
        # The spread is already paid in that comparison; do not charge it twice.
        friction = self.cost_allowance_pct
        return max(0, self.take_profit-friction) / (self.stop_loss+friction)


@dataclass(frozen=True)
class Tick:
    code: str
    price: int
    volume: int
    turnover: int
    ask: int
    bid: int
    event: datetime
    received: datetime

    @classmethod
    def parse(cls, message, received):
        header, body = message.get("header", {}), message.get("body", {})
        if "rsp_cd" in header or "tr_type" in header:
            return None
        if header.get("tr_cd") != "oc":
            return None
        code = response_code(body["code"], "실시간 체결 code")
        if header.get("tr_key") and response_code(header["tr_key"], "실시간 체결 tr_key") != code:
            raise ValueError("체결 종목코드 불일치")
        # NH's official oc channel uses price, volume, offer, bid and time.
        tick = cls(code, integer(body["price"]), integer(body["volume"]),
                   integer(body["value_won"]), integer(body["offer"]),
                   integer(body["bid"]), event_time(body["time"], received), received)
        if tick.price <= 0 or min(tick.volume, tick.turnover, tick.ask, tick.bid) < 0:
            raise ValueError("실시간 가격·거래량 응답값 오류")
        if (tick.event - received).total_seconds() > 2:
            raise ValueError("거래소 체결시각이 서버보다 미래입니다. 서버 시계를 확인하세요.")
        return tick


class Tape:
    """KRX observed bars. Partial bars cannot become entry evidence retroactively."""
    def __init__(self):
        self.bars = deque(maxlen=48)
        self.samples = deque(maxlen=122)
        self.latest = None
        self.previous_price = None
        self.started = None

    def interrupt(self):
        if self.bars:
            self.bars[-1]["partial"] = True
        self.samples.clear()
        self.started = None

    def add(self, tick):
        old = self.latest
        if old and tick.event.date() != old.event.date():
            self.bars.clear()
            self.samples.clear()
            self.latest = self.started = None
            old = None
        if old and (tick.event < old.event or tick.volume < old.volume or tick.turnover < old.turnover):
            self.interrupt()
            return False
        # A gap is an incomplete observation period. Do not bridge it for momentum.
        if old and (tick.event - old.event).total_seconds() > 5:
            self.interrupt()
        if self.started is None:
            self.started = tick.event
        bucket = tick.event.replace(minute=tick.event.minute // 5 * 5, second=0, microsecond=0)
        delta = tick.volume - old.volume if old else 0
        if old and delta > 0 and tick.turnover == old.turnover:
            self.interrupt()
            return False
        if not self.bars or self.bars[-1]["time"] != bucket.isoformat():
            self.bars.append(dict(time=bucket.isoformat(), open=tick.price,
                                  high=tick.price, low=tick.price, close=tick.price,
                                  volume=delta, partial=old is None or
                                  (tick.event - old.event).total_seconds() > 5))
        else:
            bar = self.bars[-1]
            bar.update(high=max(bar["high"], tick.price), low=min(bar["low"], tick.price),
                       close=tick.price, volume=bar["volume"] + delta)
        self.previous_price = old.price if old else None
        self.latest = tick
        sample = (tick.event.timestamp(), tick.price, tick.volume)
        if self.samples and self.samples[-1][0] == sample[0]:
            self.samples[-1] = sample
        else:
            self.samples.append(sample)
        while self.samples and sample[0] - self.samples[0][0] > 120:
            self.samples.popleft()
        return True

    def at_or_before(self, timestamp):
        return next((s for s in reversed(self.samples) if s[0] <= timestamp), None)

    def signal(self, settings, current):
        t = self.latest
        def blocked(reason, **details):
            return dict(score=0, reason=reason, eligible=False, **details)
        if not t or not self.bars:
            return blocked("실시간 체결 대기", ready_bars=0)
        age = max((current - t.received).total_seconds(), (current - t.event).total_seconds())
        if age < -2 or age > settings.freshness_seconds:
            return blocked(f"체결 지연·시각 확인 {age:.1f}초")
        if t.volume < settings.min_volume or t.turnover < settings.min_turnover:
            return blocked("거래량·거래대금 조건 미달")
        if t.ask <= 0 or t.bid <= 0 or t.ask < t.bid:
            return blocked("유효한 매수·매도 호가 대기")
        if (t.ask / t.bid - 1) * 100 > settings.max_spread_pct:
            return blocked("호가 간격이 큼")
        if settings.net_reward_risk() < settings.min_net_reward_risk:
            return blocked("예상 비용을 반영하면 목표수익 대비 손절 위험이 큼")
        # Both fields are day-cumulative on the same NH KRX oc message.
        # This is NOT an HLC3 approximation or a cross-venue average.
        vwap = t.turnover / t.volume
        if not t.price * .5 <= vwap <= t.price * 2:
            return blocked("거래량·거래대금 단위 확인 필요")
        bars = list(self.bars)
        live = bars[-1]
        if current >= datetime.fromisoformat(live["time"]) + timedelta(minutes=5):
            return blocked("새 5분봉 첫 체결 대기")
        ready, expected = [], datetime.fromisoformat(live["time"]) - timedelta(minutes=5)
        for bar in reversed(bars[:-1]):
            stamp = datetime.fromisoformat(bar["time"])
            if bar["partial"] or stamp != expected or stamp.date() != t.event.date() or not market_time(stamp):
                break
            ready.append(bar)
            expected -= timedelta(minutes=5)
            if len(ready) == 2:
                break
        if live["partial"] or len(ready) < 2:
            return blocked(f"완성 5분봉 준비 {len(ready)}/2 · 시작·수신 누락 봉 제외",
                           ready_bars=len(ready), partial=live["partial"], vwap=round(vwap, 2))
        impulse, pullback = ready[1], ready[0]
        span = impulse["high"] - impulse["low"]
        body = impulse["close"] - impulse["open"]
        midpoint = (impulse["open"] + impulse["close"]) / 2
        trigger, stop = pullback["high"], pullback["low"]
        max_entry = trigger * (1 + settings.max_chase_pct / 100)
        checks = [
            ("상승봉 힘 확인", span > 0 and body / impulse["open"] * 100 >= settings.impulse_min_pct
             and body >= span * .5 and impulse["close"] >= impulse["low"] + span * .75
             and span / impulse["open"] * 100 < 2.5),
            ("상승폭 절반 지지", pullback["low"] >= midpoint and pullback["high"] <= impulse["high"]
             and pullback["close"] < impulse["close"] and pullback["low"] < pullback["high"]),
            ("눌림 거래량 감소", 0 < pullback["volume"] <= impulse["volume"] * settings.pullback_volume_ratio),
            ("평균가격 위·이격 제한", t.bid > vwap and (t.ask / vwap - 1) * 100 <= settings.max_vwap_distance_pct),
            ("눌림 고점 재돌파", t.price > trigger and t.bid >= trigger),
            ("추격매수 제한", max(t.ask, live["high"]) <= max_entry),
            ("눌림 저점 유지", live["low"] > stop and t.bid > stop),
        ]
        passed = sum(bool(ok) for _, ok in checks)
        eligible = passed == len(checks)  # Progress display is not a discretionary score.
        waiting = next((label for label, ok in checks if not ok), "")
        return dict(score=round(passed / len(checks) * 100), eligible=eligible,
                    reason="눌림 후 재돌파 · 7개 조건 충족" if eligible else waiting + " 대기",
                    ready_bars=2, partial=False, checks=[dict(label=label, passed=bool(ok)) for label, ok in checks],
                    setup_id=impulse["time"] + "/" + pullback["time"],
                    valid_until=(datetime.fromisoformat(live["time"]) + timedelta(minutes=5)).isoformat(),
                    vwap=round(vwap, 2), volume_ratio=round(pullback["volume"] / impulse["volume"], 3) if impulse["volume"] else None,
                    trigger=trigger, structural_stop=stop, max_entry=round(max_entry, 6))


# ===== store =====

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import uuid

OPEN_STATES = ("SUBMITTING", "ACCEPTED", "PARTIAL", "UNKNOWN")


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
              id TEXT PRIMARY KEY, account TEXT NOT NULL, code TEXT NOT NULL,
              side TEXT NOT NULL, qty INTEGER NOT NULL, price INTEGER NOT NULL,
              created TEXT NOT NULL, accepted TEXT, state TEXT NOT NULL,
              broker_no TEXT, integrated_no TEXT, baseline TEXT NOT NULL,
              filled INTEGER NOT NULL DEFAULT 0, avg REAL NOT NULL DEFAULT 0,
              reason TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS positions (
              account TEXT NOT NULL, code TEXT NOT NULL, qty INTEGER NOT NULL,
              avg REAL NOT NULL, peak REAL NOT NULL, PRIMARY KEY(account,code));
            CREATE TABLE IF NOT EXISTS fills (
              id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, account TEXT NOT NULL,
              code TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
              price REAL NOT NULL, realized REAL NOT NULL, ts TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS logs (
              id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trade_context (
              order_id TEXT PRIMARY KEY, strategy TEXT NOT NULL,
              settings TEXT NOT NULL, signal TEXT NOT NULL);
            """)
            c.execute("UPDATE orders SET state='UNKNOWN',error='전송 중 프로세스 종료 · 주문내역 확인 필요' WHERE state='SUBMITTING'")

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def get(self, key, default=None):
        with self.db() as c:
            row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as c:
            c.execute("INSERT INTO config VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))

    def log(self, ts, kind, message):
        with self.db() as c:
            c.execute("INSERT INTO logs(ts,kind,message) VALUES(?,?,?)", (ts, kind, message[:600]))
            c.execute("DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id),0)-1500 FROM logs)")

    def logs(self):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT ts,kind,message FROM logs ORDER BY id DESC LIMIT 80")]

    def orders(self, account, pending=False):
        with self.db() as c:
            data = [dict(r) for r in c.execute("SELECT * FROM orders WHERE account=? ORDER BY created,id", (account,))]
        return [r for r in data if r["state"] in OPEN_STATES] if pending else data

    def positions(self, account):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM positions WHERE account=? AND qty>0", (account,))]

    def position_context(self, account, code):
        with self.db() as c:
            row = c.execute("""SELECT x.settings, x.signal FROM fills f
                LEFT JOIN trade_context x ON x.order_id=f.order_id
                WHERE f.account=? AND f.code=? AND f.side='BUY' ORDER BY f.id DESC LIMIT 1""",
                (account, code)).fetchone()
        return dict(settings=json.loads(row["settings"] or "{}"), signal=json.loads(row["signal"] or "{}")) if row else {}

    def prepare(self, account, code, side, qty, price, created, baseline, reason, context=None):
        oid = uuid.uuid4().hex
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM orders WHERE account=? AND code=? AND state IN ('SUBMITTING','ACCEPTED','PARTIAL','UNKNOWN')", (account, code)).fetchone():
                raise ValueError("앞선 주문의 체결 확인 중")
            c.execute("INSERT INTO orders(id,account,code,side,qty,price,created,state,baseline,reason) VALUES(?,?,?,?,?,?,?,'SUBMITTING',?,?)",
                      (oid, account, code, side, qty, price, created, json.dumps(baseline), reason))
            if context is not None:
                c.execute("INSERT INTO trade_context VALUES(?,?,?,?)",
                          (oid, context["strategy"], json.dumps(context["settings"], ensure_ascii=False),
                           json.dumps(context["signal"], ensure_ascii=False)))
        return oid

    def mark(self, oid, state, **values):
        allowed = {"accepted", "broker_no", "integrated_no", "error"}
        if set(values) - allowed:
            raise ValueError("허용되지 않은 주문 변경")
        columns = ["state=?"] + [f"{k}=?" for k in values]
        with self.db() as c:
            c.execute("UPDATE orders SET " + ",".join(columns) + " WHERE id=?", (state, *values.values(), oid))

    def reconcile(self, oid, total_filled, avg, terminal, integrated_no, ts, cost_pct):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            order = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if total_filled < order["filled"] or total_filled > order["qty"]:
                raise ValueError("증권사 누적체결수량 불일치")
            if total_filled and avg <= 0:
                raise ValueError("체결평균단가 누락")
            delta = total_filled - order["filled"]
            if delta:
                delta_price = (total_filled * avg - order["filled"] * order["avg"]) / delta
                if delta_price <= 0:
                    raise ValueError("증분 체결가격 불일치")
                pos = c.execute("SELECT * FROM positions WHERE account=? AND code=?", (order["account"], order["code"])).fetchone()
                qty, old_avg, peak = (pos["qty"], pos["avg"], pos["peak"]) if pos else (0, 0, 0)
                realized = 0.0
                if order["side"] == "BUY":
                    new_avg = (qty * old_avg + delta * delta_price) / (qty + delta)
                    qty += delta
                    peak = max(peak, delta_price)
                else:
                    if delta > qty:
                        raise ValueError("자동매매 보유분보다 많은 매도체결 · 계좌 확인 필요")
                    entry = c.execute("""SELECT x.settings FROM fills f
                        LEFT JOIN trade_context x ON x.order_id=f.order_id
                        WHERE f.account=? AND f.code=? AND f.side='BUY' ORDER BY f.id DESC LIMIT 1""",
                        (order["account"], order["code"])).fetchone()
                    if entry:
                        cost_pct = json.loads(entry["settings"] or "{}").get("cost_allowance_pct", cost_pct)
                    realized = delta * (delta_price - old_avg) - delta * old_avg * cost_pct / 100
                    qty -= delta
                    new_avg = old_avg if qty else 0
                    peak = peak if qty else 0
                c.execute("INSERT INTO positions VALUES(?,?,?,?,?) ON CONFLICT(account,code) DO UPDATE SET qty=excluded.qty,avg=excluded.avg,peak=excluded.peak",
                          (order["account"], order["code"], qty, new_avg, peak))
                c.execute("INSERT INTO fills(order_id,account,code,side,qty,price,realized,ts) VALUES(?,?,?,?,?,?,?,?)",
                          (oid, order["account"], order["code"], order["side"], delta, delta_price, realized, ts))
            state = "FILLED" if total_filled == order["qty"] else ("CANCELLED" if terminal else ("PARTIAL" if total_filled else "ACCEPTED"))
            c.execute("UPDATE orders SET filled=?,avg=?,state=?,integrated_no=?,error='' WHERE id=?",
                      (total_filled, avg, state, str(integrated_no), oid))

    def peak(self, account, code, price):
        with self.db() as c:
            c.execute("UPDATE positions SET peak=MAX(peak,?) WHERE account=? AND code=?", (price, account, code))

    def stats(self, account, day):
        orders = [o for o in self.orders(account) if o["created"].startswith(day) and o["side"] == "BUY" and o["state"] != "REJECTED"]
        # Submitted/unknown requests reserve the full notional until reconciled.
        amount = sum((o["qty"] * o["price"] if o["state"] in OPEN_STATES else o["filled"] * o["avg"]) for o in orders)
        with self.db() as c:
            realized = c.execute("SELECT COALESCE(SUM(realized),0) FROM fills WHERE account=? AND ts LIKE ?", (account, day+"%")).fetchone()[0]
        return dict(buy_amount=amount, buys=len(orders), realized=realized)

    def review(self, account, strategy=None):
        """Completed position episodes, not partial fills counted as trades."""
        with self.db() as c:
            fills = [dict(r) for r in c.execute("""
                SELECT f.*, o.reason, x.strategy, x.settings, x.signal FROM fills f
                JOIN orders o ON o.id=f.order_id LEFT JOIN trade_context x ON x.order_id=f.order_id
                WHERE f.account=? ORDER BY f.id""", (account,))]
        active, completed = {}, []
        for fill in fills:
            code = fill["code"]
            if fill["side"] == "BUY":
                trade = active.setdefault(code, dict(code=code, opened=fill["ts"], qty=0,
                    buy_amount=0.0, net=0.0, strategy=fill["strategy"] or "이전 버전",
                    reason=fill["reason"], settings=json.loads(fill["settings"] or "{}"),
                    signal=json.loads(fill["signal"] or "{}")))
                trade["qty"] += fill["qty"]
                trade["buy_amount"] += fill["qty"]*fill["price"]
            else:
                if code not in active or active[code]["qty"] < fill["qty"]:
                    raise ValueError("복기 기록의 매수·매도 수량이 맞지 않습니다.")
                trade = active[code]
                trade["qty"] -= fill["qty"]
                trade["net"] += fill["realized"]
                if trade["qty"] == 0:
                    trade["closed"] = fill["ts"]
                    trade["return_pct"] = trade["net"]/trade["buy_amount"]*100
                    completed.append(active.pop(code))
        if strategy is not None:
            completed = [t for t in completed if t["strategy"] == strategy]
        gains = sum(max(0, t["net"]) for t in completed)
        losses = -sum(min(0, t["net"]) for t in completed)
        equity = peak = drawdown = 0.0
        streak = 0
        curve = []
        for trade in completed:
            equity += trade["net"]
            peak = max(peak, equity)
            drawdown = max(drawdown, peak-equity)
            streak = streak+1 if trade["net"] < 0 else 0
            curve.append(dict(시각=trade["closed"], 누적추정순손익=equity))
        count = len(completed)
        return dict(count=count, net=equity, win_rate=sum(t["net"]>0 for t in completed)/count*100 if count else None,
                    expectancy=equity/count if count else None,
                    profit_factor=gains/losses if losses else None, max_drawdown=drawdown,
                    loss_streak=streak, trades=completed, curve=curve, open_count=len(active))

    def review_export(self, account):
        """An audit journal without keys, tokens or raw account numbers."""
        return dict(format="nh-trade-review-v1", review=self.review(account),
                    orders=[{k:v for k,v in o.items() if k not in {"account", "baseline"}}
                            for o in self.orders(account)])


# ===== broker =====

from datetime import datetime
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit
import json
import os
import re
import threading
import time



class BrokerError(RuntimeError):
    pass


class OrderStopped(RuntimeError):
    """Local cancellation before the order API was called."""


def safe_message(error):
    message = str(error)
    for key in ("NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NH_APP_KEY", "NH_APP_SECRET", "APP_PASSWORD", "NHPLUG_DEFAULT_ACCOUNT"):
        secret = os.environ.get(key)
        if secret:
            message = message.replace(secret, "[숨김]")
    message = re.sub(r"(?i)(appkey|appsecretkey|access_token|token|authorization)[=:][^\s&]+", r"\1=[숨김]", message)
    message = re.sub(r"\b\d{11,}\b", "[숨김]", message)
    return message[:500]


def response_message(data):
    envelope = data.get("message")
    envelope = envelope if isinstance(envelope, dict) else {}
    code = str(data.get("rsp_cd") or envelope.get("msg_code") or "").strip()
    msg = str(data.get("rsp_msg") or envelope.get("usr_msg") or "").strip()
    return code, msg


def response_summary(data):
    code, msg = response_message(data)
    blocks = ", ".join(k for k in data if re.fullmatch(r"Output_\d+", k)) or "없음"
    return safe_message(f"응답코드 {code or '미제공'} · {msg or '메시지 미제공'} · 응답 블록 {blocks}")


def response_has_error(msg):
    return bool(re.search(r"오류|실패|불가|거부|부족|초과|입력하|유효하지|잘못|권한.{0,10}없|서비스 해지|인증.{0,10}(?:만료|필요)|접근 제한|이용.*제한|시간.*아닙|error|fail|invalid|denied|unauthorized", msg, re.I))


def check_response(data, meta):
    if not isinstance(data, dict):
        raise BrokerError("JSON 객체 응답이 아닙니다.")
    code, msg = response_message(data)
    if meta.status is not None and not 200 <= meta.status < 300:
        raise BrokerError(f"HTTP {meta.status} · {code} · {msg}")
    if response_has_error(msg):
        raise BrokerError(f"{code} · {msg}")
    # No single rsp_cd is treated as universal success. Each caller checks its output.
    return msg


def parse_volume_page(content):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser", from_encoding="euc-kr" if isinstance(content, bytes) else None)
    result = []
    for a in soup.select("a.tltle[href]"):
        code = parse_qs(urlsplit(a["href"]).query).get("code", [""])[0]
        if re.fullmatch(r"[0-9A-Z]{6}", code) and re.search(r"[0-9]", code):
            result.append(code)
    return list(dict.fromkeys(result))


class NhBroker:
    def __init__(self):
        import nhplug
        from nhplug.instruments import load_master
        if not os.getenv("NHPLUG_APP_KEY") or not os.getenv("NHPLUG_APP_SECRET"):
            raise BrokerError("NH 앱키·시크릿을 설정해 주세요.")
        if os.environ.get("NHPLUG_BASE_URL", "https://api.nhplug.com:8443") != "https://api.nhplug.com:8443":
            raise BrokerError("이 패키지는 나무 실전 계좌용입니다. 접속 주소를 확인하세요.")
        self.call, self.load_master = nhplug.call, load_master
        self.rest_lock = threading.Lock()
        self.account = None
        self.identity = None
        self.universe = {}
        self.diagnostics = {}
        self.history_note = "주문내역 조회 대기"

    def query(self, path, payload=None, paginated=False, before_send=None):
        cts, flag, seen, pages = None, None, set(), []
        with self.rest_lock:
            for _ in range(100):
                if before_send is not None:
                    before_send()
                data, meta = self.call(path, payload or {}, cts=cts, cts_flag=flag,
                                       timeout=7, raise_on_error=False, want_meta=True)
                if not hasattr(self, "diagnostics"):
                    self.diagnostics = {}
                self.diagnostics[path] = response_summary(data) if isinstance(data, dict) else "JSON 객체가 아닌 응답"
                message = check_response(data, meta)
                pages.append(data)
                if not paginated or not meta.has_next:
                    return pages if paginated else data
                if not meta.cts or meta.cts in seen:
                    raise BrokerError("연속조회가 반복되어 전체 내역을 확인할 수 없습니다.")
                seen.add(meta.cts)
                cts, flag = meta.cts, meta.cts_flag
        raise BrokerError("연속조회 한도에 도달했습니다. 전체 내역 확인 필요")

    def accounts(self):
        data = self.query("/n2/acctinfo")
        return [r for r in rows(data.get("Output_0")) if str(r.get("acct_type")) in {"01", "02"}]

    def select_account(self, account):
        if account not in {str(a.get("acct_no")) for a in self.accounts()}:
            raise BrokerError("선택한 나무 실전 계좌를 찾지 못했습니다.")
        self.account = account
        self.identity = sha256((os.environ["NHPLUG_APP_KEY"] + ":" + account).encode()).hexdigest()[:24]

    def quote(self, code):
        code = code_of(code)
        data = self.query("/krstock/quote/v1/currentPrice", {"market_cd": "KRX", "iem_cd": code_of(code)})
        q = one(data.get("Output_0"))
        actual = response_code(q["iem_cd"], "현재가 조회 Output_0.iem_cd")
        if actual != code:
            raise BrokerError("시세 응답 종목 불일치")
        for field in ("stck_prpr", "acml_vol", "acml_tr_pbmn", "askp", "bidp"):
            integer(q[field])
        return dict(q, iem_cd=actual)

    def balance(self):
        pages = self.query("/krstock/inquiry/v1/balance", dict(act_no=self.account,
                           bnc_bse_cd="5", ltg_aot_dit_cd="9", aet_bse="2", qut_dit_cd="KRX"), True)
        summary = one(pages[0].get("Output_0"))
        # amt1 is 20% margin buying power. Only amt4 is 100% cash buying power.
        cash = integer(summary["orr_pbl_amt4"])
        holdings, other_assets, unresolved = [], [], []
        for page in pages:
            for index, row in enumerate(rows(page.get("Output_1")), 1):
                qty = number(row["itg_bnc_qty"])
                if qty <= 0:
                    continue
                raw = str(row.get("iem_cd", "")).strip(" \t\r\n\x00")
                kind = str(row.get("pdt_tp_nm", "")).strip()
                nonstock = bool(re.search(r"(?:^|[^A-Z])(?:RP|MMF|CMA)(?:$|[^A-Z])|펀드|채권|예금|발행어음", kind.upper()))
                try:
                    code = response_code(row.get("iem_cd"), f"잔고 조회 Output_1[{index}].iem_cd")
                    problem = ""
                except ValueError as exc:
                    code, problem = None, str(exc)
                if code is not None and not nonstock:
                    holdings.append(dict(code=code, name=str(row.get("iem_nm", "")), qty=qty,
                                         avg=number(row["phs_pr"]), price=number(row["now_pr"])))
                else:
                    # Keep non-stock and unresolved assets visible; never coerce
                    # their long identifiers into a tradable stock code.
                    asset = dict(code=raw, name=str(row.get("iem_nm", "")), kind=kind or "유형 확인 필요", qty=qty,
                                 cost=number(row["byn_amt"]) if row.get("byn_amt") not in (None, "") else None,
                                 value=number(row["eal_amt"]) if row.get("eal_amt") not in (None, "") else None,
                                 pnl=number(row["eal_pls_amt"]) if row.get("eal_pls_amt") not in (None, "") else None,
                                 note="자동매매 대상 외 자산" if nonstock else problem)
                    other_assets.append(asset)
                    if not nonstock:
                        unresolved.append(problem)
        return dict(cash=cash, deposit=integer(summary["dca"]),
                    asset=integer(summary["tot_aet_amt"]), holdings=holdings,
                    other_assets=other_assets, unresolved_holdings=unresolved)

    def history(self, day):
        pages = self.query("/krstock/inquiry/v1/dailyOrderExecution",
                           dict(orr_dt=day.replace("-", ""), act_no=self.account,
                                orr_mkt_cd="00", ost_cns_dit="0"), True)
        result = []
        for page in pages:
            block = page.get("Output_1")
            code, msg = response_message(page)
            if response_has_error(msg):
                raise BrokerError(response_summary(page))
            if block is None or block == []:
                # NH's official SDK documents 13578 as an empty inquiry result.
                # This rule is scoped to order-history reads, never order writes.
                empty = code == "13578" or bool(re.search(
                    r"(?:조회|주문|체결|내역|자료|데이터|결과|해당사항).{0,40}(?:없|0\s*건|존재하지)|조회.{0,20}완료|정상.{0,10}(?:조회|처리)|조회.{0,10}정상", msg))
                if not empty and (block is None or code or msg):
                    raise BrokerError("주문내역 조회 상태 확인 필요 · " + response_summary(page))
                # An explicit [] is an actual empty data block; {} or an
                # unrecognized missing block is never silently accepted.
            elif code == "13578":
                raise BrokerError("조회 0건 코드와 주문내역이 함께 수신됨 · " + response_summary(page))
            for row in rows(block):
                try:
                    code = response_code(row.get("iem_cd"), "주문 조회 Output_1.iem_cd")
                except ValueError:
                    code = None
                result.append(dict(row, iem_cd=code))
        ids = [str(integer(r["itg_orr_no"])) for r in result]
        if len(set(ids)) != len(ids):
            raise BrokerError("주문내역 중복 · 전체 조회 상태 확인 필요")
        self.history_note = f"{day} 주문내역 조회 완료 · {len(result)}건" + (" (정상 0건)" if not result else "")
        return result

    def buyable(self, code, price):
        data = self.query("/krstock/inquiry/v1/buyableQuantity", dict(ost_dit_cd="1",
                           act_no=self.account, iem_cd=code, nmn_pr_tp_cd="01", orr_pr=price))
        o = one(data.get("Output_0"))
        return integer(o["csh_orr_pbl_qty"]), integer(o["csh_orr_pbl_amt"])

    def sellable(self, code):
        data = self.query("/krstock/inquiry/v1/sellableQuantity",
                          dict(act_no=self.account, iem_cd=code, cfd_lon_cd="00"))
        return integer(one(data.get("Output_0"))["sll_pbl_qty"])

    @staticmethod
    def order_payload(account, code, qty, price):
        if qty <= 0 or price <= 0:
            raise ValueError("주문수량/지정가격 오류")
        return dict(act_no=account, iem_cd=code_of(code), orr_qty=int(qty), orr_pr=int(price),
                    nmn_pr_tp_cd="01", orr_cnd_dit_cd="01", ssl_nmn_pr_dit_cd="00",
                    rmt_mkt_cd="KRX", sor_mkt_sli_yn="N")

    def order(self, code, side, qty, price, before_send=None):
        if side not in {"BUY", "SELL"}:
            raise ValueError("매매 방향 오류")
        suffix = "cashBuy" if side == "BUY" else "cashSell"
        data = self.query("/krstock/order/v1/" + suffix, self.order_payload(self.account, code, qty, price), before_send=before_send)
        number_ = integer(one(data.get("Output_0"))["mkt_orr_no"])
        if number_ <= 0:
            raise BrokerError("주문번호가 없습니다. 증권사 주문내역 확인 필요")
        return str(number_)

    def load_universe(self):
        master = self.load_master("m_new_stock")
        records = master.to_dict("records") if hasattr(master, "to_dict") else master
        result = {}
        for row in records:
            code = str(row["sCode"]).strip()
            if not re.fullmatch(r"[0-9A-Z]{6}", code) or not re.search(r"[0-9]", code) or str(row["sMarket"]).strip() not in {"1", "4"}:
                continue
            if any(str(row[k]).strip() == "Y" for k in ("eUnder", "eStop", "eWarn", "sltr_yn")):
                continue
            if str(row["gVenture"]).strip() in {"8", "E"}:
                continue
            result[code] = str(row["sKorName"]).lstrip("*# ")
        if not result:
            raise BrokerError("국내주식 종목마스터가 비어 있습니다.")
        self.universe = result
        return result

    def seeds(self):
        import requests
        found = []
        for market in (0, 1):
            r = requests.get(f"https://finance.naver.com/sise/sise_quant.naver?sosok={market}",
                             timeout=7, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            parsed = parse_volume_page(r.content)
            if not parsed:
                raise BrokerError("거래량 상위 후보 페이지 형식이 변경되었습니다.")
            found.extend(parsed)
        # Public ranking is a discovery hint only; NH KRX data authorizes entries.
        return [c for c in dict.fromkeys(found) if c in self.universe]

    def indexes(self):
        import requests
        from bs4 import BeautifulSoup
        result = {}
        for code in ("KOSPI", "KOSDAQ"):
            r = requests.get(f"https://finance.naver.com/sise/sise_index.naver?code={code}",
                             timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser", from_encoding="euc-kr")
            value = soup.select_one("#now_value")
            change = soup.select_one("#change_value_and_rate")
            if value is None:
                raise BrokerError("시장지수 조회 형식 변경")
            result[code] = dict(value=value.get_text(strip=True),
                                change=change.get_text(" ", strip=True) if change else "")
        return result


def match_order(order, history, claimed):
    """Do not assume market-order ID == integrated-order ID.

    Link an unclaimed, new integrated record only when symbol, side, quantity,
    limit price and request time match uniquely. Ambiguity keeps the order blocked.
    This requires one operator per account; concurrent identical manual orders
    cannot be distinguished by this REST contract.
    """
    if order["integrated_no"]:
        row = next((r for r in history if str(integer(r["itg_orr_no"])) == order["integrated_no"]), None)
        if row is not None:
            side = "매수" if order["side"] == "BUY" else "매도"
            if (str(row.get("iem_cd", "")).strip() != order["code"] or
                integer(row["orr_qty"]) != order["qty"] or
                number(row["orr_pr"]) != order["price"] or
                side not in str(row.get("sby_dit_cd_nm", ""))):
                raise BrokerError("연결된 주문내역의 종목·방향·수량·가격 불일치")
        return row
    baseline = set(json.loads(order["baseline"]))
    created = datetime.fromisoformat(order["created"])
    accepted = datetime.fromisoformat(order["accepted"]) if order["accepted"] else created
    matches = []
    for r in history:
        rid = str(integer(r["itg_orr_no"]))
        if rid in baseline or rid in claimed or str(r.get("iem_cd", "")).strip() != order["code"]:
            continue
        side_name = str(r.get("sby_dit_cd_nm", ""))
        if ("매수" if order["side"] == "BUY" else "매도") not in side_name:
            continue
        if any(word in str(r.get("cor_can_dit_cd_nm", "")) for word in ("취소", "정정")):
            continue
        if integer(r["orr_qty"]) != order["qty"] or number(r["orr_pr"]) != order["price"]:
            continue
        raw_time = str(r.get("orr_tm", "")).strip().replace(":", "").replace(".", "")
        if not re.fullmatch(r"\d{6,9}", raw_time):
            continue
        when = event_time(raw_time[:6], created)
        latest = max((accepted - created).total_seconds(), 15)
        if -2 <= (when - created).total_seconds() <= latest + 3:
            matches.append(r)
    if len(matches) > 1:
        raise BrokerError("일치하는 주문이 여러 건입니다. 자동매매 계좌에서 동시 수동주문을 확인하세요.")
    return matches[0] if matches else None


class PriceStream:
    """One persistent oc socket; <=10 keys and <10 subscription messages/sec."""
    def __init__(self, desired, callback, status, stop):
        self.desired, self.callback, self.status, self.stop = desired, callback, status, stop

    def run(self):
        from nhplug.auth import get_token
        from nhplug.realtime import ws_url, _ssl_context
        import websocket
        while not self.stop.is_set():
            ws = None
            try:
                keys = self.desired()[:10]
                if not keys:
                    self.status("대기", "")
                    self.stop.wait(1)
                    continue
                token = get_token()
                context = _ssl_context()
                opts = {"sslopt": {"context": context}} if context else {}
                ws = websocket.create_connection(ws_url(tr_cd="oc"), timeout=7, **opts)
                ws.settimeout(1)
                subscribed, acked = set(), set()
                self.status("연결됨 · 구독 확인 중", "")
                while not self.stop.is_set():
                    desired = set(self.desired()[:10])
                    if not desired:
                        break
                    for action, changing in (("2", sorted(subscribed-desired)), ("1", sorted(desired-subscribed))):
                        for key in changing:
                            ws.send(json.dumps(dict(header=dict(token=token, tr_type=action), body=dict(tr_cd="oc", tr_key=key))))
                            if action == "1":
                                subscribed.add(key)
                            else:
                                subscribed.discard(key)
                                acked.discard(key)
                            self.stop.wait(.12)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise BrokerError("실시간 연결 종료")
                    msg = json.loads(raw)
                    header = msg.get("header", {})
                    if "rsp_cd" in header or "tr_type" in header:
                        if str(header.get("rsp_cd", "")) != "00000":
                            raise BrokerError(str(header.get("rsp_cd", "")) + " · " + str(header.get("rsp_msg", "구독 실패")))
                        key = msg.get("body", {}).get("tr_key", [])
                        if header.get("tr_type") == "1":
                            acked.update(key if isinstance(key, list) else [key])
                        self.status(f"구독 확인 {len(acked & subscribed)} / {len(subscribed)}", "")
                    else:
                        self.callback(msg)
            except Exception as exc:
                self.status("재연결 대기", safe_message(exc))
                self.stop.wait(3)
            finally:
                if ws is not None:
                    ws.close()
                self.status("연결 종료", "")


# ===== engine =====

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import copy
import os
import threading
import time



class Engine:
    def __init__(self, broker, path, clock=now, threaded=True):
        self.broker, self.clock = broker, clock
        self.lock, self.actions = threading.RLock(), threading.RLock()
        self.stop_event = threading.Event()
        self.process_lock = None
        if threaded:
            from filelock import FileLock, Timeout
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.process_lock = FileLock(str(path) + ".lock")
            try:
                self.process_lock.acquire(timeout=0)
            except Timeout:
                raise RuntimeError("동일한 데이터 폴더에서 이미 실행 중입니다. 기존 화면을 사용하세요.")
        self.store = Store(path)
        self.settings = Settings(**self.store.get("settings", {}))
        self.settings_warning = ""
        try:
            self.settings.validate()
        except ValueError as exc:
            self.settings_warning = str(exc)
        self.mode = "STOPPED"
        self.control_generation = 0
        self.control_messages = []
        self.job_busy, self.job_name, self.job_error = False, "", ""
        self.available_accounts = []
        self.status = "계좌 연결 전"
        self.connection_ready = False
        self.verified_account = None
        self.connection_error = ""
        self.connection_steps = {name: "대기" for name in ("계좌 선택", "잔고 조회", "주문내역 조회", "현재가 조회")}
        self.error = ""
        self.ws_state = "대기"
        self.ws_error = ""
        self.tapes, self.candidates, self.blockers = {}, {}, {}
        self.watch = []
        self.account_data = None
        self.balance_at = None
        self.balance_error = ""
        self.heartbeat = None
        self.cycle_at = None
        self.scan_at = None
        self.last_tick = None
        self.last_signal = None
        self.tick_count, self.invalid_ticks = 0, 0
        self.scan_done, self.scan_total = 0, 0
        self.scan_source = "후보 준비 전"
        self.scan_errors = 0
        self.last_scan_error = ""
        self.indices = {}
        self.indices_at = None
        self.index_error = ""
        self.day = self.clock().date().isoformat()
        self.threads = []
        if threaded:
            for name, fn in (("nh-scan", self.scan_loop), ("nh-control", self.control_loop),
                             ("nh-market-index", self.index_loop),
                             ("nh-price", PriceStream(self.desired_codes, self.on_message, self.stream_status, self.stop_event).run)):
                thread = threading.Thread(target=fn, name=name, daemon=True)
                thread.start()
                self.threads.append(thread)

    @property
    def account(self):
        return self.broker.identity or ""

    @property
    def ready(self):
        return bool(self.connection_ready and self.account and self.verified_account == self.account)

    def log(self, kind, message):
        self.store.log(self.clock().isoformat(), kind, safe_message(message))

    def update_settings(self, settings):
        settings.validate()
        with self.actions, self.lock:
            if self.mode != "STOPPED":
                raise ValueError("설정을 바꾸려면 전체정지를 눌러 주세요.")
            self.settings = settings
            self.settings_warning = ""
            self.store.set("settings", asdict(settings))

    def connect(self, account):
        with self.actions:
            if self.mode != "STOPPED":
                raise ValueError("계좌 변경은 전체정지 상태에서 가능합니다.")
            if self.account and (self.store.positions(self.account) or self.store.orders(self.account, True)) and account != self.broker.account:
                raise ValueError("현재 계좌에 자동매매 보유분 또는 확인 중인 주문이 있습니다.")
            with self.lock:
                self.connection_ready, self.verified_account = False, None
                self.connection_error = ""
                self.connection_steps = {name: "대기" for name in self.connection_steps}
                self.status = "계좌 연결 점검 중"
            stage = "계좌 선택"
            try:
                with self.lock:
                    self.connection_steps[stage] = "확인 중"
                previous_account = self.broker.account
                self.broker.select_account(account)
                if previous_account != self.broker.account:
                    with self.lock:
                        self.account_data, self.balance_at = None, None
                        self.balance_error = ""
                with self.lock:
                    self.connection_steps[stage] = "완료"
                for stage, check in (
                    ("잔고 조회", self.refresh_balance),
                    ("주문내역 조회", lambda: self.broker.history(self.clock().date().isoformat())),
                    ("현재가 조회", lambda: self.broker.quote("005930")),
                ):
                    with self.lock:
                        self.connection_steps[stage] = "확인 중"
                    result = check()
                    if stage == "잔고 조회" and result.get("unresolved_holdings"):
                        raise ValueError(result["unresolved_holdings"][0] + " · 해당 보유자산은 계좌 현황에서 확인할 수 있습니다")
                    with self.lock:
                        self.connection_steps[stage] = "완료"
            except Exception as exc:
                message = stage + " 실패 · " + safe_message(exc)
                with self.lock:
                    self.connection_steps[stage] = "실패"
                    self.connection_error = message
                    self.status = "계좌 연결 점검 미완료 · 자동매매 시작 보류"
                    self.mode = "STOPPED"
                    self.watch = []
                self.log("연결 점검 실패", message)
                raise RuntimeError(message) from exc
            with self.lock:
                self.connection_ready, self.verified_account = True, self.account
                self.error = ""
                self.status = "계좌·잔고·현재가·주문조회 확인 완료"
            self.log("연결", self.status)

    def start_job(self, kind, account=None):
        if kind not in {"계좌 불러오기", "연결 점검", "시작 점검", "잔고 갱신"}:
            raise ValueError("지원하지 않는 작업")
        with self.lock:
            if self.job_busy:
                raise ValueError("앞선 조회 작업이 진행 중입니다.")
            self.job_busy, self.job_name, self.job_error = True, kind, ""
            generation = self.control_generation
        def work():
            try:
                if kind == "계좌 불러오기":
                    accounts = self.broker.accounts()
                    with self.lock:
                        self.available_accounts = accounts
                elif kind == "연결 점검":
                    self.connect(account)
                elif kind == "시작 점검":
                    self.set_mode("RUNNING", expected_generation=generation)
                else:
                    with self.actions:
                        self.refresh_balance()
            except Exception as exc:
                with self.lock:
                    self.job_error = safe_message(exc)
            finally:
                with self.lock:
                    self.job_busy = False
        thread = threading.Thread(target=work, name="nh-user-check", daemon=True)
        thread.start()
        return thread

    def set_mode(self, mode, expected_generation=None):
        if mode not in {"RUNNING", "PAUSE_BUY", "STOPPED"}:
            raise ValueError("운영 상태 오류")
        # Stop commands never wait for REST or the control-loop action lock.
        if mode in {"STOPPED", "PAUSE_BUY"}:
            with self.lock:
                self.control_generation += 1
                self.mode = "STOPPED" if mode == "STOPPED" or self.mode == "STOPPED" else "PAUSE_BUY"
                self.status = "전체정지 · 새 주문 전송 중단" if self.mode == "STOPPED" else "신규매수 정지 · 보유분 매도 관리"
                if self.mode == "STOPPED":
                    self.watch = []
                self.control_messages.append(self.status)
            return
        with self.lock:
            generation = self.control_generation if expected_generation is None else expected_generation
        with self.actions:
            if generation != self.control_generation:
                raise ValueError("정지 요청이 우선 적용되어 시작 점검을 취소했습니다.")
            if mode != "STOPPED" and not self.ready:
                raise ValueError("계좌 연결 점검을 먼저 실행해 주세요.")
            if mode == "RUNNING":
                self.settings.validate()
                balance = self.refresh_balance()
                if balance.get("unresolved_holdings"):
                    raise ValueError("보유자산 종목코드 확인이 끝나지 않았습니다. 계좌 현황의 확인 항목을 봐 주세요.")
                self.reconcile_orders()
                if self.store.orders(self.account, True):
                    raise ValueError("확인 중인 주문이 있습니다. 주문내역 확인 후 시작하세요.")
                if self.store.get("loss_stop_" + self.account) == self.clock().date().isoformat():
                    raise ValueError("오늘 손실 한도에 도달했습니다. 당일 신규매수를 재개할 수 없습니다.")
                if self.store.get("quality_stop_" + self.account) == self.clock().date().isoformat():
                    raise ValueError("오늘 연속 손실 제한에 도달했습니다. 신규매수는 다음 거래일에 재개할 수 있습니다.")
            with self.lock:
                if generation != self.control_generation:
                    raise ValueError("정지 요청이 우선 적용되어 시작 점검을 취소했습니다.")
                self.mode = mode
                self.status = {"RUNNING": "실시간 조건 감시 시작", "PAUSE_BUY": "신규매수 정지 · 보유분 매도 관리", "STOPPED": "전체정지 · 주문 전송 중단"}[mode]
                if mode == "STOPPED":
                    self.watch = []
            self.log("제어", self.status)

    def refresh_balance(self):
        try:
            balance = self.broker.balance()
        except Exception as exc:
            with self.lock:
                self.balance_error = safe_message(exc)
            raise
        with self.lock:
            self.account_data = balance
            self.balance_at = self.clock()
            self.balance_error = ""
        return balance

    def reconcile_orders(self):
        if not self.account:
            return
        pending = self.store.orders(self.account, True)
        for day in sorted({o["created"][:10] for o in pending}):
            history = self.broker.history(day)
            claimed = {o["integrated_no"] for o in self.store.orders(self.account) if o["created"].startswith(day) and o["integrated_no"]}
            for order in [o for o in pending if o["created"].startswith(day)]:
                row = match_order(order, history, claimed)
                if row is None:
                    if (self.clock() - datetime.fromisoformat(order["created"])).total_seconds() > 30:
                        self.store.mark(order["id"], "UNKNOWN", error="주문내역 연결 대기 · 나무 앱 확인 필요")
                    continue
                total = integer(row["tot_cns_qty"])
                remaining = integer(row["ny_cns_qty"])
                cancelled = integer(row["can_qty"])
                if min(total, remaining, cancelled) < 0 or total + remaining + cancelled > order["qty"]:
                    raise ValueError("주문 수량 합계 불일치")
                rejected = str(row.get("orr_rjt_rsn_cd_nm", "")).strip()
                terminal = remaining == 0 and (total + cancelled >= order["qty"] or bool(rejected))
                avg = number(row["cns_avg_uit_pr"]) if total else 0
                rid = str(integer(row["itg_orr_no"]))
                stamp = self.clock().isoformat() if day == self.clock().date().isoformat() else order["created"]
                self.store.reconcile(order["id"], total, avg, terminal, rid, stamp, self.settings.cost_allowance_pct)
                claimed.add(rid)
                if total != order["filled"]:
                    self.log("체결", f"{order['code']} {order['side']} 누적 {total}주 · 평균 {avg:,.0f}원")
        if pending:
            self.refresh_balance()

    def on_message(self, message):
        try:
            tick = Tick.parse(message, self.clock())
            if tick is None:
                return
            age = (self.clock()-tick.event).total_seconds()
            if age > self.settings.freshness_seconds:
                raise ValueError(f"{age:.1f}초 지연된 체결 데이터")
            with self.lock:
                tape = self.tapes.setdefault(tick.code, Tape())
                if not tape.add(tick):
                    self.invalid_ticks += 1
                    return
                self.tick_count += 1
                self.last_tick = tick.received
                self.ws_error = ""
                signal = tape.signal(self.settings, self.clock())
                if signal["eligible"] and self.mode == "RUNNING" and tick.code in self.candidates:
                    self.last_signal = dict(code=tick.code, time=tick.received.isoformat(), score=signal["score"])
        except Exception as exc:
            with self.lock:
                self.invalid_ticks += 1
                self.ws_error = "체결 데이터 해석 오류 · " + safe_message(exc)
                for tape in self.tapes.values():
                    tape.interrupt()

    def stream_status(self, state, error):
        with self.lock:
            self.ws_state = state
            if state in {"재연결 대기", "연결 종료"}:
                for tape in self.tapes.values():
                    tape.interrupt()
            if error:
                self.ws_error = error

    def desired_codes(self):
        with self.lock:
            if not self.account or not market_time(self.clock()):
                return []
            # Display-only holdings never enter the trading watch list.
            held = [h["code"] for h in (self.account_data or {}).get("holdings", [])]
            trading = self.watch if self.mode != "STOPPED" else []
            return list(dict.fromkeys(list(trading) + held))[:10]

    def update_watch(self):
        positions = [p["code"] for p in self.store.positions(self.account)] if self.account else []
        pending = [o["code"] for o in self.store.orders(self.account, True)] if self.account else []
        with self.lock:
            if self.mode == "STOPPED":
                self.watch = []
                return
            candidates = sorted(self.candidates, key=lambda c: self.candidates[c]["turnover"], reverse=True)
            # Keep qualified subscriptions stable so observed bars can finish.
            retained = [c for c in self.watch if c in self.candidates]
            selected = list(dict.fromkeys(positions + pending + retained + candidates))[:10]
            if set(selected) != set(self.watch):
                for code in set(self.watch) - set(selected):
                    if code in self.tapes:
                        self.tapes[code].interrupt()
                self.watch = selected

    def scan_loop(self):
        codes, cursor, refreshed = [], 0, 0.0
        while not self.stop_event.is_set():
            try:
                if self.mode != "RUNNING" or not self.ready or not market_time(self.clock(), True):
                    self.stop_event.wait(1)
                    continue
                if not codes or (cursor == 0 and time.monotonic() - refreshed >= 120):
                    if not self.broker.universe:
                        self.broker.load_universe()
                    try:
                        codes = self.broker.seeds()
                        if not codes:
                            raise ValueError("상위 후보 없음")
                        source = "거래량 상위 후보 → NH KRX 재확인"
                    except Exception as exc:
                        codes = list(self.broker.universe)
                        source = "상위 후보 조회 실패 · 전체 종목 순환(시간 소요)"
                        self.log("후보 조회", safe_message(exc))
                    cursor, refreshed = 0, time.monotonic()
                    with self.lock:
                        self.scan_source, self.scan_total, self.scan_done = source, len(codes), 0
                code = codes[cursor]
                quote = self.broker.quote(code)
                stamp = self.clock()
                volume, turnover = integer(quote["acml_vol"]), integer(quote["acml_tr_pbmn"])
                risky = (str(quote.get("sltr_yn", "")).strip() == "Y" or
                         str(quote.get("mrkt_alrm_code", "")).strip() in {"2", "3", "4", "5"} or
                         str(quote.get("short_over_code", "")).strip() in {"2", "3"})
                with self.lock:
                    if not risky and volume >= self.settings.min_volume and turnover >= self.settings.min_turnover:
                        self.candidates[code] = dict(name=str(quote.get("iem_nm", code)), volume=volume,
                                                     turnover=turnover, at=stamp.isoformat())
                    else:
                        self.candidates.pop(code, None)
                    self.candidates = {c: r for c, r in self.candidates.items()
                                       if (stamp-datetime.fromisoformat(r["at"])).total_seconds() <= 300}
                    self.scan_done = cursor + 1
                    self.scan_at = stamp
                    self.last_scan_error = ""
                self.update_watch()
                cursor = (cursor + 1) % len(codes)
                self.stop_event.wait(.65)
            except Exception as exc:
                with self.lock:
                    self.scan_errors += 1
                    self.last_scan_error = safe_message(exc)
                if codes:
                    cursor = (cursor + 1) % len(codes)
                self.stop_event.wait(2)

    def index_loop(self):
        while not self.stop_event.is_set():
            try:
                values = self.broker.indexes()
                with self.lock:
                    self.indices, self.indices_at, self.index_error = values, self.clock(), ""
            except Exception as exc:
                with self.lock:
                    self.index_error = safe_message(exc)
            self.stop_event.wait(30)

    def estimated_pnl(self, positions, current):
        realized = self.store.stats(self.account, current.date().isoformat())["realized"]
        unrealized = 0.0
        with self.lock:
            for p in positions:
                tape = self.tapes.get(p["code"])
                if not tape or not tape.latest or (current-tape.latest.event).total_seconds() > self.settings.freshness_seconds:
                    return realized, False
                if tape.latest.bid <= 0:
                    return realized, False
                entry_settings = self.position_settings(p["code"])
                unrealized += p["qty"] * (tape.latest.bid-p["avg"]-p["avg"]*entry_settings.cost_allowance_pct/100)
        return realized + unrealized, True

    def position_settings(self, code):
        context = self.store.position_context(self.account, code)
        return Settings(**context["settings"]) if context.get("settings") else self.settings

    def buy_block(self, code, current, balance):
        settings = self.settings
        if not self.ready:
            return "계좌 연결 점검 미완료"
        if balance.get("unresolved_holdings"):
            return "보유자산 종목코드 확인 필요"
        if self.mode != "RUNNING" or not market_time(current, True):
            return "신규매수 운영시간 대기"
        if not self.balance_at or (current-self.balance_at).total_seconds() > 12:
            return "계좌 잔고 재확인 중"
        if balance["cash"] <= 0:
            return "현금 주문가능금액 부족"
        if self.store.orders(self.account, True):
            return "앞선 주문 체결 확인 중"
        if any(h["code"] == code for h in balance["holdings"]):
            return "계좌에 이미 보유한 종목"
        positions = self.store.positions(self.account)
        if len(positions) >= settings.max_positions:
            return "최대 보유종목 도달"
        pnl, complete = self.estimated_pnl(positions, current)
        if not complete:
            return "보유종목 최신 시세 대기"
        if pnl <= -settings.daily_loss or self.store.get("loss_stop_"+self.account) == current.date().isoformat():
            return "당일 손실 한도 도달"
        if self.store.get("quality_stop_"+self.account) == current.date().isoformat():
            return "당일 연속 손실 제한 · 전략 복기 필요"
        stats = self.store.stats(self.account, current.date().isoformat())
        if stats["buys"] >= settings.max_buys:
            return "당일 매수 횟수 한도"
        if stats["buy_amount"] >= settings.capital * .75:
            return "당일 신규매수 금액 한도"
        for order in reversed(self.store.orders(self.account)):
            if order["code"] == code and order["state"] != "REJECTED":
                if (current-datetime.fromisoformat(order["created"])).total_seconds() < settings.cooldown_seconds:
                    return "재진입 대기시간"
                break
        with self.lock:
            candidate = self.candidates.get(code)
            tape = self.tapes.get(code)
            if not candidate or (current-datetime.fromisoformat(candidate["at"])).total_seconds() > 300:
                return "종목 조건 재확인 중"
            signal = tape.signal(settings, current) if tape else {"eligible": False, "reason": "체결 대기"}
        if not signal["eligible"]:
            return signal["reason"]
        return ""

    def submit(self, code, side, reason):
        with self.actions:
            generation = self.control_generation
            if self.mode == "STOPPED" or not self.ready:
                return
            balance = self.refresh_balance()
            current = self.clock()
            if side == "BUY":
                blocker = self.buy_block(code, current, balance)
                if blocker:
                    self.blockers[code] = blocker
                    return
            if any(o["code"] == code for o in self.store.orders(self.account, True)):
                return
            with self.lock:
                tick = self.tapes[code].latest
                entry_signal = self.tapes[code].signal(self.settings, current) if side == "BUY" else None
            if max((current-tick.event).total_seconds(), (current-tick.received).total_seconds()) > self.settings.freshness_seconds:
                self.blockers[code] = "주문 직전 체결 지연"
                return
            price = tick.ask if side == "BUY" else tick.bid
            if price <= 0:
                return
            owned = self.store.positions(self.account)
            if side == "BUY":
                cash_qty, cash_amt = self.broker.buyable(code, price)
                deployed = sum(p["qty"]*p["avg"] for p in owned)
                daily = self.store.stats(self.account, current.date().isoformat())["buy_amount"]
                budget = min(self.settings.per_order, self.settings.capital-deployed, balance["cash"],
                             cash_amt, self.settings.capital*.75-daily)
                pnl, complete = self.estimated_pnl(owned, current)
                risk_rate = (self.settings.stop_loss+self.settings.cost_allowance_pct)/100
                committed_risk = sum(p["qty"]*p["avg"]*(self.position_settings(p["code"]).stop_loss+
                                     self.position_settings(p["code"]).cost_allowance_pct)/100 for p in owned)
                risk_budget = min(self.settings.capital*self.settings.risk_per_trade_pct/100,
                                  self.settings.daily_loss+min(0, pnl)-committed_risk)
                if not complete:
                    return
                qty = min(cash_qty, int(max(0, budget) // (price*1.005)),
                          int(max(0, risk_budget) // (price*risk_rate)))
            else:
                bot_qty = next((p["qty"] for p in owned if p["code"] == code), 0)
                account_qty = sum(h["qty"] for h in balance["holdings"] if h["code"] == code)
                if bot_qty > account_qty:
                    self.blockers[code] = "보유수량 불일치 · 나무 앱 확인 필요"
                    return
                qty = min(bot_qty, self.broker.sellable(code))
            if qty <= 0:
                self.blockers[code] = "현금 주문가능금액 또는 손실예산으로 1주 매수 불가" if side == "BUY" else "매도가능수량 부족"
                return
            baseline = [str(integer(r["itg_orr_no"])) for r in self.broker.history(current.date().isoformat())]
            # Validate again after slow REST calls, immediately before the write.
            with self.lock:
                latest = self.tapes[code].latest
                actual_now = self.clock()
                if generation != self.control_generation or self.mode == "STOPPED":
                    return
                if not market_time(actual_now, side == "BUY"):
                    return
                if max((actual_now-latest.event).total_seconds(), (actual_now-latest.received).total_seconds()) > self.settings.freshness_seconds:
                    self.blockers[code] = "주문 준비 중 체결 지연"
                    return
                if side == "BUY":
                    updated_signal = self.tapes[code].signal(self.settings, actual_now)
                    if (self.mode != "RUNNING" or not updated_signal["eligible"] or
                        updated_signal.get("setup_id") != entry_signal.get("setup_id") or
                        not latest.ask <= price <= updated_signal["max_entry"]):
                        self.blockers[code] = "주문 준비 중 진입 조건·호가 변경"
                        return
            with self.lock:
                context = dict(strategy=STRATEGY_VERSION, settings=asdict(self.settings),
                               signal=dict(self.tapes[code].signal(self.settings, actual_now),
                                           ask=latest.ask, bid=latest.bid, event=latest.event.isoformat()))
            oid = self.store.prepare(self.account, code, side, qty, price,
                                     actual_now.isoformat(), baseline, reason, context=context)
            self.log("주문 전송", f"{code} {side} {qty}주 · 지정가 IOC {price:,}원 · {reason}")
            try:
                def permit():
                    with self.lock:
                        if generation != self.control_generation or self.mode == "STOPPED" or (side == "BUY" and self.mode != "RUNNING"):
                            raise OrderStopped("정지 요청으로 전송 전 주문을 취소했습니다.")
                        stamp = self.clock()
                        tape = self.tapes.get(code)
                        tick_now = tape.latest if tape else None
                        if not self.ready or not market_time(stamp, side == "BUY"):
                            raise OrderStopped("연결·운영시간 변경으로 주문을 전송하지 않았습니다.")
                        if not tick_now or max((stamp-tick_now.event).total_seconds(), (stamp-tick_now.received).total_seconds()) > self.settings.freshness_seconds:
                            raise OrderStopped("통신 대기 중 시세가 지연되어 주문을 전송하지 않았습니다.")
                        if side == "BUY":
                            active_signal = tape.signal(self.settings, stamp)
                            if (not active_signal["eligible"] or active_signal.get("setup_id") != entry_signal.get("setup_id") or
                                not tick_now.ask <= price <= active_signal["max_entry"]):
                                raise OrderStopped("통신 대기 중 진입 조건·호가가 바뀌어 주문을 전송하지 않았습니다.")
                broker_no = self.broker.order(code, side, qty, price, before_send=permit)
                self.store.mark(oid, "ACCEPTED", broker_no=broker_no, accepted=self.clock().isoformat())
                self.blockers[code] = "주문 접수 · 체결내역 확인 중"
            except OrderStopped as exc:
                self.store.mark(oid, "REJECTED", error=str(exc))
                self.blockers[code] = str(exc)
            except Exception as exc:
                # Request may already have reached the broker. Never auto-resubmit.
                message = safe_message(exc)
                self.store.mark(oid, "UNKNOWN", error=message)
                self.blockers[code] = "주문 결과 미확인 · 자동 재전송 중단"
                self.log("주문 확인 필요", f"{code} · {message}")

    def step(self):
        current = self.clock()
        with self.lock:
            self.heartbeat = current
            if current.date().isoformat() != self.day:
                self.day = current.date().isoformat()
                self.candidates.clear()
                self.tapes.clear()
                self.watch.clear()
                self.broker.universe = {}
                self.tick_count = 0
                self.last_tick = self.last_signal = None
        if not self.account:
            return
        if not self.balance_at or (current-self.balance_at).total_seconds() >= 5:
            self.refresh_balance()
        self.reconcile_orders()
        if self.mode == "STOPPED" or not self.ready:
            return
        self.update_watch()
        positions = self.store.positions(self.account)
        review = self.store.review(self.account)
        today_trades = [t for t in review["trades"] if t["closed"].startswith(current.date().isoformat())]
        streak = 0
        for trade in reversed(today_trades):
            if trade["net"] >= 0:
                break
            streak += 1
        if streak >= self.settings.consecutive_loss_limit:
            self.store.set("quality_stop_"+self.account, current.date().isoformat())
            with self.lock:
                if self.mode != "STOPPED":
                    self.mode, self.status = "PAUSE_BUY", "당일 연속 손실 제한 · 신규매수 정지"
        pnl, complete = self.estimated_pnl(positions, current)
        if complete and pnl <= -self.settings.daily_loss:
            self.store.set("loss_stop_"+self.account, current.date().isoformat())
            with self.lock:
                if self.mode != "STOPPED":
                    self.mode, self.status = "PAUSE_BUY", "당일 손실 한도 도달 · 보유분 정리 관리"
        if not market_time(current):
            with self.lock:
                self.status = "정규 매매시간 대기 · 평일 09:00~15:20"
            return
        for position in positions:
            code, avg = position["code"], position["avg"]
            with self.lock:
                tape = self.tapes.get(code)
                tick = tape.latest if tape else None
            if not tick or max((current-tick.event).total_seconds(), (current-tick.received).total_seconds()) > self.settings.freshness_seconds:
                self.blockers[code] = "보유종목 실시간 시세 대기"
                continue
            if tick.bid <= 0:
                self.blockers[code] = "보유종목 유효한 매도 호가 대기"
                continue
            self.store.peak(self.account, code, tick.bid)
            peak = max(position["peak"], tick.bid)
            gain = (tick.bid/avg-1)*100
            entry_settings = self.position_settings(code)
            entry_context = self.store.position_context(self.account, code)
            structural_stop = number(entry_context.get("signal", {}).get("structural_stop", 0))
            reason = ""
            if self.store.get("loss_stop_"+self.account) == current.date().isoformat():
                reason = "당일 손실 한도 정리"
            elif current.hour == 15 and current.minute >= 15:
                reason = "장 종료 전 정리"
            elif gain <= -entry_settings.stop_loss:
                reason = f"손절 {gain:.2f}%"
            elif structural_stop > 0 and tick.bid <= structural_stop:
                reason = "눌림 저점 이탈 · 진입 근거 손절"
            elif gain >= entry_settings.take_profit:
                reason = f"익절 {gain:.2f}%"
            elif peak >= avg*(1+(entry_settings.trailing+entry_settings.cost_allowance_pct)/100) and (tick.bid/peak-1)*100 <= -entry_settings.trailing:
                reason = "최고가 대비 하락 · 이익 보호"
            if reason:
                self.submit(code, "SELL", reason)
        if self.mode == "RUNNING" and market_time(current, True):
            with self.lock:
                available = [(c, self.tapes[c].signal(self.settings, current)) for c in self.watch if c in self.tapes and c in self.candidates]
            for code, signal in sorted(available, key=lambda x: x[1]["score"], reverse=True):
                blocker = self.buy_block(code, current, self.account_data)
                self.blockers[code] = blocker or "진입 조건 충족"
                if not blocker:
                    self.submit(code, "BUY", signal["reason"])
                    break
        with self.lock:
            if self.mode != "STOPPED":
                self.status = "조건 감시 중" if self.mode == "RUNNING" else "신규매수 정지 · 보유분 매도 관리"

    def control_loop(self):
        last_error = ""
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    messages, self.control_messages = self.control_messages, []
                for message in messages:
                    self.log("제어", message)
                with self.actions:
                    self.step()
                with self.lock:
                    self.error = ""
                    self.cycle_at = self.clock()
                last_error = ""
            except Exception as exc:
                message = safe_message(exc)
                with self.lock:
                    self.error = message
                    self.status = "계좌·주문 확인 오류 · 신규 주문 대기"
                if message != last_error:
                    self.log("점검 필요", message)
                last_error = message
            self.stop_event.wait(1)

    def snapshot(self):
        current = self.clock()
        with self.lock:
            watched = []
            for code in self.watch:
                tape = self.tapes.get(code)
                signal = tape.signal(self.settings, current) if tape else {"score": 0, "reason": "첫 체결 대기"}
                tick = tape.latest if tape else None
                watched.append(dict(code=code, name=self.candidates.get(code, {}).get("name", self.broker.universe.get(code, code)),
                                    price=tick.price if tick else None, volume=tick.volume if tick else None,
                                    score=signal["score"], reason=self.blockers.get(code) if signal.get("eligible") and self.blockers.get(code) else signal["reason"],
                                    ready_bars=signal.get("ready_bars", 0), checks=signal.get("checks", []),
                                    vwap=signal.get("vwap"), trigger=signal.get("trigger"),
                                    structural_stop=signal.get("structural_stop"), volume_ratio=signal.get("volume_ratio"),
                                    seconds=round((current-tick.event).total_seconds(), 1) if tick else None))
            data = dict(mode=self.mode, status=self.status, error=self.error, ws_state=self.ws_state,
                        ws_error=self.ws_error, last_tick=self.last_tick.isoformat() if self.last_tick else None,
                        tick_count=self.tick_count, invalid_ticks=self.invalid_ticks,
                        heartbeat=self.heartbeat.isoformat() if self.heartbeat else None,
                        cycle_at=self.cycle_at.isoformat() if self.cycle_at else None,
                        scan_at=self.scan_at.isoformat() if self.scan_at else None,
                        balance_at=self.balance_at.isoformat() if self.balance_at else None,
                        balance_error=self.balance_error, connected=bool(self.account),
                        connection_ready=self.ready, connection_error=self.connection_error,
                        connection_steps=copy.deepcopy(self.connection_steps),
                        job_busy=self.job_busy, job_name=self.job_name, job_error=self.job_error,
                        history_note=getattr(self.broker, "history_note", "주문내역 조회 대기"),
                        api_diagnostics=dict(getattr(self.broker, "diagnostics", {})),
                        account_label=("•" * max(0, len(str(self.broker.account))-4) + str(self.broker.account)[-4:]) if self.account else "미연결",
                        captured_at=current.isoformat(), trading_window=market_time(current),
                        buying_window=market_time(current, True),
                        last_signal=copy.deepcopy(self.last_signal), scan_done=self.scan_done, scan_total=self.scan_total,
                        scan_source=self.scan_source, scan_errors=self.scan_errors, last_scan_error=self.last_scan_error,
                        candidates=len(self.candidates), watched=watched, account=copy.deepcopy(self.account_data),
                        indices=copy.deepcopy(self.indices), indices_at=self.indices_at.isoformat() if self.indices_at else None,
                        index_error=self.index_error, settings=asdict(self.settings))
            data["settings_warning"] = self.settings_warning
            data["quality_stop"] = self.store.get("quality_stop_"+self.account) == current.date().isoformat() if self.account else False
            # Account quantities come from balance polling; fresh exchange ticks
            # update display prices only and do not create bot-owned positions.
            holdings = []
            for h in (self.account_data or {}).get("holdings", []):
                tape = self.tapes.get(h["code"])
                tick = tape.latest if tape else None
                live = bool(tick and 0 <= max((current-tick.event).total_seconds(),
                                             (current-tick.received).total_seconds()) <= self.settings.freshness_seconds)
                price = tick.price if live else h["price"]
                price = price if price > 0 else None
                cost = h["qty"] * h["avg"] if h["avg"] > 0 else None
                value = h["qty"] * price if price is not None else None
                pnl = value - cost if value is not None and cost is not None else None
                stamp = tick.event if live else self.balance_at
                holdings.append(dict(h, price=price, cost=cost, value=value, pnl=pnl,
                                     return_pct=pnl/cost*100 if pnl is not None and cost else None,
                                     price_source="실시간 체결" if live else "잔고 조회",
                                     price_at=stamp.isoformat() if stamp else None))
            data["account_holdings"] = holdings
            totals = {}
            for key in ("cost", "value", "pnl"):
                totals[key] = sum(h[key] for h in holdings) if self.account_data is not None and all(h[key] is not None for h in holdings) else None
            totals["return_pct"] = totals["pnl"] / totals["cost"] * 100 if totals["pnl"] is not None and totals["cost"] else None
            data["account_totals"] = totals
            if data["account"] is not None:
                reported = data["account"].get("asset")
                if reported == 0 and (holdings or data["account"].get("deposit", 0) > 0 or data["account"].get("other_assets")):
                    data["account"]["asset_reported"] = reported
                    data["account"]["asset"] = None
                    data["account"]["asset_note"] = "총자산 응답이 0원으로 수신되어 확인이 필요합니다. 예수금과 주식 평가금액을 임의 합산하지 않습니다."
        data["orders"] = self.store.orders(self.account) if self.account else []
        data["positions"] = self.store.positions(self.account) if self.account else []
        data["stats"] = self.store.stats(self.account, current.date().isoformat()) if self.account else {}
        data["logs"] = self.store.logs()
        data["review"] = self.store.review(self.account, STRATEGY_VERSION) if self.account else None
        return data


_singleton = None
_singleton_lock = threading.Lock()


def get_engine():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            path = Path(os.getenv("NH_TRADER_DATA_DIR", "data")) / "trader.sqlite3"
            _singleton = Engine(NhBroker(), path)
        return _singleton
'''


@st.cache_resource(show_spinner=False)
def _load_runtime(version):
    module_name = "_nh_mobile_runtime_" + version
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    module = ModuleType(module_name)
    module.__file__ = __file__
    # Dataclasses resolve their defining module while the source executes.
    sys.modules[module_name] = module
    try:
        exec(compile(_RUNTIME_SOURCE, __file__ + ":runtime", "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


_runtime = _load_runtime(_RUNTIME_VERSION)
now = _runtime.now
safe_message = _runtime.safe_message
get_engine = _runtime.get_engine

# === MOBILE_UI ===

from dataclasses import replace
from datetime import datetime
from html import escape
import hmac
import json
import os
import time

import streamlit as st
from dotenv import load_dotenv


load_dotenv(override=False)
for dest, aliases in {
    "NHPLUG_APP_KEY": ("NHPLUG_APP_KEY", "NH_APP_KEY"),
    "NHPLUG_APP_SECRET": ("NHPLUG_APP_SECRET", "NH_APP_SECRET"),
    "APP_PASSWORD": ("APP_PASSWORD",),
    "NHPLUG_DEFAULT_ACCOUNT": ("NHPLUG_DEFAULT_ACCOUNT",),
    "NH_TRADER_DATA_DIR": ("NH_TRADER_DATA_DIR",),
}.items():
    for name in aliases:
        value = os.getenv(name, "")
        try:
            value = value or str(st.secrets.get(name, ""))
        except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
            pass
        if value:
            os.environ[dest] = value.strip()
            break

os.environ.setdefault("NHPLUG_BASE_URL", "https://api.nhplug.com:8443")
os.environ.setdefault("NHPLUG_AUTH_URL", "https://api.nhplug.com:8443")
os.environ["NHPLUG_RATE_LIMIT"] = "4"
os.environ.setdefault("NHPLUG_TOKEN_CACHE_DIR", os.path.join(os.getenv("NH_TRADER_DATA_DIR", "data"), "tokens"))

st.set_page_config(page_title="나무 자동매매 · 다시 시작", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
.block-container{max-width:1160px;padding-top:1.6rem;padding-bottom:3rem}
div[data-testid="stMetric"]{border:1px solid #dbe4e1;border-radius:12px;padding:12px}
.stButton>button{min-height:48px;font-weight:650}
.nh-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:8px 0 12px}
.nh-card{border:1px solid #879ba64d;border-radius:12px;padding:12px;min-width:0}
.nh-label{font-size:.8rem;opacity:.75;margin-bottom:4px}
.nh-value{font-size:1.13rem;font-weight:700;overflow-wrap:anywhere}
.nh-holding{border:1px solid #879ba64d;border-radius:12px;padding:13px;margin:8px 0}
.nh-holding-head{display:flex;justify-content:space-between;gap:12px;align-items:start}
.nh-holding-detail{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:9px;font-size:.9rem}
.nh-note{font-size:.78rem;opacity:.75;margin-top:8px;overflow-wrap:anywhere}
.nh-up{color:#e76868}.nh-down{color:#5e9fed}
@media(max-width:600px){.block-container{padding-left:1rem;padding-right:1rem}}
@media(max-width:600px){.nh-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.nh-card{padding:10px}}
</style>""", unsafe_allow_html=True)
st.title("나무 자동매매")
st.caption("눌림·재돌파 조건 반영 R4 · 2026.09.10 · 실전 계좌")

password = os.getenv("APP_PASSWORD", "")
if not password:
    st.info("처음 실행하셨다면 앱 비밀번호와 나무 API 연결값을 먼저 설정해 주세요.")
    st.markdown("앱의 **Secrets** 또는 서버 환경변수에 아래 세 값을 설정해 주세요. 기존에 설정했다면 그대로 사용할 수 있습니다.")
    st.code('APP_PASSWORD = "직접 정한 앱 비밀번호"\nNHPLUG_APP_KEY = "발급받은 앱키"\nNHPLUG_APP_SECRET = "발급받은 시크릿"', language="toml")
    st.caption("기존 NH_APP_KEY / NH_APP_SECRET 이름도 지원합니다. 설정을 저장한 뒤 앱을 다시 실행해 주세요.")
    st.stop()

if not st.session_state.get("authenticated"):
    with st.form("login"):
        entered = st.text_input("앱 비밀번호", type="password")
        clicked = st.form_submit_button("로그인", use_container_width=True)
    if clicked:
        blocked_until = st.session_state.get("login_retry_after", 0)
        if time.monotonic() < blocked_until:
            st.error("잠시 후 다시 시도해 주세요.")
        elif hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.session_state.login_retry_after = time.monotonic() + 3
            st.error("비밀번호가 일치하지 않습니다.")
    st.stop()

if not os.getenv("NHPLUG_APP_KEY") or not os.getenv("NHPLUG_APP_SECRET"):
    st.error("나무 앱키 또는 시크릿이 설정되지 않았습니다. Secrets에 NHPLUG_APP_KEY와 NHPLUG_APP_SECRET을 설정하고 앱을 다시 실행해 주세요. 기존 NH_APP_KEY / NH_APP_SECRET 이름도 지원합니다.")
    st.stop()

try:
    engine = get_engine()
except Exception as exc:
    st.error("실행 준비 실패 · " + safe_message(exc))
    if "이미 실행 중" in str(exc):
        st.info("파일 교체 후라면 Manage app → ⋮ → Reboot app으로 앱을 재시작해 주세요. 재시작하면 자동매매는 정지 상태에서 열립니다.")
    st.stop()


def mask(account):
    return "•" * max(0, len(account)-4) + account[-4:]


def action(fn):
    try:
        fn()
        st.rerun()
    except Exception as exc:
        st.error(safe_message(exc))


@st.fragment(run_every=2)
def controls():
    with engine.lock:
        mode, busy = engine.mode, engine.job_busy
        accounts = list(engine.available_accounts)
        checks = dict(engine.connection_steps)
        connection_error = engine.connection_error
        job_name, job_error = engine.job_name, engine.job_error
    label = {"STOPPED": "전체정지", "PAUSE_BUY": "신규매수 정지 · 보유분 관리", "RUNNING": "자동매매 실행"}[mode]
    st.caption("현재 제어 상태: " + label)
    if st.button("■ 전체정지", key="all_stop", use_container_width=True):
        action(lambda: engine.set_mode("STOPPED"))
    st.caption("전체정지는 새 매수·매도 전송을 차단합니다. 이미 전송 중이거나 접수된 주문의 체결 확인은 계속합니다.")

    selected_account = engine.broker.account
    with st.expander("계좌 연결", expanded=not engine.ready):
        if st.button("실전 계좌 불러오기", disabled=busy or mode != "STOPPED", use_container_width=True):
            action(lambda: engine.start_job("계좌 불러오기"))
        if accounts:
            selected = next((i for i,a in enumerate(accounts) if str(a["acct_no"]) == engine.broker.account), 0)
            chosen = st.selectbox("운영할 실전 계좌", range(len(accounts)), index=selected,
                                  format_func=lambda i: f"{i+1}. {mask(str(accounts[i]['acct_no']))} · 실전 {accounts[i]['acct_type']}",
                                  disabled=busy or mode != "STOPPED")
            selected_account = str(accounts[chosen]["acct_no"])
            if st.button("선택 계좌 연결 점검", disabled=busy or mode != "STOPPED", use_container_width=True):
                action(lambda: engine.start_job("연결 점검", selected_account))
        elif engine.broker.account:
            st.write("연결 계좌: " + mask(engine.broker.account))
            if st.button("현재 계좌 연결 다시 점검", disabled=busy or mode != "STOPPED", use_container_width=True):
                action(lambda: engine.start_job("연결 점검", engine.broker.account))
        else:
            st.caption("계좌 불러오기 → 계좌 선택 → 연결 점검 순서로 진행해 주세요.")
        st.caption(" · ".join(f"{name}: {state}" for name, state in checks.items()))
        if engine.ready and selected_account == engine.broker.account:
            st.success("연결 점검 완료 · 자동매매 시작을 누를 수 있습니다.")
            st.caption(getattr(engine.broker, "history_note", "주문내역 확인 완료"))
        elif engine.ready:
            st.info("선택 계좌가 바뀌었습니다. 연결 점검을 실행해 주세요.")
        elif connection_error:
            st.error(connection_error)
        st.caption("주문내역을 구분할 수 있도록 같은 계좌에서 다른 자동매매나 동일한 수동주문을 동시에 실행하지 마세요.")

    start, pause = st.columns(2)
    if start.button("▶ 자동매매 시작", type="primary", disabled=busy or mode == "RUNNING" or not engine.ready or selected_account != engine.broker.account, use_container_width=True):
        action(lambda: engine.start_job("시작 점검"))
    if pause.button("Ⅱ 신규매수 정지", use_container_width=True):
        action(lambda: engine.set_mode("PAUSE_BUY"))
    st.caption("신규매수 정지는 매수만 멈추고 보유분 매도 관리를 계속합니다. 전체정지 상태에서 누르면 정지를 유지합니다.")
    if busy:
        st.info(job_name + " 진행 중 · 결과가 자동으로 갱신됩니다.")
    elif job_error and job_error != connection_error:
        st.error(job_error)
    if not engine.ready:
        st.caption("시작 버튼 대기 이유: 계좌 연결 점검 미완료")


controls()


def seconds_since(stamp, current):
    return max(0, (current-datetime.fromisoformat(stamp)).total_seconds()) if stamp else None


def age_text(stamp, current):
    age = seconds_since(stamp, current)
    if age is None:
        return "아직 확인 안 됨"
    return f"{age:.0f}초 전" if age < 60 else f"{age/60:.0f}분 전"


def won(value, signed=False):
    return "—" if value is None else (f"{value:+,.0f}원" if signed else f"{value:,.0f}원")


def percent(value):
    return "—" if value is None else f"{value:+.2f}%"


def metric_grid(items):
    cards = "".join(f'<div class="nh-card"><div class="nh-label">{escape(str(label))}</div>'
                    f'<div class="nh-value">{escape(str(value))}</div></div>' for label, value in items)
    st.markdown('<div class="nh-grid">' + cards + '</div>', unsafe_allow_html=True)


def operation_status(data, current):
    if not data["connected"]:
        return "info", "계좌 연결 대기", "위의 계좌 연결에서 실전 계좌를 선택해 주세요."
    if not data["connection_ready"]:
        return ("error" if data["connection_error"] else "info", "계좌 연결 점검 미완료",
                data["connection_error"] or "계좌 연결에서 선택 계좌 연결 점검을 눌러 주세요.")
    if (data["account"] or {}).get("unresolved_holdings"):
        return "warning", "보유자산 코드 확인 필요", "거래용 종목코드가 확인되지 않아 신규매수를 보류합니다."
    if data["mode"] == "STOPPED":
        return "info", "자동매매 정지", "새 주문 전송은 정지된 상태입니다. 계좌 현황은 계속 조회합니다."
    if data.get("quality_stop"):
        return "warning", "연속 손실 제한", "당일 신규매수를 멈췄습니다. 보유분은 매도 조건에 따라 관리하며 전략 복기에서 결과를 확인할 수 있습니다."
    if data["error"] or data["balance_error"]:
        return "error", "작동 확인 필요", "계좌·주문 조회에 오류가 있습니다. 아래 오류 내용을 확인해 주세요."
    cycle_age = seconds_since(data["cycle_at"], current)
    if cycle_age is None or cycle_age > 15:
        return "warning", "운영 점검 대기" if cycle_age is None else "운영 점검 지연", "자동매매 제어 작업의 최신 완료를 확인하지 못했습니다."
    if not data["trading_window"]:
        return "info", "운영시간 대기", "자동매매 운영시간 밖입니다. 계좌 조회는 계속합니다."
    if any(o["state"] == "UNKNOWN" for o in data["orders"]):
        return "warning", "주문 결과 확인 중", "확인되지 않은 주문이 있어 신규매수를 보류합니다."
    if any(o["state"] in {"SUBMITTING", "ACCEPTED", "PARTIAL"} for o in data["orders"]):
        return "info", "주문·체결 확인 중", "접수된 주문의 실제 체결수량을 확인하고 있습니다."
    balance_age = seconds_since(data["balance_at"], current)
    if balance_age is None or balance_age > 12:
        return "warning", "잔고 갱신 대기", "최신 주문가능금액을 다시 확인하고 있습니다."
    if data["ws_error"]:
        return "warning", "실시간 수신 확인 필요", "연결 또는 체결 데이터 오류를 확인하고 있습니다."
    if data["mode"] == "PAUSE_BUY" and not data["positions"]:
        return "info", "신규매수 정지", "자동매매가 관리할 보유종목이 없습니다."
    if data["mode"] == "RUNNING" and not data["buying_window"] and not data["positions"]:
        return "info", "신규매수 시간 대기", "신규매수는 평일 09:10~14:50에 판단합니다."
    if not data["watched"]:
        if data["last_scan_error"]:
            return "warning", "종목 검색 확인 필요", "후보 조회 오류로 감시종목을 준비하지 못했습니다."
        return "info", "감시종목 찾는 중", "거래량 100만 주 이상 등 종목 조건을 확인하고 있습니다."
    fresh = [r for r in data["watched"] if r["seconds"] is not None and r["seconds"] <= 3]
    if not fresh:
        return "warning", "실시간 체결 대기", "감시종목의 최근 3초 이내 체결을 기다리고 있습니다."
    if data["mode"] == "PAUSE_BUY" or not data["buying_window"]:
        return "success", "보유종목 매도 관리 중", "신규매수를 멈추고 보유분의 매도 조건을 확인합니다."
    return "success", "실시간 조건 감시 중", "최근 체결을 수신하며 매수·매도 조건을 확인하고 있습니다."


@st.fragment(run_every=3)
def dashboard():
    data = engine.snapshot()
    current = datetime.fromisoformat(data["captured_at"])
    account = data["account"]
    pending = [o for o in data["orders"] if o["state"] in {"SUBMITTING", "ACCEPTED", "PARTIAL", "UNKNOWN"}]
    today_buys = [o for o in data["orders"] if o["side"] == "BUY" and o["filled"] > 0 and o["created"].startswith(current.date().isoformat())]
    fresh = [r for r in data["watched"] if r["seconds"] is not None and r["seconds"] <= 3]

    st.subheader("자동매매 작동 현황")
    st.caption(f"화면 갱신 {current:%H:%M:%S} · 화면 3초 / 잔고 약 5초 간격")
    level, title, detail = operation_status(data, current)
    getattr(st, level)(f"**{title}** · {detail}")
    metric_grid([
        ("현재 감시 종목", f"{len(data['watched'])}종목"),
        ("감시종목 실시간 체결", f"{len(fresh)}종목 수신 중" if fresh else "수신 대기"),
        ("최고 조건 충족률", f"{max((r['score'] for r in data['watched']), default=0)}% / 100%"),
        ("오늘 매수 체결", f"{len(today_buys)}건"),
        ("미체결·확인 중", f"{len(pending)}건"),
        ("자동매매 보유", f"{len(data['positions'])}종목"),
    ])
    st.caption(f"운영 점검 {age_text(data['cycle_at'], current)} · 유효 체결 {data['tick_count']:,}건 · 마지막 수신 {age_text(data['last_tick'], current)}")
    st.caption(f"실시간 연결: {data['ws_state']} · 종목 검색 {data['scan_done']}/{data['scan_total']} · 마지막 검색 {age_text(data['scan_at'], current)}")
    signal = data["last_signal"]
    st.caption("마지막 진입 신호: " + (f"{signal['time'][11:19]} · {signal['code']} · 조건 {signal['score']}%" if signal else "아직 없음"))
    st.caption("상승 5분봉 → 거래량이 줄어든 눌림 → 고점 재돌파. 7개 조건을 모두 확인하며, 충족률은 수익 확률이 아닙니다.")
    if data['watched'] and not any(r['ready_bars'] == 2 for r in data['watched']):
        st.info("완성 5분봉 2개를 준비하고 있습니다. 감시 시작 후 약 10~15분이 필요하며, 수신 누락이 있으면 더 걸릴 수 있습니다. 준비가 끝나도 매수 조건을 충족할 때만 주문합니다.")
    if data["error"]:
        st.error("계좌·주문 확인 오류 · " + data["error"])
    if data["ws_error"]:
        st.warning("실시간 수신 · " + data["ws_error"])

    st.subheader("계좌 실시간 현황")
    st.caption("연결 계좌: " + data["account_label"])
    if st.button("잔고 지금 새로고침", disabled=not data["connected"] or data["job_busy"], use_container_width=True):
        action(lambda: engine.start_job("잔고 갱신"))
    if account is None:
        st.info("잔고 조회 대기 중입니다. 위의 계좌 연결에서 계좌를 선택하고 연결 점검을 눌러 주세요." if not data["connected"] else "계좌가 선택되었습니다. 잔고 조회 완료를 기다리고 있습니다.")
    totals = data["account_totals"]
    metric_grid([
        ("현금 주문가능", won(account["cash"] if account is not None else None)),
        ("예수금", won(account["deposit"] if account is not None else None)),
        ("주식 매입금액", won(totals["cost"])),
        ("주식 평가금액", won(totals["value"])),
        ("주식 평가손익", won(totals["pnl"], True)),
        ("주식 수익률", percent(totals["return_pct"])),
    ])
    if account is not None:
        st.caption(f"총자산 {won(account['asset'])} · 잔고 확인 {data['balance_at'][11:19] if data['balance_at'] else '미확인'} ({age_text(data['balance_at'], current)})")
        if account.get("asset_note"):
            st.caption(account["asset_note"])
        if account["cash"] < data["settings"]["per_order"]:
            st.info(f"현금 주문가능금액은 {won(account['cash'])}입니다. 실제 주문은 이 금액과 종목별 주문가능수량 안에서 계산하며, 예수금 전체를 주문가능금액으로 사용하지 않습니다.")
    if data["balance_error"]:
        st.error("잔고 조회 실패 · " + data["balance_error"])
    balance_age = seconds_since(data["balance_at"], current)
    if account is not None and (balance_age is None or balance_age > 12 or data["balance_error"]):
        st.warning("잔고 갱신이 지연되었습니다. 표시된 수량·현금은 마지막 조회값입니다.")
    st.caption("보유수량·현금·총자산은 잔고 조회 기준입니다. 주식 평가에는 수신 중인 종목의 실시간 체결가를 반영하며, 수수료·세금은 제외합니다.")

    st.markdown("**보유종목별 현황**")
    owned = {p["code"]: p for p in data["positions"]}
    if data["account_holdings"]:
        for h in data["account_holdings"]:
            change = h["pnl"]
            color = "nh-up" if change is not None and change > 0 else "nh-down" if change is not None and change < 0 else ""
            title = escape(f"{h['name'] or h['code']} · {h['code']}")
            note = escape(f"{h['price_source']} {h['price_at'][11:19] if h['price_at'] else '미확인'} · 자동매매 {owned.get(h['code'], {}).get('qty', 0)}주")
            st.markdown(f'<div class="nh-holding"><div class="nh-holding-head"><strong>{title}</strong>'
                        f'<strong class="{color}">{percent(h["return_pct"])}</strong></div>'
                        '<div class="nh-holding-detail">'
                        f'<span>보유 {h["qty"]:g}주</span><span>매입가 {won(h["avg"])}</span>'
                        f'<span>현재가 {won(h["price"])}</span><span>평가손익 {won(h["pnl"], True)}</span>'
                        f'<span>매입금액 {won(h["cost"])}</span><span>평가금액 {won(h["value"])}</span>'
                        f'</div><div class="nh-note">{note}</div></div>', unsafe_allow_html=True)
    else:
        st.caption("조회된 보유종목이 없습니다." if account is not None else "계좌 연결 후 보유수량·매입가·현재가·평가손익이 표시됩니다.")
    if account is not None and account.get("other_assets"):
        st.markdown("**기타 보유자산·종목코드 확인 항목**")
        st.dataframe([dict(종목=a["name"], 응답코드=a["code"], 상품유형=a["kind"], 수량=a["qty"],
                           매입금액=a["cost"], 평가금액=a["value"], 평가손익=a["pnl"], 확인내용=a["note"])
                      for a in account["other_assets"]], hide_index=True, use_container_width=True)
        st.caption("이 표의 자산은 위 주식 평가합계에 포함하지 않습니다. 총자산에는 증권사 잔고 조회값을 표시합니다.")

    tabs = st.tabs(["감시종목·차트", "주문·매매 이유", "전략 복기", "점검 기록"])
    with tabs[0]:
        st.write("매수 대기 종목")
        if data["watched"]:
            display = [dict(종목=r["name"], 코드=r["code"], 현재가=r["price"], 거래량=r["volume"],
                            조건충족률=r["score"], 완성봉=f"{r['ready_bars']}/2", 대기사유=r["reason"], 체결경과초=r["seconds"]) for r in data["watched"]]
            st.dataframe(display, hide_index=True, use_container_width=True)
            code = st.selectbox("관측 5분봉", [r["code"] for r in data["watched"]],
                                format_func=lambda c: next(r["name"] for r in data["watched"] if r["code"] == c))
            selected = next(r for r in data["watched"] if r["code"] == code)
            if selected["checks"]:
                st.write(" · ".join(("✓ " if c["passed"] else "대기: ") + c["label"] for c in selected["checks"]))
                st.caption(f"당일 KRX 거래량 가중평균 {won(selected['vwap'])} · 재돌파 기준 {won(selected['trigger'])} · 눌림 저점 {won(selected['structural_stop'])}")
                if selected["volume_ratio"] is not None:
                    st.caption(f"눌림봉 거래량 / 직전 상승봉 거래량 {selected['volume_ratio']:.2f}배 · 과거 14일 동시간대 상대거래량과는 다른 지표입니다.")
            with engine.lock:
                tape = engine.tapes.get(code)
                bars = list(tape.bars) if tape else []
            if bars:
                import plotly.graph_objects as go
                fig = go.Figure(go.Candlestick(x=[r["time"] for r in bars], open=[r["open"] for r in bars],
                                high=[r["high"] for r in bars], low=[r["low"] for r in bars], close=[r["close"] for r in bars],
                                increasing_line_color="#df5454", decreasing_line_color="#447dcb"))
                fig.update_layout(height=300, margin=dict(l=8,r=8,t=10,b=12), xaxis_rangeslider_visible=False)
                st.plotly_chart(fig, use_container_width=True)
                partial_times = [r['time'][11:16] for r in bars if r['partial']]
                st.caption("수신 체결로 만든 관측봉입니다. 마지막 봉은 진행 중이며 재돌파 확인에만 사용합니다. 시작·수신 누락 봉은 상승봉·눌림봉 판단에서 제외합니다.")
                if partial_times:
                    st.caption("일부 데이터인 봉: " + ", ".join(partial_times))
        else:
            st.info("감시종목을 준비 중이거나 자동매매가 정지된 상태입니다. 위의 작동 현황에서 현재 단계를 확인해 주세요.")
        a, b = st.columns(2)
        for column, code in ((a, "KOSPI"), (b, "KOSDAQ")):
            ref = data["indices"].get(code)
            column.metric(code + " 참고", ref["value"] if ref else "—", ref["change"] if ref else None)
        st.caption("네이버 증권 참고 지수 · 자동주문 판단에 사용하지 않음 · 확인 " + (data["indices_at"][11:19] if data["indices_at"] else "대기"))
        if data["index_error"]:
            st.caption("시장지수 갱신 실패 · 표시값이 이전 값일 수 있습니다.")
    with tabs[1]:
        st.metric("자동매매 당일 추정 실현손익", won(data["stats"].get("realized") if data["connected"] else None, True))
        st.caption(f"체결된 자동매매 수량을 기준으로 비용을 차감한 추정치입니다. 현재 왕복 비용 가정 {data['settings']['cost_allowance_pct']:g}% · 실제 정산은 나무 앱 기준입니다.")
        states = dict(SUBMITTING="전송 준비·확인 중", ACCEPTED="접수 · 체결 확인 중", PARTIAL="부분체결", UNKNOWN="결과 확인 필요", FILLED="전체체결", CANCELLED="잔량 종료", REJECTED="거절·전송 전 중단")
        if data["orders"]:
            output = [dict(시각=o["created"][:19].replace("T"," "), 코드=o["code"], 방향="매수" if o["side"]=="BUY" else "매도",
                           주문수량=o["qty"], 체결수량=o["filled"], 체결평균가=o["avg"], 상태=states[o["state"]],
                           매매이유=o["reason"], 확인내용=o["error"]) for o in reversed(data["orders"][-100:])]
            st.dataframe(output, hide_index=True, use_container_width=True)
        else:
            st.caption("아직 주문이 없습니다. 감시종목의 대기사유에서 진입하지 않은 이유를 확인할 수 있습니다.")
        if any(o["state"] == "UNKNOWN" for o in data["orders"]):
            st.error("결과를 확인하지 못한 주문이 있습니다. 같은 종목 주문과 신규매수를 보류했습니다. 나무 앱에서 해당 시각의 주문내역을 확인해 주세요.")
        st.caption("자동매매가 실제 체결한 수량만 자동 매도 대상으로 관리합니다.")
    with tabs[2]:
        review = data["review"]
        st.markdown("**이번 버전의 실제 체결 복기**")
        st.caption("일부 수량만 매도한 거래는 종료 건수에 포함하지 않습니다. 현재 버전에서 진입하고 전량 매도가 확인된 거래를 집계합니다.")
        if review and review["count"]:
            metric_grid([
                ("종료 거래", f"{review['count']}건"),
                ("비용 반영 승률", percent(review["win_rate"])),
                ("거래당 평균 순손익", won(review["expectancy"], True)),
                ("누적 추정 순손익", won(review["net"], True)),
                ("총이익 / 총손실", f"{review['profit_factor']:.2f}" if review["profit_factor"] is not None else "손실 거래 없음"),
                ("청산손익 기준 최대 낙폭", won(review["max_drawdown"])),
            ])
            if review["count"] > 1:
                st.line_chart({"종료 거래별 누적 추정 순손익": [r["누적추정순손익"] for r in review["curve"]]}, height=200)
            st.dataframe([dict(진입=t["opened"][:19], 청산=t["closed"][:19], 코드=t["code"],
                               추정순손익=round(t["net"]), 순수익률=round(t["return_pct"], 2),
                               조건충족률=t["signal"].get("score"), 평균가격=t["signal"].get("vwap"), 손절=t["settings"].get("stop_loss"),
                               익절=t["settings"].get("take_profit"), 이유=t["reason"])
                          for t in reversed(review["trades"][-100:])], hide_index=True, use_container_width=True)
        else:
            st.info("이 버전에서 종료된 실거래 기록이 아직 없습니다. 수익성과 승률은 미검증 상태입니다.")
        st.caption("승률·순손익은 관측 기록이며 미래 수익을 예측하지 않습니다. 최대 낙폭에는 아직 보유 중인 종목의 평가손익이 포함되지 않습니다.")
        with st.expander("이번에 반영한 매매·검증 원칙"):
            settings = engine.settings
            st.write("완성된 상승봉과 다음 눌림봉을 확인하고, 그 다음 5분 안에 눌림 고점을 재돌파할 때만 진입합니다. 두 완성봉은 중단 없이 연속 관측한 봉이어야 합니다.")
            st.write(f"상승봉: 몸통 +{settings.impulse_min_pct:g}% 이상·전체 폭의 절반 이상·고가권 마감. 눌림봉: 상승 몸통의 절반 이상 지지·거래량은 상승봉의 {settings.pullback_volume_ratio:g}배 이하.")
            st.write(f"현재 매도호가는 당일 KRX 거래량 가중평균 위, 매수호가의 평균가격 이격은 {settings.max_vwap_distance_pct:g}% 이하. 돌파 기준에서 {settings.max_chase_pct:g}% 넘게 오른 봉은 추격하지 않습니다.")
            st.write(f"1. 비용 {settings.cost_allowance_pct:g}%를 뺀 목표수익과 손절 위험의 비율이 {settings.min_net_reward_risk:g} 이상일 때 진입합니다.")
            st.write(f"2. 1회 예상 손실예산은 투자한도의 {settings.risk_per_trade_pct:g}% 이내로 계산합니다. 손절 폭이 커지면 수량을 줄입니다.")
            st.write("3. 매도 가능한 현재 호가를 기준으로 판단합니다. 눌림 저점 또는 설정 손절 중 먼저 도달한 조건으로 매도를 시도합니다. 진입 당시 손절·익절·비용 설정을 보유 중 유지합니다.")
            st.write(f"4. 당일 청산 거래가 {settings.consecutive_loss_limit}회 연속 손실이면 신규매수를 정지합니다.")
            st.write("5. 진입 조건·평균가격·거래량 비율·설정·호가와 실제 체결 결과를 함께 기록합니다. 조건을 바꾸면 이전 기록과 이후 결과를 분리해 비교해야 합니다.")
            st.caption("이 수치는 운영을 위한 초기 기준이며, 과거 수익으로 최적화한 값이 아닙니다. 급변·거래정지·통신 중단 시 실제 손실은 예상 손실예산을 초과할 수 있습니다.")
            st.markdown("참고 연구: [진입 타이밍과 거래비용](https://concretumgroup.com/wp-content/uploads/2026/02/Improving-Performance-with-Fast-Alphas-A-Tactical-Overlay-for-Intraday-Trend-Trading.pdf), [VWAP](https://concretumgroup.com/wp-content/uploads/2026/02/Volume-Weighted-Average-Price.pdf), [과적합](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). 미국 시장 연구의 일부 개념을 조합한 실험 규칙입니다. 단테 기법의 검증된 재현이나 국내 수익성 검증을 뜻하지 않습니다.")
        if data["connected"]:
            st.download_button("매매 복기 기록 내려받기", json.dumps(engine.store.review_export(engine.account), ensure_ascii=False, indent=2),
                               "trade_review.json", "application/json")
            st.caption("기록은 현재 앱 서버에 보관합니다. 서버 초기화·교체에 대비해 복기 기록을 내려받아 보관해 주세요.")
    with tabs[3]:
        st.write(data["scan_source"])
        st.write(f"현재 후보 {data['candidates']}개 · 후보 조회 오류 {data['scan_errors']}건 · 해석 불가 체결 {data['invalid_ticks']}건")
        if data["last_scan_error"]:
            st.warning("마지막 후보 조회 오류 · " + data["last_scan_error"])
        if data["logs"]:
            st.dataframe(data["logs"], hide_index=True, use_container_width=True)
        st.caption(data["history_note"])
        with st.expander("증권사 응답 진단"):
            for path, detail in data["api_diagnostics"].items():
                st.write(path.rsplit("/", 1)[-1] + " · " + detail)
        report = {k:data[k] for k in ("mode","status","error","connection_ready","connection_error","connection_steps","history_note","api_diagnostics","job_busy","job_name","job_error","balance_error","balance_at","cycle_at","scan_at","ws_state","ws_error","last_tick","tick_count","invalid_ticks","scan_source","scan_done","scan_total","last_scan_error")}
        st.download_button("연결 점검 결과 내려받기", json.dumps(report, ensure_ascii=False, indent=2), "connection_check.json", "application/json")


dashboard()


with st.expander("간단 설정 · 4개 값"):
    settings = engine.settings
    if engine.settings_warning:
        st.warning(engine.settings_warning)
    with st.form("settings"):
        disabled = engine.mode != "STOPPED" or engine.job_busy
        a, b = st.columns(2)
        capital = a.number_input("총 투자한도(원)", min_value=10_000, value=settings.capital, step=50_000, disabled=disabled)
        per_order = b.number_input("1회 주문금액(원)", min_value=10_000, value=settings.per_order, step=10_000, disabled=disabled)
        loss = a.number_input("손절(%)", min_value=.1, max_value=20.0, value=float(settings.stop_loss), step=.1, disabled=disabled)
        profit = b.number_input("익절(%)", min_value=.1, max_value=30.0, value=float(settings.take_profit), step=.1, disabled=disabled)
        with st.expander("위험·비용 설정"):
            cost = st.number_input("왕복 비용 추정(%)", min_value=.01, max_value=3.0, value=float(settings.cost_allowance_pct), step=.01, disabled=disabled)
            risk = st.number_input("1회 손실예산 · 투자한도 대비(%)", min_value=.05, max_value=1.0, value=float(settings.risk_per_trade_pct), step=.05, disabled=disabled)
            daily_loss = st.number_input("당일 추정 손실 제한(원)", min_value=1_000, value=settings.daily_loss, step=1_000, disabled=disabled)
            streak = st.number_input("당일 연속 손실 제한(회)", min_value=2, max_value=5, value=settings.consecutive_loss_limit, disabled=disabled)
            st.caption("왕복 비용은 수수료·세금 등을 위한 추정치입니다. 계좌의 실제 적용 비용에 맞춰 설정해 주세요.")
        st.caption("기본값은 이전 요청을 이어받은 설정입니다. 수익성이 검증된 최적값을 뜻하지 않습니다.")
        save = st.form_submit_button("설정 저장", disabled=disabled, use_container_width=True)
    if save:
        action(lambda: engine.update_settings(replace(settings, capital=int(capital), per_order=int(per_order), stop_loss=float(loss), take_profit=float(profit),
                                                      cost_allowance_pct=float(cost), risk_per_trade_pct=float(risk), daily_loss=int(daily_loss), consecutive_loss_limit=int(streak))))
    st.write(f"조건: 100만 주 · 진입 7개 조건 모두 충족 · 최대 {settings.max_positions}종목 · 하루 누적 매수액 {settings.capital*.75:,.0f}원 · 최대 {settings.max_buys}회")
    st.caption("당일 누적 매수액 한도는 매도 후에도 복구되지 않습니다. 완성 5분봉 2개가 없으면 관측을 계속하며 신규매수를 기다립니다.")
    st.caption(f"당일 추정 손실 제한 {settings.daily_loss:,}원 · 연속 손실 {settings.consecutive_loss_limit}회 제한 · 재진입 대기 20분")
    st.caption("한국시간 평일 매수 09:10~14:50 · 15:15부터 보유분 정리 시도 · 재시작 후 전체정지 상태에서 계좌 연결을 다시 확인합니다.")

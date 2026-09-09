# 나무 자동매매 · 휴대폰 업로드용
# app.py와 requirements.txt만 같은 위치에 업로드하세요.
# API 연결값과 앱 비밀번호는 코드 대신 Secrets/환경변수에 설정합니다.
from __future__ import annotations

import sys
from types import ModuleType
import streamlit as st

# The embedded source is fixed application code, never user-provided input.
# A cached module keeps its engine and worker threads alive across UI reruns.
_RUNTIME_VERSION = "2259e77edfa23eb3"
_RUNTIME_SOURCE = r'''
from __future__ import annotations


# ===== core =====

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from math import isfinite
from zoneinfo import ZoneInfo
import re

KST = ZoneInfo("Asia/Seoul")


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
        return self


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
    """Exchange-time 5-minute observed candles; rolling one-second snapshots."""
    def __init__(self):
        self.bars = deque(maxlen=48)
        self.samples = deque(maxlen=122)
        self.latest = None
        self.previous_price = None
        self.started = None

    def add(self, tick):
        old = self.latest
        if old and tick.event.date() != old.event.date():
            self.bars.clear()
            self.samples.clear()
            self.latest = self.started = None
            old = None
        if old and (tick.event < old.event or tick.volume < old.volume):
            return False
        # A gap is an incomplete observation period. Do not bridge it for momentum.
        if old and (tick.event - old.event).total_seconds() > 5:
            self.samples.clear()
            self.started = tick.event
            if self.bars:
                self.bars[-1]["partial"] = True
        if self.started is None:
            self.started = tick.event
        bucket = tick.event.replace(minute=tick.event.minute // 5 * 5, second=0, microsecond=0)
        delta = tick.volume - old.volume if old else 0
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
        if not t or not self.bars:
            return dict(score=0, reason="실시간 체결 대기", eligible=False)
        age = max((current - t.received).total_seconds(), (current - t.event).total_seconds())
        if age > settings.freshness_seconds:
            return dict(score=0, reason=f"체결 지연 {age:.1f}초", eligible=False)
        if t.volume < settings.min_volume or t.turnover < settings.min_turnover:
            return dict(score=0, reason="거래량·거래대금 조건 미달", eligible=False)
        if t.ask <= 0 or t.bid <= 0 or t.ask < t.bid:
            return dict(score=0, reason="유효한 매수·매도 호가 대기", eligible=False)
        if (t.ask / t.bid - 1) * 100 > settings.max_spread_pct:
            return dict(score=0, reason="호가 간격이 큼", eligible=False)
        previous = self.at_or_before(t.event.timestamp() - 20)
        middle = self.at_or_before(t.event.timestamp() - 10)
        if not previous or not middle:
            elapsed = (t.event - self.started).total_seconds()
            return dict(score=0, reason=f"현재봉 관측 중 · {max(0, 20-int(elapsed))}초 더 필요", eligible=False)
        bar = self.bars[-1]
        rise = (t.price / bar["open"] - 1) * 100
        if rise >= 2.5:
            return dict(score=0, reason="현재 관측봉 과열 · 진입 대기", eligible=False)
        score, why = 0, []
        checks = [(t.price > bar["open"], 15, "관측 5분봉 상승"),
                  (rise >= 0.20, 15, "관측봉 +0.2%"),
                  (t.price >= bar["high"] * .9985, 15, "봉 고가권"),
                  (self.previous_price is not None and t.price > self.previous_price, 15, "직전 체결 상승"),
                  ((t.price / previous[1] - 1) * 100 >= .15, 20, "20초 상승 +0.15%"),
                  (t.volume - middle[2] > 0 and middle[2] - previous[2] > 0 and
                   t.volume - middle[2] >= (middle[2] - previous[2]) * 1.5, 20, "10초 거래량 가속")]
        for passed, points, label in checks:
            if passed:
                score += points
                why.append(label)
        return dict(score=score, reason=" · ".join(why) or "진입 조건 대기",
                    eligible=score >= settings.buy_score, partial=bar["partial"])


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

    def prepare(self, account, code, side, qty, price, created, baseline, reason):
        oid = uuid.uuid4().hex
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM orders WHERE account=? AND code=? AND state IN ('SUBMITTING','ACCEPTED','PARTIAL','UNKNOWN')", (account, code)).fetchone():
                raise ValueError("앞선 주문의 체결 확인 중")
            c.execute("INSERT INTO orders(id,account,code,side,qty,price,created,state,baseline,reason) VALUES(?,?,?,?,?,?,?,'SUBMITTING',?,?)",
                      (oid, account, code, side, qty, price, created, json.dumps(baseline), reason))
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


def safe_message(error):
    message = str(error)
    for key in ("NHPLUG_APP_KEY", "NHPLUG_APP_SECRET", "NH_APP_KEY", "NH_APP_SECRET", "APP_PASSWORD", "NHPLUG_DEFAULT_ACCOUNT"):
        secret = os.environ.get(key)
        if secret:
            message = message.replace(secret, "[숨김]")
    message = re.sub(r"(?i)(appkey|appsecretkey|access_token|token|authorization)[=:][^\s&]+", r"\1=[숨김]", message)
    message = re.sub(r"\b\d{11,}\b", "[숨김]", message)
    return message[:500]


def check_response(data, meta):
    if not isinstance(data, dict):
        raise BrokerError("JSON 객체 응답이 아닙니다.")
    envelope = data.get("message")
    msg = str(data.get("rsp_msg") or (envelope.get("usr_msg") if isinstance(envelope, dict) else "") or "")
    code = str(data.get("rsp_cd") or (envelope.get("msg_code") if isinstance(envelope, dict) else "") or "")
    if meta.status is not None and not 200 <= meta.status < 300:
        raise BrokerError(f"HTTP {meta.status} · {code} · {msg}")
    if re.search(r"오류|실패|불가|거부|부족|초과|입력하|유효하지|잘못|권한이 없|서비스 해지", msg):
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

    def query(self, path, payload=None, paginated=False):
        cts, flag, seen, pages = None, None, set(), []
        with self.rest_lock:
            for _ in range(100):
                data, meta = self.call(path, payload or {}, cts=cts, cts_flag=flag,
                                       timeout=7, raise_on_error=False, want_meta=True)
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
            if block is None:
                # Empty history is allowed only when the server explicitly says so.
                msg = str(page.get("rsp_msg", "")) + str(page.get("message", ""))
                if not re.search(r"없|0건|완료|정상", msg):
                    raise BrokerError("주문내역 블록이 없고 조회 성공을 확인하지 못했습니다.")
            for row in rows(block):
                try:
                    code = response_code(row.get("iem_cd"), "주문 조회 Output_1.iem_cd")
                except ValueError:
                    code = None
                result.append(dict(row, iem_cd=code))
        ids = [str(integer(r["itg_orr_no"])) for r in result]
        if len(set(ids)) != len(ids):
            raise BrokerError("주문내역 중복 · 전체 조회 상태 확인 필요")
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

    def order(self, code, side, qty, price):
        if side not in {"BUY", "SELL"}:
            raise ValueError("매매 방향 오류")
        suffix = "cashBuy" if side == "BUY" else "cashSell"
        data = self.query("/krstock/order/v1/" + suffix, self.order_payload(self.account, code, qty, price))
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
        self.settings = Settings(**self.store.get("settings", {})).validate()
        self.mode = "STOPPED"
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

    def set_mode(self, mode):
        if mode not in {"RUNNING", "PAUSE_BUY", "STOPPED"}:
            raise ValueError("운영 상태 오류")
        with self.actions:
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
            with self.lock:
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

    def stream_status(self, state, error):
        with self.lock:
            self.ws_state = state
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
            candidates = sorted(self.candidates, key=lambda c: self.candidates[c]["turnover"], reverse=True)
            selected = list(dict.fromkeys(positions + pending + candidates))[:10]
            if set(selected) != set(self.watch):
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
                unrealized += p["qty"] * (tape.latest.price-p["avg"]-p["avg"]*self.settings.cost_allowance_pct/100)
        return realized + unrealized, True

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
                qty = min(cash_qty, int(max(0, budget) // (price*1.005)))
            else:
                bot_qty = next((p["qty"] for p in owned if p["code"] == code), 0)
                account_qty = sum(h["qty"] for h in balance["holdings"] if h["code"] == code)
                if bot_qty > account_qty:
                    self.blockers[code] = "보유수량 불일치 · 나무 앱 확인 필요"
                    return
                qty = min(bot_qty, self.broker.sellable(code))
            if qty <= 0:
                self.blockers[code] = "현금 주문가능수량 부족" if side == "BUY" else "매도가능수량 부족"
                return
            baseline = [str(integer(r["itg_orr_no"])) for r in self.broker.history(current.date().isoformat())]
            # Validate again after slow REST calls, immediately before the write.
            with self.lock:
                latest = self.tapes[code].latest
                actual_now = self.clock()
                if not market_time(actual_now, side == "BUY"):
                    return
                if max((actual_now-latest.event).total_seconds(), (actual_now-latest.received).total_seconds()) > self.settings.freshness_seconds:
                    self.blockers[code] = "주문 준비 중 체결 지연"
                    return
                if side == "BUY" and (self.mode != "RUNNING" or not self.tapes[code].signal(self.settings, actual_now)["eligible"]):
                    return
            oid = self.store.prepare(self.account, code, side, qty, price,
                                     actual_now.isoformat(), baseline, reason)
            self.log("주문 전송", f"{code} {side} {qty}주 · 지정가 IOC {price:,}원 · {reason}")
            try:
                broker_no = self.broker.order(code, side, qty, price)
                self.store.mark(oid, "ACCEPTED", broker_no=broker_no, accepted=self.clock().isoformat())
                self.blockers[code] = "주문 접수 · 체결내역 확인 중"
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
        pnl, complete = self.estimated_pnl(positions, current)
        if complete and pnl <= -self.settings.daily_loss:
            self.store.set("loss_stop_"+self.account, current.date().isoformat())
            with self.lock:
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
            self.store.peak(self.account, code, tick.price)
            peak = max(position["peak"], tick.price)
            gain = (tick.price/avg-1)*100
            reason = ""
            if self.store.get("loss_stop_"+self.account) == current.date().isoformat():
                reason = "당일 손실 한도 정리"
            elif current.hour == 15 and current.minute >= 15:
                reason = "장 종료 전 정리"
            elif gain <= -self.settings.stop_loss:
                reason = f"손절 {gain:.2f}%"
            elif gain >= self.settings.take_profit:
                reason = f"익절 {gain:.2f}%"
            elif peak >= avg*(1+self.settings.trailing/100) and (tick.price/peak-1)*100 <= -self.settings.trailing:
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
            self.status = "조건 감시 중" if self.mode == "RUNNING" else "신규매수 정지 · 보유분 매도 관리"

    def control_loop(self):
        last_error = ""
        while not self.stop_event.is_set():
            try:
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
                        account_label=("•" * max(0, len(str(self.broker.account))-4) + str(self.broker.account)[-4:]) if self.account else "미연결",
                        captured_at=current.isoformat(), trading_window=market_time(current),
                        buying_window=market_time(current, True),
                        last_signal=copy.deepcopy(self.last_signal), scan_done=self.scan_done, scan_total=self.scan_total,
                        scan_source=self.scan_source, scan_errors=self.scan_errors, last_scan_error=self.last_scan_error,
                        candidates=len(self.candidates), watched=watched, account=copy.deepcopy(self.account_data),
                        indices=copy.deepcopy(self.indices), indices_at=self.indices_at.isoformat() if self.indices_at else None,
                        index_error=self.index_error, settings=asdict(self.settings))
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
        data["orders"] = self.store.orders(self.account) if self.account else []
        data["positions"] = self.store.positions(self.account) if self.account else []
        data["stats"] = self.store.stats(self.account, current.date().isoformat()) if self.account else {}
        data["logs"] = self.store.logs()
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
st.caption("종목코드·연결점검 수정본 · 2026.09.09 · 실전 계좌")

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


selected_account = engine.broker.account
with st.expander("계좌 연결", expanded=not engine.ready):
    if st.button("실전 계좌 불러오기", use_container_width=True):
        try:
            st.session_state.accounts = engine.broker.accounts()
        except Exception as exc:
            st.error(safe_message(exc))
    accounts = st.session_state.get("accounts", [])
    if accounts:
        # Index values remain unique even if two masked account numbers collide.
        chosen = st.selectbox("운영할 실전 계좌", range(len(accounts)),
                              format_func=lambda i: f"{i+1}. {mask(str(accounts[i]['acct_no']))} · 실전 {accounts[i]['acct_type']}",
                              disabled=engine.mode != "STOPPED")
        selected_account = str(accounts[chosen]["acct_no"])
        if st.button("선택 계좌 연결 점검", disabled=engine.mode != "STOPPED", use_container_width=True):
            with st.spinner("계좌·잔고·현재가·주문내역을 확인하고 있습니다…"):
                action(lambda: engine.connect(str(accounts[chosen]["acct_no"])))
    elif engine.broker.account:
        st.write("연결 계좌: " + mask(engine.broker.account))
    else:
        st.caption("계좌를 불러온 뒤 운영할 계좌를 선택해 주세요. 연결 점검은 조회만 수행합니다.")
    with engine.lock:
        checks = dict(engine.connection_steps)
        connection_error = engine.connection_error
    st.caption(" · ".join(f"{name}: {state}" for name, state in checks.items()))
    if engine.ready and selected_account == engine.broker.account:
        st.success("연결 점검 완료 · 계좌 현황을 확인할 수 있습니다.")
    elif engine.ready:
        st.info("선택 계좌가 바뀌었습니다. 선택 계좌 연결 점검을 눌러 주세요.")
    elif connection_error:
        st.error(connection_error)
    st.caption("주문내역을 정확히 구분하도록 이 계좌에서는 같은 시간에 다른 자동매매나 동일한 수동주문을 실행하지 마세요.")

buttons = st.columns(3)
with buttons[0]:
    if st.button("▶ 자동매매 시작", type="primary", disabled=not engine.ready or selected_account != engine.broker.account, use_container_width=True):
        action(lambda: engine.set_mode("RUNNING"))
with buttons[1]:
    if st.button("Ⅱ 신규매수 정지", disabled=not engine.ready, use_container_width=True):
        action(lambda: engine.set_mode("PAUSE_BUY"))
with buttons[2]:
    if st.button("■ 전체정지", use_container_width=True):
        action(lambda: engine.set_mode("STOPPED"))
st.caption("신규매수 정지: 보유분 매도 관리는 계속합니다. 전체정지: 새 주문 전송을 멈춥니다. 이미 접수된 주문은 별도이며 체결조회는 계속합니다.")




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
        ("최고 진입점수", f"{max((r['score'] for r in data['watched']), default=0)}점 / 70점"),
        ("오늘 매수 체결", f"{len(today_buys)}건"),
        ("미체결·확인 중", f"{len(pending)}건"),
        ("자동매매 보유", f"{len(data['positions'])}종목"),
    ])
    st.caption(f"운영 점검 {age_text(data['cycle_at'], current)} · 유효 체결 {data['tick_count']:,}건 · 마지막 수신 {age_text(data['last_tick'], current)}")
    st.caption(f"실시간 연결: {data['ws_state']} · 종목 검색 {data['scan_done']}/{data['scan_total']} · 마지막 검색 {age_text(data['scan_at'], current)}")
    signal = data["last_signal"]
    st.caption("마지막 진입 신호: " + (f"{signal['time'][11:19]} · {signal['code']} · {signal['score']}점" if signal else "아직 없음"))
    if data["error"]:
        st.error("계좌·주문 확인 오류 · " + data["error"])
    if data["ws_error"]:
        st.warning("실시간 수신 · " + data["ws_error"])

    st.subheader("계좌 실시간 현황")
    st.caption("연결 계좌: " + data["account_label"])
    if st.button("잔고 지금 새로고침", disabled=not data["connected"], use_container_width=True):
        try:
            with engine.actions:
                engine.refresh_balance()
            st.rerun()
        except Exception as exc:
            st.error("잔고 갱신 실패 · " + safe_message(exc))
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

    tabs = st.tabs(["감시종목·차트", "주문·매매 이유", "점검 기록"])
    with tabs[0]:
        st.write("매수 대기 종목")
        if data["watched"]:
            display = [dict(종목=r["name"], 코드=r["code"], 현재가=r["price"], 거래량=r["volume"],
                            진입점수=r["score"], 대기사유=r["reason"], 체결경과초=r["seconds"]) for r in data["watched"]]
            st.dataframe(display, hide_index=True, use_container_width=True)
            code = st.selectbox("관측 5분봉", [r["code"] for r in data["watched"]],
                                format_func=lambda c: next(r["name"] for r in data["watched"] if r["code"] == c))
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
                st.caption("감시 시작 이후 체결로 만든 관측봉입니다. 시작·연결 누락 구간의 봉은 일부 데이터이며, 증권사 과거 분봉과 다를 수 있습니다.")
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
        st.caption("체결된 자동매매 수량을 기준으로 매매비용 0.3% 여유분을 차감한 추정치입니다. 실제 정산은 나무 앱 기준입니다.")
        states = dict(SUBMITTING="전송 중", ACCEPTED="접수 · 체결 확인 중", PARTIAL="부분체결", UNKNOWN="결과 확인 필요", FILLED="전체체결", CANCELLED="잔량 종료", REJECTED="거절")
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
        st.write(data["scan_source"])
        st.write(f"현재 후보 {data['candidates']}개 · 후보 조회 오류 {data['scan_errors']}건 · 해석 불가 체결 {data['invalid_ticks']}건")
        if data["last_scan_error"]:
            st.warning("마지막 후보 조회 오류 · " + data["last_scan_error"])
        if data["logs"]:
            st.dataframe(data["logs"], hide_index=True, use_container_width=True)
        import json
        report = {k:data[k] for k in ("mode","status","error","connection_ready","connection_error","connection_steps","balance_error","balance_at","cycle_at","scan_at","ws_state","ws_error","last_tick","tick_count","invalid_ticks","scan_source","scan_done","scan_total","last_scan_error")}
        st.download_button("연결 점검 결과 내려받기", json.dumps(report, ensure_ascii=False, indent=2), "connection_check.json", "application/json")


dashboard()


with st.expander("간단 설정 · 4개 값"):
    settings = engine.settings
    with st.form("settings"):
        disabled = engine.mode != "STOPPED"
        a, b = st.columns(2)
        capital = a.number_input("총 투자한도(원)", min_value=10_000, value=settings.capital, step=50_000, disabled=disabled)
        per_order = b.number_input("1회 주문금액(원)", min_value=10_000, value=settings.per_order, step=10_000, disabled=disabled)
        loss = a.number_input("손절(%)", min_value=.1, max_value=20.0, value=float(settings.stop_loss), step=.1, disabled=disabled)
        profit = b.number_input("익절(%)", min_value=.1, max_value=30.0, value=float(settings.take_profit), step=.1, disabled=disabled)
        st.caption("기본값은 이전 요청을 이어받은 설정입니다. 수익성이 검증된 최적값을 뜻하지 않습니다.")
        save = st.form_submit_button("설정 저장", disabled=disabled, use_container_width=True)
    if save:
        action(lambda: engine.update_settings(replace(settings, capital=int(capital), per_order=int(per_order), stop_loss=float(loss), take_profit=float(profit))))
    st.write(f"고정 조건: 100만 주 · 70점 이상 · 최대 3종목 · 하루 매수액 {capital*.75:,.0f}원 · 최대 6회")
    st.caption("당일 추정 손실 한도 50,000원 · 재진입 대기 20분 · 매수 09:10~14:50 · 15:15부터 보유분 정리 시도")

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit_autorefresh import st_autorefresh

from nhplug.realtime import subscribe

# =========================================================
# NH AUTO TRADER - LIVE / ONE APP + BACKGROUND WORKER
# 휴대폰 화면을 닫아도 "서버 프로세스가 살아 있는 동안" Worker는 계속 실행됩니다.
# Streamlit Community Cloud의 sleep/restart는 피할 수 없으므로,
# Dockerfile로 항상 켜져 있는 서버(VPS/Render/Railway 등)에 1개 인스턴스로 배포하세요.
# =========================================================

KST = ZoneInfo("Asia/Seoul")
DB_PATH = os.getenv("NH_TRADER_DB", "/tmp/nh_trader_live.db")

DEFAULT = {
    "capital": 1_000_000,
    "per_stock": 250_000,
    "max_positions": 3,
    "daily_buy_limit": 750_000,
    "daily_loss_limit": 50_000,
    "max_trades": 6,
    "buy_score": 70,
    "stop_loss": 0.8,
    "take_profit": 1.5,
    "trailing": 0.6,
    "cooldown_min": 20,
    "min_turnover": 500_000_000,
    "scan_batch": 80,
    "candidate_count": 15,
    "ui_refresh_sec": 30,
}

def now_kst():
    return datetime.now(KST)

def to_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default

def to_int(v, default=0):
    return int(to_float(v, default))

def first_of(d: dict, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return default

def listify(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return [x]
    return []

def market_buy_time(dt=None):
    dt = dt or now_kst()
    if dt.weekday() >= 5:
        return False
    # 신규매수는 장 시작 직후/마감 직전 회피
    return dtime(9, 10) <= dt.time() <= dtime(14, 50)

def market_manage_time(dt=None):
    dt = dt or now_kst()
    if dt.weekday() >= 5:
        return False
    return dtime(9, 0) <= dt.time() <= dtime(15, 20)

# ---------- SQLite ----------
def db():
    c = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS journal(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        side TEXT NOT NULL,
        code TEXT NOT NULL,
        qty INTEGER NOT NULL,
        price REAL NOT NULL,
        score INTEGER,
        reason TEXT,
        status TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        msg TEXT NOT NULL
    )""")
    return c

def kv_get(k, default):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except Exception:
        return default

def kv_set(k, value):
    with db() as c:
        c.execute("""INSERT INTO kv(k,v) VALUES(?,?)
                     ON CONFLICT(k) DO UPDATE SET v=excluded.v""",
                  (k, json.dumps(value, ensure_ascii=False)))

def get_state():
    d = {"mode":"STOPPED", "started_at":"", "heartbeat":"", "last_action":"대기",
         "scan_cursor":0, "scan_total":0, "scan_cycle":0}
    d.update(kv_get("state", {}))
    return d

def set_state(**kwargs):
    s = get_state()
    s.update(kwargs)
    kv_set("state", s)
    return s

def get_settings():
    x = dict(DEFAULT)
    x.update(kv_get("settings", {}))
    return x

def set_settings(x):
    kv_set("settings", x)

def log(kind, msg):
    with db() as c:
        c.execute("INSERT INTO logs(ts,kind,msg) VALUES(?,?,?)",
                  (now_kst().strftime("%Y-%m-%d %H:%M:%S"), kind, str(msg)[:1000]))

def journal(side, code, qty, price, score, reason, status):
    with db() as c:
        c.execute("""INSERT INTO journal(ts,side,code,qty,price,score,reason,status)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                   side, code, int(qty), float(price or 0), score, reason, status))

def today_stats():
    today = now_kst().strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute("""SELECT side,qty,price,status FROM journal
                            WHERE ts LIKE ? ORDER BY id""", (today+"%",)).fetchall()
    buy_amount = 0
    trades = 0
    local_realized = 0
    # 실현손익은 자동매매 내부 추정치가 아니라 주문 기록 중심으로만 관리.
    # 실제 계좌 실현손익은 NH 앱/계좌가 최종 기준입니다.
    for side, qty, price, status in rows:
        if "요청" in status or "체결" in status:
            trades += 1
            if side == "매수":
                buy_amount += int(qty * price)
    return buy_amount, trades, local_realized

def recent_journal(limit=300):
    with db() as c:
        return c.execute("""SELECT ts,side,code,qty,price,score,reason,status
                            FROM journal ORDER BY id DESC LIMIT ?""",(limit,)).fetchall()

def recent_logs(limit=200):
    with db() as c:
        return c.execute("""SELECT ts,kind,msg FROM logs
                            ORDER BY id DESC LIMIT ?""",(limit,)).fetchall()

# ---------- Secrets / NH SDK ----------
def secret(name, default=""):
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return os.getenv(name, default)

NH_KEY = secret("NH_APP_KEY") or secret("NHPLUG_APP_KEY")
NH_SECRET = secret("NH_APP_SECRET") or secret("NHPLUG_APP_SECRET")
APP_PASSWORD = secret("APP_PASSWORD")
NH_ACCOUNT = secret("NHPLUG_DEFAULT_ACCOUNT")
BASE_URL = "https://api.nhplug.com:8443"
AUTH_URL = "https://api.nhplug.com:8443"

if NH_KEY:
    os.environ["NHPLUG_APP_KEY"] = NH_KEY
if NH_SECRET:
    os.environ["NHPLUG_APP_SECRET"] = NH_SECRET
os.environ["NHPLUG_BASE_URL"] = BASE_URL
os.environ["NHPLUG_AUTH_URL"] = AUTH_URL
os.environ.setdefault("NHPLUG_RATE_LIMIT", "4")

from nhplug import call, NhplugError
from nhplug.instruments import load_master

def nh_error(e):
    return f"{getattr(e,'category','error')} / {getattr(e,'code','')} / {getattr(e,'message',str(e))}"

def live_accounts():
    r = call("/n2/acctinfo", {})
    return [x for x in listify(r.get("Output_0"))
            if str(x.get("acct_type","")) in {"01","02"}]

def resolve_account():
    global NH_ACCOUNT
    selected = kv_get("selected_account", "")
    if selected:
        return str(selected)

    if NH_ACCOUNT:
        return NH_ACCOUNT

    accs = live_accounts()
    if not accs:
        raise RuntimeError("실전 계좌(01/02)를 찾지 못했습니다.")

    NH_ACCOUNT = str(accs[0].get("acct_no",""))
    if not NH_ACCOUNT:
        raise RuntimeError("계좌번호 응답이 비어 있습니다.")
    return NH_ACCOUNT

def balance():
    return call("/krstock/inquiry/v1/balance", {
        "act_no": resolve_account(),
        "bnc_bse_cd": "5",
        "ltg_aot_dit_cd": "9",
        "aet_bse": "2",
        "qut_dit_cd": "UNT",
    })

def current_price(code):
    return call("/krstock/quote/v1/currentPrice",
                {"iem_cd": code, "market_cd": "UNT"})

def current_daily(code, n=30):
    return call("/krstock/quote/v1/currentDaily",
                {"market_cd":"UNT","iem_cd":code,"array_cnt":str(n)})

def build_order(code, qty, side):
    p = {
        "act_no": resolve_account(),
        "iem_cd": code,
        "orr_qty": int(qty),
        "nmn_pr_tp_cd": "05",
        "orr_cnd_dit_cd": "00",
        "ssl_nmn_pr_dit_cd": "00",
        "rmt_mkt_cd": "KRX",
        "sor_mkt_sli_yn": "N",
    }
    path = "/krstock/order/v1/cashBuy" if side == "BUY" else "/krstock/order/v1/cashSell"
    return path, p

def send_market_order(code, qty, side, reason, score=None, ref_price=0):
    path, payload = build_order(code, qty, side)
    log("ORDER_PRE", f"{side} {code} {qty}주 · {reason}")
    r = call(path, payload)
    journal("매수" if side=="BUY" else "매도", code, qty, ref_price, score,
            reason, "실주문 요청 성공")
    log("ORDER_OK", f"{side} {code} {qty}주 실주문 요청 성공")
    return r

# ---------- Data normalization ----------
def normalize_daily(payload):
    out = []
    for r in listify(payload):
        if not isinstance(r, dict):
            continue
        date = str(first_of(r, ["bsop_date","date"], ""))
        out.append({
            "date": date,
            "open": to_float(first_of(r, ["stck_oprc","open_pr","open"], 0)),
            "high": to_float(first_of(r, ["stck_hgpr","high_pr","high"], 0)),
            "low": to_float(first_of(r, ["stck_lwpr","low_pr","low"], 0)),
            "close": to_float(first_of(r, ["stck_clpr","close_pr","close"], 0)),
            "volume": to_float(first_of(r, ["acml_vol","volume"], 0)),
            "turnover": to_float(first_of(r, ["acml_tr_pbmn","turnover"], 0)),
        })
    df = pd.DataFrame(out)
    if df.empty:
        return df
    if "date" in df.columns and df["date"].str.len().eq(8).any():
        df = df[df["date"].str.len()==8].sort_values("date").reset_index(drop=True)
    else:
        df = df.iloc[::-1].reset_index(drop=True)
    return df

def strategy_score(df):
    if df is None or len(df) < 21:
        return 0, "데이터 부족"
    x = df.copy()
    x["ma5"] = x["close"].rolling(5).mean()
    x["ma20"] = x["close"].rolling(20).mean()
    x["ret5"] = x["close"].pct_change(5)
    x["vol20"] = x["volume"].rolling(20).mean()
    x["prev_high20"] = x["high"].rolling(20).max().shift(1)
    z = x.iloc[-1]

    score = 0
    reasons = []
    if z["ma5"] > z["ma20"]:
        score += 30; reasons.append("MA5>MA20 +30")
    if z["ret5"] > 0:
        score += 25; reasons.append("5일 모멘텀 +25")
    if pd.notna(z["vol20"]) and z["vol20"] > 0 and z["volume"] > z["vol20"]*1.5:
        score += 25; reasons.append("거래량급증 +25")
    if pd.notna(z["prev_high20"]) and z["close"] >= z["prev_high20"]:
        score += 20; reasons.append("20일고점돌파 +20")
    return score, " / ".join(reasons) if reasons else "조건 미충족"


def update_5m_bar(code, price, cum_volume):
    """현재가 스냅샷을 5분 OHLCV로 집계합니다.
    국내주식 REST에는 과거 분봉 API가 없어, 앱 실행 중 수집한 현재가/누적거래량으로 구성합니다.
    """
    if price <= 0:
        return
    now = now_kst()
    minute = (now.minute // 5) * 5
    bucket = now.replace(minute=minute, second=0, microsecond=0)
    key = bucket.strftime("%Y-%m-%d %H:%M")

    with runtime_lock:
        store = runtime["minute5"].setdefault(code, {"bars": [], "last_cum_volume": None})
        bars = store["bars"]
        prev_cum = store.get("last_cum_volume")
        vol_delta = 0
        if cum_volume is not None and cum_volume >= 0:
            if prev_cum is not None and cum_volume >= prev_cum:
                vol_delta = max(0, int(cum_volume - prev_cum))
            store["last_cum_volume"] = int(cum_volume)

        if bars and bars[-1]["bucket"] == key:
            b = bars[-1]
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            b["volume"] += vol_delta
        else:
            bars.append({
                "bucket": key,
                "open": price, "high": price, "low": price, "close": price,
                "volume": vol_delta,
            })
            # 최근 24개(2시간)만 유지
            if len(bars) > 24:
                del bars[:-24]

def score_5m(code):
    """현재 진행 중인 5분봉을 포함한 즉시 단타 진입 점수.

    과거 분봉을 기다리지 않고 현재 5분봉부터 바로 검색합니다.
    - 현재봉 양봉: +25
    - 현재봉 시가 대비 +0.30% 이상: +25
    - 현재가가 현재봉 고가의 0.2% 이내: +20
    - 5분 MA3 > MA6 (데이터 확보 시): +15
    - 현재봉 거래량 > 직전 3봉 평균 1.5배 (데이터 확보 시): +15

    시작 직후 과거 5분봉이 없어도 현재봉 자체 조건만으로 최대 70점이므로,
    강한 현재봉이면 기본 70점 기준에서 바로 후보가 될 수 있습니다.
    """
    with runtime_lock:
        raw = [b.copy() for b in runtime["minute5"].get(code, {}).get("bars", [])]

    if not raw:
        return 0, "현재 5분봉 수집중", 0

    df = pd.DataFrame(raw)
    z = df.iloc[-1]

    op = float(z["open"])
    hi = float(z["high"])
    cl = float(z["close"])
    vol = float(z.get("volume", 0) or 0)

    score = 0
    reasons = []

    # 1) 현재 진행 중인 5분봉 자체를 즉시 평가
    if cl > op:
        score += 25
        reasons.append("현재 5분봉 양봉 +25")

    rise = ((cl / op) - 1) * 100 if op > 0 else 0.0
    if rise >= 0.30:
        score += 25
        reasons.append(f"현재봉 +{rise:.2f}% +25")

    if hi > 0 and cl >= hi * 0.998:
        score += 20
        reasons.append("현재가 5분봉 고가권 +20")

    # 2) 앱이 수집한 과거 5분봉이 쌓이면 추가 확인점수 부여
    closes = df["close"].astype(float)
    if len(df) >= 6:
        ma3 = closes.tail(3).mean()
        ma6 = closes.tail(6).mean()
        if ma3 > ma6:
            score += 15
            reasons.append("5분 MA3>MA6 +15")

    if len(df) >= 4:
        prev_vol3 = df["volume"].astype(float).iloc[-4:-1].mean()
        if prev_vol3 > 0 and vol > prev_vol3 * 1.5:
            score += 15
            reasons.append("현재봉 거래량 1.5배 +15")

    return score, " / ".join(reasons) if reasons else f"현재봉 {rise:+.2f}% · 조건 미충족", len(df)


def update_scalp_tick(code, price, cum_volume):
    """REST 현재가 스냅샷으로 초단기 가격/거래량 변화율을 추적합니다."""
    now_ts = time.time()
    with runtime_lock:
        arr = runtime["scalp_ticks"].setdefault(code, [])
        arr.append({
            "ts": now_ts,
            "price": float(price),
            "cum_volume": int(cum_volume or 0),
        })
        # 최근 90초만 유지
        cutoff = now_ts - 90
        runtime["scalp_ticks"][code] = [x for x in arr if x["ts"] >= cutoff][-30:]


def score_scalp(code):
    """5분봉 방향 + 최근 스냅샷 순간가속을 결합한 스캘핑 진입 점수(100점).

    5분봉:
      - 양봉 +15
      - 시가 대비 +0.20% 이상 +15
      - 현재가가 봉 고가 0.15% 이내 +15

    순간 흐름:
      - 직전 스냅샷 대비 상승 +15
      - 약 15~60초 전 대비 +0.15% 이상 +20
      - 누적거래량 증가 가속 +20

    과열 추격 방지:
      - 현재 5분봉 시가 대비 +2.5% 이상이면 신규진입 제외
    """
    with runtime_lock:
        bars = [b.copy() for b in runtime["minute5"].get(code, {}).get("bars", [])]
        ticks = list(runtime["scalp_ticks"].get(code, []))

    if not bars:
        return 0, "현재 5분봉 수집중", {}

    b = bars[-1]
    op = float(b["open"])
    hi = float(b["high"])
    cl = float(b["close"])

    rise5 = ((cl / op) - 1) * 100 if op > 0 else 0.0
    if rise5 >= 2.5:
        return 0, f"5분봉 과열 +{rise5:.2f}% · 추격금지", {"rise5": rise5}

    score = 0
    reasons = []

    if cl > op:
        score += 15; reasons.append("5분 양봉 +15")
    if rise5 >= 0.20:
        score += 15; reasons.append(f"5분 +{rise5:.2f}% +15")
    if hi > 0 and cl >= hi * 0.9985:
        score += 15; reasons.append("5분 고가권 +15")

    mom1 = 0.0
    mom_short = 0.0
    vol_delta = 0
    vol_prev_delta = 0

    if len(ticks) >= 2:
        p0 = float(ticks[-2]["price"])
        p1 = float(ticks[-1]["price"])
        mom1 = ((p1 / p0) - 1) * 100 if p0 > 0 else 0.0
        if mom1 > 0:
            score += 15; reasons.append(f"직전틱 상승 {mom1:+.2f}% +15")

        # 가능한 한 15~60초 전 값 사용
        target = ticks[-1]["ts"] - 20
        past = min(ticks[:-1], key=lambda x: abs(x["ts"] - target))
        pp = float(past["price"])
        mom_short = ((p1 / pp) - 1) * 100 if pp > 0 else 0.0
        if mom_short >= 0.15:
            score += 20; reasons.append(f"순간가속 {mom_short:+.2f}% +20")

        vol_delta = max(0, int(ticks[-1]["cum_volume"]) - int(ticks[-2]["cum_volume"]))

    if len(ticks) >= 3:
        vol_prev_delta = max(0, int(ticks[-2]["cum_volume"]) - int(ticks[-3]["cum_volume"]))
        if vol_delta > 0 and (vol_prev_delta == 0 or vol_delta >= vol_prev_delta * 1.5):
            score += 20; reasons.append("거래량 가속 +20")

    detail = {
        "rise5": rise5,
        "mom1": mom1,
        "mom_short": mom_short,
        "vol_delta": vol_delta,
        "ticks": len(ticks),
    }
    return score, " / ".join(reasons) if reasons else "스캘핑 조건 미충족", detail

def holding_rows(bal):
    raw = listify(bal.get("Output_1"))
    out = []
    for h in raw:
        code = str(first_of(h, ["iem_cd","pdno","stck_shrn_iscd"], "")).strip()
        if code.isdigit():
            code = code.zfill(6)
        qty = to_int(first_of(h, ["itg_bnc_qty","hldg_qty","hold_qty","stock_qty"], 0))
        if len(code) != 6 or qty <= 0:
            continue
        out.append({
            "code": code,
            "name": str(first_of(h, ["iem_nm","prdt_name","hts_kor_isnm"], code)),
            "qty": qty,
            "avg": to_float(first_of(h, ["phs_pr","pchs_avg_pric","avg_pric"], 0)),
            "now": to_float(first_of(h, ["now_pr","prpr","stck_prpr"], 0)),
            "raw": h,
        })
    return out

def orderable_cash(bal):
    s = bal.get("Output_0")
    if isinstance(s, list) and s:
        s = s[0]
    if not isinstance(s, dict):
        s = {}
    return to_int(first_of(s, ["orr_pbl_amt1","orr_pbl_amt2","orr_pbl_amt3","orr_pbl_amt4","orr_pbl_amt","ord_psbl_cash","dnca_tot_amt","cash_ord_psbl_amt"], 0))

# ---------- Universe ----------
_universe_cache = None
_universe_lock = threading.Lock()

def universe_codes():
    global _universe_cache
    with _universe_lock:
        if _universe_cache:
            return _universe_cache
        df = load_master("m_new_stock")
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)
        best = []
        for col in df.columns:
            s = df[col].astype(str).str.strip().str.replace("A","",regex=False)
            mask = s.str.fullmatch(r"\d{6}")
            if len(s) and mask.mean() > 0.40:
                vals = s[mask].drop_duplicates().tolist()
                if len(vals) > len(best):
                    best = vals
        if not best:
            raise RuntimeError("NH 종목마스터에서 국내주식 6자리 코드를 찾지 못했습니다.")
        _universe_cache = best
        return best

# ---------- Live runtime state ----------
runtime = {
    "peaks": {},
    "pending_until": {},
    "last_buy": {},
    "candidate_map": {},
    "minute5": {},
    "scalp_ticks": {},
    "scalp_status": {},
    "ws_watch_codes": [],
    "ws_connected": False,
    "ws_last_message": "",
    "ws_last_error": "",
    "ws_message_count": 0,
    "ws_session_count": 0,
    "ws_last_body_keys": [],
    "entry_signals": {},
    "last_signal_time": "",
    "last_signal_code": "",
    "last_order_time": "",
    "last_order_text": "",
    "worker_started": False,
}
runtime_lock = threading.Lock()

def is_pending(code):
    with runtime_lock:
        until = runtime["pending_until"].get(code, 0)
    return time.time() < until

def set_pending(code, sec=180):
    with runtime_lock:
        runtime["pending_until"][code] = time.time() + sec

def local_risk_allows(code, price, qty, holds, settings):
    if code in {h["code"] for h in holds}:
        return False, "이미 보유 중"
    if is_pending(code):
        return False, "주문 처리 대기 중"
    if len(holds) >= int(settings["max_positions"]):
        return False, "최대 보유종목 도달"
    amount = int(price) * int(qty)
    if amount <= 0:
        return False, "주문금액 오류"
    if amount > int(settings["per_stock"]):
        return False, "종목당 한도 초과"

    buy_amount, trades, _ = today_stats()
    if buy_amount + amount > int(settings["daily_buy_limit"]):
        return False, "오늘 신규매수 한도 초과"
    if trades >= int(settings["max_trades"]):
        return False, "오늘 최대 거래횟수 도달"

    with runtime_lock:
        last = runtime["last_buy"].get(code)
    if last:
        elapsed = (now_kst() - last).total_seconds()/60
        if elapsed < int(settings["cooldown_min"]):
            return False, "재매수 쿨다운"
    return True, "통과"


def realtime_value(body, keys, default=0):
    if not isinstance(body, dict):
        return default
    return first_of(body, keys, default)


def on_realtime_execution(msg):
    """NHPLUG KRX 실시간 체결가(oc) 콜백.
    신호 계산만 빠르게 수행하고 실제 주문은 별도 executor 스레드가 처리합니다.
    """
    try:
        if not isinstance(msg, dict):
            return
        header = msg.get("header") if isinstance(msg.get("header"), dict) else {}
        body = msg.get("body") if isinstance(msg.get("body"), dict) else {}

        code = str(header.get("tr_key") or realtime_value(
            body, ["iem_cd", "stck_shrn_iscd", "code"], ""
        )).replace("A", "").strip()

        price = to_int(realtime_value(
            body,
            ["stck_prpr", "now_pr", "prpr", "cur_pr", "close_pr", "trade_pr"],
            0
        ))
        cum_volume = to_int(realtime_value(
            body,
            ["acml_vol", "acml_voln", "volume", "cum_volume"],
            0
        ))

        nowtxt = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        with runtime_lock:
            runtime["ws_connected"] = True
            runtime["ws_last_message"] = nowtxt
            runtime["ws_message_count"] += 1
            runtime["ws_last_body_keys"] = list(body.keys())[:30]

        if not code or price <= 0:
            return

        # 실시간 체결로 현재 5분봉/초단기 틱을 갱신
        update_5m_bar(code, price, cum_volume)
        update_scalp_tick(code, price, cum_volume)
        score, reason, detail = score_scalp(code)

        with runtime_lock:
            runtime["scalp_status"][code] = {
                "code": code,
                "price": price,
                "score": score,
                "reason": reason,
                "rise5": float(detail.get("rise5", 0) or 0),
                "momentum": float(detail.get("mom_short", 0) or 0),
                "vol_delta": int(detail.get("vol_delta", 0) or 0),
                "updated": now_kst().strftime("%H:%M:%S"),
            }

        state = get_state()
        settings = get_settings()
        if state.get("mode") != "RUNNING" or not market_buy_time():
            return
        if score < int(settings["buy_score"]):
            return

        # 거래량 100만주 필터는 REST 후보 선정에서 이미 적용.
        # 실시간 신호는 주문 실행 스레드로 넘겨 중복 주문을 막습니다.
        with runtime_lock:
            runtime["entry_signals"][code] = {
                "code": code,
                "price": price,
                "score": score,
                "reason": reason,
                "ts": time.time(),
            }
            runtime["last_signal_time"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
            runtime["last_signal_code"] = code

    except Exception as e:
        with runtime_lock:
            runtime["ws_last_error"] = f"콜백: {nh_error(e)}"


def realtime_watch_loop():
    """REST로 찾은 거래량 100만주 이상 상위 후보를 KRX 실시간 체결가로 감시."""
    log("WS", "실시간 체결 감시 스레드 시작")
    while True:
        try:
            state = get_state()
            if state.get("mode") == "STOPPED":
                with runtime_lock:
                    runtime["ws_connected"] = False
                    runtime["ws_watch_codes"] = []
                time.sleep(2)
                continue

            with runtime_lock:
                cand = sorted(
                    runtime["candidate_map"].values(),
                    key=lambda x: (x.get("score", 0), x.get("turnover", 0)),
                    reverse=True
                )

            # 주문 시장이 KRX이므로 체결가도 KRX 전용 oc를 사용.
            # 서버 한도에 맞춰 10종목만 한 세션에서 집중 감시.
            codes = [str(x["code"]) for x in cand[:10] if x.get("code")]
            if not codes:
                with runtime_lock:
                    runtime["ws_connected"] = False
                    runtime["ws_watch_codes"] = []
                time.sleep(2)
                continue

            with runtime_lock:
                runtime["ws_watch_codes"] = codes
                runtime["ws_connected"] = False
                runtime["ws_last_error"] = ""
                runtime["ws_session_count"] += 1

            # 300건 수신 또는 8초 무수신 시 후보목록을 다시 평가해 재구독
            n = subscribe(
                codes,
                on_realtime_execution,
                tr_cd="oc",
                max_messages=300,
                timeout=8,
            )

            with runtime_lock:
                if n == 0:
                    runtime["ws_connected"] = False

        except Exception as e:
            with runtime_lock:
                runtime["ws_connected"] = False
                runtime["ws_last_error"] = nh_error(e)
            log("WS_ERROR", nh_error(e))
            time.sleep(3)


def realtime_price_for(code, fallback=0):
    with runtime_lock:
        row = runtime["scalp_status"].get(code, {})
    p = to_float(row.get("price", 0))
    return p if p > 0 else fallback


def trade_executor_loop():
    """실시간 신호를 실제 주문으로 연결하고 보유종목의 손절/익절도 짧은 주기로 관리."""
    log("EXECUTOR", "실시간 주문 실행기 시작")
    last_balance_check = 0.0
    cached_holds = []
    cached_cash = 0

    while True:
        try:
            state = get_state()
            settings = get_settings()

            if state.get("mode") == "STOPPED":
                time.sleep(1)
                continue

            now_ts = time.time()
            if now_ts - last_balance_check >= 2.0:
                bal = balance()
                cached_holds = holding_rows(bal)
                cached_cash = orderable_cash(bal)
                last_balance_check = now_ts

            # 1) 보유종목 실시간 손절/익절/트레일링
            if market_manage_time():
                for h in list(cached_holds):
                    code, qty, avg = h["code"], int(h["qty"]), float(h["avg"])
                    if qty <= 0 or avg <= 0 or is_pending(code):
                        continue

                    nowp = realtime_price_for(code, float(h.get("now", 0) or 0))
                    if nowp <= 0:
                        continue

                    with runtime_lock:
                        peak = max(runtime["peaks"].get(code, nowp), nowp)
                        runtime["peaks"][code] = peak

                    pnl = (nowp / avg - 1) * 100
                    trail = (nowp / peak - 1) * 100 if peak > 0 else 0
                    sell_reason = None
                    if pnl <= -abs(float(settings["stop_loss"])):
                        sell_reason = f"실시간 손절 {pnl:.2f}%"
                    elif pnl >= abs(float(settings["take_profit"])):
                        sell_reason = f"실시간 익절 {pnl:.2f}%"
                    elif peak > avg and trail <= -abs(float(settings["trailing"])):
                        sell_reason = f"실시간 트레일링 {trail:.2f}%"

                    if sell_reason:
                        set_pending(code, 120)
                        try:
                            send_market_order(code, qty, "SELL", sell_reason, None, nowp)
                            with runtime_lock:
                                runtime["last_order_time"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
                                runtime["last_order_text"] = f"SELL {code} {qty}주 · {sell_reason}"
                            last_balance_check = 0
                        except Exception:
                            with runtime_lock:
                                runtime["pending_until"][code] = 0
                            raise

            # 2) 실시간 매수신호 실제 주문
            if state.get("mode") == "RUNNING" and market_buy_time():
                with runtime_lock:
                    signals = sorted(
                        runtime["entry_signals"].values(),
                        key=lambda x: (x.get("score", 0), x.get("ts", 0)),
                        reverse=True
                    )
                    # 오래된 신호 제거
                    runtime["entry_signals"] = {
                        k: v for k, v in runtime["entry_signals"].items()
                        if now_ts - float(v.get("ts", 0)) <= 8
                    }

                for sig in signals:
                    code = sig["code"]
                    if now_ts - float(sig.get("ts", 0)) > 8:
                        continue
                    if code in {h["code"] for h in cached_holds} or is_pending(code):
                        continue

                    price = int(sig["price"])
                    usable_capital = min(
                        int(settings["capital"]),
                        cached_cash if cached_cash > 0 else int(settings["capital"])
                    )
                    budget = min(int(settings["per_stock"]), usable_capital)
                    qty = budget // price if price > 0 else 0
                    if qty <= 0:
                        continue

                    ok, why = local_risk_allows(code, price, qty, cached_holds, settings)
                    if not ok:
                        continue

                    # 웹소켓이 끊긴 상태의 오래된 신호로 주문하지 않음
                    with runtime_lock:
                        last_ws = runtime["ws_last_message"]
                    if last_ws:
                        try:
                            age = (now_kst() - datetime.strptime(
                                last_ws, "%Y-%m-%d %H:%M:%S"
                            ).replace(tzinfo=KST)).total_seconds()
                        except Exception:
                            age = 999
                    else:
                        age = 999
                    if age > 5:
                        continue

                    reason = f"실시간 체결 스캘핑 {sig['score']}점 · {sig['reason']}"
                    set_pending(code, 120)
                    try:
                        send_market_order(code, qty, "BUY", reason, sig["score"], price)
                        with runtime_lock:
                            runtime["last_buy"][code] = now_kst()
                            runtime["entry_signals"].pop(code, None)
                            runtime["last_order_time"] = now_kst().strftime("%Y-%m-%d %H:%M:%S")
                            runtime["last_order_text"] = f"BUY {code} {qty}주 · {reason}"
                        cached_holds.append({
                            "code": code, "qty": qty, "avg": price,
                            "now": price, "name": code, "raw": {}
                        })
                        cached_cash = max(0, cached_cash - price * qty)
                    except Exception:
                        with runtime_lock:
                            runtime["pending_until"][code] = 0
                        raise

            time.sleep(0.25)

        except Exception as e:
            log("EXECUTOR_ERROR", nh_error(e))
            time.sleep(2)


def preflight():
    if not NH_KEY or not NH_SECRET:
        raise RuntimeError("NH APP KEY/SECRET이 없습니다.")
    acc = resolve_account()
    b = balance()
    # 실제 계좌 조회 성공이 확인되어야 START 허용
    current_price("005930")
    return acc, b

def worker_loop():
    log("WORKER", "백그라운드 Worker 시작")
    cursor = 0
    err_streak = 0

    while True:
        try:
            set_state(heartbeat=now_kst().strftime("%Y-%m-%d %H:%M:%S"))
            state = get_state()
            settings = get_settings()

            if state["mode"] == "STOPPED":
                time.sleep(2)
                continue

            bal = balance()
            holds = holding_rows(bal)

            # 보유종목 손절/익절/트레일링과 실제 주문은
            # trade_executor_loop 한 곳에서만 처리하여 중복 주문 경쟁을 막습니다.

            if state["mode"] != "RUNNING" or not market_buy_time():
                time.sleep(3)
                continue

            # 2) 전체 시장 순환 스캔
            codes = universe_codes()
            batch_n = max(20, min(int(settings["scan_batch"]), 150))
            batch = codes[cursor:cursor+batch_n]
            if not batch:
                cursor = 0
                batch = codes[:batch_n]

            for code in batch:
                try:
                    dresp = current_daily(code, 30)
                    df = normalize_daily(dresp.get("Output_0"))
                    if df.empty:
                        continue
                    score, reason = strategy_score(df)
                    last = df.iloc[-1]
                    turnover = int(last["turnover"])
                    volume = int(last["volume"])
                    close = int(last["close"])
                    with runtime_lock:
                        # 신규매수 대상: 당일 누적 거래량 100만주 이상 + 최소 거래대금 충족
                        if volume >= 1_000_000 and turnover >= int(settings["min_turnover"]):
                            runtime["candidate_map"][code] = {
                                "code":code, "score":score, "reason":reason,
                                "volume":volume, "turnover":turnover, "close":close,
                                "updated":now_kst().strftime("%H:%M:%S")
                            }
                        else:
                            runtime["candidate_map"].pop(code, None)
                except Exception as e:
                    log("SCAN_SKIP", f"{code} · {nh_error(e)}")

            cursor += len(batch)
            cycle = int(get_state().get("scan_cycle",0))
            if cursor >= len(codes):
                cursor = 0
                cycle += 1
                log("SCAN", f"전체시장 1회 순환 완료 · 총 {len(codes)}종목")

            set_state(scan_cursor=cursor, scan_total=len(codes), scan_cycle=cycle)

            with runtime_lock:
                candidates = sorted(
                    runtime["candidate_map"].values(),
                    key=lambda x:(x["score"], x["turnover"]),
                    reverse=True
                )[:int(settings["candidate_count"])]

            # 3) 실제 진입은 realtime_watch_loop + trade_executor_loop가 담당
            # REST Worker는 거래량 100만주 이상 후보를 찾는 역할에 집중합니다.

            err_streak = 0
            time.sleep(1)

        except Exception as e:
            err_streak += 1
            log("ERROR", f"{type(e).__name__}: {nh_error(e)}")
            if err_streak >= 3:
                set_state(mode="STOPPED", last_action="API/Worker 오류 3회 → 자동정지")
                log("SAFETY", "연속 오류 3회로 자동매매 STOP")
            time.sleep(min(30, 5*err_streak))

@st.cache_resource
def start_worker_once():
    t1 = threading.Thread(target=worker_loop, daemon=True, name="NH-LIVE-SCANNER")
    t2 = threading.Thread(target=realtime_watch_loop, daemon=True, name="NH-LIVE-WS")
    t3 = threading.Thread(target=trade_executor_loop, daemon=True, name="NH-LIVE-EXECUTOR")
    t1.start()
    t2.start()
    t3.start()
    return (t1, t2, t3)

# =========================================================
# UI
# =========================================================
st.set_page_config(page_title="NH Auto Trader LIVE · SCALPING", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""
<style>
.stApp{background:#07111f;color:#f5f7fb}
.block-container{max-width:1180px;padding-top:1rem}
div[data-testid="stMetric"]{background:#101b2b;border:1px solid #20364f;border-radius:16px;padding:12px}
.stButton>button{width:100%;min-height:52px;border-radius:13px;font-weight:850}
.card{background:#101b2b;border:1px solid #20364f;border-radius:16px;padding:16px;margin:8px 0}
.big{font-size:1.65rem;font-weight:900}
.muted{color:#9fb1c4}
.green{color:#22d67c}.orange{color:#ffb02e}.red{color:#ff5b6b}
</style>
""", unsafe_allow_html=True)

st.title("⚡ NH Auto Trader · LIVE SCALPING v14")
st.caption("거래량 100만주 이상 후보선별 → KRX 실시간 체결 감시 → 실시간 신호 주문 · 서버 프로세스가 살아 있는 동안 실행")

if not APP_PASSWORD:
    st.error("APP_PASSWORD 환경변수/Secrets가 필요합니다.")
    st.stop()
if "login" not in st.session_state:
    st.session_state.login = False
if not st.session_state.login:
    pw = st.text_input("앱 비밀번호", type="password")
    if st.button("로그인"):
        if pw == APP_PASSWORD:
            st.session_state.login = True
            st.rerun()
        else:
            st.error("비밀번호가 맞지 않습니다.")
    st.stop()

if not NH_KEY or not NH_SECRET:
    st.error("NH_APP_KEY / NH_APP_SECRET이 없습니다.")
    st.stop()

# Worker starts only after authenticated app process is ready.
start_worker_once()

settings = get_settings()
st_autorefresh(interval=max(3,int(settings["ui_refresh_sec"]))*1000, key="live_refresh")

def mask_account(s):
    s = str(s or "")
    return "*"*max(0,len(s)-4)+s[-4:] if s else "****"

@st.cache_data(ttl=20, show_spinner=False)
def market_refs():
    out = {}
    for code in ["KOSPI","KOSDAQ"]:
        try:
            u = f"https://finance.naver.com/sise/sise_index.naver?code={code}"
            txt = requests.get(u, timeout=3, headers={"User-Agent":"Mozilla/5.0"}).text
            soup = BeautifulSoup(txt, "html.parser")
            v = soup.select_one("#now_value")
            r = soup.select_one("#change_value_and_rate")
            out[code] = (v.get_text(strip=True) if v else "-",
                         r.get_text(" ",strip=True) if r else "-")
        except Exception:
            out[code] = ("-","조회불가")
    return out

refs = market_refs()
state = get_state()

m1,m2,m3,m4 = st.columns(4)
m1.metric("KOSPI 참고", refs["KOSPI"][0], refs["KOSPI"][1])
m2.metric("KOSDAQ 참고", refs["KOSDAQ"][0], refs["KOSDAQ"][1])
try:
    accs = live_accounts()
    acct_options = []
    acct_map = {}
    for a in accs:
        acct_no = str(a.get("acct_no",""))
        if not acct_no:
            continue
        acct_type = str(a.get("acct_type",""))
        label = f"{mask_account(acct_no)} · {'일반' if acct_type == '01' else '위탁'}"
        acct_options.append(label)
        acct_map[label] = acct_no

    saved_acct = str(kv_get("selected_account", "") or "")
    default_index = 0
    for i, label in enumerate(acct_options):
        if acct_map[label] == saved_acct:
            default_index = i
            break

    if acct_options:
        selected_label = st.selectbox(
            "실전 계좌 선택",
            acct_options,
            index=default_index,
            key="live_account_select"
        )
        selected_acct = acct_map[selected_label]
        if selected_acct != saved_acct:
            kv_set("selected_account", selected_acct)
            log("ACCOUNT", f"실전 계좌 변경 → {mask_account(selected_acct)}")
            st.rerun()
        acc_label = mask_account(selected_acct)
    else:
        acc_label = "실전계좌 없음"
except Exception as e:
    acc_label = "연결 실패"
    st.warning("실전 계좌 목록 조회: " + nh_error(e))

m3.metric("선택 계좌", acc_label)
m4.metric("Worker", "연결됨" if state.get("heartbeat") else "대기", state.get("heartbeat","-")[-8:])

st.caption("KOSPI/KOSDAQ은 화면 참고용이며 자동주문 판단에는 사용하지 않습니다.")

# Controls
st.subheader("자동매매 제어")
a,b,c = st.columns(3)
with a:
    if st.button("▶ 실전 자동매매 시작", type="primary"):
        try:
            preflight()
            set_state(mode="RUNNING", started_at=now_kst().strftime("%Y-%m-%d %H:%M:%S"),
                      last_action="실전 자동매매 시작")
            log("CONTROL","실전 자동매매 시작 · 사전점검 통과")
            st.rerun()
        except Exception as e:
            st.error("자동 사전점검 실패: "+nh_error(e))
with b:
    if st.button("⏸ 신규매수 정지"):
        set_state(mode="PAUSE_BUY", last_action="신규매수 정지")
        log("CONTROL","신규매수 정지")
        st.rerun()
with c:
    if st.button("⛔ 긴급 전체정지"):
        set_state(mode="STOPPED", last_action="긴급 전체정지")
        log("CONTROL","긴급 전체정지")
        st.rerun()

state = get_state()
label = {"RUNNING":"🟢 실전 자동매매 실행중","PAUSE_BUY":"🟠 신규매수 정지 · 보유종목 관리중","STOPPED":"⚫ 전체정지"}.get(state["mode"],"⚫ 전체정지")
st.markdown(f"""
<div class="card">
<div class="big">{label}</div>
<div class="muted">시작: {state.get('started_at') or '-'} · 마지막 동작: {state.get('last_action','-')}</div>
<div class="muted">전체시장 순환: {state.get('scan_cursor',0):,}/{state.get('scan_total',0):,} · 완료 {state.get('scan_cycle',0)}회</div>
</div>
""", unsafe_allow_html=True)

# =========================================================
# 실시간 운영 현황판
# =========================================================
st.subheader("⚡ 실시간 자동매매 운영 현황")

with runtime_lock:
    ws_codes = list(runtime["ws_watch_codes"])
    ws_connected = bool(runtime["ws_connected"])
    ws_last = runtime["ws_last_message"]
    ws_count = int(runtime["ws_message_count"])
    ws_error = str(runtime["ws_last_error"] or "")
    scalp_snapshot = list(runtime["scalp_status"].values())
    signal_snapshot = list(runtime["entry_signals"].values())
    last_signal_time = runtime["last_signal_time"]
    last_signal_code = runtime["last_signal_code"]
    last_order_time = runtime["last_order_time"]
    last_order_text = runtime["last_order_text"]

best_score = max([int(x.get("score",0)) for x in scalp_snapshot], default=0)
waiting = sorted(
    signal_snapshot,
    key=lambda x: x.get("score",0),
    reverse=True
)
waiting_text = waiting[0]["code"] if waiting else "-"

# 장중에는 마지막 체결 수신이 5초 이내일 때만 '실시간 정상'으로 표시
ws_age = None
if ws_last:
    try:
        ws_dt = datetime.strptime(ws_last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        ws_age = (now_kst() - ws_dt).total_seconds()
    except Exception:
        ws_age = None

if ws_connected and ws_age is not None and ws_age <= 5:
    ws_label = "🟢 정상 수신"
elif ws_codes:
    ws_label = "🟠 연결/수신 대기"
else:
    ws_label = "⚫ 감시후보 대기"

z1,z2,z3,z4,z5 = st.columns(5)
z1.metric("현재 감시 종목", f"{len(ws_codes)}개")
z2.metric("실시간 체결", ws_label)
z3.metric("최고 진입점수", f"{best_score}점")
z4.metric("매수 대기 종목", waiting_text)
z5.metric("누적 체결 수신", f"{ws_count:,}건")

z6,z7,z8 = st.columns(3)
z6.metric("마지막 체결수신", ws_last[-8:] if ws_last else "-")
z7.metric("마지막 신호", f"{last_signal_code or '-'} · {last_signal_time[-8:] if last_signal_time else '-'}")
z8.metric("마지막 주문", last_order_time[-8:] if last_order_time else "-")

if ws_codes:
    st.caption("실시간 감시: " + ", ".join(ws_codes))
if last_order_text:
    st.caption("최근 주문: " + last_order_text)
if ws_error:
    st.warning("실시간 연결 최근 오류: " + ws_error)

if ws_count > 0 and not scalp_snapshot:
    with runtime_lock:
        _body_keys = list(runtime.get("ws_last_body_keys", []))
    st.warning("실시간 체결은 수신 중이지만 가격 필드 해석이 아직 되지 않았습니다. 아래 수신 필드명을 확인하세요.")
    if _body_keys:
        st.caption("최근 실시간 body 필드: " + ", ".join(_body_keys))

# Account / holdings
st.subheader("실전 계좌")
bal = {}
holds = []
cash = 0
try:
    bal = balance()
    holds = holding_rows(bal)
    cash = orderable_cash(bal)
except Exception as e:
    st.warning("잔고 조회: "+nh_error(e))

# 잔고가 0원으로 표시될 때 NH 응답 필드 확인용.
# APP KEY/SECRET/토큰/계좌번호는 출력하지 않습니다.
def safe_balance_debug(payload):
    if not isinstance(payload, dict):
        return {}
    out = {}
    for section in ("Output_0", "Output_1"):
        obj = payload.get(section)
        rows = obj if isinstance(obj, list) else ([obj] if isinstance(obj, dict) else [])
        clean_rows = []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            clean = {}
            for k, v in row.items():
                lk = str(k).lower()
                # 계좌/인증/개인식별 가능 필드는 표시 금지
                if any(x in lk for x in ("act_no","acct","account","token","key","secret","name","nm")):
                    continue
                clean[str(k)] = v
            clean_rows.append(clean)
        out[section] = clean_rows
    return out

# =========================================================
# 계좌 잔고 현황판
# =========================================================
st.markdown("### 💰 계좌 잔고 현황판")

total_buy_value = sum(int(h["avg"] * h["qty"]) for h in holds if h["avg"] > 0)
total_eval_value = sum(int(h["now"] * h["qty"]) for h in holds if h["now"] > 0)
total_eval_pnl = total_eval_value - total_buy_value
total_return = (total_eval_pnl / total_buy_value * 100) if total_buy_value > 0 else 0.0

# NH 잔고 응답의 계좌 요약값이 있으면 우선 사용하고,
# 없으면 보유종목 + 주문가능금액으로 안전하게 계산
summary = bal.get("Output_0") if isinstance(bal, dict) else {}
if isinstance(summary, list) and summary:
    summary = summary[0]
if not isinstance(summary, dict):
    summary = {}

api_total_asset = to_int(first_of(summary, [
    "tot_aet_amt", "nas_amt", "tot_aset_amt", "tot_evlu_amt", "tot_evlt_amt", "tot_asset_amt"
], 0))
api_eval_amt = to_int(first_of(summary, [
    "tot_eal_amt", "evlu_amt", "evlt_amt", "tot_evlu_stk_amt", "stock_evlu_amt"
], 0))
api_buy_amt = to_int(first_of(summary, [
    "tot_byn_amt", "pchs_amt", "tot_pchs_amt", "buy_amt"
], 0))
api_pnl = to_int(first_of(summary, [
    "tot_eal_pls", "evlu_pfls_amt", "evlt_pls_amt", "eal_pls_amt", "pnl_amt"
], 0))
api_return = to_float(first_of(summary, ["pft_rt"], 0))

account_value = api_total_asset if api_total_asset > 0 else int(cash) + int(total_eval_value)
display_eval = api_eval_amt if api_eval_amt > 0 else total_eval_value
display_buy = api_buy_amt if api_buy_amt > 0 else total_buy_value
display_pnl = api_pnl if api_pnl != 0 else total_eval_pnl
display_return = api_return if api_return != 0 else ((display_pnl / display_buy * 100) if display_buy > 0 else 0.0)

r1,r2,r3,r4 = st.columns(4)
r1.metric("총 계좌자산", f"{account_value:,}원")
r2.metric("주문가능금액", f"{int(cash):,}원")
r3.metric("총 매입금액", f"{display_buy:,}원")
r4.metric("주식 평가금액", f"{display_eval:,}원")

r5,r6,r7,r8 = st.columns(4)
r5.metric("평가손익", f"{display_pnl:+,}원")
r6.metric("수익률", f"{display_return:+.2f}%")
r7.metric("보유종목", f"{len(holds)}개")
buy_amt,trades,_ = today_stats()
r8.metric("오늘 자동매수", f"{buy_amt:,}원")

st.caption(f"오늘 자동주문 요청 {trades}건 · 계좌/잔고 값은 선택한 실전계좌 기준으로 갱신됩니다.")


# =========================================================
# 보유/매수 종목 실시간 현황
# =========================================================
st.markdown("### 📈 매수 종목 실시간 현황")

live_rows = []
for h in holds:
    qty = int(h.get("qty", 0) or 0)
    avg = float(h.get("avg", 0) or 0)
    now = float(h.get("now", 0) or 0)
    buy_amt_live = int(avg * qty)
    eval_amt_live = int(now * qty)
    pnl_live = eval_amt_live - buy_amt_live
    rtn_live = ((now - avg) / avg * 100) if avg > 0 else 0.0

    live_rows.append({
        "종목명": h.get("name", ""),
        "종목코드": h.get("code", ""),
        "보유수량": qty,
        "매수가": int(avg),
        "현재가": int(now),
        "매수진행금액": buy_amt_live,
        "평가금액": eval_amt_live,
        "평가손익": pnl_live,
        "수익률(%)": round(rtn_live, 2),
    })

if live_rows:
    live_df = pd.DataFrame(live_rows)
    st.dataframe(
        live_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "보유수량": st.column_config.NumberColumn(format="%d주"),
            "매수가": st.column_config.NumberColumn(format="%d원"),
            "현재가": st.column_config.NumberColumn(format="%d원"),
            "매수진행금액": st.column_config.NumberColumn(format="%d원"),
            "평가금액": st.column_config.NumberColumn(format="%d원"),
            "평가손익": st.column_config.NumberColumn(format="%+d원"),
            "수익률(%)": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )

    # 종목별 핵심 현황 카드
    st.caption("현재가·매수가·매수진행금액·수익률은 잔고 재조회 시 함께 갱신됩니다.")
    for row in live_rows:
        with st.container(border=True):
            st.markdown(f"**{row['종목명']} ({row['종목코드']})**")
            c1, c2 = st.columns(2)
            c1.metric("현재가", f"{row['현재가']:,}원")
            c2.metric("매수가", f"{row['매수가']:,}원")
            c3, c4 = st.columns(2)
            c3.metric("매수진행금액", f"{row['매수진행금액']:,}원")
            c4.metric(
                "수익률",
                f"{row['수익률(%)']:+.2f}%",
                delta=f"{row['평가손익']:+,}원",
            )
else:
    st.info("현재 보유 중인 종목이 없습니다. 매수가 체결되면 이곳에 종목별 실시간 현황이 표시됩니다.")

# 오늘 매수 종목별 확인
today = now_kst().strftime("%Y-%m-%d")
with db() as c:
    buy_rows = c.execute("""SELECT code, qty, price, score, reason, status, ts
                            FROM journal
                            WHERE ts LIKE ? AND side='매수'
                            ORDER BY id DESC""", (today+"%",)).fetchall()

st.markdown("#### 오늘 매수 종목별")
if buy_rows:
    buy_df = pd.DataFrame(
        buy_rows,
        columns=["종목코드","수량","기준가격","점수","매수이유","상태","시간"]
    )
    buy_df["매수금액"] = (buy_df["수량"] * buy_df["기준가격"]).astype(int)
    buy_df["시간"] = buy_df["시간"].astype(str).str[-8:]
    buy_df = buy_df[["시간","종목코드","수량","기준가격","매수금액","점수","매수이유","상태"]]
    st.dataframe(buy_df, use_container_width=True, hide_index=True)

    grouped = buy_df.groupby("종목코드", as_index=False).agg(
        매수횟수=("종목코드","size"),
        총매수수량=("수량","sum"),
        총매수금액=("매수금액","sum"),
    )
    st.caption("종목별 누적 매수")
    st.dataframe(grouped, use_container_width=True, hide_index=True)
else:
    st.caption("오늘 자동매수 기록이 없습니다.")

if holds:
    display_holds = []
    for h in holds:
        buy_amt = int(h["avg"] * h["qty"]) if h["avg"] > 0 else 0
        eval_amt = int(h["now"] * h["qty"]) if h["now"] > 0 else 0
        pnl_amt = eval_amt - buy_amt if buy_amt > 0 and eval_amt > 0 else 0
        pnl_rate = (pnl_amt / buy_amt * 100) if buy_amt > 0 else 0.0
        display_holds.append({
            "종목명": h["name"],
            "종목코드": h["code"],
            "보유수량": h["qty"],
            "평균매입가": int(h["avg"]) if h["avg"] else 0,
            "매입금액": buy_amt,
            "현재가": int(h["now"]) if h["now"] else 0,
            "평가금액": eval_amt,
            "평가손익": pnl_amt,
            "수익률(%)": round(pnl_rate, 2),
        })
    st.dataframe(pd.DataFrame(display_holds),
                 use_container_width=True, hide_index=True)
    total_buy = sum(x["매입금액"] for x in display_holds)
    total_eval = sum(x["평가금액"] for x in display_holds)
    total_pnl = sum(x["평가손익"] for x in display_holds)
    total_rate = (total_pnl / total_buy * 100) if total_buy > 0 else 0.0
    b1,b2,b3,b4 = st.columns(4)
    b1.metric("총 매입금액", f"{total_buy:,}원")
    b2.metric("총 평가금액", f"{total_eval:,}원")
    b3.metric("총 평가손익", f"{total_pnl:+,}원")
    b4.metric("총 수익률", f"{total_rate:+.2f}%")

# Settings
with st.expander("⚙️ 금액 · 전략 · 안전설정", expanded=False):
    l,r = st.columns(2)
    with l:
        capital = st.number_input("총 운용한도", 100_000, 100_000_000, int(settings["capital"]), 100_000)
        per_stock = st.number_input("종목당 최대 매수", 10_000, 50_000_000, int(settings["per_stock"]), 10_000)
        max_positions = st.slider("최대 보유종목",1,10,int(settings["max_positions"]))
        daily_buy_limit = st.number_input("하루 신규매수 한도",10_000,100_000_000,int(settings["daily_buy_limit"]),10_000)
        max_trades = st.slider("하루 최대 자동주문 요청",1,30,int(settings["max_trades"]))
    with r:
        buy_score = st.slider("스캘핑 매수 최소점수",50,100,int(settings["buy_score"]),5)
        stop_loss = st.slider("손절 %",0.3,5.0,float(settings["stop_loss"]),0.1)
        take_profit = st.slider("익절 %",0.5,8.0,float(settings["take_profit"]),0.1)
        trailing = st.slider("트레일링 %",0.2,5.0,float(settings["trailing"]),0.1)
        cooldown = st.slider("동일종목 재매수 쿨다운(분)",1,120,int(settings["cooldown_min"]))
        min_turnover = st.number_input("최소 일 거래대금",100_000_000,50_000_000_000,int(settings["min_turnover"]),100_000_000)
        refresh = st.selectbox("화면 갱신(초)",[3,5,10,30,60],
                               index=[3,5,10,30,60].index(int(settings["ui_refresh_sec"])) if int(settings["ui_refresh_sec"]) in [3,5,10,30,60] else 1)
    if st.button("💾 설정 저장"):
        n = dict(settings)
        n.update({
            "capital":int(capital),"per_stock":int(per_stock),"max_positions":int(max_positions),
            "daily_buy_limit":int(daily_buy_limit),"max_trades":int(max_trades),
            "buy_score":int(buy_score),"stop_loss":float(stop_loss),"take_profit":float(take_profit),
            "trailing":float(trailing),"cooldown_min":int(cooldown),
            "min_turnover":int(min_turnover),"ui_refresh_sec":int(refresh)
        })
        set_settings(n)
        log("SETTINGS","금액/전략 설정 저장")
        st.success("저장했습니다.")
        st.rerun()

# Candidates
with runtime_lock:
    cand = sorted(runtime["candidate_map"].values(),
                  key=lambda x:(x["score"],x["turnover"]), reverse=True)[:20]
st.subheader("현재 자동매매 후보")
if cand:
    st.dataframe(pd.DataFrame(cand), use_container_width=True, hide_index=True)
else:
    st.caption("Worker가 시장을 순환검색하면서 후보가 여기에 표시됩니다.")

# Chart
st.subheader("종목 차트")
jrows = recent_journal()
codes = [h["code"] for h in holds]
for row in jrows:
    if row[2] not in codes:
        codes.append(row[2])
if not codes:
    codes = ["005930"]
sel = st.selectbox("종목", codes)

try:
    df = normalize_daily(current_daily(sel,60).get("Output_0"))
    if not df.empty:
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=df["date"],open=df["open"],high=df["high"],low=df["low"],close=df["close"],name="가격"))
        fig.add_trace(go.Scatter(x=df["date"],y=df["ma5"],name="MA5"))
        fig.add_trace(go.Scatter(x=df["date"],y=df["ma20"],name="MA20"))
        fig.update_layout(height=480,xaxis_rangeslider_visible=False,template="plotly_dark",margin=dict(l=10,r=10,t=30,b=10))
        st.plotly_chart(fig,use_container_width=True)
        sc,why = strategy_score(df)
        st.info(f"현재 전략점수 {sc}점 · {why}")
except Exception as e:
    st.warning("차트 조회: "+nh_error(e))

# Journal / logs
st.subheader("왜 샀나 / 왜 팔았나")
if jrows:
    jdf = pd.DataFrame(jrows, columns=["시간","구분","종목","수량","기준가격","점수","이유","상태"])
    st.dataframe(jdf,use_container_width=True,hide_index=True)
    st.download_button("⬇️ 매매기록 CSV",
                       jdf.to_csv(index=False).encode("utf-8-sig"),
                       "NH_LIVE_trade_journal.csv","text/csv")
else:
    st.caption("아직 자동매매 기록이 없습니다.")

with st.expander("🧾 시스템 로그", expanded=False):
    lr = recent_logs()
    if lr:
        st.dataframe(pd.DataFrame(lr,columns=["시간","구분","내용"]),use_container_width=True,hide_index=True)

st.divider()
st.success("LIVE 주문 구조 활성화 · START 버튼은 자동 사전점검을 통과해야 실행됩니다.")
st.caption("중요: 이 버전은 실전 주문용입니다. 서버 프로세스가 살아 있는 동안 휴대폰 화면을 닫아도 Worker는 계속 동작합니다. 서버 재시작 시 안전상 자동매매 상태는 확인 후 다시 START하는 것을 권장합니다.")
st.caption("구조: REST 전체시장 순환검색(100만주 이상) → 상위 10종목 KRX 실시간 체결 WebSocket → 실시간 진입신호 → 별도 주문 실행기. WebSocket 체결이 5초 이상 끊긴 오래된 신호는 신규매수에 사용하지 않습니다.")

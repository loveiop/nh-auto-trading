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
    "stop_loss": 2.0,
    "take_profit": 4.0,
    "trailing": 1.8,
    "cooldown_min": 20,
    "min_turnover": 500_000_000,
    "scan_batch": 80,
    "candidate_count": 20,
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

            # 1) 보유종목 리스크 관리: PAUSE_BUY에서도 계속 작동
            if market_manage_time():
                for h in holds:
                    code, qty, avg = h["code"], h["qty"], h["avg"]
                    nowp = h["now"]
                    if nowp <= 0:
                        pr = current_price(code)
                        o = pr.get("Output_0") if isinstance(pr.get("Output_0"),dict) else {}
                        nowp = to_float(first_of(o, ["stck_prpr","now_pr","prpr"], 0))
                    if avg <= 0 or nowp <= 0 or is_pending(code):
                        continue

                    with runtime_lock:
                        peak = max(runtime["peaks"].get(code, nowp), nowp)
                        runtime["peaks"][code] = peak

                    pnl = (nowp/avg - 1)*100
                    trail = (nowp/peak - 1)*100 if peak > 0 else 0
                    reason = None
                    if pnl <= -abs(float(settings["stop_loss"])):
                        reason = f"손절 {pnl:.2f}%"
                    elif pnl >= abs(float(settings["take_profit"])):
                        reason = f"익절 {pnl:.2f}%"
                    elif peak > avg and trail <= -abs(float(settings["trailing"])):
                        reason = f"트레일링 {trail:.2f}%"

                    if reason:
                        set_pending(code)
                        try:
                            send_market_order(code, qty, "SELL", reason, None, nowp)
                        except Exception:
                            with runtime_lock:
                                runtime["pending_until"][code] = 0
                            raise

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
                    close = int(last["close"])
                    with runtime_lock:
                        if turnover >= int(settings["min_turnover"]):
                            runtime["candidate_map"][code] = {
                                "code":code, "score":score, "reason":reason,
                                "turnover":turnover, "close":close,
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

            # 3) 후보 실제 매수
            bal = balance()
            holds = holding_rows(bal)
            cash = orderable_cash(bal)
            usable_capital = min(int(settings["capital"]), cash if cash > 0 else int(settings["capital"]))

            for c in candidates:
                if len(holds) >= int(settings["max_positions"]):
                    break
                if c["score"] < int(settings["buy_score"]):
                    continue
                code = c["code"]
                if code in {h["code"] for h in holds} or is_pending(code):
                    continue

                pr = current_price(code)
                o = pr.get("Output_0") if isinstance(pr.get("Output_0"),dict) else {}
                price = to_int(first_of(o, ["stck_prpr","now_pr","prpr"], c["close"]))
                alarm = str(first_of(o, ["mrkt_alrm_code","market_alarm_code"], ""))
                if alarm not in ("","00"):
                    log("FILTER", f"{code} 시장경보 {alarm} 신규매수 제외")
                    continue
                if price <= 0:
                    continue

                budget = min(int(settings["per_stock"]), usable_capital)
                qty = budget // price
                if qty <= 0:
                    continue

                ok, why = local_risk_allows(code, price, qty, holds, settings)
                if not ok:
                    continue

                reason = f"종합점수 {c['score']}점 · {c['reason']} · 거래대금 {c['turnover']:,}원"
                set_pending(code)
                try:
                    send_market_order(code, qty, "BUY", reason, c["score"], price)
                    with runtime_lock:
                        runtime["last_buy"][code] = now_kst()
                    # 다음 잔고 조회 전 중복방지용
                    holds.append({"code":code,"qty":qty,"avg":price,"now":price,"name":code,"raw":{}})
                    usable_capital -= price*qty
                except Exception:
                    with runtime_lock:
                        runtime["pending_until"][code] = 0
                    raise

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
    t = threading.Thread(target=worker_loop, daemon=True, name="NH-LIVE-WORKER")
    t.start()
    return t

# =========================================================
# UI
# =========================================================
st.set_page_config(page_title="NH Auto Trader LIVE", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")
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

st.title("📈 NH Auto Trader · LIVE")
st.caption("실전 자동주문 · 전체시장 순환스캔 · 휴대폰을 닫아도 서버가 켜져 있으면 Worker 계속 실행")

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
        buy_score = st.slider("매수 최소점수",50,100,int(settings["buy_score"]),5)
        stop_loss = st.slider("손절 %",0.5,10.0,float(settings["stop_loss"]),0.5)
        take_profit = st.slider("익절 %",1.0,20.0,float(settings["take_profit"]),0.5)
        trailing = st.slider("트레일링 %",0.5,10.0,float(settings["trailing"]),0.1)
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
st.caption("NH SDK 호출 한도 때문에 전체 종목을 동시에 1초 감시하지 않고 전체시장 순환스캔 → 상위 후보 집중매매 구조를 사용합니다.")

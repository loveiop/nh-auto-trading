import os
import io
import time
from datetime import datetime, time as dtime

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(
    page_title="NH Auto Trader v6",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────
st.markdown("""
<style>
:root {
  --panel:#101b2b;
  --panel2:#0c1625;
  --line:#1d334c;
  --text:#f5f7fb;
  --muted:#9fb3c8;
  --green:#20d67a;
  --orange:#f5a623;
  --red:#ff4d5f;
}
html, body, [class*="css"] { font-family: Pretendard, "Noto Sans KR", sans-serif; }
.stApp { background: linear-gradient(180deg,#07111f 0%,#091522 100%); color:var(--text); }
.block-container {max-width: 1180px; padding-top: 1rem; padding-bottom: 3rem;}
h1,h2,h3 {letter-spacing:-0.03em;}
div[data-testid="stMetric"] {
  background:var(--panel); border:1px solid var(--line);
  padding:14px 16px; border-radius:16px;
}
div[data-testid="stMetricLabel"] {color:var(--muted);}
.stButton>button {
  width:100%; min-height:54px; border-radius:14px;
  font-size:1.02rem; font-weight:800; border:1px solid #28445f;
}
.status-card {
  background:var(--panel); border:1px solid var(--line); border-radius:18px;
  padding:18px 20px; margin:8px 0 16px 0;
}
.status-big {font-size:2rem; font-weight:900; margin:2px 0 4px 0;}
.muted {color:var(--muted);}
.green {color:var(--green);}
.orange {color:var(--orange);}
.red {color:var(--red);}
.reason {
  background:#0b1726; border:1px solid #223b55; border-radius:14px;
  padding:12px 14px; margin:8px 0;
}
.small {font-size:.86rem; color:var(--muted);}
hr {border-color:#1b2c42;}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Helpers / Secrets
# ─────────────────────────────────────────────
def secret(*names, default=""):
    for name in names:
        try:
            if name in st.secrets and str(st.secrets[name]).strip():
                return str(st.secrets[name]).strip()
        except Exception:
            pass
        v = os.getenv(name)
        if v:
            return str(v).strip()
    return default

APP_KEY = secret("NH_APP_KEY", "NHPLUG_APP_KEY", "APP_KEY")
APP_SECRET = secret("NH_APP_SECRET", "NHPLUG_APP_SECRET", "APP_SECRET")
APP_PASSWORD = secret("APP_PASSWORD")

def num(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", ""))
    except Exception:
        return default

def intval(v, default=0):
    return int(num(v, default))

def first(d, keys, default=""):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in ("", None):
            return d[k]
    return default

def rows(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return [x]
    return []

def mask_account(v):
    s = str(v or "")
    return ("*" * max(0, len(s)-4) + s[-4:]) if s else "****"

def market_state():
    now = datetime.now()
    weekday = now.weekday()
    t = now.time()
    if weekday >= 5:
        return "장마감", False
    if dtime(9, 0) <= t <= dtime(15, 30):
        return "장중", True
    return "장마감", False

def safe_error(e):
    return f"{getattr(e,'category','')} / {getattr(e,'code','')} / {getattr(e,'message',str(e))}"

# ─────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────
st.title("📈 NH Auto Trader v6")
st.caption("NH투자증권 NAMUH PLUG · 실전 계좌 대시보드 · 종합점수형 전략")

if not APP_PASSWORD:
    st.error("Streamlit Secrets에 APP_PASSWORD가 없습니다.")
    st.stop()

if "auth_ok" not in st.session_state:
    st.session_state.auth_ok = False

if not st.session_state.auth_ok:
    pw = st.text_input("앱 비밀번호", type="password")
    if st.button("로그인"):
        if pw == APP_PASSWORD:
            st.session_state.auth_ok = True
            st.rerun()
        st.error("비밀번호가 맞지 않습니다.")
    st.stop()

if not APP_KEY or not APP_SECRET:
    st.error("NH_APP_KEY 또는 NH_APP_SECRET이 없습니다.")
    st.stop()

os.environ["NHPLUG_APP_KEY"] = APP_KEY
os.environ["NHPLUG_APP_SECRET"] = APP_SECRET
os.environ["NHPLUG_BASE_URL"] = "https://api.nhplug.com:8443"
os.environ["NHPLUG_AUTH_URL"] = "https://api.nhplug.com:8443"

try:
    from nhplug import call, NhplugError
except Exception as e:
    st.error("nhplug 패키지를 불러오지 못했습니다.")
    st.code(str(e))
    st.stop()

# ─────────────────────────────────────────────
# State
# ─────────────────────────────────────────────
defaults = {
    "accounts": [],
    "account": "",
    "auto_state": "정지",
    "started_at": "",
    "last_action": "대기",
    "logs": [],
    "journal": [],
    "balance_raw": {},
    "last_scan": [],
    "settings": {
        "capital": 1_000_000,
        "per_stock": 250_000,
        "max_positions": 3,
        "daily_buy_limit": 750_000,
        "daily_loss": 50_000,
        "max_trades": 6,
        "stop_loss": 2.0,
        "take_profit": 4.0,
        "trailing": 1.8,
        "buy_score": 70,
        "cooldown": 20,
        "watchlist": ["005930","000660","005380","000270","035420"],
    },
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

def log(kind, msg):
    st.session_state.logs.insert(0, {
        "시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "구분": kind,
        "내용": msg,
    })
    st.session_state.logs = st.session_state.logs[:300]

def journal_add(side, code, qty, price, score, reason, result=""):
    st.session_state.journal.insert(0, {
        "시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "구분": side,
        "종목코드": code,
        "수량": int(qty),
        "가격": int(price) if price else 0,
        "점수": int(score) if score is not None else "",
        "매매이유": reason,
        "결과": result,
    })
    st.session_state.journal = st.session_state.journal[:500]

# ─────────────────────────────────────────────
# Auto refresh
# ─────────────────────────────────────────────
with st.expander("🔄 화면 갱신 설정", expanded=False):
    auto_refresh = st.toggle("1분 자동 새로고침", value=True)
if auto_refresh:
    st_autorefresh(interval=60_000, key="v6_refresh")

# ─────────────────────────────────────────────
# Account / Header
# ─────────────────────────────────────────────
top1, top2, top3 = st.columns([2.4, 1.2, 1.2])

with top1:
    if st.button("🔌 실전 계좌 연결 / 새로고침"):
        try:
            r = call("/n2/acctinfo", {})
            acc = [x for x in rows(r.get("Output_0", []))
                   if str(x.get("acct_type","")).strip() in {"01","02"}]
            st.session_state.accounts = acc
            if acc and not st.session_state.account:
                st.session_state.account = str(acc[0].get("acct_no",""))
            log("시스템", f"실전 계좌 {len(acc)}개 확인")
            st.rerun()
        except Exception as e:
            st.error("실전 계좌 연결 실패")
            st.code(safe_error(e))

with top2:
    state_txt, is_open = market_state()
    st.metric("시장 상태", state_txt)

with top3:
    st.metric("현재 시각", datetime.now().strftime("%H:%M:%S"))

if st.session_state.accounts:
    opts = {
        f"{mask_account(a.get('acct_no',''))} · 실전계좌": str(a.get("acct_no",""))
        for a in st.session_state.accounts
    }
    sel = st.selectbox("연결 계좌", list(opts.keys()))
    st.session_state.account = opts[sel]
else:
    st.info("먼저 `실전 계좌 연결 / 새로고침`을 눌러주세요.")

account = st.session_state.account

# ─────────────────────────────────────────────
# 3 control buttons + state board
# ─────────────────────────────────────────────
st.subheader("자동매매 제어")

b1, b2, b3 = st.columns(3)
with b1:
    if st.button("▶ 자동매매 시작", type="primary"):
        st.session_state.auto_state = "실행중"
        st.session_state.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state.last_action = "자동매매 시작"
        log("제어", "자동매매 시작")
        st.rerun()

with b2:
    if st.button("⏸ 신규매수 정지"):
        st.session_state.auto_state = "신규매수 정지"
        st.session_state.last_action = "신규매수 정지"
        log("제어", "신규매수 정지 · 보유종목 관리는 유지")
        st.rerun()

with b3:
    if st.button("⛔ 긴급 전체정지"):
        st.session_state.auto_state = "정지"
        st.session_state.last_action = "긴급 전체정지"
        log("제어", "긴급 전체정지")
        st.rerun()

state = st.session_state.auto_state
if state == "실행중":
    cls, icon, desc = "green", "🟢", "자동매매 실행중"
elif state == "신규매수 정지":
    cls, icon, desc = "orange", "🟠", "신규매수 정지 · 보유종목 관리"
else:
    cls, icon, desc = "red", "⚫", "자동매매 정지"

st.markdown(
    f"""
    <div class="status-card">
      <div class="muted">자동매매 현재 상태</div>
      <div class="status-big {cls}">{icon} {desc}</div>
      <div class="small">
        시작시간: {st.session_state.started_at or "-"} &nbsp; | &nbsp;
        전략: 종합점수형 ({st.session_state.settings["buy_score"]}점 이상) &nbsp; | &nbsp;
        마지막 동작: {st.session_state.last_action}
      </div>
    </div>
    """,
    unsafe_allow_html=True
)

# ─────────────────────────────────────────────
# Balance / dashboard
# ─────────────────────────────────────────────
st.subheader("계좌 현황")

def fetch_balance():
    if not account:
        st.warning("실전 계좌를 먼저 연결하세요.")
        return
    try:
        data = call("/krstock/inquiry/v1/balance", {
            "act_no": account,
            "bnc_bse_cd": "5",
            "ltg_aot_dit_cd": "9",
            "aet_bse": "2",
            "qut_dit_cd": "UNT",
        })
        st.session_state.balance_raw = data
        log("조회", "잔고 / 보유종목 조회 완료")
    except Exception as e:
        st.error("잔고 조회 실패")
        st.code(safe_error(e))

if st.button("💰 잔고 / 보유종목 새로고침"):
    fetch_balance()
    st.rerun()

bal = st.session_state.balance_raw
summary = {}
holdings_df = pd.DataFrame()

if isinstance(bal, dict) and bal:
    all_outputs = []
    for k in ("Output_0","Output_1","Output_2","output","output1","output2"):
        rr = rows(bal.get(k, []))
        if rr:
            all_outputs.append((k, rr))

    for _, rr in all_outputs:
        for r in rr:
            if any(k in r for k in ("tot_asst_amt","tot_evlu_amt","dnca_tot_amt","ord_psbl_cash","tot_pfls_amt","evlu_pfls_amt")):
                summary = r
                break
        if summary:
            break

    # choose largest table as likely holdings
    if all_outputs:
        holdings_df = max((pd.DataFrame(rr) for _, rr in all_outputs), key=lambda d: len(d))

total_asset = intval(first(summary, ["tot_asst_amt","tot_evlu_amt","nass_amt"]))
cash = intval(first(summary, ["ord_psbl_cash","dnca_tot_amt","cash_amt"]))
stock_eval = intval(first(summary, ["scts_evlu_amt","evlu_amt","stck_evlu_amt"]))
pnl = intval(first(summary, ["tot_pfls_amt","evlu_pfls_amt","pchs_pfls_amt"]))

m1,m2,m3,m4,m5 = st.columns(5)
m1.metric("총자산", f"{total_asset:,}원" if total_asset else "-")
m2.metric("주문가능금액", f"{cash:,}원" if cash else "-")
m3.metric("주식 평가금액", f"{stock_eval:,}원" if stock_eval else "-")
m4.metric("평가손익", f"{pnl:,}원" if pnl else "-")
m5.metric("최대 보유", f"{st.session_state.settings['max_positions']}종목")

if not holdings_df.empty:
    with st.expander("📋 보유종목 / 잔고 상세", expanded=True):
        st.dataframe(holdings_df, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────
st.subheader("자동매매 설정")

S = st.session_state.settings
with st.expander("⚙️ 금액 · 손절 · 익절 · 리스크 설정", expanded=True):
    c1,c2 = st.columns(2)
    with c1:
        capital = st.number_input("총 운용한도", 100_000, 100_000_000, int(S["capital"]), 100_000)
        per_stock = st.number_input("종목당 최대 매수금액", 10_000, 20_000_000, int(S["per_stock"]), 10_000)
        max_positions = st.slider("최대 보유종목", 1, 10, int(S["max_positions"]))
        daily_buy_limit = st.number_input("하루 최대 신규매수 금액", 10_000, 100_000_000, int(S["daily_buy_limit"]), 10_000)
        max_trades = st.slider("하루 최대 매매횟수", 1, 30, int(S["max_trades"]))
    with c2:
        daily_loss = st.number_input("일일 최대 손실금액", 10_000, 20_000_000, int(S["daily_loss"]), 10_000)
        stop_loss = st.slider("손절률 (%)", .5, 10.0, float(S["stop_loss"]), .5)
        take_profit = st.slider("익절률 (%)", 1.0, 20.0, float(S["take_profit"]), .5)
        trailing = st.slider("트레일링 스탑 (%)", .5, 10.0, float(S["trailing"]), .1)
        buy_score = st.slider("매수 점수 기준", 50, 100, int(S["buy_score"]), 5)
        cooldown = st.slider("동일종목 재매수 대기 (분)", 1, 120, int(S["cooldown"]), 1)

    watchlist_text = st.text_area("관심종목 코드", ",".join(S["watchlist"]))

    if st.button("💾 설정 저장"):
        st.session_state.settings = {
            "capital": int(capital),
            "per_stock": int(per_stock),
            "max_positions": int(max_positions),
            "daily_buy_limit": int(daily_buy_limit),
            "daily_loss": int(daily_loss),
            "max_trades": int(max_trades),
            "stop_loss": float(stop_loss),
            "take_profit": float(take_profit),
            "trailing": float(trailing),
            "buy_score": int(buy_score),
            "cooldown": int(cooldown),
            "watchlist": [x.strip() for x in watchlist_text.split(",") if x.strip()],
        }
        log("설정", "자동매매 설정 저장")
        st.success("설정 저장 완료")

# ─────────────────────────────────────────────
# Strategy scan + WHY
# ─────────────────────────────────────────────
st.subheader("전략 스캔 · 왜 매수하는지")

def normalize_daily(rr):
    out = []
    for r in rr:
        if not isinstance(r, dict):
            continue
        out.append({
            "date": first(r, ["bsop_date","date","stck_bsop_date"]),
            "open": num(first(r, ["stck_oprc","open","oprc"])),
            "high": num(first(r, ["stck_hgpr","high","hgpr"])),
            "low": num(first(r, ["stck_lwpr","low","lwpr"])),
            "close": num(first(r, ["stck_clpr","close","clpr"])),
            "volume": num(first(r, ["acml_vol","volume","vol"])),
        })
    d = pd.DataFrame(out)
    if not d.empty:
        d = d.iloc[::-1].reset_index(drop=True)
    return d

def score_strategy(d):
    if len(d) < 21:
        return 0, {}, "데이터 부족"
    x = d.copy()
    x["ma5"] = x.close.rolling(5).mean()
    x["ma20"] = x.close.rolling(20).mean()
    x["vol20"] = x.volume.rolling(20).mean()
    x["ret5"] = x.close.pct_change(5)
    prev_high20 = x.high.rolling(20).max().shift(1)

    last = x.iloc[-1]
    score = 0
    parts = {"추세":0,"모멘텀":0,"거래량":0,"돌파":0}
    reasons = []

    if last.ma5 > last.ma20:
        score += 30; parts["추세"] = 30; reasons.append("5일선이 20일선 위(+30)")
    else:
        reasons.append("단기 추세 조건 미충족(+0)")

    if last.ret5 > 0:
        score += 25; parts["모멘텀"] = 25; reasons.append("5일 수익률 양수(+25)")
    else:
        reasons.append("5일 모멘텀 약함(+0)")

    if last.volume > last.vol20 * 1.5:
        score += 25; parts["거래량"] = 25; reasons.append("거래량 20일 평균 대비 1.5배 초과(+25)")
    else:
        reasons.append("거래량 조건 미충족(+0)")

    if pd.notna(prev_high20.iloc[-1]) and last.close >= prev_high20.iloc[-1]:
        score += 20; parts["돌파"] = 20; reasons.append("20일 고점 돌파(+20)")
    else:
        reasons.append("20일 돌파 조건 미충족(+0)")

    return score, parts, " / ".join(reasons)

if st.button("🔎 관심종목 전략 스캔"):
    result = []
    codes = st.session_state.settings["watchlist"]
    prog = st.progress(0)
    for i, code in enumerate(codes):
        try:
            data = call("/krstock/quote/v1/currentDaily", {
                "market_cd":"KRX",
                "iem_cd": code,
                "array_cnt":"30",
            })
            d = normalize_daily(rows(data.get("Output_0", [])))
            score, parts, reason = score_strategy(d)
            close = int(d.iloc[-1].close) if not d.empty else 0
            decision = "매수후보" if score >= st.session_state.settings["buy_score"] else "대기"
            result.append({
                "종목":code, "점수":score, "최근종가":close,
                "추세":parts.get("추세",0), "모멘텀":parts.get("모멘텀",0),
                "거래량":parts.get("거래량",0), "돌파":parts.get("돌파",0),
                "판정":decision, "왜?":reason,
            })
        except Exception as e:
            result.append({
                "종목":code,"점수":0,"최근종가":0,"추세":0,"모멘텀":0,
                "거래량":0,"돌파":0,"판정":"조회오류","왜?":str(e)
            })
        prog.progress((i+1)/max(1,len(codes)))
        time.sleep(.25)
    st.session_state.last_scan = sorted(result, key=lambda x:x["점수"], reverse=True)
    st.session_state.last_action = "전략 스캔 완료"
    log("전략", f"관심종목 {len(codes)}개 스캔")
    st.rerun()

if st.session_state.last_scan:
    df_scan = pd.DataFrame(st.session_state.last_scan)
    st.dataframe(df_scan, use_container_width=True, hide_index=True)

    st.markdown("#### 매수 후보 상세 설명")
    for r in st.session_state.last_scan:
        if r["판정"] == "매수후보":
            st.markdown(
                f"""
                <div class="reason">
                  <b>{r['종목']} · {r['점수']}점 · 매수후보</b><br>
                  <span class="small">{r['왜?']}</span>
                </div>
                """,
                unsafe_allow_html=True
            )

# ─────────────────────────────────────────────
# Manual order + reason recording
# ─────────────────────────────────────────────
st.subheader("수동 실전주문")

with st.expander("🔴 실전 주문창", expanded=False):
    code = st.text_input("종목코드", value="005930", max_chars=6, key="manual_code")
    side = st.radio("주문", ["매수","매도"], horizontal=True)
    order_type = st.radio("가격방식", ["지정가","시장가"], horizontal=True)
    qty = st.number_input("수량", min_value=1, max_value=100000, value=1, step=1)
    price = None
    if order_type == "지정가":
        price = st.number_input("주문가격", 1, 100_000_000, 70_000, 100)

    reason_default = (
        "수동 매수"
        if side == "매수"
        else "수동 매도"
    )
    trade_reason = st.text_area("매매 이유", value=reason_default)

    est = int(qty * price) if price else 0
    if est:
        st.write(f"예상 주문금액: **{est:,}원**")
        if est > st.session_state.settings["per_stock"]:
            st.error(f"종목당 한도 {st.session_state.settings['per_stock']:,}원을 초과했습니다.")

    ready = bool(account) and code.isdigit() and len(code)==6
    if est and est > st.session_state.settings["per_stock"]:
        ready = False

    if st.button(f"실제 {side} 주문 전송", disabled=not ready):
        payload = {
            "act_no": account,
            "iem_cd": code,
            "orr_qty": int(qty),
            "nmn_pr_tp_cd": "05" if order_type=="시장가" else "01",
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
        if price is not None:
            payload["orr_pr"] = int(price)

        endpoint = "/krstock/order/v1/cashBuy" if side=="매수" else "/krstock/order/v1/cashSell"
        try:
            log("주문", f"{side} 전송 전 · {code} · {qty}주 · 이유: {trade_reason}")
            res = call(endpoint, payload)
            journal_add(side, code, qty, price or 0, None, trade_reason, "주문요청 성공")
            st.session_state.last_action = f"{side} 주문 {code}"
            st.success("NH에 실제 주문 요청을 전송했습니다.")
        except Exception as e:
            journal_add(side, code, qty, price or 0, None, trade_reason, f"실패: {safe_error(e)}")
            st.error("주문 실패")
            st.code(safe_error(e))

# ─────────────────────────────────────────────
# WHY buy / WHY sell journal
# ─────────────────────────────────────────────
st.subheader("왜 샀나 / 왜 팔았나")

if st.session_state.journal:
    jdf = pd.DataFrame(st.session_state.journal)
    st.dataframe(jdf, use_container_width=True, hide_index=True)

    csv_data = jdf.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ 매매기록 CSV 저장",
        data=csv_data,
        file_name=f"NH_trade_journal_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )
else:
    st.info("매매가 발생하면 여기에서 `왜 샀는지 / 왜 팔았는지`와 결과가 함께 기록됩니다.")

st.caption("이 CSV를 나중에 ChatGPT에 올리면 승률, 손익비, 매수점수별 성과, 손절·익절 적정성 등을 다시 분석할 수 있습니다.")

# ─────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────
st.subheader("최근 상태 로그")
if st.session_state.logs:
    ldf = pd.DataFrame(st.session_state.logs)
    st.dataframe(ldf, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ 상태 로그 CSV 저장",
        data=ldf.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"NH_system_log_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )
else:
    st.caption("로그가 아직 없습니다.")

st.divider()
st.success("✅ 시스템 대시보드 정상 동작 중")
st.caption("보안: APP KEY / APP SECRET / 전체 계좌번호는 화면과 로그에 출력하지 않습니다.")
st.caption("중요: Streamlit Community Cloud는 sleep/restart가 가능하므로, 화면을 닫아도 계속 주문하는 완전 무인 자동매매는 별도 상시 worker가 필요합니다.")
st.caption("v6 FINAL · " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

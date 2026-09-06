import os, time, math
from datetime import datetime
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="NH Auto Trader v6",
    page_icon="📈",
    layout="centered",
)

st.markdown("""
<style>
.block-container {max-width: 920px; padding-top: .8rem; padding-bottom: 3rem;}
.stButton>button {width:100%; min-height:46px; font-weight:700;}
div[data-testid="stMetric"] {border:1px solid rgba(128,128,128,.18); padding:10px; border-radius:14px;}
.statusbox {padding:12px;border-radius:14px;margin:8px 0 14px 0;background:rgba(128,128,128,.08);}
</style>
""", unsafe_allow_html=True)

# -------------------- helpers --------------------
def get_secret(*names, default=""):
    for n in names:
        try:
            if n in st.secrets and str(st.secrets[n]).strip():
                return str(st.secrets[n]).strip()
        except Exception:
            pass
        v = os.getenv(n)
        if v:
            return str(v).strip()
    return default

APP_KEY = get_secret("NH_APP_KEY","NHPLUG_APP_KEY","APP_KEY")
APP_SECRET = get_secret("NH_APP_SECRET","NHPLUG_APP_SECRET","APP_SECRET")
APP_PASSWORD = get_secret("APP_PASSWORD")

def num(v, default=0.0):
    try:
        if v is None or v == "": return default
        return float(str(v).replace(",",""))
    except Exception:
        return default

def intval(v, default=0):
    return int(num(v, default))

def first(d, keys, default=""):
    if not isinstance(d, dict): return default
    for k in keys:
        if k in d and d[k] not in ("", None):
            return d[k]
    return default

def mask_account(v):
    s = str(v or "")
    return ("*"*max(0,len(s)-4)+s[-4:]) if s else "****"

def safe_rows(x):
    if isinstance(x, list): return x
    if isinstance(x, dict): return [x]
    return []

# -------------------- login --------------------
st.title("📈 NH Auto Trader v6")
st.caption("NH투자증권 NAMUH PLUG · 실전 계좌 대시보드 · 자동매매 통합판")

if not APP_PASSWORD:
    st.error("Streamlit Secrets에 APP_PASSWORD가 없습니다.")
    st.stop()

if "auth" not in st.session_state:
    st.session_state.auth = False
if not st.session_state.auth:
    pw = st.text_input("앱 비밀번호", type="password")
    if st.button("로그인"):
        if pw == APP_PASSWORD:
            st.session_state.auth = True
            st.rerun()
        else:
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
    st.error("nhplug 설치 오류")
    st.code(str(e))
    st.stop()

# -------------------- state --------------------
defaults = {
    "accounts": [],
    "account": "",
    "balance": {},
    "auto_state": "정지",
    "new_buy_block": False,
    "logs": [],
    "settings_saved": False,
    "last_scan": [],
    "last_order_ts": 0.0,
}
for k,v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

def log(msg):
    st.session_state.logs.insert(0, {
        "시간": datetime.now().strftime("%H:%M:%S"),
        "내용": msg
    })
    st.session_state.logs = st.session_state.logs[:100]

# -------------------- account --------------------
st.subheader("1. 실전 계좌")

if st.button("🔌 실전 계좌 연결 / 새로고침"):
    try:
        res = call("/n2/acctinfo", {})
        rows = safe_rows(res.get("Output_0", []))
        live = [r for r in rows if str(r.get("acct_type","")).strip() in {"01","02"}]
        st.session_state.accounts = live
        if live and not st.session_state.account:
            st.session_state.account = str(live[0].get("acct_no",""))
        log(f"실전 계좌 {len(live)}개 확인")
        st.success(f"실전 계좌 {len(live)}개 연결")
    except Exception as e:
        st.error("계좌 연결 실패")
        st.code(str(e))

if st.session_state.accounts:
    opts = {
        f"{mask_account(a.get('acct_no',''))} · 유형 {a.get('acct_type','')}": str(a.get("acct_no",""))
        for a in st.session_state.accounts
    }
    label = st.selectbox("사용 계좌", list(opts.keys()))
    st.session_state.account = opts[label]

account = st.session_state.account

# -------------------- dashboard / balance --------------------
st.subheader("2. 계좌 현황")

def load_balance():
    if not account:
        st.warning("먼저 실전 계좌를 연결하세요.")
        return
    try:
        data = call("/krstock/inquiry/v1/balance", {
            "act_no": account,
            "bnc_bse_cd": "5",
            "ltg_aot_dit_cd": "9",
            "aet_bse": "2",
            "qut_dit_cd": "UNT",
        })
        st.session_state.balance = data
        log("잔고 조회 완료")
    except Exception as e:
        st.error("잔고 조회 실패")
        st.code(str(e))

if st.button("💰 잔고 / 보유종목 조회"):
    load_balance()

bal = st.session_state.balance
if bal:
    outputs = []
    for key in ("Output_0","Output_1","Output_2"):
        rows = safe_rows(bal.get(key, []))
        if rows:
            outputs.append((key, rows))
    if outputs:
        # Try to identify a summary row without assuming a fixed response layout.
        summary = None
        for _, rows in outputs:
            for r in rows:
                if isinstance(r, dict) and any(k in r for k in (
                    "tot_evlu_amt","tot_asst_amt","dnca_tot_amt","ord_psbl_cash","tot_pfls_amt","evlu_pfls_amt"
                )):
                    summary = r
                    break
            if summary: break

        if summary:
            total_asset = intval(first(summary, ["tot_asst_amt","tot_evlu_amt","nass_amt"]))
            cash = intval(first(summary, ["ord_psbl_cash","dnca_tot_amt","cash_amt"]))
            pnl = intval(first(summary, ["tot_pfls_amt","evlu_pfls_amt","pchs_pfls_amt"]))
            stock_eval = intval(first(summary, ["scts_evlu_amt","evlu_amt","stck_evlu_amt"]))
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("총자산", f"{total_asset:,}원" if total_asset else "-")
            c2.metric("주문가능", f"{cash:,}원" if cash else "-")
            c3.metric("주식평가", f"{stock_eval:,}원" if stock_eval else "-")
            c4.metric("평가손익", f"{pnl:,}원" if pnl else "-")

        with st.expander("보유/잔고 상세", expanded=True):
            for key, rows in outputs:
                df = pd.DataFrame(rows)
                if not df.empty:
                    st.caption(key)
                    st.dataframe(df, use_container_width=True, hide_index=True)

# -------------------- auto state controls --------------------
st.subheader("3. 자동매매 제어")

state = st.session_state.auto_state
if state == "실행중":
    st.markdown('<div class="statusbox">🟢 <b>자동매매 실행중</b></div>', unsafe_allow_html=True)
elif state == "신규매수 정지":
    st.markdown('<div class="statusbox">🟠 <b>신규매수 정지 · 보유종목 관리 유지</b></div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="statusbox">⚫ <b>자동매매 정지</b></div>', unsafe_allow_html=True)

c1,c2,c3 = st.columns(3)
with c1:
    if st.button("▶ 자동매매 시작"):
        st.session_state["show_arm"] = True
with c2:
    if st.button("⏸ 신규매수 정지"):
        st.session_state.auto_state = "신규매수 정지"
        st.session_state.new_buy_block = True
        log("신규매수 정지")
        st.rerun()
with c3:
    if st.button("⛔ 긴급 전체정지"):
        st.session_state.auto_state = "정지"
        st.session_state.new_buy_block = True
        log("긴급 전체정지")
        st.rerun()

if st.session_state.get("show_arm"):
    st.warning("실전 자동매매 시작 확인")
    arm = st.checkbox("설정된 한도 안에서 실제 주문이 발생할 수 있음을 확인합니다.")
    phrase = st.text_input('확인문구: "자동매매시작"')
    if st.button("실전 자동매매 최종 시작", disabled=not (arm and phrase=="자동매매시작")):
        st.session_state.auto_state = "실행중"
        st.session_state.new_buy_block = False
        st.session_state.show_arm = False
        log("자동매매 실행 시작")
        st.success("자동매매 상태를 실행중으로 변경했습니다.")
        st.rerun()

# -------------------- settings --------------------
st.subheader("4. 자동매매 설정")

with st.expander("⚙️ 금액 / 리스크 / 전략 설정", expanded=True):
    capital = st.number_input("총 운용한도", 100_000, 100_000_000, 1_000_000, 100_000)
    per_stock = st.number_input("종목당 최대 매수금액", 10_000, 10_000_000, 250_000, 10_000)
    max_positions = st.slider("최대 보유종목 수", 1, 10, 3)
    daily_buy_limit = st.number_input("하루 최대 신규매수 금액", 10_000, 100_000_000, 750_000, 10_000)
    daily_loss = st.number_input("일일 최대 손실금액", 10_000, 10_000_000, 50_000, 10_000)
    max_trades = st.slider("하루 최대 매매횟수", 1, 30, 6)
    stop_loss = st.slider("손절률 (%)", 0.5, 10.0, 2.0, 0.5)
    take_profit = st.slider("1차 목표 익절률 (%)", 1.0, 20.0, 4.0, 0.5)
    trailing = st.slider("트레일링 스탑 (%)", 0.5, 10.0, 1.8, 0.1)
    buy_score = st.slider("매수 점수 기준", 50, 100, 70, 5)
    cooldown = st.slider("동일종목 재매수 대기(분)", 1, 120, 20, 1)
    watchlist = st.text_area(
        "관심종목 코드 (쉼표 구분)",
        value="005930,000660,035420,005380,000270",
        help="처음에는 관심종목 방식으로 검증한 뒤 자동 종목선정을 붙이는 것을 권장합니다."
    )
    if st.button("💾 설정 저장"):
        st.session_state.trade_settings = {
            "capital": capital, "per_stock": per_stock, "max_positions": max_positions,
            "daily_buy_limit": daily_buy_limit, "daily_loss": daily_loss,
            "max_trades": max_trades, "stop_loss": stop_loss,
            "take_profit": take_profit, "trailing": trailing,
            "buy_score": buy_score, "cooldown": cooldown,
            "watchlist": [x.strip() for x in watchlist.split(",") if x.strip()],
        }
        st.session_state.settings_saved = True
        log("자동매매 설정 저장")
        st.success("설정 저장 완료")

# -------------------- strategy scanner --------------------
st.subheader("5. 종합점수 전략")

st.caption("추세 + 단기모멘텀 + 거래량 + 20일 돌파를 합산해 매수 후보를 고릅니다.")

def normalize_daily(rows):
    out = []
    for r in rows:
        if not isinstance(r, dict): continue
        out.append({
            "date": first(r, ["bsop_date","date","stck_bsop_date"]),
            "open": num(first(r, ["stck_oprc","open","oprc"])),
            "high": num(first(r, ["stck_hgpr","high","hgpr"])),
            "low": num(first(r, ["stck_lwpr","low","lwpr"])),
            "close": num(first(r, ["stck_clpr","close","clpr"])),
            "volume": num(first(r, ["acml_vol","volume","vol"])),
        })
    df = pd.DataFrame(out)
    if not df.empty:
        # NH currentDaily usually returns newest first.
        df = df.iloc[::-1].reset_index(drop=True)
    return df

def score_df(df):
    if len(df) < 21:
        return 0, {}
    d = df.copy()
    d["ma5"] = d.close.rolling(5).mean()
    d["ma20"] = d.close.rolling(20).mean()
    d["vol20"] = d.volume.rolling(20).mean()
    d["ret5"] = d.close.pct_change(5)
    prev_high20 = d.high.rolling(20).max().shift(1)
    x = d.iloc[-1]
    score = 0
    parts = {}
    if x.ma5 > x.ma20:
        score += 30; parts["추세"] = 30
    else: parts["추세"] = 0
    if x.ret5 > 0:
        score += 25; parts["모멘텀"] = 25
    else: parts["모멘텀"] = 0
    if x.volume > x.vol20 * 1.5:
        score += 25; parts["거래량"] = 25
    else: parts["거래량"] = 0
    if pd.notna(prev_high20.iloc[-1]) and x.close >= prev_high20.iloc[-1]:
        score += 20; parts["돌파"] = 20
    else: parts["돌파"] = 0
    return score, parts

if st.button("🔎 관심종목 전략 스캔"):
    settings = st.session_state.get("trade_settings", {})
    codes = settings.get("watchlist") or [x.strip() for x in watchlist.split(",") if x.strip()]
    results = []
    prog = st.progress(0)
    for i, code in enumerate(codes):
        try:
            data = call("/krstock/quote/v1/currentDaily", {
                "market_cd":"KRX",
                "iem_cd": code,
                "array_cnt":"30",
            })
            df = normalize_daily(safe_rows(data.get("Output_0", [])))
            score, parts = score_df(df)
            last_close = int(df.iloc[-1].close) if not df.empty else 0
            results.append({
                "종목":code, "점수":score, "최근종가":last_close,
                "추세":parts.get("추세",0),
                "모멘텀":parts.get("모멘텀",0),
                "거래량":parts.get("거래량",0),
                "돌파":parts.get("돌파",0),
                "판정":"매수후보" if score >= buy_score else "대기"
            })
        except Exception as e:
            results.append({"종목":code,"점수":0,"최근종가":0,"추세":0,"모멘텀":0,"거래량":0,"돌파":0,"판정":"조회오류"})
        prog.progress((i+1)/max(1,len(codes)))
        time.sleep(0.3)
    st.session_state.last_scan = sorted(results, key=lambda x:x["점수"], reverse=True)
    log("관심종목 전략 스캔 완료")

if st.session_state.last_scan:
    st.dataframe(pd.DataFrame(st.session_state.last_scan), hide_index=True, use_container_width=True)

# -------------------- manual live order --------------------
st.subheader("6. 수동 실전주문")

with st.expander("실전 주문창"):
    order_code = st.text_input("종목코드", value="005930", max_chars=6, key="order_code")
    side = st.radio("주문구분", ["매수","매도"], horizontal=True)
    order_type = st.radio("가격방식", ["지정가","시장가"], horizontal=True)
    qty = st.number_input("수량", 1, 100000, 1, 1)
    order_price = None
    if order_type == "지정가":
        order_price = st.number_input("가격", 1, 100_000_000, 70_000, 100)

    estimated = int(qty * order_price) if order_price else None
    if estimated:
        st.write(f"예상 주문금액: **{estimated:,}원**")
        if estimated > per_stock:
            st.error(f"종목당 설정 한도 {per_stock:,}원을 초과했습니다.")

    chk = st.checkbox("실제 주문임을 확인합니다.", key="manual_chk")
    phrase = st.text_input('확인문구 "실전주문"', key="manual_phrase")
    ready = bool(account) and chk and phrase=="실전주문" and order_code.isdigit() and len(order_code)==6
    if estimated and estimated > per_stock:
        ready = False

    if st.button(f"🔴 실제 {side} 주문 전송", disabled=not ready):
        payload = {
            "act_no":account,
            "iem_cd":order_code,
            "orr_qty":int(qty),
            "nmn_pr_tp_cd":"05" if order_type=="시장가" else "01",
            "orr_cnd_dit_cd":"00",
            "ssl_nmn_pr_dit_cd":"00",
            "rmt_mkt_cd":"KRX",
            "sor_mkt_sli_yn":"N",
        }
        if order_price is not None:
            payload["orr_pr"] = int(order_price)
        endpoint = "/krstock/order/v1/cashBuy" if side=="매수" else "/krstock/order/v1/cashSell"
        try:
            result = call(endpoint, payload)
            log(f"실전 {side} 주문: {order_code} {int(qty)}주")
            st.success("NH에 실제 주문 요청이 접수되었습니다.")
            # Do not expose input/account.
            if isinstance(result, dict):
                safe = {k:v for k,v in result.items() if k != "Input_0"}
                st.json(safe)
        except Exception as e:
            st.error("주문 실패")
            st.code(str(e))

# -------------------- logs --------------------
st.subheader("7. 자동매매 로그 / 내역")
if st.session_state.logs:
    st.dataframe(pd.DataFrame(st.session_state.logs), hide_index=True, use_container_width=True)
else:
    st.caption("아직 앱 로그가 없습니다.")

st.divider()
st.caption("v6: 공식 nhplug SDK / 운영계좌 01·02 / 잔고 API / currentDaily 전략 스캔 / 실전 현금주문")
st.caption("자동매매 시작·정지 상태와 전략 스캔은 앱에서 제어합니다.")
st.caption("중요: Streamlit Community Cloud는 항상 켜진 장중 worker를 보장하지 않으므로, 완전 무인 자동주문은 별도 상시실행 worker 배포가 필요합니다.")
st.caption(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

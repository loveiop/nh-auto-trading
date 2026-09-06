import os
import time
from datetime import datetime

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="NH 자동매매 연구실 v5",
    page_icon="📈",
    layout="centered",
)

st.markdown("""
<style>
.block-container {max-width: 900px; padding-top: 1rem; padding-bottom: 3rem;}
.stButton > button {width: 100%; min-height: 46px;}
div[data-testid="stMetric"] {background: rgba(120,120,120,.07); padding: 12px; border-radius: 12px;}
.small {font-size: .86rem; opacity: .72;}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# Secret / config helpers
# ─────────────────────────────────────────────────────────────
def get_secret(*names, default=""):
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

APP_KEY = get_secret("NH_APP_KEY", "NHPLUG_APP_KEY", "APP_KEY")
APP_SECRET = get_secret("NH_APP_SECRET", "NHPLUG_APP_SECRET", "APP_SECRET")
APP_PASSWORD = get_secret("APP_PASSWORD")

# NH 공식 SDK가 읽는 환경변수
if APP_KEY:
    os.environ["NHPLUG_APP_KEY"] = APP_KEY
if APP_SECRET:
    os.environ["NHPLUG_APP_SECRET"] = APP_SECRET

# 토큰 발급은 운영 인증 서버
os.environ["NHPLUG_AUTH_URL"] = "https://api.nhplug.com:8443"

# ─────────────────────────────────────────────────────────────
# Access gate
# ─────────────────────────────────────────────────────────────
st.title("📈 NH 자동매매 연구실 v5")
st.caption("NH투자증권 NAMUH PLUG · 모바일용 · 100만원 테스트")

if not APP_PASSWORD:
    st.error("APP_PASSWORD가 아직 설정되지 않았습니다.")
    st.code('APP_PASSWORD = "본인만 아는 비밀번호"', language="toml")
    st.info("Streamlit → Manage app → Settings → Secrets에 위 한 줄을 추가하고 저장하세요.")
    st.stop()

if "login_ok" not in st.session_state:
    st.session_state.login_ok = False

if not st.session_state.login_ok:
    pw = st.text_input("앱 비밀번호", type="password")
    if st.button("로그인"):
        if pw == APP_PASSWORD:
            st.session_state.login_ok = True
            st.rerun()
        else:
            st.error("비밀번호가 맞지 않습니다.")
    st.stop()

# ─────────────────────────────────────────────────────────────
# SDK
# ─────────────────────────────────────────────────────────────
try:
    from nhplug import call, NhplugError
except Exception as e:
    st.error("nhplug 설치 오류")
    st.code(str(e))
    st.stop()

if not APP_KEY or not APP_SECRET:
    st.error("NH_APP_KEY 또는 NH_APP_SECRET이 없습니다.")
    st.stop()

# ─────────────────────────────────────────────────────────────
# Session defaults
# ─────────────────────────────────────────────────────────────
for key, value in {
    "accounts": [],
    "last_quote": None,
    "last_error": "",
}.items():
    if key not in st.session_state:
        st.session_state[key] = value

# ─────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────
st.subheader("1. 접속 환경")

env = st.radio(
    "환경",
    ["모의투자", "운영 조회전용"],
    horizontal=True,
    index=0,
)

if env == "모의투자":
    os.environ["NHPLUG_BASE_URL"] = "https://moapi.nhplug.com:8443"
    allowed_types = {"03"}
    st.success("🟢 모의투자 서버")
else:
    os.environ["NHPLUG_BASE_URL"] = "https://api.nhplug.com:8443"
    allowed_types = {"01", "02"}
    st.warning("🔴 운영 서버 · 이 버전은 실제 주문을 전송하지 않습니다.")

st.info("🔒 v5는 인증·계좌·시세·전략 점검까지 가능합니다. 실주문 API는 코드에서 차단되어 있습니다.")

# ─────────────────────────────────────────────────────────────
# Connection and account list
# ─────────────────────────────────────────────────────────────
st.subheader("2. NH 연결 / 계좌 확인")

if st.button("API 연결 확인"):
    try:
        res = call("/n2/acctinfo", {})
        rows = res.get("Output_0", []) if isinstance(res, dict) else []
        if isinstance(rows, dict):
            rows = [rows]

        usable = [
            r for r in rows
            if str(r.get("acct_type", "")).strip() in allowed_types
        ]
        st.session_state.accounts = usable
        st.session_state.last_error = ""
        st.success(f"✅ 연결 성공 · 사용 가능한 계좌 {len(usable)}개")
    except NhplugError as e:
        st.session_state.last_error = f"{getattr(e,'category','')} / {getattr(e,'code','')} / {getattr(e,'message',str(e))}"
        st.error("NH API 연결 실패")
        st.code(st.session_state.last_error)
    except Exception as e:
        st.session_state.last_error = str(e)
        st.error("연결 오류")
        st.code(str(e))

def mask_account(v):
    s = str(v or "")
    if len(s) <= 4:
        return "****"
    return "*" * (len(s) - 4) + s[-4:]

if st.session_state.accounts:
    table = []
    for a in st.session_state.accounts:
        table.append({
            "계좌": mask_account(a.get("acct_no", "")),
            "구분": a.get("acct_type", ""),
        })
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

# ─────────────────────────────────────────────────────────────
# Quote
# ─────────────────────────────────────────────────────────────
st.subheader("3. 국내주식 현재가")

c1, c2 = st.columns([2, 1])
with c1:
    stock_code = st.text_input("종목코드", value="005930", max_chars=6)
with c2:
    market = st.selectbox("시장", ["KRX", "NXT"], index=0)

if st.button("현재가 조회"):
    if not (stock_code.isdigit() and len(stock_code) == 6):
        st.error("종목코드는 숫자 6자리로 입력하세요.")
    else:
        try:
            q = call(
                "/krstock/quote/v1/currentPrice",
                {"iem_cd": stock_code, "market_cd": market},
            )
            out = q.get("Output_0", {}) if isinstance(q, dict) else q
            st.session_state.last_quote = out
            st.success("✅ 현재가 조회 성공")

            if isinstance(out, list):
                st.dataframe(pd.DataFrame(out), hide_index=True, use_container_width=True)
            elif isinstance(out, dict):
                st.dataframe(pd.DataFrame([out]), hide_index=True, use_container_width=True)
            else:
                st.write(out)
        except NhplugError as e:
            st.error("현재가 조회 실패")
            st.code(f"{getattr(e,'category','')} / {getattr(e,'code','')} / {getattr(e,'message',str(e))}")
        except Exception as e:
            st.error("조회 오류")
            st.code(str(e))

# ─────────────────────────────────────────────────────────────
# Strategy settings (no trade execution)
# ─────────────────────────────────────────────────────────────
st.subheader("4. 자동매매 전략 설정")

capital = st.number_input(
    "운용 자금",
    min_value=100_000,
    max_value=100_000_000,
    value=1_000_000,
    step=100_000,
)
per_trade = st.slider("1회 최대 투자비중 (%)", 5, 50, 25, 5)
stop_loss = st.slider("손절 (%)", 0.5, 10.0, 2.0, 0.5)
take_profit = st.slider("익절 (%)", 1.0, 20.0, 4.0, 0.5)
max_positions = st.slider("동시 보유 종목 수", 1, 5, 3)
buy_score = st.slider("매수 후보 점수 기준", 50, 95, 70, 5)

m1, m2, m3 = st.columns(3)
m1.metric("1회 최대금액", f"{int(capital * per_trade / 100):,}원")
m2.metric("손절", f"-{stop_loss:.1f}%")
m3.metric("익절", f"+{take_profit:.1f}%")

st.caption(
    f"최대 {max_positions}종목 · 매수후보 점수 {buy_score}점 이상 · "
    "현재 버전에서는 주문을 전송하지 않습니다."
)

# ─────────────────────────────────────────────────────────────
# CSV strategy checker
# ─────────────────────────────────────────────────────────────
st.subheader("5. CSV 전략 점검")
st.caption("열 이름: Date, Open, High, Low, Close, Volume")

f = st.file_uploader("주가 CSV 업로드", type=["csv"])

if f is not None:
    try:
        d = pd.read_csv(f)
        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(d.columns):
            st.error("Open, High, Low, Close, Volume 열이 필요합니다.")
        else:
            d = d.copy()
            d["MA5"] = d["Close"].rolling(5).mean()
            d["MA20"] = d["Close"].rolling(20).mean()
            d["VOL20"] = d["Volume"].rolling(20).mean()
            d["RET5"] = d["Close"].pct_change(5)

            d["Score"] = 0
            d.loc[d["MA5"] > d["MA20"], "Score"] += 30
            d.loc[d["RET5"] > 0, "Score"] += 25
            d.loc[d["Volume"] > d["VOL20"] * 1.5, "Score"] += 25
            prev_high20 = d["High"].rolling(20).max().shift(1)
            d.loc[d["Close"] >= prev_high20, "Score"] += 20
            d["Signal"] = d["Score"].apply(
                lambda x: "매수후보" if x >= buy_score else "대기"
            )

            st.dataframe(
                d.tail(30),
                hide_index=True,
                use_container_width=True,
            )

            if len(d):
                last = d.iloc[-1]
                st.metric("최근 점수", int(last["Score"]))
                st.write("판정:", last["Signal"])
    except Exception as e:
        st.error("CSV 처리 오류")
        st.code(str(e))

# ─────────────────────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────────────────────
st.divider()
st.caption("v5 · NH 공식 Python SDK(nhplug) 사용")
st.caption("보안: APP KEY/SECRET/토큰/전체 계좌번호는 화면에 출력하지 않습니다.")
st.caption("주문 안전장치: 실주문 호출 코드 없음")
st.caption(f"화면 갱신: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

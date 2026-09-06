import os
from datetime import datetime
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="NH 실전 자동매매 콘솔 v5",
    page_icon="📈",
    layout="centered",
)

st.markdown("""
<style>
.block-container {max-width: 900px; padding-top: 1rem; padding-bottom: 3rem;}
.stButton > button {width: 100%; min-height: 48px;}
</style>
""", unsafe_allow_html=True)

def secret(*names, default=""):
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

APP_KEY = secret("NH_APP_KEY", "NHPLUG_APP_KEY", "APP_KEY")
APP_SECRET = secret("NH_APP_SECRET", "NHPLUG_APP_SECRET", "APP_SECRET")
APP_PASSWORD = secret("APP_PASSWORD")
MAX_ORDER_KRW = int(secret("MAX_ORDER_KRW", default="250000"))

st.title("📈 NH 실전 자동매매 콘솔 v5")
st.caption("NH투자증권 NAMUH PLUG · 실전주문 가능 · 100만원 테스트 기준")

# ── 앱 로그인 ────────────────────────────────────────────────
if not APP_PASSWORD:
    st.error("APP_PASSWORD가 Streamlit Secrets에 없습니다.")
    st.stop()

if "auth_ok" not in st.session_state:
    st.session_state.auth_ok = False

if not st.session_state.auth_ok:
    pw = st.text_input("앱 비밀번호", type="password")
    if st.button("로그인"):
        if pw == APP_PASSWORD:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("비밀번호가 맞지 않습니다.")
    st.stop()

# ── NH 인증 ─────────────────────────────────────────────────
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

# ── 상태 ─────────────────────────────────────────────────────
if "live_accounts" not in st.session_state:
    st.session_state.live_accounts = []
if "selected_account" not in st.session_state:
    st.session_state.selected_account = ""

def mask(v):
    s = str(v or "")
    if not s:
        return "****"
    return "*" * max(0, len(s)-4) + s[-4:]

def safe_error(e):
    return f"{getattr(e,'category','')} / {getattr(e,'code','')} / {getattr(e,'message',str(e))}"

# ── 계좌 연결 ────────────────────────────────────────────────
st.subheader("1. 실전 계좌 연결")
st.warning("🔴 운영 서버입니다. 아래 주문 버튼을 누르면 실제 주문이 접수될 수 있습니다.")

if st.button("실전 계좌 불러오기"):
    try:
        res = call("/n2/acctinfo", {})
        rows = res.get("Output_0", []) if isinstance(res, dict) else []
        if isinstance(rows, dict):
            rows = [rows]
        live = [r for r in rows if str(r.get("acct_type", "")).strip() in {"01", "02"}]
        st.session_state.live_accounts = live
        if live and not st.session_state.selected_account:
            st.session_state.selected_account = str(live[0].get("acct_no", ""))
        st.success(f"✅ 실전 계좌 {len(live)}개 확인")
    except NhplugError as e:
        st.error("계좌 조회 실패")
        st.code(safe_error(e))
    except Exception as e:
        st.error("계좌 조회 오류")
        st.code(str(e))

accounts = st.session_state.live_accounts
if accounts:
    options = {f"{mask(a.get('acct_no',''))} · 유형 {a.get('acct_type','')}": str(a.get("acct_no","")) for a in accounts}
    label = st.selectbox("사용할 실전 계좌", list(options.keys()))
    st.session_state.selected_account = options[label]
    st.success(f"선택 계좌: {mask(st.session_state.selected_account)}")

# ── 현재가 ───────────────────────────────────────────────────
st.subheader("2. 종목 확인")
code = st.text_input("종목코드 6자리", value="005930", max_chars=6)
market = st.selectbox("시장", ["KRX"], index=0)

if st.button("현재가 확인"):
    if not (code.isdigit() and len(code) == 6):
        st.error("종목코드는 숫자 6자리로 입력하세요.")
    else:
        try:
            q = call("/krstock/quote/v1/currentPrice", {"iem_cd": code, "market_cd": market})
            out = q.get("Output_0", {}) if isinstance(q, dict) else q
            st.success("✅ 현재가 조회 성공")
            if isinstance(out, dict):
                st.dataframe(pd.DataFrame([out]), hide_index=True, use_container_width=True)
            elif isinstance(out, list):
                st.dataframe(pd.DataFrame(out), hide_index=True, use_container_width=True)
            else:
                st.write(out)
        except NhplugError as e:
            st.error("현재가 조회 실패")
            st.code(safe_error(e))
        except Exception as e:
            st.error("현재가 조회 오류")
            st.code(str(e))

# ── 실전 주문 ────────────────────────────────────────────────
st.subheader("3. 실전 주문")
st.caption(f"1회 주문 안전한도: {MAX_ORDER_KRW:,}원")

side = st.radio("주문", ["매수", "매도"], horizontal=True)
price_type = st.radio("가격 방식", ["지정가", "시장가"], horizontal=True)
qty = st.number_input("수량", min_value=1, max_value=100000, value=1, step=1)

price = None
if price_type == "지정가":
    price = st.number_input("주문가격(원)", min_value=1, max_value=100_000_000, value=70_000, step=100)

estimated = int(qty * price) if price is not None else None
if estimated is not None:
    st.metric("예상 주문금액", f"{estimated:,}원")
    if estimated > MAX_ORDER_KRW:
        st.error(f"1회 안전한도 {MAX_ORDER_KRW:,}원을 초과했습니다.")

st.markdown("#### 최종 확인")
confirm_box = st.checkbox("실제 돈이 움직이는 실전 주문임을 확인했습니다.")
confirm_text = st.text_input('확인문구 입력: "실전주문"', type="default")

ready = (
    bool(st.session_state.selected_account)
    and code.isdigit()
    and len(code) == 6
    and confirm_box
    and confirm_text.strip() == "실전주문"
)

if price_type == "지정가" and estimated is not None and estimated > MAX_ORDER_KRW:
    ready = False

button_label = f"🔴 실제 {side} 주문 전송"

if st.button(button_label, disabled=not ready):
    act_no = st.session_state.selected_account
    payload = {
        "act_no": act_no,
        "iem_cd": code,
        "orr_qty": int(qty),
        "nmn_pr_tp_cd": "05" if price_type == "시장가" else "01",
        "orr_cnd_dit_cd": "00",
        "ssl_nmn_pr_dit_cd": "00",
        "rmt_mkt_cd": "KRX",
        "sor_mkt_sli_yn": "N",
    }
    if price_type == "지정가":
        payload["orr_pr"] = int(price)

    endpoint = "/krstock/order/v1/cashBuy" if side == "매수" else "/krstock/order/v1/cashSell"

    try:
        st.write(f"주문 전송: {side} / {code} / {int(qty)}주 / {price_type}")
        result = call(endpoint, payload)
        st.success("✅ 실제 주문 요청이 NH투자증권에 접수되었습니다.")
        # 계좌번호/키는 출력하지 않음
        if isinstance(result, dict):
            safe_result = {k:v for k,v in result.items() if k not in {"Input_0"}}
            st.json(safe_result)
        else:
            st.write(result)
        st.session_state["last_order_time"] = datetime.now().isoformat()
    except NhplugError as e:
        st.error("❌ 주문이 접수되지 않았습니다.")
        st.code(safe_error(e))
    except Exception as e:
        st.error("❌ 주문 전송 오류")
        st.code(str(e))

st.divider()
st.caption("보안: APP KEY/SECRET/전체 계좌번호는 화면에 출력하지 않습니다.")
st.caption("실전 자동화는 수동 실주문 검증 후 별도 워커로 구성하는 것을 권장합니다.")
st.caption(f"화면 갱신: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hmac
import os
import time

import streamlit as st
from dotenv import load_dotenv

from nhtrader.core import now
from nhtrader.broker import safe_message
from nhtrader.engine import get_engine

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
@media(max-width:600px){.block-container{padding-left:1rem;padding-right:1rem}}
</style>""", unsafe_allow_html=True)
st.title("나무 자동매매")
st.caption("5분봉 관측 · 거래량 100만 주 이상 · 실전 계좌")

password = os.getenv("APP_PASSWORD", "")
if not password:
    st.info("처음 실행하셨다면 앱 비밀번호와 나무 API 연결값을 먼저 설정해 주세요.")
    st.markdown("압축파일 안의 **처음_실행하기.md**에 설정 방법이 있습니다. 설정을 마치고 앱을 다시 실행하면 연결 점검 화면이 열립니다.")
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
    st.error("나무 앱키 또는 시크릿이 설정되지 않았습니다. 실행 안내에 따라 설정해 주세요.")
    st.stop()

try:
    engine = get_engine()
except Exception as exc:
    st.error("실행 준비 실패 · " + safe_message(exc))
    st.stop()


def mask(account):
    return "•" * max(0, len(account)-4) + account[-4:]


def action(fn):
    try:
        fn()
        st.rerun()
    except Exception as exc:
        st.error(safe_message(exc))


with st.expander("계좌 연결", expanded=not bool(engine.account)):
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
        if st.button("선택 계좌 연결 점검", disabled=engine.mode != "STOPPED", use_container_width=True):
            with st.spinner("계좌·잔고·현재가·주문내역을 확인하고 있습니다…"):
                action(lambda: engine.connect(str(accounts[chosen]["acct_no"])))
    elif engine.broker.account:
        st.write("연결 계좌: " + mask(engine.broker.account))
    else:
        st.caption("계좌를 불러온 뒤 운영할 계좌를 선택해 주세요. 연결 점검은 조회만 수행합니다.")
    st.caption("주문내역을 정확히 구분하도록 이 계좌에서는 같은 시간에 다른 자동매매나 동일한 수동주문을 실행하지 마세요.")

buttons = st.columns(3)
with buttons[0]:
    if st.button("▶ 자동매매 시작", type="primary", disabled=not bool(engine.account), use_container_width=True):
        action(lambda: engine.set_mode("RUNNING"))
with buttons[1]:
    if st.button("Ⅱ 신규매수 정지", disabled=not bool(engine.account), use_container_width=True):
        action(lambda: engine.set_mode("PAUSE_BUY"))
with buttons[2]:
    if st.button("■ 전체정지", use_container_width=True):
        action(lambda: engine.set_mode("STOPPED"))
st.caption("신규매수 정지: 보유분 매도 관리는 계속합니다. 전체정지: 새 주문 전송을 멈춥니다. 이미 접수된 주문은 별도이며 체결조회는 계속합니다.")

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


def seconds_since(stamp):
    return (now()-datetime.fromisoformat(stamp)).total_seconds() if stamp else None


@st.fragment(run_every=3)
def dashboard():
    data = engine.snapshot()
    beat_age, tick_age = seconds_since(data["heartbeat"]), seconds_since(data["last_tick"])
    status = {"STOPPED": "전체정지", "RUNNING": "자동매매 켜짐", "PAUSE_BUY": "신규매수 정지"}[data["mode"]]
    st.subheader(status)
    st.write(data["status"])
    if data["error"]:
        st.error(data["error"])
    if data["ws_error"]:
        st.warning(data["ws_error"])
    if beat_age is not None and beat_age > 15:
        st.error(f"운영 확인이 {beat_age:.0f}초 지연되었습니다. 새 주문을 확인해 주세요.")
    a, b, c, d = st.columns(4)
    a.metric("현재 감시", f"{len(data['watched'])}종목")
    b.metric("실시간 체결", "수신 중" if tick_age is not None and tick_age <= 3 else "수신 대기")
    c.metric("최고 진입점수", max((r["score"] for r in data["watched"]), default=0))
    d.metric("유효 체결 수신", f"{data['tick_count']:,}건")
    signal = data["last_signal"]
    st.caption("마지막 신호: " + (f"{signal['time'][11:19]} · {signal['code']} · {signal['score']}점" if signal else "아직 없음") +
               f" | 연결: {data['ws_state']} | 후보 확인 {data['scan_done']}/{data['scan_total']}")
    if data["last_tick"]:
        st.caption(f"마지막 체결 수신 {data['last_tick'][11:19]} · {tick_age:.1f}초 전")

    account = data["account"]
    if account:
        a, b, c = st.columns(3)
        a.metric("현금 주문가능", f"{account['cash']:,}원")
        b.metric("자동매매 보유", f"{len(data['positions'])}종목")
        c.metric("당일 추정 실현손익", f"{data['stats'].get('realized',0):+,.0f}원")
        st.caption("손익은 매매비용 0.3% 여유분을 적용한 추정치입니다. 실제 수수료·세금 정산은 나무 앱 기준입니다.")
        st.caption("잔고 확인: " + (data["balance_at"][11:19] if data["balance_at"] else "미확인"))

    tabs = st.tabs(["현황판", "보유·주문", "점검 기록"])
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
            st.info("시작 후 종목 조건을 확인하면 감시 목록이 표시됩니다. 장외에는 실시간 체결이 없을 수 있습니다.")
        a, b = st.columns(2)
        for column, code in ((a, "KOSPI"), (b, "KOSDAQ")):
            ref = data["indices"].get(code)
            column.metric(code + " 참고", ref["value"] if ref else "—", ref["change"] if ref else None)
        st.caption("네이버 증권 참고 지수 · 자동주문 판단에 사용하지 않음 · 확인 " + (data["indices_at"][11:19] if data["indices_at"] else "대기"))
        if data["index_error"]:
            st.caption("시장지수 갱신 실패 · 표시값이 이전 값일 수 있습니다.")
    with tabs[1]:
        st.write("계좌 보유종목")
        owned = {p["code"]: p for p in data["positions"]}
        if account and account["holdings"]:
            output = []
            for h in account["holdings"]:
                with engine.lock:
                    tape = engine.tapes.get(h["code"])
                    latest = tape.latest if tape else None
                price = latest.price if latest and (now()-latest.event).total_seconds() <= 3 else h["price"]
                output.append(dict(종목=h["name"], 보유수량=h["qty"], 자동매매수량=owned.get(h["code"], {}).get("qty",0),
                                   매입가=h["avg"], 현재가=price, 매입금액=h["qty"]*h["avg"],
                                   수익률=f"{(price/h['avg']-1)*100:+.2f}%" if h["avg"] else "—"))
            st.dataframe(output, hide_index=True, use_container_width=True)
        else:
            st.caption("확인된 보유종목이 없습니다.")
        st.caption("자동매매가 체결한 수량만 자동 매도 대상으로 관리합니다.")
        st.write("주문·체결 기록")
        states = dict(SUBMITTING="전송 중", ACCEPTED="접수 · 체결 확인 중", PARTIAL="부분체결", UNKNOWN="결과 확인 필요", FILLED="전체체결", CANCELLED="잔량 종료", REJECTED="거절")
        if data["orders"]:
            output = [dict(시각=o["created"][:19].replace("T"," "), 코드=o["code"], 방향="매수" if o["side"]=="BUY" else "매도",
                           주문수량=o["qty"], 체결수량=o["filled"], 체결평균가=o["avg"], 상태=states[o["state"]],
                           매매이유=o["reason"], 확인내용=o["error"]) for o in reversed(data["orders"][-100:])]
            st.dataframe(output, hide_index=True, use_container_width=True)
        else:
            st.caption("아직 주문이 없습니다. 조건 충족 전에는 주문하지 않습니다.")
        if any(o["state"] == "UNKNOWN" for o in data["orders"]):
            st.error("결과를 확인하지 못한 주문이 있습니다. 같은 종목 주문과 신규매수를 보류했습니다. 나무 앱에서 해당 시각의 주문내역을 확인해 주세요.")
    with tabs[2]:
        st.write(data["scan_source"])
        st.write(f"현재 후보 {data['candidates']}개 · 후보 조회 오류 {data['scan_errors']}건 · 해석 불가 체결 {data['invalid_ticks']}건")
        if data["last_scan_error"]:
            st.warning("마지막 후보 조회 오류 · " + data["last_scan_error"])
        if data["logs"]:
            st.dataframe(data["logs"], hide_index=True, use_container_width=True)
        import json
        report = {k:data[k] for k in ("mode","status","error","ws_state","ws_error","last_tick","tick_count","invalid_ticks","scan_source","scan_done","scan_total","last_scan_error")}
        st.download_button("연결 점검 결과 내려받기", json.dumps(report, ensure_ascii=False, indent=2), "connection_check.json", "application/json")


dashboard()

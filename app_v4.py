import os
import streamlit as st
import pandas as pd
import numpy as np

st.set_page_config(page_title="모바일 자동매매 연구실 v4", page_icon="📈", layout="centered")
st.title("📈 모바일 자동매매 연구실 v4")
st.caption("100만원 테스트 · NH NAMUH PLUG 연결 확인 · 실제 주문 잠금")

def get_secret(*names):
    for name in names:
        try:
            if name in st.secrets:
                return str(st.secrets[name])
        except Exception:
            pass
        v = os.getenv(name)
        if v:
            return v
    return ""

APP_KEY = get_secret("NH_APP_KEY", "NHPLUG_APP_KEY", "APP_KEY")
APP_SECRET = get_secret("NH_APP_SECRET", "NHPLUG_APP_SECRET", "APP_SECRET")

st.subheader("🔐 API 보안 상태")
a,b=st.columns(2)
a.metric("APP KEY","등록됨" if APP_KEY else "미등록")
b.metric("APP SECRET","등록됨" if APP_SECRET else "미등록")

if APP_KEY: os.environ["NHPLUG_APP_KEY"]=APP_KEY
if APP_SECRET: os.environ["NHPLUG_APP_SECRET"]=APP_SECRET

st.warning("🔒 안전모드: 이 버전은 실제 매수·매도 주문을 실행하지 않습니다.")

capital=st.number_input("테스트 자금",100000,100000000,1000000,100000)
score_cut=st.slider("종합점수 매수 기준",50,95,70)
stop_loss=st.slider("손절 %",0.5,10.0,2.0,0.5)
take_profit=st.slider("기본 익절 %",1.0,20.0,4.0,0.5)
position_pct=st.slider("1회 투자비중 %",5,100,25,5)

st.divider()
st.subheader("🔌 NH NAMUH PLUG 연결 테스트")
st.caption("토큰은 24시간 유효하므로 연결 버튼을 반복해서 누르지 마세요.")

if st.button("NH API 연결 확인"):
    if not APP_KEY or not APP_SECRET:
        st.error("Streamlit Secrets에 APP KEY / APP Secret이 없습니다.")
    else:
        try:
            from nhplug import call
            data=call("/n2/acctinfo",{})
            rows=data.get("Output_0",[]) if isinstance(data,dict) else []
            st.success("✅ NH API 인증 및 연결 성공")
            st.write("조회된 계좌:",len(rows),"개")
        except ImportError:
            st.error("nhplug가 설치되지 않았습니다. requirements.txt에 nhplug를 추가하세요.")
        except Exception as e:
            st.error("연결 실패")
            st.code(str(e))

st.divider()
st.subheader("📁 CSV 전략 연구")
f=st.file_uploader("주가 CSV (Date,Open,High,Low,Close,Volume)",type=["csv"])
if f:
    try:
        d=pd.read_csv(f)
        need={"Open","High","Low","Close","Volume"}
        if not need.issubset(d.columns):
            st.error("Open, High, Low, Close, Volume 열이 필요합니다.")
        else:
            d["MA5"]=d["Close"].rolling(5).mean()
            d["MA20"]=d["Close"].rolling(20).mean()
            d["VOL20"]=d["Volume"].rolling(20).mean()
            d["RET5"]=d["Close"].pct_change(5)
            d["Score"]=np.where(d["MA5"]>d["MA20"],30,0)+np.where(d["RET5"]>0,25,0)+np.where(d["Volume"]>d["VOL20"]*1.5,25,0)+np.where(d["Close"]>=d["High"].rolling(20).max().shift(1),20,0)
            d["Signal"]=np.where(d["Score"]>=score_cut,"관심/매수후보","대기")
            st.dataframe(d.tail(30),use_container_width=True)
    except Exception as e:
        st.error(str(e))

st.caption("※ v4는 연결·조회·전략 연구용입니다. 실주문 기능은 별도 검증 후 추가합니다.")

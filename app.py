
import streamlit as st
import pandas as pd
import numpy as np

st.set_page_config(page_title="모바일 자동매매 연구실 v3", page_icon="📈", layout="centered")
st.markdown("""
<style>
.block-container{max-width:760px;padding-top:1rem;padding-bottom:5rem}
.stButton button{width:100%;min-height:46px;border-radius:12px;font-weight:700}
div[data-testid="stMetric"]{background:rgba(128,128,128,.08);padding:10px;border-radius:14px}
</style>""", unsafe_allow_html=True)

st.title("📈 모바일 자동매매 연구실 v3")
st.caption("100만원 테스트 · 패턴/추세/거래량 점수화 · 실제 주문 없음")

capital = st.number_input("테스트 자금", 100000, 100000000, 1000000, 100000)
mode = st.selectbox("전략", [
    "종합 점수형",
    "N자·눌림목",
    "추세전환 스윙",
    "볼린저 돌파",
    "중장기 추세",
    "W바닥(더블바텀) 근사"
])
score_cut = st.slider("종합점수 매수 기준", 40, 100, 70, 5)
stop = st.slider("손절 %", 0.5, 10.0, 2.0, 0.5)
take = st.slider("기본 익절 %", 1.0, 20.0, 4.0, 0.5)
pos = st.slider("1회 투자비중 %", 5, 50, 25, 5)

f = st.file_uploader("주가 CSV (Date,Open,High,Low,Close,Volume)", type="csv")

def indicators(d):
    d=d.copy()
    for n in [5,20,60,112,224]:
        d[f"MA{n}"]=d.Close.rolling(n).mean()
    d["V20"]=d.Volume.rolling(20).mean()
    d["STD40"]=d.Close.rolling(40).std()
    d["MID40"]=d.Close.rolling(40).mean()
    d["BBU"]=d.MID40+2*d.STD40
    d["BBL"]=d.MID40-2*d.STD40
    d["HH20"]=d.High.rolling(20).max().shift(1)
    d["LL20"]=d.Low.rolling(20).min().shift(1)
    d["ATR"]=pd.concat([
        d.High-d.Low,
        (d.High-d.Close.shift()).abs(),
        (d.Low-d.Close.shift()).abs()
    ],axis=1).max(axis=1).rolling(14).mean()
    return d

def signals(d):
    d=d.copy()
    d["trend"]=(d.MA20>d.MA60)&(d.Close>d.MA20)
    d["longtrend"]=(d.MA60>d.MA112)&(d.MA112>d.MA224)
    d["volume"]=(d.Volume>d.V20*1.5)
    d["breakout"]=(d.Close>d.HH20)
    d["bbbreak"]=(d.Close>d.BBU)&(d.Close.shift(1)<=d.BBU.shift(1))
    d["pullback"]=(d.MA20>d.MA60)&(d.Low<=d.MA20*1.015)&(d.Close>d.MA20)
    # W-bottom approximation: two recent lows close in price + neckline breakout proxy
    low10=d.Low.rolling(10).min()
    priorlow=low10.shift(10)
    d["doublebottom"]=(abs(low10/priorlow-1)<0.04)&(d.Close>d.High.rolling(10).max().shift(1))
    d["score"]=(
        d.trend.astype(int)*20 +
        d.longtrend.astype(int)*20 +
        d.volume.astype(int)*20 +
        d.breakout.astype(int)*20 +
        d.pullback.astype(int)*10 +
        d.bbbreak.astype(int)*10
    )
    return d

def choose(d, mode, cut):
    if mode=="종합 점수형": return d.score>=cut
    if mode=="N자·눌림목": return d.pullback & d.volume
    if mode=="추세전환 스윙": return d.trend & d.breakout & d.volume
    if mode=="볼린저 돌파": return d.bbbreak & d.volume
    if mode=="중장기 추세": return d.longtrend & d.trend & d.breakout
    return d.doublebottom & d.volume

def bt(d, sig, capital, stop, take, pos):
    cash=float(capital); q=0; entry=0; trades=[]; curve=[]
    buyfee=.00015; sellfee=.00015; tax=.002; slip=.0005
    for i,r in d.iterrows():
        p=float(r.Close)
        if q==0 and bool(sig.iloc[i]):
            budget=cash*pos/100
            ep=p*(1+slip); qty=int(budget//(ep*(1+buyfee)))
            if qty:
                cash-=qty*ep*(1+buyfee); q=qty; entry=ep; ed=r.Date
        elif q:
            ret=(p/entry-1)*100
            reason=None
            if ret<=-stop: reason="손절"
            elif ret>=take: reason="익절"
            elif p<r.MA20 and ret>0: reason="추세이탈"
            if reason:
                xp=p*(1-slip); proceeds=q*xp*(1-sellfee-tax)
                pnl=proceeds-q*entry*(1+buyfee)
                cash+=proceeds
                trades.append([ed,r.Date,q,entry,xp,pnl,reason])
                q=0
        curve.append([r.Date,cash+q*p])
    if q:
        r=d.iloc[-1]; p=float(r.Close); xp=p*(1-slip)
        proceeds=q*xp*(1-sellfee-tax); pnl=proceeds-q*entry*(1+buyfee)
        cash+=proceeds; trades.append([ed,r.Date,q,entry,xp,pnl,"기간종료"])
        curve[-1]=[r.Date,cash]
    t=pd.DataFrame(trades,columns=["진입일","청산일","수량","진입가","청산가","손익","사유"])
    e=pd.DataFrame(curve,columns=["Date","Equity"])
    e["Peak"]=e.Equity.cummax(); e["DD"]=e.Equity/e.Peak-1
    wins=(t.손익>0).sum() if len(t) else 0
    gains=t.loc[t.손익>0,"손익"].sum() if len(t) else 0
    losses=-t.loc[t.손익<0,"손익"].sum() if len(t) else 0
    return cash,(cash/capital-1)*100,e.DD.min()*100,(wins/len(t)*100 if len(t) else 0),(gains/losses if losses>0 else 0),t,e

if f:
    d=pd.read_csv(f)
    req={"Date","Open","High","Low","Close","Volume"}
    if not req.issubset(d.columns):
        st.error("필수 열: Date, Open, High, Low, Close, Volume")
    else:
        d["Date"]=pd.to_datetime(d.Date); d=d.sort_values("Date").reset_index(drop=True)
        d=signals(indicators(d)); sig=choose(d,mode,score_cut)
        st.write(f"매수 후보 신호: **{int(sig.sum())}회**")
        if st.button("▶ 전략 테스트",type="primary"):
            final,ret,mdd,wr,pf,t,e=bt(d,sig,capital,stop,take,pos)
            a,b=st.columns(2); a.metric("최종자산",f"{final:,.0f}원"); b.metric("수익률",f"{ret:.2f}%")
            c,d2=st.columns(2); c.metric("MDD",f"{mdd:.2f}%"); d2.metric("승률",f"{wr:.1f}%")
            st.metric("Profit Factor",f"{pf:.2f}")
            st.line_chart(e.set_index("Date")[["Equity"]])
            st.subheader("매매내역")
            st.dataframe(t,use_container_width=True,hide_index=True)
            if len(t):
                st.download_button("매매내역 저장",t.to_csv(index=False).encode("utf-8-sig"),
                                   "v3_trades.csv","text/csv",use_container_width=True)
            st.subheader("최근 신호 점수")
            show=d.loc[sig,["Date","Close","score"]].tail(20)
            st.dataframe(show,use_container_width=True,hide_index=True)
else:
    st.info("CSV를 올리면 전략을 바로 테스트할 수 있습니다.")

st.divider()
st.caption("사진 자료의 검색식을 그대로 복제하지 않고, 반복되는 핵심 조건을 수치화한 연구용 버전입니다. 실계좌 주문 기능 없음.")

# NH Auto Trader v6 FINAL

모바일에서 보기 쉽게 다시 구성한 NH투자증권 NAMUH PLUG 실전 대시보드입니다.

## 핵심 UI
- 자동매매 시작
- 신규매수 정지
- 긴급 전체정지
- 실행중 / 정지중 현황판
- 실전 계좌 연결
- 총자산 / 주문가능금액 / 평가금액 / 평가손익
- 보유종목 상세
- 자동매매 금액/리스크 설정
- 관심종목 종합점수 전략 스캔
- "왜 샀나 / 왜 팔았나" 설명 기록
- 매매기록 CSV 다운로드
- 시스템 로그 CSV 다운로드
- 1분 화면 자동 새로고침

## 종합점수형 전략
- 5일선 > 20일선: +30
- 5일 수익률 > 0: +25
- 거래량 > 20일 평균 1.5배: +25
- 20일 고점 돌파: +20
- 기본 매수후보 기준: 70점

## Streamlit Secrets
실제 키/비밀번호는 GitHub에 올리지 않습니다.

```toml
NH_APP_KEY = "발급받은 APP KEY"
NH_APP_SECRET = "발급받은 APP SECRET"
APP_PASSWORD = "본인만 아는 앱 비밀번호"
```

## NH API
공식 Python SDK `nhplug` 사용:
- 계좌목록: `/n2/acctinfo`
- 잔고: `/krstock/inquiry/v1/balance`
- 일자별 시세: `/krstock/quote/v1/currentDaily`
- 현금 매수: `/krstock/order/v1/cashBuy`
- 현금 매도: `/krstock/order/v1/cashSell`

## 중요
Streamlit Community Cloud는 UI/조종석 용도로 사용합니다.
앱이 sleep/restart될 수 있으므로, 화면을 닫아도 계속 동작하는 완전 무인 자동매매 엔진은 다음 단계에서 별도 상시 worker로 분리해야 합니다.

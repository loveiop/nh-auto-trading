# NH Auto Trader v6

사용 편의성을 우선한 모바일 실전 계좌 대시보드입니다.

## 포함 기능
- 앱 비밀번호 로그인
- NH 실전 계좌(01/02) 연결
- 잔고/보유종목 실시간 조회
- 자동매매 시작 / 신규매수 정지 / 긴급 전체정지
- 총 운용한도 / 종목당 금액 / 최대 보유종목 / 일일 손실한도 / 손절 / 익절 / 트레일링 / 거래횟수 / 쿨다운 설정
- 관심종목 전략 스캔
- 종합점수형 전략: 추세 30 + 모멘텀 25 + 거래량 25 + 20일 돌파 20
- 수동 실전 현금 매수/매도
- 앱 동작 로그
- 계좌번호 마스킹 / 키 비노출

## Streamlit Secrets
기존 값은 유지하고 실제 값을 넣습니다.

```toml
NH_APP_KEY = "..."
NH_APP_SECRET = "..."
APP_PASSWORD = "..."
```

## 중요한 구조
이 Streamlit 앱은 조종석(UI)입니다.
Streamlit Community Cloud는 앱 sleep/restart가 있을 수 있으므로 24시간 상시 무인매매 엔진으로는 적합하지 않습니다.
v6에서 화면/전략/실전 API 연결을 검증한 다음, 다음 단계에서 동일 전략을 사용하는 상시 worker를 별도 서버에 올리는 구조가 안전합니다.

## NH 공식 API 사용
- 계좌목록: /n2/acctinfo
- 국내주식 잔고: /krstock/inquiry/v1/balance
- 일자별 시세: /krstock/quote/v1/currentDaily
- 현재가: /krstock/quote/v1/currentPrice
- 현금 매수: /krstock/order/v1/cashBuy
- 현금 매도: /krstock/order/v1/cashSell

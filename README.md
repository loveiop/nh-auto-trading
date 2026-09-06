# NH 실전 자동매매 콘솔 v5

NH투자증권 NAMUH PLUG 운영 서버에 연결하여 실제 국내주식 현금 매수/매도 주문을 보낼 수 있는 버전입니다.

## Streamlit Secrets
```toml
NH_APP_KEY = "발급받은 APP KEY"
NH_APP_SECRET = "발급받은 APP SECRET"
APP_PASSWORD = "본인만 아는 앱 비밀번호"
MAX_ORDER_KRW = "250000"
```

## 안전장치
- 운영계좌(01/02)만 선택
- 계좌번호 마스킹
- 1회 주문금액 기본 25만원 제한
- 실제주문 체크박스 + 확인문구 입력
- APP KEY/SECRET 비표시

주의: 주문 버튼을 누르면 실제 주문이 접수될 수 있습니다.

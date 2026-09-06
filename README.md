# NH 자동매매 연구실 v5

모바일 Streamlit + NH투자증권 NAMUH PLUG 연결용 안전 버전입니다.

## 파일
- `app.py` : 앱 본체
- `requirements.txt` : 설치 패키지
- `.gitignore` : 비밀키/로컬파일 커밋 방지
- `secrets.example.toml` : Streamlit Secrets 입력 예시

## Streamlit Secrets
실제 값은 GitHub 코드에 넣지 않습니다.

```toml
NH_APP_KEY = "발급받은 APP KEY"
NH_APP_SECRET = "발급받은 APP SECRET"
APP_PASSWORD = "본인만 아는 앱 비밀번호"
```

## v5 기능
- 앱 비밀번호 보호
- 모의투자 / 운영 조회전용 선택
- NH API 인증 및 계좌목록 확인
- 계좌번호 마스킹
- 국내주식 현재가 조회
- 100만원 기준 리스크 설정
- CSV 전략 점수 확인
- 실주문 API 차단

## 주의
v5에는 실제 매수/매도 주문 전송 코드가 없습니다.
먼저 모의투자 환경에서 인증·계좌·시세 조회가 정상인지 확인한 뒤 주문 기능을 단계적으로 붙이는 구조입니다.

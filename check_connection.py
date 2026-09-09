"""Read-only connection check. Does not submit, amend or cancel orders."""
from importlib.metadata import version
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from nhtrader.broker import NhBroker, safe_message
from nhtrader.core import now


def main():
    load_dotenv()
    for canonical, old in (("NHPLUG_APP_KEY","NH_APP_KEY"),("NHPLUG_APP_SECRET","NH_APP_SECRET")):
        if not os.getenv(canonical) and os.getenv(old):
            os.environ[canonical] = os.environ[old]
    report = {"checked_at": now().isoformat(), "checks": []}
    def check(name, fn):
        try:
            detail = fn()
            report["checks"].append(dict(name=name, status="확인", detail=detail))
            return detail
        except Exception as exc:
            report["checks"].append(dict(name=name, status="확인 필요", detail=safe_message(exc)))
            return None
    check("프로그램 버전",lambda: {name:version(name) for name in ("nhplug","streamlit","websocket-client")})
    broker = None
    try:
        broker = NhBroker()
    except Exception as exc:
        report["checks"].append(dict(name="연결값",status="설정 필요",detail=safe_message(exc)))
    if broker:
        account_list = []
        def accounts():
            account_list.extend(broker.accounts())
            return {"실전계좌수":len(account_list)}
        check("인증·계좌목록",accounts)
        selected = os.getenv("NHPLUG_DEFAULT_ACCOUNT")
        if not selected and len(account_list)==1:
            selected = str(account_list[0]["acct_no"])
        if selected:
            check("운영 계좌 선택",lambda: broker.select_account(selected) or "선택 확인")
            check("현금 잔고 응답",lambda: {"주문가능금액필드":isinstance(broker.balance()["cash"],int)})
            check("주문내역 조회",lambda: {"조회건수":len(broker.history(now().date().isoformat()))})
        elif account_list:
            report["checks"].append(dict(name="운영 계좌 선택",status="화면에서 선택",detail="여러 계좌가 있어 첫 계좌를 자동 선택하지 않았습니다."))
        check("KRX 현재가",lambda: {"현재가필드":"stck_prpr" in broker.quote("005930")})
    Path("connection_check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()


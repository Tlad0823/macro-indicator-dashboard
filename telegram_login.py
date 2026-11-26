"""
텔레그램 로그인 (처음 한 번만 실행)
------------------------------------------------------------
터미널에서:  python telegram_login.py

1) 휴대폰 번호를 국가번호 포함해 입력합니다.   예: +821012345678
2) 텔레그램 앱으로 온 인증 코드를 입력합니다.
3) 2단계 인증 비밀번호를 설정해 두었다면 그 비밀번호도 입력합니다.

성공하면 data 폴더에 telegram.session 파일이 생깁니다.
이 파일은 내 텔레그램 계정에 접속하는 열쇠이므로 다른 사람에게 주거나 인터넷에 올리지 마세요.
로그인을 해제하려면 이 파일을 지우면 됩니다.
"""

import sys

try:
    from telethon.sync import TelegramClient
except ImportError:
    print("telethon 패키지가 없습니다. 먼저  pip install telethon  을 실행하세요.")
    sys.exit(1)

import config
from telegram_brief import SESSION_PATH, channels_from_config

api_id = getattr(config, "TELEGRAM_API_ID", 0)
api_hash = getattr(config, "TELEGRAM_API_HASH", "")
if not api_id or not api_hash:
    print("config.py에 TELEGRAM_API_ID와 TELEGRAM_API_HASH를 먼저 입력하세요.")
    sys.exit(1)

SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
print("텔레그램에 로그인합니다. 안내에 따라 입력하세요.\n")
with TelegramClient(str(SESSION_PATH), int(api_id), api_hash) as client:
    client.start()   # 번호·인증 코드·(있으면) 2단계 비밀번호를 차례로 물어봄
    me = client.get_me()
    print(f"\n로그인 성공: {me.first_name or ''} (@{me.username or '사용자명 없음'})")
    for ch in channels_from_config():
        try:
            entity = client.get_entity(ch)
            print(f"  채널 확인: {getattr(entity, 'title', ch)} (@{ch})")
        except Exception as e:
            print(f"  채널 @{ch}을(를) 찾지 못했습니다: {e}")
print("\n이제 대시보드를 열고 '업데이트'를 누르면 텔레그램 브리핑이 나옵니다.")

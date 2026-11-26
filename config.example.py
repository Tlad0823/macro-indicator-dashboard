# ============================================================
#  설정 양식
#  이 파일을 복사해 이름을 config.py 로 바꾼 뒤 키를 입력하세요.
#  config.py 는 .gitignore 에 들어 있어 GitHub에 올라가지 않습니다.
# ============================================================

# FRED (미국 세인트루이스 연준) API 키 — https://fred.stlouisfed.org 에서 무료 발급
FRED_API_KEY = ""

# 한국은행 ECOS API 키 — https://ecos.bok.or.kr/api/ 에서 무료 발급
# 비워 두면 한국 지표는 FRED 대체 자료로 받습니다(일부는 갱신이 늦거나 받을 수 없음).
ECOS_API_KEY = ""

# 텔레그램 (선택) — https://my.telegram.org 에서 api_id, api_hash 발급
# 비워 두면 텔레그램 브리핑 없이 지표 대시보드만 동작합니다.
TELEGRAM_API_ID = 0               # 숫자, 따옴표 없이
TELEGRAM_API_HASH = ""
TELEGRAM_CHANNELS = ["채널주소"]   # t.me/ 뒤의 주소. 예: t.me/example → "example"

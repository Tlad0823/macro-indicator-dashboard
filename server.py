"""
경제 지표 대시보드 서버
------------------------------------------------------------
실행:  '대시보드 열기' 파일을 더블클릭 (또는 터미널에서 python server.py)
접속:  브라우저에서 http://localhost:5000  (실행하면 자동으로 열립니다)
대시보드 창을 모두 닫으면 AUTO_STOP_MINUTES 분 뒤 서버가 스스로 꺼집니다.

페이지의 '업데이트' 버튼을 누르면 이 서버가 FRED·ECOS에서 최신 값을 받아
직전 발표값과의 차이, 1년 추세를 계산해 페이지로 보내 줍니다.

필요한 패키지:  pip install flask requests pandas numpy
같은 폴더에 market_indicators.py, config.py, dashboard.html 이 있어야 합니다.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

# 창 없이(pythonw) 실행되면 출력할 곳이 없으므로 기록 파일로 보냄
if sys.stdout is None or sys.stderr is None:
    _log_dir = Path(__file__).resolve().parent / "data"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log = open(_log_dir / "server.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pandas as pd
from flask import Flask, jsonify, send_from_directory

import insights
import telegram_brief
import market_indicators as mi

# 클라우드에 올릴 때는 config.py 대신 환경 변수로 키를 넣을 수 있게 함
mi.config.FRED_API_KEY = os.environ.get("FRED_API_KEY", mi.config.FRED_API_KEY)
mi.config.ECOS_API_KEY = os.environ.get("ECOS_API_KEY", mi.config.ECOS_API_KEY)

RESULT_FILE = mi.DATA_DIR / "latest_result.json"
app = Flask(__name__)
update_lock = threading.Lock()

# 대시보드 창이 모두 닫히고 이 시간(분)이 지나면 서버 자동 종료. 0이면 끄지 않음
AUTO_STOP_MINUTES = float(os.environ.get("AUTO_STOP_MINUTES", 5))
last_seen = time.time()


def auto_stop_watcher():
    while True:
        time.sleep(15)
        idle = time.time() - last_seen
        if idle > AUTO_STOP_MINUTES * 60 and not update_lock.locked():
            print(f"[{datetime.now():%H:%M:%S}] 대시보드가 {AUTO_STOP_MINUTES:g}분 동안 열려 있지 않아 서버를 종료합니다.")
            os._exit(0)


@app.before_request
def mark_seen():
    global last_seen
    last_seen = time.time()


@app.route("/api/ping")
def ping():
    return jsonify(ok=True)


def _iso(ts):
    return None if ts is None else pd.Timestamp(ts).strftime("%Y-%m-%d")


def _num(v):
    return None if v is None or pd.isna(v) else round(float(v), 4)


def summarize(base, s, source, state):
    """변환된 시계열 하나로 화면에 보낼 결과(최신값, 직전값, 추세, 10년 위치)를 만듦"""
    latest, latest_date = s.iloc[-1], s.index[-1]
    prev, prev_date = mi.previous_value(s, base["compare"])
    recent, fitted, chg = mi.trend_1y(s)
    stale = (pd.Timestamp(date.today()) - latest_date).days > mi.STALE_DAYS[base["freq"]]
    is_new = bool(state) and state.get(base["key"]) != latest_date.isoformat()
    pos = mi.historical_position(s)

    return dict(
        base, ok=True, source=source,
        latest=_num(latest), latest_date=_iso(latest_date),
        prev=_num(prev), prev_date=_iso(prev_date),
        diff=None if prev is None else _num(latest - prev),
        trend=mi.trend_label(chg, recent), trend_chg=_num(chg),
        stale=bool(stale), is_new=bool(is_new),
        position=None if pos is None else dict(
            pct=pos["pct"], label=mi.position_label(pos["pct"]), years=pos["years"],
            min=_num(pos["min"]), max=_num(pos["max"]), avg=_num(pos["avg"]),
            min_date=_iso(pos["min_date"]), max_date=_iso(pos["max_date"])),
        series=[[_iso(d), _num(v)] for d, v in recent.items()],
        trend_line=None if fitted is None else
        [[_iso(fitted.index[0]), _num(fitted.iloc[0])],
         [_iso(fitted.index[-1]), _num(fitted.iloc[-1])]],
        _latest_key=latest_date.isoformat(),
        _series=s,
    )


def build_one(ind, start, state):
    base = dict(key=ind["key"], country=ind["country"], name=ind["name"],
                unit=ind["unit"], freq=ind["freq"], compare=ind["compare"])
    try:
        raw, source = mi.fetch_indicator(ind, start)
    except mi.SourceError as e:
        return dict(base, ok=False, error=str(e))
    except Exception as e:  # 예상하지 못한 오류도 페이지 전체를 멈추지 않게
        return dict(base, ok=False, error=f"알 수 없는 오류: {e}")

    raw.to_csv(mi.HISTORY_DIR / f"{ind['key']}.csv", header=["value"], encoding="utf-8-sig")
    s = mi.apply_transform(raw, ind["transform"], ind["freq"])
    if s.empty:
        return dict(base, ok=False, error="계산할 자료 부족")
    return summarize(base, s, source, state)


def build_derived(d, results, state):
    """한미 금리차처럼 이미 받은 두 지표로 계산하는 지표"""
    base = dict(key=d["key"], country=d["country"], name=d["name"], unit=d["unit"],
                freq=d["freq"], compare=d["compare"], note=d.get("note", ""))
    by_key = {r["key"]: r for r in results}
    a, b = (by_key.get(k) for k in d["parts"])
    missing = [r["name"] if r else k for k, r in zip(d["parts"], (a, b)) if not (r and r["ok"])]
    if missing:
        return dict(base, ok=False, error="계산에 필요한 지표를 불러오지 못함: " + ", ".join(missing))
    s = mi.combine_diff(a["_series"], b["_series"])
    if len(s) < 2:
        return dict(base, ok=False, error="계산할 자료 부족")
    return summarize(base, s, f"계산: {a['name']} − {b['name']}", state)


def run_update():
    for d in (mi.DATA_DIR, mi.HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)
    start = date.today() - timedelta(days=365 * mi.HISTORY_YEARS + 30)
    state = mi.load_state()
    # 여러 지표를 동시에 받아 시간을 줄임
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda ind: build_one(ind, start, state), mi.INDICATORS))
    results += [build_derived(d, results, state) for d in mi.DERIVED]

    new_state = dict(state)
    series = {}
    for r in results:
        if r["ok"]:
            new_state[r["key"]] = r.pop("_latest_key")
            series[r["key"]] = r.pop("_series")
    mi.save_state(new_state)

    payload = dict(
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        first_run=not state,
        has_ecos_key=bool(mi.config.ECOS_API_KEY),
        indicators=results,
        insights=insights.generate(results, series, first_run=not state),
        telegram=telegram_brief.build_brief(),
    )
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


@app.route("/")
def index():
    return send_from_directory(mi.BASE_DIR, "dashboard.html")


@app.route("/api/latest")
def latest():
    """마지막으로 업데이트한 결과(페이지를 열 때 바로 보여 주기용)"""
    if RESULT_FILE.exists():
        return app.response_class(RESULT_FILE.read_text(encoding="utf-8"),
                                  mimetype="application/json")
    return jsonify(dict(updated_at=None, indicators=[]))


@app.route("/api/update", methods=["POST"])
def update():
    if not update_lock.acquire(blocking=False):
        return jsonify(dict(error="이미 업데이트 중입니다. 잠시 후 다시 눌러 주세요.")), 409
    try:
        return jsonify(run_update())
    except Exception as e:
        return jsonify(dict(error=f"업데이트 실패: {e}")), 500
    finally:
        update_lock.release()


# ------------------------------------------------------------
# 새 발표 확인: 반영하지 않고 '새 데이터가 나온 지표가 몇 개인지'만 셈
# ------------------------------------------------------------
CHECK_WINDOW_DAYS = {"D": 45, "W": 60, "M": 200, "Q": 500}   # 확인할 때 받아 볼 최근 기간
check_lock = threading.Lock()


def latest_available(ind):
    start = date.today() - timedelta(days=CHECK_WINDOW_DAYS[ind["freq"]])
    raw, source = mi.fetch_indicator(ind, start)
    return raw.index[-1]


def run_check():
    state = mi.load_state()
    pending, errors = [], []

    def one(ind):
        try:
            latest = latest_available(ind)
        except Exception as e:
            return ind, None, str(e)
        return ind, latest, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        checked = list(pool.map(one, mi.INDICATORS))

    new_keys = set()
    for ind, latest, err in checked:
        if err:
            errors.append(dict(key=ind["key"], name=ind["name"], country=ind["country"], error=err))
            continue
        saved = state.get(ind["key"])
        if saved is None or latest > pd.Timestamp(saved):
            new_keys.add(ind["key"])
            pending.append(dict(key=ind["key"], name=ind["name"], country=ind["country"],
                                freq=ind["freq"], latest=_iso(latest),
                                saved=_iso(pd.Timestamp(saved)) if saved else None))
    # 한미 금리차는 재료 지표 중 하나라도 새 값이 있으면 갱신 대상
    for d in mi.DERIVED:
        if any(k in new_keys for k in d["parts"]):
            pending.append(dict(key=d["key"], name=d["name"], country=d["country"],
                                freq=d["freq"], latest=None, saved=None))
    return dict(checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                never_updated=not state, pending=pending, errors=errors)


@app.route("/api/check")
def check():
    if update_lock.locked():
        return jsonify(dict(busy=True))
    if not check_lock.acquire(blocking=False):
        return jsonify(dict(busy=True))
    try:
        return jsonify(run_check())
    except Exception as e:
        return jsonify(dict(error=f"확인 실패: {e}")), 500
    finally:
        check_lock.release()


if __name__ == "__main__":
    import socket
    port = int(os.environ.get("PORT", 5000))
    url = f"http://localhost:{port}"
    open_browser = not os.environ.get("NO_BROWSER")

    with socket.socket() as sock:
        already_running = sock.connect_ex(("127.0.0.1", port)) == 0
    if already_running:
        # 이미 켜져 있으면 새로 켜지 않고 대시보드만 엶
        print(f"서버가 이미 실행 중입니다. 대시보드를 엽니다: {url}")
        if open_browser:
            webbrowser.open(url)
        raise SystemExit(0)

    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 서버 시작")
    print(f"읽은 설정 파일: {Path(mi.config.__file__).resolve()}")
    print(f"FRED 키: {'인식됨' if mi.config.FRED_API_KEY else '없음'}   "
          f"ECOS 키: {'인식됨 (' + mi.config.ECOS_API_KEY[:4] + '…)' if mi.config.ECOS_API_KEY else '없음 ← config.py 확인'}")
    print(f"대시보드 주소: {url}")
    if AUTO_STOP_MINUTES > 0:
        print(f"대시보드 창을 모두 닫으면 {AUTO_STOP_MINUTES:g}분 뒤 서버가 자동으로 꺼집니다.")
        threading.Thread(target=auto_stop_watcher, daemon=True).start()
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # 기본은 이 컴퓨터에서만 접속 가능. 휴대폰 등 같은 와이파이 기기에서 보려면 HOST=0.0.0.0
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=False)

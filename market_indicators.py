"""
주요 경제 지표 업데이트 프로그램
------------------------------------------------------------
실행할 때마다 각 지표의 최신 값을 API에서 받아와서
  1) 직전 발표값과의 차이
  2) 최근 1년 추세선(선형 회귀)
을 계산하고, 표(콘솔·HTML)와 차트(PNG)로 보여 줍니다.

사용법
  python market_indicators.py              # 전체 업데이트 + 보고서 열기
  python market_indicators.py --no-open    # 보고서를 자동으로 열지 않음
  python market_indicators.py --ecos-items 901Y027   # ECOS 통계표의 항목 코드 확인

필요한 패키지
  pip install requests pandas numpy matplotlib
  (선택) pip install yfinance   # ECOS 키가 없을 때 코스피를 받기 위해 사용
"""

import argparse
import base64
import io
import json
import platform
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    import config
except ImportError:
    print("config.py 파일이 없습니다. 같은 폴더에 config.py를 두세요.")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
REPORT_DIR = BASE_DIR / "reports"
STATE_FILE = DATA_DIR / "last_state.json"

HISTORY_YEARS = 11      # 10년 위치 계산 + 전년 대비 계산(1년)을 위해 11년치를 받아 둠
POSITION_YEARS = 10     # 역사적 위치를 계산할 기간(최근 10년)
TREND_DAYS = 365        # 추세선 기간(1년)

# ------------------------------------------------------------
# 지표 목록
#   transform : 화면에 보여 줄 값의 형태
#       level    = 원자료 그대로
#       mom_pct  = 전월(전기) 대비 증감률 %
#       yoy_pct  = 전년 동기 대비 증감률 %
#       diff     = 직전 대비 증감(예: 비농업 고용 '월간 증가폭')
#       yoy_diff = 전년 동기 대비 증감(예: 한국 취업자 수 증감)
#   compare   : 'prev'   = 바로 앞 관측값과 비교
#               'change' = 값이 마지막으로 바뀌기 전 값과 비교(정책금리용)
#   sources   : 위에서부터 차례로 시도. 실패하면 다음 출처로 넘어감
# ------------------------------------------------------------
INDICATORS = [
    # ---------------- 미국 ----------------
    dict(key="US_GDP", country="미국", name="GDP 성장률(전기비 연율)", unit="%",
         freq="Q", transform="level", compare="prev",
         sources=[("fred", "A191RL1Q225SBEA")]),
    dict(key="US_RETAIL", country="미국", name="소매판매(전월비)", unit="%",
         freq="M", transform="mom_pct", compare="prev",
         sources=[("fred", "RSAFS")]),
    dict(key="US_INDPRO", country="미국", name="산업생산(전월비)", unit="%",
         freq="M", transform="mom_pct", compare="prev",
         sources=[("fred", "INDPRO")]),
    dict(key="US_CPI", country="미국", name="소비자물가지수(전년비)", unit="%",
         freq="M", transform="yoy_pct", compare="prev",
         sources=[("fred", "CPIAUCSL")]),
    dict(key="US_PCE", country="미국", name="개인소비지출 물가(전년비)", unit="%",
         freq="M", transform="yoy_pct", compare="prev",
         sources=[("fred", "PCEPI")]),
    dict(key="US_FFR", country="미국", name="연방기금금리(목표 상단)", unit="%",
         freq="D", transform="level", compare="change",
         sources=[("fred", "DFEDTARU")]),
    dict(key="US_10Y", country="미국", name="국채 10년물 금리", unit="%",
         freq="D", transform="level", compare="prev",
         sources=[("fred", "DGS10")]),
    dict(key="US_NFP", country="미국", name="비농업 고용(월간 증가폭)", unit="천 명",
         freq="M", transform="diff", compare="prev",
         sources=[("fred", "PAYEMS")]),
    dict(key="US_UNRATE", country="미국", name="실업률", unit="%",
         freq="M", transform="level", compare="prev",
         sources=[("fred", "UNRATE")]),
    dict(key="US_CLAIMS", country="미국", name="신규 실업수당 청구", unit="건",
         freq="W", transform="level", compare="prev",
         sources=[("fred", "ICSA")]),
    dict(key="US_VIX", country="미국", name="변동성지수(VIX)", unit="pt",
         freq="D", transform="level", compare="prev",
         sources=[("fred", "VIXCLS")]),
    dict(key="US_SPREAD", country="미국", name="장단기 금리차(10년-2년)", unit="%p",
         freq="D", transform="level", compare="prev",
         sources=[("fred", "T10Y2Y")]),
    dict(key="US_DOLLAR", country="미국", name="달러지수(광의 무역가중)", unit="pt",
         freq="D", transform="level", compare="prev",
         sources=[("fred", "DTWEXBGS")]),

    # ---------------- 한국 ----------------
    # ECOS 소스 형식: ("ecos", 통계표코드, 주기, 항목코드, [항목명 키워드])
    #   항목코드로 못 찾으면 항목명 키워드로 다시 찾습니다.
    dict(key="KR_GDP", country="한국", name="GDP 성장률(전기비)", unit="%",
         freq="Q", transform="level", compare="prev",
         sources=[("ecos", "200Y102", "Q", "10111", ["국내총생산"]),
                  ("fred", "NAEXKP01KRQ657S")]),
    dict(key="KR_EXPORT", country="한국", name="수출액(전년비)", unit="%",
         freq="M", transform="yoy_pct", compare="prev",
         sources=[("ecos", "901Y011", "M", "", ["수출"]),
                  ("fred", "XTEXVA01KRM667S"),
                  ("fred", "VALEXPKRM052N")]),
    dict(key="KR_CPI", country="한국", name="소비자물가지수(전년비)", unit="%",
         freq="M", transform="yoy_pct", compare="prev",
         sources=[("ecos", "901Y009", "M", "0", ["총지수"])]),
    dict(key="KR_BASE", country="한국", name="한국은행 기준금리", unit="%",
         freq="D", transform="level", compare="change",
         sources=[("ecos", "722Y001", "D", "0101000", ["기준금리"])]),
    dict(key="KR_10Y", country="한국", name="국고채 10년물 금리", unit="%",
         freq="D", transform="level", compare="prev",
         sources=[("ecos", "817Y002", "D", "010210000", ["국고채(10년)"]),
                  ("fred", "IRLTLT01KRM156N")]),
    dict(key="KR_EMP", country="한국", name="취업자 수(전년동월비 증감)", unit="천 명",
         freq="M", transform="yoy_diff", compare="prev",
         sources=[("ecos", "901Y027", "M", "", ["취업자"]),
                  ("fred", "LFEMTTTTKRM647S")]),
    dict(key="KR_UNRATE", country="한국", name="실업률", unit="%",
         freq="M", transform="level", compare="prev",
         sources=[("ecos", "901Y027", "M", "", ["실업률"]),
                  ("fred", "LRHUTTTTKRM156S")]),
    dict(key="KR_KRW", country="한국", name="원/달러 환율", unit="원",
         freq="D", transform="level", compare="prev",
         sources=[("ecos", "731Y001", "D", "0000001", ["원/미국달러"]),
                  ("fred", "DEXKOUS")]),
    dict(key="KR_KOSPI", country="한국", name="코스피", unit="pt",
         freq="D", transform="level", compare="prev",
         sources=[("ecos", "802Y001", "D", "0001000", ["KOSPI"]),
                  ("yfinance", "^KS11")]),
]

# ------------------------------------------------------------
# 계산 지표: 위에서 받은 두 지표의 차이(앞 지표 − 뒤 지표)
#   한미 금리차는 '한국 − 미국'. 음수면 미국 금리가 더 높은 '역전' 상태
# ------------------------------------------------------------
DERIVED = [
    dict(key="KRUS_BASE", country="한미", name="한미 기준금리차(한국−미국)", unit="%p",
         freq="D", compare="change", parts=("KR_BASE", "US_FFR"),
         note="미국은 연방기금금리 목표 상단 기준"),
    dict(key="KRUS_10Y", country="한미", name="한미 10년물 금리차(한국−미국)", unit="%p",
         freq="D", compare="prev", parts=("KR_10Y", "US_10Y"), note=""),
]


def combine_diff(a, b):
    """두 시계열의 차이(a − b). 날짜가 다르면 마지막 값을 이어 붙여 맞춤"""
    df = pd.concat([a.rename("a"), b.rename("b")], axis=1).sort_index().ffill().dropna()
    # 두 자료 중 하나라도 새 값이 있는 날짜만 남김(주말·휴일 중복 제거)
    return (df["a"] - df["b"]).round(4)


def historical_position(s, years=POSITION_YEARS):
    """최신값이 최근 N년 자료 안에서 어디쯤인지(0=최저, 100=최고)"""
    window = s[s.index >= s.index[-1] - pd.DateOffset(years=years)]
    if len(window) < 8:
        return None
    latest = window.iloc[-1]
    below = (window < latest).sum()
    equal = (window == latest).sum()
    pct = (below + 0.5 * equal) / len(window) * 100
    span = (window.index[-1] - window.index[0]).days / 365.25
    return dict(pct=round(float(pct), 1), min=float(window.min()), max=float(window.max()),
                min_date=window.idxmin(), max_date=window.idxmax(),
                years=round(span, 1), avg=float(window.mean()))


def position_label(pct):
    if pct is None:
        return ""
    if pct >= 90:
        return "매우 높음"
    if pct >= 70:
        return "높은 편"
    if pct > 30:
        return "보통"
    if pct > 10:
        return "낮은 편"
    return "매우 낮음"


# 이 기간보다 최신 관측값이 오래되면 '갱신 지연'으로 표시
STALE_DAYS = {"D": 10, "W": 21, "M": 80, "Q": 200}


# ============================================================
# 1. 데이터 받아오기
# ============================================================
class SourceError(Exception):
    pass


def fetch_fred(series_id, start):
    if not config.FRED_API_KEY:
        raise SourceError("FRED 키 없음")
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = dict(series_id=series_id, api_key=config.FRED_API_KEY,
                  file_type="json", observation_start=start.isoformat())
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise SourceError(f"FRED 응답 오류 {r.status_code}: {r.text[:120]}")
    obs = r.json().get("observations", [])
    s = pd.Series({pd.Timestamp(o["date"]): o["value"] for o in obs})
    s = pd.to_numeric(s, errors="coerce").dropna()   # FRED는 결측을 '.'로 줌
    if s.empty:
        raise SourceError("FRED 데이터 없음")
    return s.sort_index()


def _ecos_time_fmt(d, cycle):
    if cycle == "D":
        return d.strftime("%Y%m%d")
    if cycle == "M":
        return d.strftime("%Y%m")
    if cycle == "Q":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    return str(d.year)


def _ecos_parse_time(t, cycle):
    if cycle == "D":
        return pd.Timestamp(datetime.strptime(t, "%Y%m%d"))
    if cycle == "M":
        return pd.Timestamp(datetime.strptime(t, "%Y%m"))
    if cycle == "Q":
        y, q = t.split("Q")
        return pd.Timestamp(int(y), (int(q) - 1) * 3 + 1, 1)
    return pd.Timestamp(int(t), 1, 1)


def _ecos_rows(stat_code, cycle, start, end, item_code=""):
    path = (f"https://ecos.bok.or.kr/api/StatisticSearch/{config.ECOS_API_KEY}/json/kr/1/100000/"
            f"{stat_code}/{cycle}/{_ecos_time_fmt(start, cycle)}/{_ecos_time_fmt(end, cycle)}")
    if item_code:
        path += f"/{item_code}"
    r = requests.get(path, timeout=60)
    if r.status_code != 200:
        raise SourceError(f"ECOS 응답 오류 {r.status_code}")
    js = r.json()
    if "StatisticSearch" not in js:
        msg = js.get("RESULT", {}).get("MESSAGE", "알 수 없는 오류")
        return [], msg
    return js["StatisticSearch"].get("row", []), ""


def fetch_ecos(stat_code, cycle, item_code, keywords, start):
    if not config.ECOS_API_KEY:
        raise SourceError("ECOS 키 없음")
    end = date.today()
    rows, msg = ([], "")
    if item_code:
        rows, msg = _ecos_rows(stat_code, cycle, start, end, item_code)
    if not rows:
        # 항목코드로 못 찾으면 통계표 전체를 받아 항목명으로 찾기
        all_rows, msg = _ecos_rows(stat_code, cycle, start, end)
        matched = [r for r in all_rows
                   if all(k in (r.get("ITEM_NAME1") or "") for k in keywords)]
        if not matched:
            raise SourceError(f"ECOS {stat_code}에서 '{' '.join(keywords)}' 항목을 찾지 못함 ({msg})")
        # 가장 먼저 나온 항목 조합 하나만 사용(세부 분류가 여러 개일 때)
        first = matched[0]
        combo = tuple(first.get(f"ITEM_CODE{i}") for i in (1, 2, 3, 4))
        rows = [r for r in matched
                if tuple(r.get(f"ITEM_CODE{i}") for i in (1, 2, 3, 4)) == combo]
        print(f"      └ ECOS 항목 자동 선택: {first.get('ITEM_NAME1')} "
              f"{first.get('ITEM_NAME2') or ''} (코드 {first.get('ITEM_CODE1')})")
    s = pd.Series({_ecos_parse_time(r["TIME"], cycle): r["DATA_VALUE"] for r in rows})
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        raise SourceError("ECOS 데이터 없음")
    return s.sort_index()


def fetch_yfinance(ticker, start):
    try:
        import yfinance as yf
    except ImportError:
        raise SourceError("yfinance 미설치 (pip install yfinance)")
    df = yf.download(ticker, start=start.isoformat(), progress=False, auto_adjust=False)
    if df.empty:
        raise SourceError("yfinance 데이터 없음")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.dropna().astype(float)


def fetch_indicator(ind, start):
    """지표의 출처 목록을 차례로 시도해 첫 번째로 성공한 자료를 돌려줌"""
    errors = []
    for src in ind["sources"]:
        kind = src[0]
        try:
            if kind == "fred":
                return fetch_fred(src[1], start), f"FRED {src[1]}"
            if kind == "ecos":
                return fetch_ecos(src[1], src[2], src[3], src[4], start), f"ECOS {src[1]}"
            if kind == "yfinance":
                return fetch_yfinance(src[1], start), f"Yahoo {src[1]}"
        except SourceError as e:
            errors.append(f"{kind}: {e}")
        except requests.RequestException as e:
            errors.append(f"{kind}: 네트워크 오류 {e.__class__.__name__}")
    raise SourceError(" / ".join(errors))


# ============================================================
# 2. 계산: 변환, 직전값 비교, 1년 추세
# ============================================================
def periods_per_year(freq):
    return {"M": 12, "Q": 4, "W": 52}.get(freq)


def apply_transform(s, transform, freq):
    if transform == "level":
        return s
    if transform == "diff":
        return s.diff().dropna()
    if transform == "mom_pct":
        return (s.pct_change() * 100).dropna()
    if transform in ("yoy_pct", "yoy_diff"):
        # 날짜 기준으로 1년 전 값을 찾아 비교(누락 월이 있어도 안전)
        prev = s.copy()
        prev.index = prev.index + pd.DateOffset(years=1)
        prev = prev.reindex(s.index)
        out = (s / prev - 1) * 100 if transform == "yoy_pct" else s - prev
        return out.dropna()
    raise ValueError(transform)


def previous_value(s, compare):
    """비교 기준이 되는 '직전 발표값'과 그 날짜"""
    if len(s) < 2:
        return None, None
    latest = s.iloc[-1]
    if compare == "change":
        earlier = s.iloc[:-1][s.iloc[:-1] != latest]
        if earlier.empty:
            return s.iloc[-2], s.index[-2]
        return earlier.iloc[-1], earlier.index[-1]
    return s.iloc[-2], s.index[-2]


def trend_1y(s):
    """최근 1년 자료에 직선을 맞춰 1년 동안의 변화폭(기울기×365일)을 구함"""
    recent = s[s.index >= s.index[-1] - pd.Timedelta(days=TREND_DAYS)]
    if len(recent) < 3:
        return recent, None, None
    x = (recent.index - recent.index[0]).days.values.astype(float)
    slope, intercept = np.polyfit(x, recent.values, 1)
    fitted = pd.Series(slope * x + intercept, index=recent.index)
    return recent, fitted, slope * 365


def trend_label(change_per_year, recent):
    if change_per_year is None:
        return "자료 부족"
    spread = recent.std()
    if spread == 0 or abs(change_per_year) < 0.25 * spread:
        return "횡보"
    return "상승" if change_per_year > 0 else "하락"


def fmt_num(v, unit):
    if v is None or pd.isna(v):
        return "-"
    if unit in ("건",):
        return f"{v:,.0f}"
    if abs(v) >= 1000:
        return f"{v:,.1f}"
    return f"{v:,.2f}"


def fmt_date(ts, freq):
    if ts is None:
        return "-"
    if freq == "Q":
        return f"{ts.year}년 {(ts.month - 1) // 3 + 1}분기"
    if freq == "M":
        return f"{ts.year}.{ts.month:02d}"
    return ts.strftime("%Y.%m.%d")


# ============================================================
# 3. 저장(이전 실행과 비교해 '새 발표' 판단)
# ============================================================
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# 4. 출력: 차트, HTML 보고서
# ============================================================
def set_korean_font():
    system = platform.system()
    if system == "Windows":
        plt.rcParams["font.family"] = "Malgun Gothic"
    elif system == "Darwin":
        plt.rcParams["font.family"] = "AppleGothic"
    else:
        from matplotlib import font_manager
        installed = {f.name for f in font_manager.fontManager.ttflist}
        for name in ("NanumGothic", "Noto Sans CJK KR", "Noto Sans KR"):
            if name in installed:
                plt.rcParams["font.family"] = name
                break
    plt.rcParams["axes.unicode_minus"] = False


LINE_COLOR = "#2a6fdb"
TREND_COLOR = "#7a7a7a"
INK = "#1f1f1f"
MUTED = "#6b6b6b"


def make_chart(results):
    set_korean_font()
    ok = [r for r in results if r["ok"]]
    n = len(ok)
    cols = 4
    rows = max(1, int(np.ceil(n / cols)))
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 4.6, rows * 3.1))
    axes = np.array(axes).reshape(-1)
    for ax, r in zip(axes, ok):
        recent, fitted = r["recent"], r["fitted"]
        step = "steps-post" if r.get("compare") == "change" else "default"
        ax.plot(recent.index, recent.values, color=LINE_COLOR, linewidth=1.8, drawstyle=step)
        if fitted is not None:
            ax.plot(fitted.index, fitted.values, color=TREND_COLOR,
                    linewidth=1.4, linestyle="--")
        ax.scatter([recent.index[-1]], [recent.values[-1]], color=LINE_COLOR, s=28, zorder=3)
        diff_txt = "" if r["diff"] is None else f"  (직전 대비 {r['diff']:+,.2f})"
        ax.text(0.0, 1.13, f"[{r['country']}] {r['name']}", transform=ax.transAxes,
                fontsize=10.5, color=INK, va="bottom")
        ax.text(0.0, 1.03, f"최신 {fmt_num(r['latest'], r['unit'])} {r['unit']}{diff_txt}",
                transform=ax.transAxes, fontsize=8.5, color=MUTED, va="bottom")
        ax.grid(axis="y", color="#e6e6e6", linewidth=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#cccccc")
        ax.tick_params(labelsize=7.5, colors=MUTED)
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%y.%m"))
    for ax in axes[n:]:
        ax.axis("off")
    fig.suptitle("최근 1년 추이 (실선: 실제값, 점선: 1년 추세선)", fontsize=13,
                 color=INK, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.97), h_pad=3.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def make_html(results, png_bytes, run_time):
    def row(r):
        if not r["ok"]:
            return (f"<tr class='err'><td>{r['country']}</td><td>{r['name']}</td>"
                    f"<td colspan='7'>불러오기 실패: {r['error']}</td></tr>")
        badge = ""
        if r["is_new"]:
            badge += "<span class='new'>새 발표</span>"
        if r["stale"]:
            badge += "<span class='stale'>갱신 지연</span>"
        d = r["diff"]
        trend_txt = "" if r["trend_chg"] is None else f"1년간 {r['trend_chg']:+,.2f}"
        arrow = "" if d is None or d == 0 else ("▲ " if d > 0 else "▼ ")
        return (f"<tr><td>{r['country']}</td><td>{r['name']} {badge}</td>"
                f"<td>{fmt_date(r['latest_date'], r['freq'])}</td>"
                f"<td class='num'>{fmt_num(r['latest'], r['unit'])}</td>"
                f"<td class='num'>{fmt_num(r['prev'], r['unit'])}"
                f"<div class='sub'>{fmt_date(r['prev_date'], r['freq'])}</div></td>"
                f"<td class='num'>{arrow}{'-' if d is None else f'{d:+,.2f}'}</td>"
                f"<td>{r['unit']}</td>"
                f"<td>{r['trend']}<div class='sub'>{trend_txt}</div></td>"
                f"<td class='sub'>{r['source']}</td></tr>")

    rows_html = "\n".join(row(r) for r in results)
    img = base64.b64encode(png_bytes).decode()
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>경제 지표 업데이트 {run_time:%Y-%m-%d %H:%M}</title>
<style>
 body {{ font-family: 'Malgun Gothic','Apple SD Gothic Neo',sans-serif; margin: 28px; color:#1f1f1f; }}
 h1 {{ font-size: 22px; margin-bottom: 4px; }}
 .meta {{ color:#6b6b6b; font-size: 13px; margin-bottom: 18px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
 th, td {{ border-bottom: 1px solid #e6e6e6; padding: 7px 9px; text-align: left; vertical-align: top; }}
 th {{ background: #f6f6f6; font-weight: 600; }}
 td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
 .sub {{ color:#8a8a8a; font-size: 11.5px; }}
 .new {{ background:#e8f0fe; color:#1a4fb4; border-radius:4px; padding:1px 6px; font-size:11px; margin-left:4px; }}
 .stale {{ background:#fff4e0; color:#8a5a00; border-radius:4px; padding:1px 6px; font-size:11px; margin-left:4px; }}
 tr.err td {{ color:#a33; }}
 img {{ max-width: 100%; margin-top: 26px; }}
</style></head><body>
<h1>주요 경제 지표 업데이트</h1>
<div class="meta">실행 시각 {run_time:%Y-%m-%d %H:%M} · '직전값'은 바로 앞 발표값
(정책금리는 마지막으로 바뀌기 전 금리) · '1년 추세'는 최근 1년 자료에 맞춘 직선의 1년간 변화폭</div>
<table>
<tr><th>국가</th><th>지표</th><th>기준 시점</th><th>최신값</th><th>직전값</th>
<th>차이</th><th>단위</th><th>1년 추세</th><th>출처</th></tr>
{rows_html}
</table>
<img src="data:image/png;base64,{img}" alt="지표별 최근 1년 추이 차트">
</body></html>"""


# ============================================================
# 5. 실행
# ============================================================
def run(open_report=True):
    for d in (DATA_DIR, HISTORY_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    start = date.today() - timedelta(days=365 * HISTORY_YEARS + 30)
    state = load_state()
    new_state = dict(state)
    results = []

    print(f"\n경제 지표 업데이트 시작 ({len(INDICATORS)}개)\n")
    for ind in INDICATORS:
        label = f"[{ind['country']}] {ind['name']}"
        print(f"  · {label} ...", end=" ", flush=True)
        base = dict(key=ind["key"], country=ind["country"], name=ind["name"],
                    unit=ind["unit"], freq=ind["freq"], compare=ind["compare"])
        try:
            raw, source = fetch_indicator(ind, start)
        except SourceError as e:
            print("실패")
            results.append(dict(base, ok=False, error=str(e)))
            continue

        raw.to_csv(HISTORY_DIR / f"{ind['key']}.csv", header=["value"], encoding="utf-8-sig")
        s = apply_transform(raw, ind["transform"], ind["freq"])
        if s.empty:
            print("실패(계산할 자료 부족)")
            results.append(dict(base, ok=False, error="계산할 자료 부족"))
            continue

        latest, latest_date = s.iloc[-1], s.index[-1]
        prev, prev_date = previous_value(s, ind["compare"])
        diff = None if prev is None else latest - prev
        recent, fitted, chg = trend_1y(s)
        # 월·분기 자료의 날짜는 '기간 시작일'(예: 10월 자료 = 10월 1일)이라 기준을 넉넉히 둠
        stale = (pd.Timestamp(date.today()) - latest_date).days > STALE_DAYS[ind["freq"]]
        # 지난 실행 때 저장한 최신 날짜와 다르면 '새 발표'
        is_new = bool(state) and state.get(ind["key"]) != latest_date.isoformat()
        new_state[ind["key"]] = latest_date.isoformat()

        results.append(dict(base, ok=True, source=source, latest=latest,
                            latest_date=latest_date, prev=prev, prev_date=prev_date,
                            diff=diff, recent=recent, fitted=fitted, trend_chg=chg,
                            trend=trend_label(chg, recent), stale=stale, is_new=is_new))
        tag = " (새 발표)" if is_new else ""
        print(f"완료  {fmt_date(latest_date, ind['freq'])}  "
              f"{fmt_num(latest, ind['unit'])} {ind['unit']}"
              f"  직전 대비 {'-' if diff is None else f'{diff:+,.2f}'}{tag}")

    save_state(new_state)

    # 콘솔 요약
    print("\n" + "=" * 70)
    first_run = not state
    new_list = [r for r in results if r["ok"] and r["is_new"]]
    if first_run:
        print("첫 실행입니다. 다음 실행부터 새로 발표된 지표를 따로 알려 드립니다.")
    elif new_list:
        print("지난 실행 이후 새로 반영된 지표:")
        for r in new_list:
            print(f"  - [{r['country']}] {r['name']}: {fmt_num(r['latest'], r['unit'])} "
                  f"{r['unit']} (직전 대비 {r['diff']:+,.2f})")
    else:
        print("지난 실행 이후 새로 발표된 지표가 없습니다.")
    fails = [r for r in results if not r["ok"]]
    if fails:
        print("\n불러오지 못한 지표:")
        for r in fails:
            print(f"  - [{r['country']}] {r['name']}: {r['error']}")
    stale_list = [r for r in results if r["ok"] and r["stale"]]
    if stale_list:
        print("\n갱신이 늦은 지표(출처 자료가 오래됨):")
        for r in stale_list:
            print(f"  - [{r['country']}] {r['name']}: 최신 {fmt_date(r['latest_date'], r['freq'])} ({r['source']})")

    # 보고서
    if not any(r["ok"] for r in results):
        print("\n받아온 지표가 없어 보고서를 만들지 않았습니다. 인터넷 연결과 API 키를 확인하세요.")
        return
    run_time = datetime.now()
    png = make_chart(results)
    html = make_html(results, png, run_time)
    report = REPORT_DIR / f"report_{run_time:%Y%m%d_%H%M}.html"
    report.write_text(html, encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(html, encoding="utf-8")
    (REPORT_DIR / f"chart_{run_time:%Y%m%d_%H%M}.png").write_bytes(png)
    print(f"\n보고서 저장: {report}")
    if open_report:
        webbrowser.open(report.as_uri())


def list_ecos_items(stat_code):
    """ECOS 통계표에 어떤 항목(코드)이 있는지 출력"""
    if not config.ECOS_API_KEY:
        print("config.py에 ECOS_API_KEY를 먼저 입력하세요.")
        return
    url = f"https://ecos.bok.or.kr/api/StatisticItemList/{config.ECOS_API_KEY}/json/kr/1/1000/{stat_code}"
    js = requests.get(url, timeout=30).json()
    rows = js.get("StatisticItemList", {}).get("row", [])
    if not rows:
        print("항목을 찾지 못했습니다:", js.get("RESULT", {}).get("MESSAGE"))
        return
    print(f"\n{stat_code} {rows[0].get('STAT_NAME')}")
    for r in rows:
        print(f"  {r.get('GRP_NAME') or '':10s} {r.get('ITEM_CODE'):12s} {r.get('ITEM_NAME')}  "
              f"[{r.get('CYCLE')}] {r.get('START_TIME')}~{r.get('END_TIME')}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="주요 경제 지표 업데이트")
    p.add_argument("--no-open", action="store_true", help="보고서를 브라우저로 열지 않음")
    p.add_argument("--ecos-items", metavar="통계표코드", help="ECOS 통계표의 항목 목록 보기")
    a = p.parse_args()
    if a.ecos_items:
        list_ecos_items(a.ecos_items)
    else:
        run(open_report=not a.no_open)

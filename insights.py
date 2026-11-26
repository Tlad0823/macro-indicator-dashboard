"""
경제 지표 해설(규칙 기반)
------------------------------------------------------------
업데이트 결과를 정해 둔 기준에 비춰 보고, 기준에 걸린 것만 해설 문구로 만듭니다.

  - 이번 업데이트에서 주목할 점 : 새로 발표된 지표에서 생긴 변화
  - 현재 이슈가 될 만한 점      : 발표 시점과 상관없이 지금 계속 유효한 신호

기준값은 아래 THRESHOLDS 에서 바꿀 수 있습니다.
해설은 숫자 규칙으로 만든 참고 문구이며, 뉴스 맥락이나 시장 예상치는 반영하지 않습니다.
"""

import pandas as pd

# ============================================================
# 기준값 (여기 숫자만 바꾸면 해설 기준이 바뀝니다)
# ============================================================
THRESHOLDS = {
    "inflation_target": 2.0,       # 물가 목표(%) — 미국 연준·한국은행 모두 2%
    "inflation_rise_months": 2,    # 몇 달 연속 오르면 '둔화 멈춤'으로 볼지
    "sahm_trigger": 0.5,           # 삼 법칙: 실업률 3개월 평균이 최근 12개월 최저보다 이만큼(%p) 높으면 신호
    "sahm_watch": 0.3,             # 이 수준부터는 '주의 구간'으로 안내
    "vix_caution": 20,             # 변동성지수: 이 이상이면 불안 확대
    "vix_alert": 30,               # 이 이상이면 높은 불안
    "unusual_multiple": 2.0,       # 이번 변화폭이 최근 1년 평균 변화폭의 몇 배 이상이면 '이례적'
    "reversal_min_multiple": 0.5,  # 방향 전환으로 볼 최소 변화폭(평균 변화폭의 배수)
    "policy_recent_days": 45,      # 정책금리가 이 기간 안에 바뀌었으면 '최근 변경'으로 안내
    "stagflation_lookback": 3,     # 물가·고용을 몇 달 전과 비교할지
    "unemployment_rise": 0.2,      # 실업률이 이만큼(%p) 이상 오르면 '고용 둔화'
    "inflation_rise_min": 0.1,     # 물가가 이만큼(%p) 이상 올라야 '상승'으로 봄
    "krus_gap_alert": -1.5,        # 한미 기준금리차(한국−미국)가 이 값 이하면 '관심'
    "position_extreme": 5,         # 최근 10년 중 상위·하위 이 %(백분위) 안이면 '10년 최고·최저권'
}

INFLATION_KEYS = {"US_PCE": "미국 개인소비지출 물가", "US_CPI": "미국 소비자물가",
                  "KR_CPI": "한국 소비자물가"}
POLICY_KEYS = {"US_FFR": ("미국 연준이", "연방기금금리 목표 상단을"),
               "KR_BASE": ("한국은행이", "기준금리를")}

LEVEL_ORDER = {"주의": 0, "관심": 1, "참고": 2}


def _f(v, d=2):
    return f"{v:,.{d}f}"


def _pp(v):
    return f"{v:+.2f}%p"


def _item(level, key, info, title, text, basis):
    return dict(level=level, key=key, country=info["country"] if info else "",
                indicator=info["name"] if info else "", title=title, text=text, basis=basis)


def _mean_abs_change(s, days=365):
    recent = s[s.index >= s.index[-1] - pd.Timedelta(days=days)]
    ch = recent.diff().abs().dropna()
    ch = ch[ch > 0]
    return None if ch.empty else float(ch.mean())


# ------------------------------------------------------------
# 규칙 1. 물가 목표
# ------------------------------------------------------------
def rule_inflation(S, info, new_keys):
    out_new, out_now = [], []
    target, n = THRESHOLDS["inflation_target"], THRESHOLDS["inflation_rise_months"]
    for key, label in INFLATION_KEYS.items():
        s = S.get(key)
        if s is None or len(s) < n + 1:
            continue
        latest = s.iloc[-1]
        last = s.iloc[-(n + 1):]
        rising = all(last.diff().dropna() >= 0.05)   # 0.05%p 미만 변화는 보합으로 봄
        if latest > target and rising:
            item = _item("주의", key, info.get(key), "물가 둔화 흐름이 멈췄습니다",
                         f"{label} 상승률이 목표({_f(target, 1)}%)보다 높은 상태에서 {n}개월 연속 올랐습니다. "
                         "금리 인하 기대가 약해질 수 있습니다.",
                         f"최근 {n + 1}개월: " + " → ".join(_f(v) for v in last.values) + "%")
            (out_new if key in new_keys else out_now).append(item)
        elif latest > target + 0.5:
            out_now.append(_item("참고", key, info.get(key), "물가가 목표보다 높은 수준입니다",
                                 f"{label} 상승률이 목표({_f(target, 1)}%)보다 {_f(latest - target)}%p 높습니다.",
                                 f"최신 {_f(latest)}%"))
        elif latest <= target and key in new_keys and len(s) >= 2 and s.iloc[-2] > target:
            out_new.append(_item("관심", key, info.get(key), "물가가 목표 수준까지 내려왔습니다",
                                 f"{label} 상승률이 목표({_f(target, 1)}%) 이하로 내려왔습니다. "
                                 "금리 인하 여건이 넓어질 수 있습니다.",
                                 f"{_f(s.iloc[-2])}% → {_f(latest)}%"))
    return out_new, out_now


# ------------------------------------------------------------
# 규칙 2. 장단기 금리 역전
# ------------------------------------------------------------
def rule_yield_curve(S, info, new_keys):
    out_new, out_now = [], []
    s = S.get("US_SPREAD")
    if s is None or len(s) < 2:
        return out_new, out_now
    latest, prev = s.iloc[-1], s.iloc[-2]
    inf = info.get("US_SPREAD")
    if prev < 0 <= latest:
        out_new.append(_item("주의", "US_SPREAD", inf, "장단기 금리 역전이 풀렸습니다",
                             "10년물 금리가 다시 2년물보다 높아졌습니다. 과거에는 역전이 풀린 뒤에 "
                             "경기 둔화가 실제로 나타난 경우가 많아 주의 깊게 볼 시점입니다.",
                             f"{_pp(prev)} → {_pp(latest)}"))
    elif prev >= 0 > latest:
        out_new.append(_item("주의", "US_SPREAD", inf, "장단기 금리가 역전됐습니다",
                             "2년물 금리가 10년물보다 높아졌습니다. 장단기 금리 역전은 "
                             "대표적인 경기 침체 선행 신호로 꼽힙니다.",
                             f"{_pp(prev)} → {_pp(latest)}"))
    year = s[s.index >= s.index[-1] - pd.Timedelta(days=365)]
    if latest < 0:
        out_now.append(_item("주의", "US_SPREAD", inf, "장단기 금리 역전이 이어지고 있습니다",
                             "단기 금리가 장기 금리보다 높은 상태입니다. 시장이 앞으로 금리 인하와 "
                             "경기 둔화를 예상하고 있다는 뜻으로 해석됩니다.",
                             f"최신 {_pp(latest)}"))
    elif year.min() < 0:
        out_now.append(_item("관심", "US_SPREAD", inf, "최근 1년 안에 금리 역전이 풀렸습니다",
                             "역전이 풀린 직후는 과거 경기 둔화가 나타나기 시작한 시기와 겹치는 경우가 많았습니다.",
                             f"1년 최저 {_pp(year.min())}, 최신 {_pp(latest)}"))
    return out_new, out_now


# ------------------------------------------------------------
# 규칙 3. 삼 법칙(미국 실업률)
# ------------------------------------------------------------
def rule_sahm(S, info, new_keys):
    out_new, out_now = [], []
    s = S.get("US_UNRATE")
    if s is None or len(s) < 16:
        return out_new, out_now
    m3 = s.rolling(3).mean()
    gap = (m3 - m3.shift(1).rolling(12).min()).dropna()
    if len(gap) < 2:
        return out_new, out_now
    g, g_prev = gap.iloc[-1], gap.iloc[-2]
    trig, watch = THRESHOLDS["sahm_trigger"], THRESHOLDS["sahm_watch"]
    inf = info.get("US_UNRATE")
    basis = f"3개월 평균 실업률이 최근 12개월 최저보다 {_f(g)}%p 높음 (기준 {_f(trig, 1)}%p)"
    if g >= trig:
        item = _item("주의", "US_UNRATE", inf, "경기 침체 초기 신호(삼 법칙)에 도달했습니다",
                     "실업률 상승 속도가 과거 경기 침체 초입과 비슷한 수준입니다.", basis)
        if g_prev < trig and "US_UNRATE" in new_keys:
            out_new.append(item)
        else:
            out_now.append(item)
    elif g >= watch:
        out_now.append(_item("관심", "US_UNRATE", inf, "실업률이 경기 침체 신호 기준에 가까워지고 있습니다",
                             "아직 기준에는 못 미치지만 고용 둔화 속도를 지켜볼 구간입니다.", basis))
    return out_new, out_now


# ------------------------------------------------------------
# 규칙 4. 변동성지수
# ------------------------------------------------------------
def rule_vix(S, info, new_keys):
    out_new, out_now = [], []
    s = S.get("US_VIX")
    if s is None or len(s) < 2:
        return out_new, out_now
    latest, prev = s.iloc[-1], s.iloc[-2]
    c, a = THRESHOLDS["vix_caution"], THRESHOLDS["vix_alert"]
    inf = info.get("US_VIX")
    for line, title, level in ((a, "변동성지수가 높은 불안 구간에 들어섰습니다", "주의"),
                               (c, "변동성지수가 불안 확대 구간에 들어섰습니다", "관심")):
        if prev < line <= latest:
            out_new.append(_item(level, "US_VIX", inf, title,
                                 f"변동성지수가 {line}을 넘었습니다. 주식시장의 불확실성이 커졌다는 뜻입니다.",
                                 f"{_f(prev)} → {_f(latest)}"))
            break
    if latest >= a:
        out_now.append(_item("주의", "US_VIX", inf, "시장 불안이 매우 큰 상태입니다",
                             f"변동성지수가 {a} 이상입니다. 급격한 가격 변동이 나타나기 쉬운 구간입니다.",
                             f"최신 {_f(latest)}"))
    elif latest >= c:
        out_now.append(_item("관심", "US_VIX", inf, "시장 불안이 커진 상태입니다",
                             f"변동성지수가 {c} 이상입니다.", f"최신 {_f(latest)}"))
    return out_new, out_now


# ------------------------------------------------------------
# 규칙 5. 정책금리 변경
# ------------------------------------------------------------
def rule_policy(S, info, new_keys):
    out_new, out_now = [], []
    for key, (who, label) in POLICY_KEYS.items():
        s = S.get(key)
        if s is None or len(s) < 2:
            continue
        latest = s.iloc[-1]
        changed = s[s != latest]
        if changed.empty:
            continue
        before = changed.iloc[-1]
        change_date = s[s.index > changed.index[-1]].index[0]
        days = (s.index[-1] - change_date).days
        move = latest - before
        verb = "올렸습니다" if move > 0 else "내렸습니다"
        if days <= THRESHOLDS["policy_recent_days"]:
            out_new.append(_item("주의", key, info.get(key), f"{who} 금리를 {verb}",
                                 f"{label} {_f(before)}%에서 {_f(latest)}%로 "
                                 f"{_f(abs(move))}%p {'인상' if move > 0 else '인하'}했습니다.",
                                 f"{change_date:%Y.%m.%d} 변경: {_f(before)}% → {_f(latest)}%"))
        else:
            months = days // 30
            out_now.append(_item("참고", key, info.get(key), f"{who[:-1]} 금리 동결 {months}개월째",
                                 f"마지막 변경({change_date:%Y.%m}) 이후 {label} {_f(latest)}%로 유지하고 있습니다.",
                                 f"직전 변경: {_f(before)}% → {_f(latest)}%"))
    return out_new, out_now


# ------------------------------------------------------------
# 규칙 6·7. 이례적 변화, 방향 전환 (모든 지표 공통)
# ------------------------------------------------------------
def rule_change_patterns(S, info, new_keys, results, skip=()):
    out_new = []
    by_key = {r["key"]: r for r in results if r.get("ok")}
    for key in new_keys:
        if key in skip:   # 이미 다른 규칙으로 해설한 지표는 겹치지 않게 건너뜀
            continue
        s, r = S.get(key), by_key.get(key)
        if s is None or r is None or r.get("compare") == "change" or r.get("diff") is None:
            continue
        avg = _mean_abs_change(s)
        diff = r["diff"]
        if not avg or diff == 0:
            continue
        unit = r["unit"]
        if abs(diff) >= THRESHOLDS["unusual_multiple"] * avg:
            out_new.append(_item("관심", key, info.get(key), "평소보다 큰 폭으로 움직였습니다",
                                 f"이번 변화폭이 최근 1년 평균 변화폭의 {_f(abs(diff) / avg, 1)}배입니다.",
                                 f"이번 {diff:+,.2f}{unit}, 1년 평균 ±{_f(avg)}{unit}"))
            continue
        # 방향 전환: 일별 자료는 매일 오르내리므로 제외
        chg = r.get("trend_chg")
        if r["freq"] in ("M", "Q", "W") and chg and r.get("trend") in ("상승", "하락") \
                and diff * chg < 0 and abs(diff) >= THRESHOLDS["reversal_min_multiple"] * avg:
            out_new.append(_item("참고", key, info.get(key),
                                 f"1년 {r['trend']} 추세와 반대로 움직였습니다",
                                 "한 번의 반대 움직임이라 일시적일 수 있습니다. 다음 발표에서도 "
                                 "같은 방향이면 추세 전환을 의심해 볼 수 있습니다.",
                                 f"이번 {diff:+,.2f}{unit}, 1년 추세 {chg:+,.2f}{unit}"))
    return out_new, []


# ------------------------------------------------------------
# 규칙 8. 스태그플레이션 조합(물가 상승 + 성장 둔화)
# ------------------------------------------------------------
def rule_stagflation(S, info, new_keys):
    out_now = []
    lb = THRESHOLDS["stagflation_lookback"]
    target = THRESHOLDS["inflation_target"]
    combos = [("미국", "US_PCE", "US_GDP", "US_UNRATE"), ("한국", "KR_CPI", "KR_GDP", "KR_UNRATE")]
    for country, k_inf, k_gdp, k_un in combos:
        inf_s, gdp_s, un_s = S.get(k_inf), S.get(k_gdp), S.get(k_un)
        if inf_s is None or len(inf_s) <= lb:
            continue
        inflation_up = inf_s.iloc[-1] > target and \
            inf_s.iloc[-1] - inf_s.iloc[-1 - lb] >= THRESHOLDS["inflation_rise_min"]
        if not inflation_up:
            continue
        reasons = []
        if gdp_s is not None and len(gdp_s) >= 2 and gdp_s.iloc[-1] < gdp_s.iloc[-2]:
            reasons.append(f"성장률 둔화({_f(gdp_s.iloc[-2])}% → {_f(gdp_s.iloc[-1])}%)")
        if un_s is not None and len(un_s) > lb and \
                un_s.iloc[-1] - un_s.iloc[-1 - lb] >= THRESHOLDS["unemployment_rise"]:
            reasons.append(f"실업률 상승({_f(un_s.iloc[-1 - lb])}% → {_f(un_s.iloc[-1])}%)")
        if reasons:
            out_now.append(dict(level="주의", key=f"{country}_STAG", country=country,
                                indicator="물가·성장 조합",
                                title="스태그플레이션 우려가 나올 수 있는 조합입니다",
                                text="물가는 오르는데 경기는 둔화하는 신호가 함께 나타났습니다. "
                                     "중앙은행이 금리를 내리기도, 올리기도 어려워지는 상황입니다.",
                                basis=f"물가 {_f(inf_s.iloc[-1 - lb])}% → {_f(inf_s.iloc[-1])}% "
                                      f"({lb}개월 전 대비 상승), " + ", ".join(reasons)))
    return [], out_now


# ------------------------------------------------------------
# 규칙 9. 한미 금리차
# ------------------------------------------------------------
def rule_krus(S, info, new_keys):
    out_new, out_now = [], []
    s = S.get("KRUS_BASE")
    if s is not None and len(s) >= 2:
        latest = s.iloc[-1]
        inf = info.get("KRUS_BASE")
        changed = s[s != latest]
        if not changed.empty:
            before = changed.iloc[-1]
            change_date = s[s.index > changed.index[-1]].index[0]
            if (s.index[-1] - change_date).days <= THRESHOLDS["policy_recent_days"]:
                wider = abs(latest) > abs(before)
                out_new.append(_item("관심", "KRUS_BASE", inf,
                                     f"한미 기준금리차가 {'벌어졌습니다' if wider else '좁혀졌습니다'}",
                                     "한국이나 미국의 기준금리 변경으로 두 나라 금리차가 바뀌었습니다. "
                                     "금리차는 원/달러 환율과 외국인 자금 흐름에 영향을 줍니다.",
                                     f"{change_date:%Y.%m.%d}: {_pp(before)} → {_pp(latest)}"))
        if latest < 0:
            level = "관심" if latest <= THRESHOLDS["krus_gap_alert"] else "참고"
            out_now.append(_item(level, "KRUS_BASE", inf, "한미 기준금리 역전이 이어지고 있습니다",
                                 f"미국 기준금리가 한국보다 {_f(-latest)}%p 높습니다. 금리차가 클수록 "
                                 "원화 약세(환율 상승)와 외국인 자금 유출 압력이 커질 수 있습니다.",
                                 f"최신 {_pp(latest)} (한국−미국)"))
    s = S.get("KRUS_10Y")
    if s is not None and len(s) >= 1 and s.iloc[-1] < 0:
        out_now.append(_item("참고", "KRUS_10Y", info.get("KRUS_10Y"), "장기 금리도 미국이 더 높습니다",
                             f"10년물 금리 기준으로도 미국이 한국보다 {_f(-s.iloc[-1])}%p 높습니다.",
                             f"최신 {_pp(s.iloc[-1])} (한국−미국)"))
    return out_new, out_now


# ------------------------------------------------------------
# 규칙 10. 역사적 위치(최근 10년 최고·최저권)
# ------------------------------------------------------------
def rule_position(S, info, new_keys):
    out_new = []
    edge = THRESHOLDS["position_extreme"]
    for key in new_keys:
        r = info.get(key)
        pos = r.get("position") if r else None
        if not pos or r.get("compare") == "change":
            continue
        yrs = f"최근 {pos['years']:g}년"
        if pos["pct"] >= 100 - edge:
            out_new.append(_item("관심", key, r, f"{yrs} 중 가장 높은 수준권입니다",
                                 f"최신값이 {yrs} 자료 중 상위 {_f(100 - pos['pct'], 1)}% 안에 듭니다.",
                                 f"최신 {_f(r['latest'])}{r['unit']}, {yrs} 최고 {_f(pos['max'])}{r['unit']}"))
        elif pos["pct"] <= edge:
            out_new.append(_item("관심", key, r, f"{yrs} 중 가장 낮은 수준권입니다",
                                 f"최신값이 {yrs} 자료 중 하위 {_f(pos['pct'], 1)}% 안에 듭니다.",
                                 f"최신 {_f(r['latest'])}{r['unit']}, {yrs} 최저 {_f(pos['min'])}{r['unit']}"))
    return out_new, []


RULES = [rule_policy, rule_inflation, rule_yield_curve, rule_sahm, rule_vix, rule_stagflation,
         rule_krus, rule_position]


def generate(results, series, first_run):
    """results: 지표별 결과 목록, series: {지표키: 변환된 시계열}"""
    info = {r["key"]: r for r in results if r.get("ok")}
    S = {k: v for k, v in series.items() if k in info}
    # 첫 업데이트는 비교할 지난 결과가 없으므로 모든 지표를 '이번 발표'로 봄
    new_keys = set(info) if first_run else {k for k, r in info.items() if r.get("is_new")}

    new_items, now_items = [], []
    for rule in RULES:
        try:
            a, b = rule(S, info, new_keys)
        except Exception as e:  # 규칙 하나가 실패해도 나머지 해설은 보여 줌
            print(f"해설 규칙 오류({rule.__name__}): {e}")
            continue
        new_items += a
        now_items += b
    try:
        a, _ = rule_change_patterns(S, info, new_keys, results,
                                    skip={i["key"] for i in new_items})
        new_items += a
    except Exception as e:
        print(f"해설 규칙 오류(rule_change_patterns): {e}")

    # 같은 지표가 두 곳에 나오면 '이번 업데이트' 쪽만 남김
    shown = {(i["key"], i["title"]) for i in new_items}
    now_items = [i for i in now_items if (i["key"], i["title"]) not in shown]
    sort = lambda xs: sorted(xs, key=lambda i: (LEVEL_ORDER[i["level"]], i["country"] != "미국"))
    return dict(new=sort(new_items), now=sort(now_items), first_run=first_run,
                new_count=len(new_keys))

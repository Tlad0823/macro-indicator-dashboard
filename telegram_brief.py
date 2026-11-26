"""
텔레그램 브리핑 (규칙 기반)
------------------------------------------------------------
구독 중인 채널의 최근 메시지를 읽어, 정해 둔 문장 틀에 맞춰
  - 미 증시 제목  → 반응 / 원인 / 반대 재료
  - 특징 종목     → 상승 종목 / 하락 종목
  - 한국 증시, 비트코인, 업종별 코멘트
  - 원인 설명(변화요인)과 '예상치 상회·하회' 언급
으로 나눕니다. 현재 규칙은 서상영 채널(@ehdwl)의 글 형식에 맞춰져 있습니다.

규칙이 어떻게 나누는지 확인하려면 터미널에서:
  python telegram_brief.py          # 실제 채널 메시지로 정리 결과 출력
  python telegram_brief.py --raw    # 메시지 원문 앞부분도 함께 출력
"""

import asyncio
import re
import sys
from datetime import timedelta, timezone
from pathlib import Path

import config

BASE_DIR = Path(__file__).resolve().parent
SESSION_PATH = BASE_DIR / "data" / "telegram"      # 실제 파일명은 telegram.session
KST = timezone(timedelta(hours=9))

FETCH_LIMIT = 60          # 채널마다 최근 몇 개 메시지를 읽을지
GROUP_GAP_MIN = 40        # 이 시간(분) 안에 연달아 올라온 글은 한 묶음(같은 날 시황)으로 봄
SHOW_GROUPS = 3           # 화면에 보여 줄 묶음 수
MAX_TEXT = 110            # 화면에 보여 줄 문장 최대 길이(원문은 링크로)

# ------------------------------------------------------------
# 문장 규칙에 쓰는 단어 (서상영 채널의 실제 글 형식에 맞춤)
# ------------------------------------------------------------
# (찾을 표현, 원인 쪽에 남길 앞부분) — 예: "국제유가 급등 여파로 하락" → 원인 "국제유가 급등"
CAUSE_MARKERS = [
    ("에 힘입어", ""), ("에 따른", ""), ("에 따라", ""), ("으로 인해", ""), ("로 인해", ""),
    ("때문에", ""), ("덕분에", ""), ("탓에", ""),
    ("여파로", ""), ("영향으로", ""), ("영향에", ""), ("발표로", "발표"),
    ("확대로", "확대"), ("상회로", "상회"), ("하회로", "하회"), ("부각으로", "부각"), ("부각되며", "부각"),
    ("되며 ", ""), ("하며 ", ""),
    ("에 ", ""),            # 이 채널에서 가장 많이 쓰는 형태: "옵션 수급 변화에 반도체 중심 하락"
]
NOT_CAUSE_AFTER_E = ("대한", "대해", "이어", "걸쳐", "비해")   # "~에 대한" 등은 원인 표현이 아님
CONTRAST_MARKERS = ["에도 불구하고", "에도", "했으나", "했지만"]

# 두 단어 이상 표현을 먼저 판단(단어 하나만 보면 뜻이 반대가 되는 경우)
DIR_PHRASES = [
    ("낙폭 축소", "만회"), ("낙폭 만회", "만회"), ("하락폭 축소", "만회"), ("하락 폭 축소", "만회"),
    ("상승폭 축소", "반납"), ("상승 폭 축소", "반납"), ("상승분 반납", "반납"),
    ("상승 전환", "상승"), ("하락 전환", "하락"), ("반발 매수", "상승"),
    ("매물 소화", "보합"), ("보합권", "보합"),
]
DIR_WORDS = [
    ("폭등", "상승"), ("급등", "상승"), ("상승", "상승"), ("강세", "상승"), ("반등", "상승"), ("호조", "상승"),
    ("폭락", "하락"), ("급락", "하락"), ("하락", "하락"), ("약세", "하락"), ("부진", "하락"), ("위축", "하락"),
    ("보합", "보합"), ("혼조", "보합"),
]


def _cut(text, n=MAX_TEXT):
    text = re.sub(r"\s+", " ", text or "").strip(" *-·:,")
    return text if len(text) <= n else text[: n - 1] + "…"


def _dir_hits(text):
    """문장 안의 방향 표현 위치 목록 [(위치, 끝, 방향)] — 두 단어 표현이 우선"""
    hits, taken = [], set()
    for phrase, d in DIR_PHRASES:
        for m in re.finditer(re.escape(phrase), text):
            hits.append((m.start(), m.end(), d))
            taken.update(range(m.start(), m.end()))
    for word, d in DIR_WORDS:
        for m in re.finditer(word, text):
            if m.start() not in taken:
                hits.append((m.start(), m.end(), d))
    # 단어 '하락' 앞에 '베팅'처럼 방향이 아닌 경우가 이어지면 제외
    hits = [h for h in hits if not re.match(r"\s*(베팅|우려|압력|위험|가능성)", text[h[1]:])]
    return sorted(hits)


def direction(text):
    """'A 후 B' 흐름이면 '하락 후 만회'처럼, 'A 상승 Vs. B 하락'이면 '엇갈림', 아니면 마지막 방향 표현"""
    sides = re.split(r"\s*[Vv][Ss]\.?\s*,?\s*", text or "")
    if len(sides) > 1:
        ds = {direction(x) for x in sides} - {None}
        if len(ds) > 1:
            return "엇갈림"
        return ds.pop() if ds else None
    parts = re.split(r"\s+후\s+", text or "")
    labels = []
    for part in parts:
        hits = _dir_hits(part)
        if hits:
            labels.append(hits[-1][2])
    if not labels:
        return None
    if len(labels) > 1 and labels[0] != labels[-1]:
        return f"{labels[0]} 후 {labels[-1]}"
    return labels[-1]


def _split_one(text):
    """한 단계 문장을 '[반대 재료]에도 [원인]에 [반응]'으로 나눔"""
    text = re.sub(r"\s+", " ", text).strip(" ,")
    contra = None
    for c in CONTRAST_MARKERS:
        i = text.find(c)
        if i > 0:
            contra, text = text[:i].strip(" ,"), text[i + len(c):].strip(" ,")
            break
    hits = _dir_hits(text)
    limit = hits[-1][0] if hits else len(text)      # 원인 표현은 마지막 방향 단어보다 앞에 있어야 함
    found = None
    for marker, keep in CAUSE_MARKERS:
        for m in re.finditer(re.escape(marker), text[:limit]):
            i = m.start()
            if marker == "에 " and text[i + 2:i + 4] in NOT_CAUSE_AFTER_E:
                continue
            if i > 0 and (found is None or i > found[0]):
                found = (i, marker, keep)
    cause, reaction = None, text
    if found:
        i, marker, keep = found
        cause = (text[:i] + keep).strip(" ,")
        if not re.search(r"(급|폭|반|상|고|평)등$", cause):     # '급등' 등은 그대로 두고, '위험등'만 '위험 등'으로
            cause = re.sub(r"(\S)등$", r"\1 등", cause)
        reaction = text[i + len(marker):].strip(" ,")
    return dict(cause=_cut(cause) if cause else None, reaction=_cut(reaction),
                contra=_cut(contra) if contra else None, direction=direction(reaction))


def split_cause(text):
    """'A에 하락 후 B에 낙폭 축소'처럼 '후'로 이어진 흐름은 단계별로 나눔"""
    parts = [p for p in re.split(r"\s+후\s+", re.sub(r"\s+", " ", text).strip()) if p]
    steps = [_split_one(p) for p in parts] or [_split_one(text)]
    for prev, cur in zip(steps, steps[1:]):
        # '상승 후 … 폭 축소' = 상승분 반납, '하락 후 … 폭 축소' = 낙폭 만회
        if re.search(r"(?<![낙상])\s?폭\s?축소", cur["reaction"]) and prev["direction"] in ("상승", "하락"):
            cur["direction"] = "반납" if prev["direction"] == "상승" else "만회"
    first = steps[0]
    labels = [s["direction"] for s in steps if s["direction"]]
    overall = None
    if labels:
        overall = labels[0] if len(labels) == 1 or labels[0] == labels[-1] else f"{labels[0]} 후 {labels[-1]}"
    return dict(cause=first["cause"], reaction=first["reaction"], contra=first["contra"],
                direction=overall, steps=steps if len(steps) > 1 else [])


PCT = re.compile(r"\(?[+\-−]?\d+(?:\.\d+)?%\)?")


def split_items(text):
    """특징 종목: 'A, B 하락 Vs. C 상승 전환, D 하락 후 낙폭 축소' → 상승 / 하락 / 등락"""
    text = re.sub(r"\s*[Vv][Ss]\.?\s*,?\s*", ", ", text)
    items = [x.strip(" .") for x in re.split(r"[,，]\s*", text) if x.strip(" .")]
    parsed = []
    for it in items:
        d = direction(it)
        hits = _dir_hits(it)
        cut_at = min([h[0] for h in hits] + [m.start() for m in re.finditer(r"\s후\s", it)] + [len(it)])
        name = re.sub(r"\s*등$", "", it[:cut_at].strip())
        if name and len(name) <= 25:
            parsed.append([name, d])
    carry = None                       # 'A, B, C 하락'처럼 뒤에 붙은 방향을 앞 종목에도 적용
    for p in reversed(parsed):
        carry = p[1] or carry
        p[1] = p[1] or carry
    carry = None
    for p in parsed:                   # 맨 끝 종목에 방향이 없으면 앞 종목 방향을 따름
        carry = p[1] or carry
        p[1] = p[1] or carry
    up = [n for n, d in parsed if d == "상승"]
    down = [n for n, d in parsed if d == "하락"]
    groups = {}
    for n, d in parsed:            # 같은 상태끼리 묶음: '엔비디아, 마이크론(하락 후 만회)'
        if d not in ("상승", "하락"):
            groups.setdefault(d or "방향 없음", []).append(n)
    mixed = [f"{', '.join(ns)}({d})" for d, ns in groups.items()]
    return dict(up=up[:10], down=down[:10], mixed=mixed[:8])


# 본문 문장에서 원인을 찾을 때 쓰는 표현(요약 줄에 원인이 없을 때만 사용)
BODY_CAUSE_MARKERS = ["여파로 ", "영향으로 ", "에 힘입어 ", "때문에 ", "로 인해 ", "으로 ", "에 따른 "]


def body_cause(sentence):
    """'비트코인은 A에 따른 B 개선으로 … 상승했지만, …' → 원인 'A에 따른 B 개선'"""
    text = re.sub(r"^\S+(은|는|이|가)\s+", "", sentence.strip())        # 주어 '비트코인은' 제거
    text = re.split(r"했지만|했으나|하였으나|지만,|으나,", text)[0]      # 역접 뒤는 다음 흐름이라 제외
    hits = _dir_hits(text)
    if not hits:
        return None
    limit = hits[-1][0]
    best = None
    for mk in BODY_CAUSE_MARKERS:
        for m in re.finditer(re.escape(mk), text[:limit]):
            if m.start() > 0 and (best is None or m.start() > best):
                best = m.start()
    if best is None:
        return None
    cause = text[:best].strip(" ,")
    return _cut(cause) if 2 <= len(cause) <= 80 else None


def _fill_cause_from_body(item, body_line):
    """요약 줄에 원인이 없으면 바로 아래 본문 첫 문장에서 원인을 찾아 채움"""
    if not item or not body_line:
        return
    first = sentences(body_line)[0] if sentences(body_line) else ""
    c = body_cause(first)
    if not c:
        return
    target = item["steps"][0] if item.get("steps") else item
    if not target.get("cause"):
        target["cause"] = c
        item["cause"] = item.get("cause") or c
        item["cause_from_body"] = True


HEADLINE = re.compile(r"(\d{1,2}/\d{1,2})\s*미\s*증시\s*[,:：]?\s*(.+)")
LABELED = re.compile(r"^\*?\s*([가-힣A-Za-z0-9·,/&\s]{2,25}?)\s*[:：]\s*(.+)$")
EXPECT_WORDS = re.compile(r"예상(?:치)?\s*(?:을|를|보다|에)?\s*(?:상회|하회|부합|웃돌|밑돌)")
MACRO_WORDS = ("PCE", "CPI", "PPI", "물가", "고용", "GDP", "성장률", "소매판매", "PMI", "ISM", "실업",
               "산업생산", "주택", "소비심리", "신규 실업")


def sentences(text):
    """소수점(1.31%)은 자르지 않고 문장 단위로 나눔"""
    return [x.strip() for x in re.split(r"(?<!\d)\.(?!\d)|[。\n]", text or "") if x.strip()]


def parse_message(text):
    """메시지 하나를 규칙에 따라 나눔. 해당 없는 칸은 비워 둠"""
    out = dict(headline=None, stocks=None, korea=None, bitcoin=None, sectors=[], factors=[],
               causes=[], expects=[])
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    in_cause_block = False
    await_body = None          # 직전에 읽은 '비트코인:' 같은 요약 줄(본문 첫 줄에서 원인 보충용)
    for ln in lines:
        if await_body and not LABELED.match(ln) and not ln.startswith("*"):
            _fill_cause_from_body(out[await_body], ln)
            await_body = None
            continue
        await_body = None
        m = HEADLINE.search(ln)
        if m and out["headline"] is None:
            out["headline"] = dict(date=m.group(1), title=_cut(m.group(2), 140), **split_cause(m.group(2)))
            continue
        m = LABELED.match(ln)
        if m:
            label, body = re.sub(r"\s+", " ", m.group(1)).strip(), m.group(2)
            key = label.replace(" ", "")
            in_cause_block = False
            if key == "특징종목":
                out["stocks"] = split_items(body)
            elif key in ("변화요인", "주요변화요인"):
                out["factors"] = [x.strip(" .") for x in re.split(r"[,，]\s*|(?<=\S)[과와]\s", body)
                                  if x.strip(" .")][:8]
            elif "한국" in key and "증시" in key:
                out["korea"] = split_cause(body)
                await_body = "korea"
            elif key == "비트코인":
                out["bitcoin"] = split_cause(body)
                await_body = "bitcoin"
            elif len(ln) < 220 and not re.search(r"\d{1,2}:\d{2}", label):
                out["sectors"].append(dict(name=label, text=_cut(body), direction=direction(body)))
            continue
        if "변화요인" in ln.replace(" ", ""):
            in_cause_block = True
            continue
        if ln.startswith("*"):
            in_cause_block = False
        if in_cause_block and len(out["causes"]) < 3 and any(mk in ln for mk, _ in CAUSE_MARKERS[:-1]):
            out["causes"].append(_cut(ln, 160))
    for sent in sentences(text):
        if EXPECT_WORDS.search(sent) and len(out["expects"]) < 6:
            sent = re.sub(r"^(한편|또한|이에|다만)\s*,?\s*", "", sent)
            kind = "지표" if any(w in sent for w in MACRO_WORDS) else "실적"
            out["expects"].append(dict(kind=kind, text=_cut(sent, 150)))
    return out


def group_messages(msgs):
    """시간 간격이 짧은 글끼리 묶음(한 번의 시황이 여러 글로 나뉘어 올라오기 때문)"""
    groups = []
    for m in sorted(msgs, key=lambda x: x["date"]):
        if groups and (m["date"] - groups[-1][-1]["date"]) <= timedelta(minutes=GROUP_GAP_MIN):
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


# ------------------------------------------------------------
# 핵심 요인 메모: 요인 단어가 들어간 문장을 그날 글 전체에서 찾음
# ------------------------------------------------------------
NOTE_MAX = 3              # 요인마다 보여 줄 문장 수
# 요인 이름이 본문에 그대로 나오지 않는 경우를 위한 관련어(필요하면 추가)
FACTOR_SYNONYMS = {
    "경제지표": ["지표", "PCE", "GDP", "고용", "물가", "소비심리", "실업"],
    "경기지표": ["지표", "PCE", "GDP", "고용", "물가", "소비심리", "실업"],
    "자금조달": ["자금조달", "자금 조달", "회사채", "채권 발행", "대출"],
    "옵션 수급": ["옵션"],
    "관세": ["관세"],
}
GENERIC_TOKENS = {"AI", "변화", "산업", "관련", "확대", "우려", "부담", "기대", "시장", "부과", "추가"}


def _note_keywords(factor):
    """요인에서 찾을 단어 목록(우선순위 순서)"""
    words = [factor]
    tokens = [t for t in re.split(r"[\s·/]+", factor) if len(t) >= 2 and t not in GENERIC_TOKENS]
    if len(tokens) > 1:
        words.append(tuple(tokens))          # 모든 핵심 단어가 함께 들어간 문장
    words += FACTOR_SYNONYMS.get(factor.replace(" ", ""), []) + FACTOR_SYNONYMS.get(factor, [])
    words += sorted(tokens, key=len, reverse=True)
    return words


def factor_notes(factors, group):
    """{요인: [{text, link, hit}]} — 요인과 관련된 원문 문장을 최대 NOTE_MAX개"""
    sents = []
    for m in group:
        for line in (m["text"] or "").splitlines():
            if HEADLINE.search(line) or re.match(r"^\*?\s*변화\s*요인", line.strip()):
                continue                       # 제목과 요인 목록 줄은 제외(요인 이름만 반복되므로)
            for x in sentences(line):
                if len(x) >= 12:
                    sents.append((x, m["link"]))
    def has(text, word):          # 띄어쓰기가 달라도 같은 단어로 봄('신용위험' = '신용 위험')
        return word.replace(" ", "") in text.replace(" ", "")

    notes = {}
    for f in factors:
        found, used = [], set()
        for kw in _note_keywords(f):
            for text, link in sents:
                if len(found) >= NOTE_MAX:
                    break
                hit = all(has(text, k) for k in kw) if isinstance(kw, tuple) else has(text, kw)
                if hit and text not in used:
                    used.add(text)
                    found.append(dict(text=_cut(text, 180), link=link,
                                      hit=list(kw) if isinstance(kw, tuple) else [kw]))
            if len(found) >= NOTE_MAX:
                break
        notes[f] = found
    return notes


def merge_group(channel, group):
    """한 묶음 안의 글들을 합쳐 화면용 정리 하나로 만듦"""
    merged = dict(channel=channel, start=group[0]["date"].astimezone(KST).strftime("%m/%d %H:%M"),
                  links=[m["link"] for m in group], headline=None, stocks=None, korea=None,
                  bitcoin=None, sectors=[], factors=[], causes=[], expects=[], fallback=[])
    for m in group:
        p = parse_message(m["text"])
        for k in ("headline", "stocks", "korea", "bitcoin"):
            if p[k] and not merged[k]:
                merged[k] = dict(p[k], link=m["link"])
        merged["sectors"] += p["sectors"]
        merged["factors"] = merged["factors"] or p["factors"]
        merged["causes"] += p["causes"]
        merged["expects"] += p["expects"]
    merged["sectors"] = merged["sectors"][:8]
    merged["causes"] = merged["causes"][:3]
    merged["factor_notes"] = factor_notes(merged["factors"], group)
    seen, expects = set(), []
    for e in merged["expects"]:
        if e["text"] in merged["causes"]:
            continue
        key = e["text"]
        if e["kind"] == "지표":
            # '근원 PCE 예상 상회'가 비트코인·달러·국채 설명에 반복되면 첫 문장만 남김
            word = next((w for w in MACRO_WORDS if w in e["text"]), "")
            verdict = EXPECT_WORDS.search(e["text"]).group(0)[-2:]
            key = ("지표", "근원" in e["text"], word, verdict)
        if key not in seen:
            seen.add(key); expects.append(e)
    merged["expects"] = sorted(expects, key=lambda e: e["kind"] != "지표")[:5]   # 지표 언급을 먼저
    parsed_any = any(merged[k] for k in ("headline", "stocks", "korea", "bitcoin")) or merged["sectors"]
    if not parsed_any:
        # 규칙에 맞는 부분이 없으면 각 글의 첫 줄만 보여 줌
        merged["fallback"] = [dict(text=_cut(m["text"].strip().splitlines()[0] if m["text"].strip() else "(내용 없음)"),
                                   link=m["link"]) for m in group][:3]
    return merged


# ------------------------------------------------------------
# 텔레그램에서 메시지 읽기
# ------------------------------------------------------------
def channels_from_config():
    chs = getattr(config, "TELEGRAM_CHANNELS", ["ehdwl"])
    return [c.replace("https://", "").replace("t.me/", "").strip("@/ ") for c in chs]


async def _fetch(channels, limit):
    from telethon import TelegramClient
    client = TelegramClient(str(SESSION_PATH), int(config.TELEGRAM_API_ID), config.TELEGRAM_API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("텔레그램 로그인이 필요합니다. 터미널에서 python telegram_login.py 를 한 번 실행하세요.")
        result = {}
        for ch in channels:
            entity = await client.get_entity(ch)
            msgs = []
            async for msg in client.iter_messages(entity, limit=limit):
                if msg.message:
                    msgs.append(dict(id=msg.id, date=msg.date, text=msg.message,
                                     link=f"https://t.me/{ch}/{msg.id}"))
            result[ch] = dict(title=getattr(entity, "title", ch), messages=msgs)
        return result
    finally:
        await client.disconnect()


def build_brief():
    """대시보드용 텔레그램 브리핑. 실패해도 예외 대신 안내 문구를 돌려줌"""
    api_id = getattr(config, "TELEGRAM_API_ID", 0)
    api_hash = getattr(config, "TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        return dict(status="off", message="config.py에 텔레그램 api_id와 api_hash를 넣으면 브리핑이 나옵니다.")
    try:
        import telethon  # noqa: F401
    except ImportError:
        return dict(status="error", message="telethon 패키지가 없습니다. 터미널에서 pip install telethon 을 실행하세요.")
    if not SESSION_PATH.with_suffix(".session").exists():
        return dict(status="error", message="텔레그램 로그인이 필요합니다. 터미널에서 python telegram_login.py 를 한 번 실행하세요.")
    try:
        data = asyncio.run(asyncio.wait_for(_fetch(channels_from_config(), FETCH_LIMIT), timeout=40))
    except Exception as e:
        return dict(status="error", message=f"텔레그램 메시지를 읽지 못했습니다: {e}")

    channels = []
    for ch, info in data.items():
        groups = list(reversed([merge_group(ch, g) for g in group_messages(info["messages"])]))  # 최신부터
        parsed = [g for g in groups if not g["fallback"]][:SHOW_GROUPS]
        # 시황 형식이 아닌 글(공지, 기사 공유 등)은 카드 대신 '그 밖의 글' 목록으로
        others = [x for g in groups if g["fallback"] for x in g["fallback"]][:5]
        channels.append(dict(id=ch, title=info["title"], url=f"https://t.me/{ch}", groups=parsed,
                             others=others, count=len(info["messages"])))
    return dict(status="ok", channels=channels)


if __name__ == "__main__":
    import json
    brief = build_brief()
    if brief["status"] != "ok":
        print(brief["message"])
        sys.exit(1)
    for ch in brief["channels"]:
        print(f"\n===== {ch['title']} (@{ch['id']}) 최근 메시지 {ch['count']}개 =====")
        for g in ch["groups"]:
            print(f"\n[{g['start']}] 글 {len(g['links'])}개  {g['links'][0]}")
            print(json.dumps({k: v for k, v in g.items() if k not in ('links', 'channel', 'start')},
                             ensure_ascii=False, indent=1))
    if "--raw" in sys.argv:
        data = asyncio.run(_fetch(channels_from_config(), 8))
        for ch, info in data.items():
            for m in info["messages"]:
                print(f"\n----- {m['link']} -----\n{m['text'][:600]}")

"""5교시 v2: Loop A — 예외만 판정(B안) + 검색 근거 원문 대조 + 노드별 모델 분배

실행:  python lesson5_v2.py haiku | sonnet | mix
  mix = 열람·열람판단은 Haiku, 검색·검색판단은 Sonnet
"""
import sys
import time
import operator
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing_extensions import TypedDict
from langchain_anthropic import ChatAnthropic
from langgraph.graph import StateGraph, START, END

load_dotenv()

# ── 모델 설정 ────────────────────────────────────────────
PRICES = {"haiku": (1.0, 5.0), "sonnet": (2.0, 10.0)}     # $/백만토큰 (입력, 출력) — 9/30 Pricing 확인
MODEL_IDS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5-5"}
SEARCH_PRICE = 10 / 1000

MODE = sys.argv[1] if len(sys.argv) > 1 else "mix"
NODE_MODEL = {                                             # 노드별로 어떤 모델을 쓸지
    "haiku":  {"page": "haiku",  "search": "haiku"},
    "sonnet": {"page": "sonnet", "search": "sonnet"},
    "mix":    {"page": "haiku",  "search": "sonnet"},
}[MODE]


def make_llm(key):
    extra = {"temperature": 0} if key == "haiku" else {}   # Sonnet 5.5는 temperature 변경 불가
    return ChatAnthropic(model=MODEL_IDS[key], max_tokens=2048, **extra)


# ── 답안지 양식 (B안: 예외만 판정) ─────────────────────────
Kind = Literal["전면휴관", "부분휴관", "특별개관", "예외공지없음", "정보부족"]


class Judgement(BaseModel):
    kind: Kind = Field(description="여행 날짜의 '정기 정보와 다른 예외' 유형")
    quote: str = Field(description="판단 근거 원문을 한 글자도 바꾸지 말고 그대로 복사. "
                                   "예외공지없음·정보부족이면 빈 문자열")


CRITERIA = (
    "정기 영업정보(아래)는 이미 확인된 사실이다. 너는 여행 날짜에 이와 **다른 예외**가 있는지만 판단한다.\n"
    "분류 기준:\n"
    "- 전면휴관: 여행 날짜에 시설 전체가 임시로 닫힘\n"
    "- 부분휴관: 여행 날짜에 일부 시설·구역만 닫힘\n"
    "- 특별개관: 여행 날짜에 정기 영업시간 외(야간 등)에 추가로 열거나, 정기 휴무일인데 열림\n"
    "- 예외공지없음: 자료를 읽었고, 여행 날짜에 해당하는 예외 공지가 없음\n"
    "- 정보부족: 자료를 읽을 수 없거나, '시기·행사에 따라 시간이 다름', '별도 페이지 확인' 등 "
    "여행 날짜의 예외 여부를 이 자료만으로 판단할 수 없음\n"
)

BADGE = {                                                  # 배지는 코드가 고정 매핑 (Claude가 바꾸지 않음)
    "전면휴관": "🔴 휴관",
    "부분휴관": "🟠 일부 휴관",
    "특별개관": "🔵 특별 개관·연장",
    "예외공지없음_공식": "🟢 공식 공지 확인 · 변동 없음",          # 공식 사이트를 읽고 확인
    "예외공지없음_검색": "🟡 예외 공지 없음 (검색 기준)",          # 검색만으로 확인
    "확인불가": "⚪ 확인불가",
}


def badge_of(out):
    """같은 '예외공지없음'이라도 어느 단계에서 판정됐는지(=그래프 경로)로 배지를 나눈다"""
    kind = out["kind"]
    if kind == "예외공지없음":
        kind += "_공식" if "judge_page:예외공지없음" in out["path"] else "_검색"
    return BADGE[kind]


# ── 서류철 ───────────────────────────────────────────────
class LoopAState(TypedDict):
    spot: str
    trip_date: str
    url: Optional[str]
    regular_hours: str                         # Places API에서 받았다고 치는 정기 정보
    material: str
    evidence: str                              # 인용 대조용 '원문'만
    kind: str
    quote: str                                 # 검증을 통과한 근거만 (유저에게 보일 값)
    rejected_quote: str                        # 검증에서 탈락한 인용 (로그·정답셋 분석용, 유저에겐 안 보임)
    path: Annotated[list, operator.add]
    usage: Annotated[list, operator.add]


def usage_of(resp, step, model_key):
    u = resp.usage_metadata or {}
    raw = (resp.response_metadata or {}).get("usage", {}) or {}
    stu = raw.get("server_tool_use") or {}
    searches = stu.get("web_search_requests", 0) if isinstance(stu, dict) else 0
    return [{"step": step, "model": model_key, "in": u.get("input_tokens", 0),
             "out": u.get("output_tokens", 0), "searches": searches or 0}]


def extract_fetched_text(content):
    if isinstance(content, str):
        return None, "no_tool_call"
    for block in content:
        if isinstance(block, dict) and block.get("type") == "web_fetch_tool_result":
            c = block.get("content", {})
            if c.get("type") == "web_fetch_tool_result_error":
                return None, c.get("error_code", "error")
            src = c.get("content", {}).get("source", {})
            if src.get("type") == "text":
                return src.get("data", ""), None
            return None, "non_text_content"
    return None, "no_tool_call"


def extract_search_answer(content):
    """검색 후 Claude의 요약문과, 검색 결과에서 그대로 뽑힌 원문 인용(cited_text) 목록"""
    if isinstance(content, str):
        return content, []
    texts, cited = [], []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
            for c in block.get("citations") or []:
                if c.get("cited_text") and c["cited_text"] not in cited:
                    cited.append(c["cited_text"])
    return "".join(texts), cited


# ── 담당자들 ─────────────────────────────────────────────
def fetch_official(state):
    key = NODE_MODEL["page"]
    fetch_llm = make_llm(key).bind_tools([{"type": "web_fetch_20250910", "name": "web_fetch",
                                           "max_uses": 1, "max_content_tokens": 12000}])
    resp = fetch_llm.invoke(f"web_fetch 도구로 다음 URL을 열어줘. 열기만 하고 요약은 하지 마.\n{state['url']}")
    text, err = extract_fetched_text(resp.content)
    print(f"  [fetch_official:{key}] {'열람 성공 ' + str(len(text)) + '자' if text else '열람 실패: ' + err}")
    return {"material": text or "", "evidence": text or "",
            "path": ["열람"], "usage": usage_of(resp, "fetch", key)}


def judge(state, step, key):
    if not state["material"].strip():
        print(f"  [{step}:{key}] 자료 없음 → 정보부족")
        return {"kind": "정보부족", "quote": "", "path": [f"{step}:자료없음"]}
    judge_llm = make_llm(key).with_structured_output(Judgement, method="json_schema", include_raw=True)
    prompt = (f"여행지: {state['spot']}\n여행 날짜: {state['trip_date']}\n"
              f"정기 영업정보(확인됨): {state['regular_hours']}\n\n{CRITERIA}\n"
              f"근거(quote)는 반드시 아래 자료의 원문에서 그대로 복사해. 번역·요약 금지.\n\n"
              f"자료:\n{state['material'][:30000]}")
    out = judge_llm.invoke(prompt)
    kind, quote = out["parsed"].kind, out["parsed"].quote
    needs_quote = kind in ("전면휴관", "부분휴관", "특별개관")
    if needs_quote and (not quote or quote not in state["evidence"]):   # 레버 4: 원문 대조
        print(f"  [{step}:{key}] Claude: {kind} / 인용 {quote[:40]!r} → ⚠ 원문에 없는 인용 → 정보부족 처리")
        return {"kind": "정보부족", "quote": "", "rejected_quote": quote,       # 탈락한 인용은 quote에 남기지 않음
                "path": [f"{step}:인용탈락"], "usage": usage_of(out["raw"], step, key)}
    print(f"  [{step}:{key}] Claude: {kind} / 인용 {quote[:60]!r}")
    return {"kind": kind, "quote": quote, "path": [f"{step}:{kind}"],
            "usage": usage_of(out["raw"], step, key)}


def judge_page(state):
    return judge(state, "judge_page", NODE_MODEL["page"])


def judge_search(state):
    return judge(state, "judge_search", NODE_MODEL["search"])


def web_search(state):
    key = NODE_MODEL["search"]
    search_llm = make_llm(key).bind_tools([{"type": "web_search_20250305", "name": "web_search",
                                            "max_uses": 3}])
    msg = (f"{state['spot']}의 {state['trip_date']} 방문에 영향을 주는 예외 공지"
           f"(臨時休館・一部休館・特別拝観・夜間拝観・営業時間延長 등)를 web_search로 찾아줘. "
           f"정기 영업정보는 이미 알고 있다: {state['regular_hours']}. 공식 사이트 정보를 우선해. "
           f"찾은 내용을 날짜와 함께 짧게 정리하고, 반드시 출처 원문을 인용해. 못 찾으면 못 찾았다고 말해.")
    resp = search_llm.invoke(msg)
    answer, cited = extract_search_answer(resp.content)
    u = usage_of(resp, "search", key)
    print(f"  [web_search:{key}] 검색 {u[0]['searches']}회, 요약 {len(answer)}자, 원문 인용 {len(cited)}개")
    cited_block = "\n".join(f"- {c}" for c in cited)
    return {"material": f"[검색 요약]\n{answer}\n\n[원문 인용 — quote는 여기서만 고를 것]\n{cited_block}",
            "evidence": "\n".join(cited),       # ← 요약이 아니라 원문 인용만 대조 대상
            "path": ["검색"], "usage": u}


def finish(state):
    return {"path": ["끝"]}


def give_up(state):
    return {"kind": "확인불가", "path": ["확인불가"]}


# ── 안내데스크 ───────────────────────────────────────────
def route_start(state):
    return "URL 있음" if state["url"] else "URL 없음"


def route_after_judge(state):
    return "부족" if state["kind"] == "정보부족" else "판단됨"


builder = StateGraph(LoopAState)
for name, fn in [("fetch_official", fetch_official), ("judge_page", judge_page),
                 ("web_search", web_search), ("judge_search", judge_search),
                 ("finish", finish), ("give_up", give_up)]:
    builder.add_node(name, fn)
builder.add_conditional_edges(START, route_start, {"URL 있음": "fetch_official", "URL 없음": "web_search"})
builder.add_edge("fetch_official", "judge_page")
builder.add_conditional_edges("judge_page", route_after_judge, {"판단됨": "finish", "부족": "web_search"})
builder.add_edge("web_search", "judge_search")
builder.add_conditional_edges("judge_search", route_after_judge, {"판단됨": "finish", "부족": "give_up"})
builder.add_edge("finish", END)
builder.add_edge("give_up", END)
graph = builder.compile()


# ── 실제 스팟 (정기 정보는 실서비스라면 Places API 값 — 여기선 시뮬레이션) ──
# 예상에는 판정과 배지(확인 경로)를 함께 적는다 (개발설계노트 8-5절)
CASES = [
    ("京都国立博物館", "2026-10-02", "https://www.kyohaku.go.jp/",
     "9:30~17:00, 월요일 휴관", "부분휴관 → 🟠 (9/7~10/5 전시실 휴관, 정원만 개방)"),
    ("金閣寺", "2026-10-02", "https://www.shokoku-ji.jp/kinkakuji/",
     "9:00~17:00, 연중무휴", "예외공지없음 → 🟢 (검색 없이 끝나는 게 이상적)"),
    ("清水寺", "2026-11-25", "https://www.kiyomizudera.or.jp/",
     "6:00~18:00 (구글맵 현재 시즌 기준)", "특별개관 → 🔵 (11/21~11/30 秋の夜間特別拝観)"),
    ("伏見稲荷大社", "2026-10-02", None,
     "24시간 개방", "예외공지없음 → 🟡 (URL 없이 검색 경로)"),
]

if __name__ == "__main__":
    print(f"##### 모드: {MODE}  (열람판단={NODE_MODEL['page']}, 검색={NODE_MODEL['search']}) #####\n")
    total = 0.0
    for spot, date, url, hours, expected in CASES:
        print(f"=== {spot} ({date}) ===")
        t0 = time.perf_counter()                               # 스팟별 소요 시간 측정 (UI 대기 판단용)
        out = graph.invoke({"spot": spot, "trip_date": date, "url": url, "regular_hours": hours,
                            "material": "", "evidence": "", "kind": "", "quote": "",
                            "rejected_quote": "", "path": [], "usage": []},
                           {"recursion_limit": 12})   # Loop A 최장 경로는 5단계 → 넉넉히 12. 기본값(10007)에 맡기지 않음
        cost = 0.0
        for u in out["usage"]:
            pin, pout = PRICES[u["model"]]
            cost += u["in"] / 1e6 * pin + u["out"] / 1e6 * pout + u["searches"] * SEARCH_PRICE
        total += cost
        tin = sum(u["in"] for u in out["usage"]); tout = sum(u["out"] for u in out["usage"])
        srch = sum(u["searches"] for u in out["usage"])
        print(f"  경로: {' → '.join(out['path'])}")
        print(f"  배지: {badge_of(out)}   | 예상: {expected}")
        if out["quote"]:
            print(f"  근거: {out['quote'][:120]}")
        print(f"  토큰: 입력 {tin:,} / 출력 {tout:,} / 검색 {srch}회 → 약 ${cost:.4f}")
        print(f"  시간: {time.perf_counter() - t0:.1f}초\n")
    print(f"총 예상 비용: 약 ${total:.4f}")

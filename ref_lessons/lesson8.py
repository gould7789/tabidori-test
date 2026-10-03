"""8교시: 스팟 탐색 (설계도 ①) — 7교시 채팅 편집 에이전트에 '찾기' 도구 2개를 더한다

실행:  python lesson8.py           ← 가짜 Claude (대본: "노을 보기 좋은 데 있을까?")
       python lesson8.py sonnet    ← 진짜 Claude, 대화형 (칩이 뜨면 번호로 고르기)

① 탐색은 새 그래프가 아니라 **같은 에이전트에 도구만 추가**한 것 (기존 설계 9-7절: "도구 목록만 확장")
"""
import sys
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool, InjectedToolCallId
from langgraph.graph import StateGraph, START
from langgraph.prebuilt import ToolNode, tools_condition, InjectedState
from langgraph.types import Command
from langgraph.errors import GraphRecursionError
from langgraph.checkpoint.memory import InMemorySaver

import lesson7                                  # 7교시 도구·서류철 재사용
from lesson7 import ChatState, ScriptedClaude, add_spots, remove_spot, reorder_spot, recheck_closure, undo_last


# ── 가짜 데이터 (실서비스: 큐레이션 DB 70~80곳 / Google Places Text Search) ──
CURATED = [                                    # 큐레이션 DB: 팀이 조사한 스팟 + 취향 태그
    {"name": "円山公園",   "tags": ["노을", "공원", "벚꽃"]},
    {"name": "八坂神社",   "tags": ["노을", "신사", "야경"]},
    {"name": "祇園白川",   "tags": ["노을", "거리", "사진"]},
    {"name": "清水寺",     "tags": ["전통", "사찰", "야경"]},
    {"name": "嵐山竹林",   "tags": ["자연", "사진"]},
]
PLACES = {"銀閣寺", "哲学の道", "南禅寺", "伏見稲荷大社", "錦市場", "金閣寺"}   # Places에 실존하는 곳(가짜)


class SearchState(ChatState):
    miss_count: int                            # '못 찾음' 누적 — 5회면 큐레이션 목록 제안 (기존 설계 A9)


@tool
def search_curated(keywords: list[str],
                   tool_call_id: Annotated[str, InjectedToolCallId]):
    """큐레이션 DB에서 취향 키워드(노을·야경·전통·자연 등)로 스팟을 찾는다. 후보를 찾을 때 **항상 이것부터** 쓴다.
    keywords: 유저 말에서 뽑은 취향 키워드(한국어)."""
    hits = [s["name"] for s in CURATED if set(keywords) & set(s["tags"])]
    text = f"큐레이션 DB 결과: {hits}" if hits else "큐레이션 DB에 맞는 곳 없음 — verify_places로 네가 아는 후보를 확인해 볼 것"
    return Command(update={"messages": [ToolMessage(text, tool_call_id=tool_call_id)]})


@tool
def verify_places(names: list[str],
                  miss: Annotated[int, InjectedState("miss_count")],
                  tool_call_id: Annotated[str, InjectedToolCallId]):
    """장소 이름이 실제로 존재하는지 Google Places로 확인한다. 큐레이션 DB에 없을 때, 또는 유저가 콕 집어 말한 곳의
    정식 명칭을 확인할 때 쓴다. **확인된 곳만** 유저에게 보여줄 수 있다(지어낸 장소 금지).
    names: 확인할 일본어 정식 명칭 목록(한국어 이름·오타는 네가 먼저 일본어 정식 명칭으로 바꿔서 넘김)."""
    found = [n for n in names if n in PLACES]
    missing = [n for n in names if n not in PLACES]
    update = {"messages": [ToolMessage(f"실존 확인됨: {found} / 못 찾음: {missing}", tool_call_id=tool_call_id)]}
    if not found:                                                      # 하나도 못 찾으면 '못 찾음' +1
        update["miss_count"] = miss + 1
        if miss + 1 >= 5:
            update["messages"][0].content += " / 못 찾음 5회 — 유저에게 큐레이션 목록에서 직접 고르기를 제안할 것"
    return Command(update=update)


TOOLS = [search_curated, verify_places, add_spots, remove_spot, reorder_spot, recheck_closure, undo_last]

SYSTEM = (
    "너는 교토 여행 어드바이저야. 트레이 편집과 스팟 담기는 반드시 도구로만 해.\n"
    "- 유저가 취향만 말하면(예: 노을 보기 좋은 데): search_curated → (없으면) 네가 아는 후보를 verify_places → "
    "확인된 곳만 add_spots(mode='suggest')로 제안.\n"
    "- 유저가 장소를 콕 집으면: verify_places로 정식 명칭 확인 → add_spots(mode='confirm').\n"
    "- 말이 너무 모호하면 도구 없이 짧게 되물어.\n"
    "현재 트레이: {tray} / 여행 날짜: {date}"
)


def make_llm(mode):
    if mode == "fake":
        return ScriptedClaude(responses=[
            AIMessage("", tool_calls=[{"name": "search_curated", "args": {"keywords": ["노을"]}, "id": "s1"}]),
            AIMessage("", tool_calls=[{"name": "add_spots", "args": {"names": ["円山公園", "八坂神社", "祇園白川"], "mode": "suggest"}, "id": "s2"}]),
            AIMessage("마루야마 공원이랑 기온시라카와를 담았어요. 해 지기 30분 전쯤 도착하는 순서가 좋아요 [↶ 되돌리기]"),
        ])
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model="claude-sonnet-5-5", max_tokens=1024).bind_tools(TOOLS, parallel_tool_calls=False)


MODE = sys.argv[1] if len(sys.argv) > 1 else "fake"
llm = make_llm(MODE)


def agent(state: SearchState):
    resp = llm.invoke([SystemMessage(SYSTEM.format(tray=state["tray"], date=state["date"]))] + state["messages"])
    calls = [f"{c['name']}({c['args']})" for c in resp.tool_calls] or ["(도구 없음 — 답변)"]
    print(f"  [agent] Claude → {', '.join(calls)}")
    out = {"messages": [resp]}
    if isinstance(state["messages"][-1], HumanMessage):
        out["req_start_tray"] = state["tray"]
    return out


builder = StateGraph(SearchState)
builder.add_node("agent", agent)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": "__end__"})
builder.add_edge("tools", "agent")
graph = builder.compile(checkpointer=InMemorySaver())


if __name__ == "__main__":
    cfg = {"configurable": {"thread_id": "plan-demo"}, "recursion_limit": 20}
    first = {"tray": ["清水寺"], "prev_tray": None, "req_start_tray": [], "date": "2026-10-18", "miss_count": 0}
    print(f"##### 모드: {MODE} · 트레이 {first['tray']} · 날짜 {first['date']} #####")
    print("채팅 입력 (빈 줄 = 종료). 예: 노을 보기 좋은 데 있을까? / 철학의 길도 넣어줘 / 쿠마몬 성 넣어줘")
    while True:
        user = input("\n유저> ").strip()
        if not user:
            break
        inp = {"messages": [HumanMessage(user)], **first}
        first = {}
        try:
            out = graph.invoke(inp, cfg)
            while "__interrupt__" in out:
                q = out["__interrupt__"][0].value
                title = "이 곳들 말씀하시는 걸까요?" if q["type"] == "spot_confirm" else "이런 곳은 어떠세요? (골라 주세요)"
                print(f"  ⏸️ {title}")
                for i, c in enumerate(q["cards"], 1):
                    print(f"     [{i}] {'☑' if c['checked'] else '☐'} {c['name']}" + ("  🚫 이날 휴무예요" if c["closed"] else ""))
                ans = input("  담을 번호 (엔터 = 체크된 그대로, 0 = 안 담기, 예: 1,3)> ").strip()
                if ans == "":
                    sel = [c["name"] for c in q["cards"] if c["checked"]]
                elif ans == "0":
                    sel = []
                else:
                    sel = [q["cards"][int(x) - 1]["name"] for x in ans.replace(" ", "").split(",") if x.isdigit()]
                out = graph.invoke(Command(resume=sel), cfg)
        except GraphRecursionError:
            print("Claude> 요청이 너무 길어요. 나눠서 말씀해 주세요.")
            continue
        print(f"Claude> {out['messages'][-1].content}")
        print(f"  트레이: {out['tray']}   (못 찾음 누적: {out['miss_count']})")

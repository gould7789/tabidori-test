"""7교시: 채팅 편집 에이전트 — LLM ↔ 도구 루프 (설계도 ④)

실행:  python lesson7.py            ← 가짜 Claude (API 키·비용 없음, 정해진 대본대로 도구 호출)
       python lesson7.py sonnet     ← 진짜 Claude Sonnet 5.5 (.env의 ANTHROPIC_API_KEY 사용)
"""
import sys
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool, InjectedToolCallId
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langgraph.graph import StateGraph, MessagesState, START
from langgraph.prebuilt import ToolNode, tools_condition, InjectedState
from langgraph.types import Command, interrupt
from langgraph.errors import GraphRecursionError

load_dotenv()


# ── ① 서류철: 채팅 기록(messages) + 트레이 ─────────────────
class ChatState(MessagesState):        # MessagesState = messages 칸(리듀서로 쌓임)이 이미 들어있는 양식
    tray: list                         # 트레이는 교체 방식 (빼기·순서 변경이 있으니까 — 2교시)
    prev_tray: list | None             # 되돌리기용: 직전 '요청' 바로 전 트레이 (1단계만 보관)
    req_start_tray: list               # 이번 채팅 요청이 시작될 때의 트레이 (요청 단위 되돌리기용)
    date: str | None                   # 여행 날짜 (plan 단위)


# ── ② 도구들: Claude가 "이걸 불러줘"라고 요청할 수 있는 함수 ──
#   docstring과 인자 설명이 그대로 Claude에게 전달된다 (4교시 Field(description)과 같은 원리)
def done(tray, tool_call_id, text, before=None):
    """도구 결과: 트레이 교체 + '이렇게 했어요' 메시지를 서류철에 함께 넣는다.
    before가 있으면 = 실제로 트레이를 바꾼 편집 → 되돌리기 지점을 '이번 요청 시작 시점 트레이'로 저장.
    한 요청 안에서 편집이 여러 번이어도 같은 값을 쓰므로 → 되돌리기 한 번에 요청 전체가 취소됨."""
    update = {"tray": tray, "messages": [ToolMessage(text, tool_call_id=tool_call_id)]}
    if before is not None:
        update["prev_tray"] = before
    return Command(update=update)


CLOSED = {("錦市場", "2026-10-18")}      # 가짜 정기휴무 (실서비스: Places / 큐레이션 DB)


@tool
def add_spots(names: list[str],
              tray: Annotated[list, InjectedState("tray")],
              date: Annotated[str | None, InjectedState("date")],
              start: Annotated[list, InjectedState("req_start_tray")],
              tool_call_id: Annotated[str, InjectedToolCallId],
              mode: str = "confirm"):
    """트레이에 스팟을 추가한다. 여러 곳이면 한 번에 목록으로 넘긴다. names: 스팟 이름(일본어 정식 명칭) 목록.
    mode: "confirm" = 유저가 콕 집어 말한 곳(카드가 처음부터 체크됨),
          "suggest" = 네가 찾은 후보를 제안(체크 안 된 상태로 보여주고 유저가 고름).
    항상 유저에게 정보 카드로 '이 곳이 맞는지·담을지'를 확인받은 뒤, 유저가 고른 곳만 담는다.
    결과에 유저가 고른 것/안 고른 것이 나오니 그대로 유저에게 전달할 것. 다시 묻지 말 것."""
    already = [n for n in names if n in tray]
    cards = [{"name": n,                                         # 실서비스: Places 검색 결과(사진·설명·출처 배지)
              "closed": bool(date and (n, date) in CLOSED),       # 휴무면 카드에 🚫 표시
              "checked": mode == "confirm"}                      # 확인형=체크된 채로 / 후보제시형=비어 있는 채로
             for n in names if n not in tray]
    if not cards:
        return done(tray, tool_call_id, f"{already}은(는) 이미 트레이에 있어요")
    print(f"    [add_spots] 카드 {len(cards)}장 → interrupt (유저 선택 대기)")
    selected = interrupt({"type": "spot_" + mode, "date": date, "cards": cards})   # ⏸️ 한 메시지에 카드 묶음
    picked = [c["name"] for c in cards if c["name"] in selected]
    skipped = [c["name"] for c in cards if c["name"] not in selected]
    if not picked:
        return done(tray, tool_call_id, f"유저가 아무것도 담지 않기로 했어요 ({skipped})")
    new = tray + picked
    closed_picked = [c["name"] for c in cards if c["name"] in picked and c["closed"]]
    note = f" · 휴무지만 담음(휴무 표시 유지): {closed_picked}" if closed_picked else ""
    return done(new, tool_call_id,
                f"유저가 고름: {picked} / 안 고름: {skipped}{note} / 이미 있음: {already} → {new}", before=start)


@tool
def remove_spot(names: list[str],
                tray: Annotated[list, InjectedState("tray")],
                start: Annotated[list, InjectedState("req_start_tray")],
                tool_call_id: Annotated[str, InjectedToolCallId]):
    """트레이에서 스팟을 뺀다. 여러 개를 뺄 때는 한 번에 목록으로 넘긴다.
    names: 트레이에 있는 스팟 이름 그대로의 목록."""
    missing = [n for n in names if n not in tray]
    if missing:
        return done(tray, tool_call_id, f"오류: {missing}은(는) 트레이에 없어요. 아무것도 빼지 않았어요. 현재 트레이: {tray}")
    new = [s for s in tray if s not in names]
    return done(new, tool_call_id, f"{names} 뺌 → {new}", before=start)


@tool
def reorder_spot(name: str, position: int,
                 tray: Annotated[list, InjectedState("tray")],
                 start: Annotated[list, InjectedState("req_start_tray")],
                 tool_call_id: Annotated[str, InjectedToolCallId]):
    """스팟의 방문 순서를 바꾼다. position: 1부터 시작하는 새 순서 번호."""
    if name not in tray:
        return done(tray, tool_call_id, f"오류: {name}은(는) 트레이에 없어요. 현재 트레이: {tray}")
    rest = [s for s in tray if s != name]
    pos = max(1, min(position, len(tray)))
    new = rest[:pos - 1] + [name] + rest[pos - 1:]
    return done(new, tool_call_id, f"{name}을(를) {pos}번째로 → {new}", before=start)


@tool
def recheck_closure(name: str,
                    tray: Annotated[list, InjectedState("tray")],
                    tool_call_id: Annotated[str, InjectedToolCallId]):
    """스팟의 임시휴무·특별개관(Loop A)을 지금 다시 확인하도록 예약한다."""
    return done(tray, tool_call_id, f"{name} 예외 공지 확인을 예약했어요 (카드에 ⏳ 표시)")


@tool
def undo_last(prev_tray: Annotated[list | None, InjectedState("prev_tray")],
              tool_call_id: Annotated[str, InjectedToolCallId]):
    """직전 편집 한 번을 되돌린다. 유저가 '다시 넣어줘', '방금 거 취소' 등을 말하면 사용.
    빠진 스팟을 add_spots로 다시 넣지 말 것(원래 순서가 사라짐)."""
    if prev_tray is None:
        return Command(update={"messages": [ToolMessage("되돌릴 편집이 없어요", tool_call_id=tool_call_id)]})
    return Command(update={"tray": prev_tray, "prev_tray": None,      # 되돌린 뒤엔 비움 = 1단계
                           "messages": [ToolMessage(f"직전 편집을 되돌림 → {prev_tray}", tool_call_id=tool_call_id)]})


TOOLS = [add_spots, remove_spot, reorder_spot, recheck_closure, undo_last]


# ── ③ 전화기: 진짜 Claude 또는 대본대로 말하는 가짜 ──────────
class ScriptedClaude(FakeMessagesListChatModel):
    """API 키 없이 연습용. 정해진 응답을 순서대로 돌려준다(도구 요청 포함)."""
    def bind_tools(self, tools, **kw):
        return self


def make_llm(mode):
    if mode == "fake":
        return ScriptedClaude(responses=[
            AIMessage("", tool_calls=[{"name": "remove_spot", "args": {"names": ["金閣寺"]}, "id": "call_1"}]),
            AIMessage("", tool_calls=[{"name": "reorder_spot", "args": {"name": "清水寺", "position": 1}, "id": "call_2"}]),
            AIMessage("금각사를 빼고 청수사를 첫 번째로 옮겼어요. 순서가 바뀌었으니 이동시간을 다시 계산할게요."),
        ])
    from langchain_anthropic import ChatAnthropic
    # parallel_tool_calls=False: 한 번에 도구 하나만 요청하게 함 (Anthropic API의 disable_parallel_tool_use)
    # 도구 여러 개가 같은 트레이를 동시에 고치면 InvalidUpdateError — 아래 설명 참고
    return ChatAnthropic(model="claude-sonnet-5-5", max_tokens=1024).bind_tools(TOOLS, parallel_tool_calls=False)


MODE = sys.argv[1] if len(sys.argv) > 1 else "fake"
llm = make_llm(MODE)


# ── ④ 담당자 두 명 ──────────────────────────────────────
def agent(state: ChatState):
    """(요청 단위 되돌리기) 마지막 메시지가 유저 말이면 = 새 요청 시작 → 지금 트레이를 기억해 둔다.
    Claude에게 '지금 트레이 + 지금까지 대화'를 보여주고 다음 행동을 받는다.
    답이 도구 요청이면 → tools 노드로, 그냥 말이면 → 끝."""
    system = SystemMessage(
        "너는 여행 일정 편집을 돕는 어드바이저야. 트레이 편집은 반드시 도구로만 해. "
        "도구 결과를 확인하고, 끝나면 무엇을 바꿨는지 한두 문장으로 알려줘.\n"
        f"현재 트레이(방문 순서): {state['tray']}")
    resp = llm.invoke([system] + state["messages"])
    new_request = isinstance(state["messages"][-1], HumanMessage)
    calls = [f"{c['name']}({c['args']})" for c in resp.tool_calls] or ["(도구 없음 — 답변)"]
    print(f"  [agent] Claude → {', '.join(calls)}")
    out = {"messages": [resp]}
    if new_request:
        out["req_start_tray"] = state["tray"]
    return out


tool_node = ToolNode(TOOLS)     # 도구 요청을 받아 실제 함수를 실행해 주는 기성품 노드


# ── ⑤ 흐름도: agent ⇄ tools 루프 ─────────────────────────
builder = StateGraph(ChatState)
builder.add_node("agent", agent)
builder.add_node("tools", tool_node)
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": "__end__"})
builder.add_edge("tools", "agent")          # 도구 실행 후엔 항상 Claude에게 돌아가 결과를 보게 함
graph = builder.compile()


if __name__ == "__main__":
    # 대화형 실행: 채팅을 계속 입력할 수 있고, 휴무 칩이 뜨면 예/아니오를 입력 (같은 plan = 같은 thread_id)
    from langgraph.checkpoint.memory import InMemorySaver
    app = builder.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "plan-demo"}, "recursion_limit": 20}
    first = {"tray": ["金閣寺", "清水寺"], "prev_tray": None, "req_start_tray": [], "date": "2026-10-18"}
    print(f"##### 모드: {MODE} · 날짜 2026-10-18(일) · 트레이 {first['tray']} · 錦市場은 이날 휴무 #####")
    print("채팅을 입력하세요 (빈 줄 = 종료). 예: 니시키시장이랑 은각사 넣어줘 / 방금 거 취소해줘")
    if MODE == "fake":
        print("※ fake 모드는 대본 고정이라 첫 입력만 의미 있어요. 자유 대화는 sonnet 모드로.")
    while True:
        user = input("\n유저> ").strip()
        if not user:
            break
        inp = {"messages": [HumanMessage(user)], **first}
        first = {}
        try:
            out = app.invoke(inp, cfg)
            while "__interrupt__" in out:                          # ⏸️ 카드 — 답할 때까지 다른 입력은 받지 않음(잠금)
                q = out["__interrupt__"][0].value
                print(f"  ⏸️ 이 곳들 말씀하시는 걸까요? ({q['date']})")
                for i, c in enumerate(q["cards"], 1):
                    print(f"     [{i}] {'☑' if c['checked'] else '☐'} {c['name']}" + ("  🚫 이날 휴무예요" if c["closed"] else ""))
                ans = input("  담을 번호 (엔터 = 체크된 그대로, 0 = 아무것도 안 담기, 예: 1,3)> ").strip()
                if ans == "":
                    sel = [c["name"] for c in q["cards"] if c["checked"]]
                elif ans == "0":
                    sel = []
                else:
                    sel = [q["cards"][int(x) - 1]["name"] for x in ans.replace(" ", "").split(",") if x.isdigit()]
                out = app.invoke(Command(resume=sel), cfg)
        except GraphRecursionError:
            print("Claude> 요청이 너무 길어요. 나눠서 말씀해 주세요.")
            continue
        print(f"Claude> {out['messages'][-1].content}")
        print(f"  트레이: {out['tray']}")

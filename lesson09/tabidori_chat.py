"""9교시 후반 — 채팅 편집 에이전트 그래프 (7·8교시 코드를 웹 서버용으로 옮긴 것)

7·8교시와 달라진 점
  1) 노드가 async — 웹 서버(async def) 안에서 돌기 때문. 9교시 전반 /async-bad 사고를 막으려고
     Claude도 await llm.ainvoke(...)로 부른다.
  2) 체크포인터를 밖에서 받는다 — build_graph(checkpointer). 웹 서버가 PostgresSaver를 넘겨준다.
  3) 테스트용 모델(RuleModel) — 정해진 문장 몇 개만 알아듣고 정해진 도구를 부르는 모델.
     API 키·비용 없이 웹 흐름을 확인하고, 12교시 부하 테스트에서도 쓴다.
     진짜 Claude로 돌리려면 환경변수 MODEL=sonnet (.env의 ANTHROPIC_API_KEY 사용).
"""
import asyncio
import os
import uuid
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import MessagesState, START, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode, tools_condition
from langgraph.types import Command, interrupt

load_dotenv()


# ── ① state(서류철) ───────────────────────────────────────
class ChatState(MessagesState):          # messages 칸(대화 기록, 쌓임)이 이미 들어 있는 양식
    tray: list                           # 담은 스팟 (교체 방식)
    prev_tray: list | None               # 되돌리기 지점 (10교시에 plans.undo_snapshot으로 옮길 예정)
    req_start_tray: list                 # 이번 요청이 시작될 때의 트레이
    date: str | None                     # 여행 날짜
    miss_count: int                      # verify_places '못 찾음' 누적


# ── ② 가짜 데이터 (실서비스: 큐레이션 DB · Google Places) ────
CURATED = [
    {"name": "円山公園", "tags": ["노을", "공원", "벚꽃"]},
    {"name": "八坂神社", "tags": ["노을", "신사", "야경"]},
    {"name": "祇園白川", "tags": ["노을", "거리", "사진"]},
    {"name": "清水寺", "tags": ["전통", "사찰", "야경"]},
    {"name": "嵐山竹林", "tags": ["자연", "사진"]},
]
PLACES = {"銀閣寺", "哲学の道", "南禅寺", "伏見稲荷大社", "錦市場", "金閣寺"}
CLOSED = {("錦市場", "2026-10-18")}      # 가짜 정기휴무


# ── ③ 도구 (7·8교시와 같음) ────────────────────────────────
def done(tray, tool_call_id, text, before=None):
    update = {"tray": tray, "messages": [ToolMessage(text, tool_call_id=tool_call_id)]}
    if before is not None:
        update["prev_tray"] = before
    return Command(update=update)


@tool
def search_curated(keywords: list[str], tool_call_id: Annotated[str, InjectedToolCallId]):
    """큐레이션 DB에서 취향 키워드(노을·야경·전통·자연 등)로 스팟을 찾는다. 후보를 찾을 때 항상 이것부터 쓴다."""
    hits = [s["name"] for s in CURATED if set(keywords) & set(s["tags"])]
    text = f"큐레이션 DB 결과: {hits}" if hits else "큐레이션 DB에 맞는 곳 없음"
    return Command(update={"messages": [ToolMessage(text, tool_call_id=tool_call_id)]})


@tool
def verify_places(names: list[str],
                  miss: Annotated[int, InjectedState("miss_count")],
                  tool_call_id: Annotated[str, InjectedToolCallId]):
    """장소 이름이 실제로 존재하는지 Google Places로 확인한다. 확인된 곳만 유저에게 보여줄 수 있다.
    names: 일본어 정식 명칭 목록."""
    found = [n for n in names if n in PLACES]
    missing = [n for n in names if n not in PLACES]
    update = {"messages": [ToolMessage(f"실존 확인됨: {found} / 못 찾음: {missing}", tool_call_id=tool_call_id)]}
    if not found:
        update["miss_count"] = miss + 1
    return Command(update=update)


@tool
def add_spots(names: list[str],
              tray: Annotated[list, InjectedState("tray")],
              date: Annotated[str | None, InjectedState("date")],
              start: Annotated[list, InjectedState("req_start_tray")],
              tool_call_id: Annotated[str, InjectedToolCallId],
              mode: str = "confirm"):
    """트레이에 스팟을 추가한다. 항상 정보 카드로 유저 확인을 받은 뒤 고른 곳만 담는다.
    mode: "confirm" = 유저가 콕 집은 곳(체크된 채로) / "suggest" = 네가 찾은 후보(빈 채로)."""
    already = [n for n in names if n in tray]
    cards = [{"name": n, "closed": bool(date and (n, date) in CLOSED), "checked": mode == "confirm"}
             for n in names if n not in tray]
    if not cards:
        return done(tray, tool_call_id, f"{already}은(는) 이미 트레이에 있어요")
    selected = interrupt({"type": "spot_" + mode, "date": date, "cards": cards})   # ⏸️ 여기서 멈춤
    picked = [c["name"] for c in cards if c["name"] in selected]
    skipped = [c["name"] for c in cards if c["name"] not in selected]
    if not picked:
        return done(tray, tool_call_id, f"유저가 아무것도 담지 않기로 했어요 ({skipped})")
    new = tray + picked
    return done(new, tool_call_id, f"유저가 고름: {picked} / 안 고름: {skipped} → {new}", before=start)


@tool
def remove_spot(names: list[str],
                tray: Annotated[list, InjectedState("tray")],
                start: Annotated[list, InjectedState("req_start_tray")],
                tool_call_id: Annotated[str, InjectedToolCallId]):
    """트레이에서 스팟을 뺀다. 여러 개면 목록으로 한 번에."""
    missing = [n for n in names if n not in tray]
    if missing:
        return done(tray, tool_call_id, f"오류: {missing}은(는) 트레이에 없어요. 현재 트레이: {tray}")
    new = [s for s in tray if s not in names]
    return done(new, tool_call_id, f"{names} 뺌 → {new}", before=start)


@tool
def reorder_spot(name: str, position: int,
                 tray: Annotated[list, InjectedState("tray")],
                 start: Annotated[list, InjectedState("req_start_tray")],
                 tool_call_id: Annotated[str, InjectedToolCallId]):
    """스팟의 방문 순서를 바꾼다. position: 1부터 시작."""
    if name not in tray:
        return done(tray, tool_call_id, f"오류: {name}은(는) 트레이에 없어요. 현재 트레이: {tray}")
    rest = [s for s in tray if s != name]
    pos = max(1, min(position, len(tray)))
    new = rest[:pos - 1] + [name] + rest[pos - 1:]
    return done(new, tool_call_id, f"{name}을(를) {pos}번째로 → {new}", before=start)


@tool
def undo_last(prev_tray: Annotated[list | None, InjectedState("prev_tray")],
              tool_call_id: Annotated[str, InjectedToolCallId]):
    """직전 요청의 편집을 되돌린다. '다시 넣어줘', '방금 거 취소'에 사용."""
    if prev_tray is None:
        return Command(update={"messages": [ToolMessage("되돌릴 편집이 없어요", tool_call_id=tool_call_id)]})
    return Command(update={"tray": prev_tray, "prev_tray": None,
                           "messages": [ToolMessage(f"직전 편집을 되돌림 → {prev_tray}", tool_call_id=tool_call_id)]})


TOOLS = [search_curated, verify_places, add_spots, remove_spot, reorder_spot, undo_last]


# ── ④ 테스트용 모델: 정해진 문장만 알아듣는 '규칙 모델' ──────────
KO = {"금각사": "金閣寺", "청수사": "清水寺", "니시키": "錦市場", "은각사": "銀閣寺", "철학의 길": "哲学の道",
      "남선사": "南禅寺", "후시미": "伏見稲荷大社", "마루야마": "円山公園", "야사카": "八坂神社", "기온": "祇園白川"}
HELP = ("테스트용 모델이라 이런 문장만 알아들어요: '노을 보기 좋은 데 있을까?' · '니시키랑 은각사 넣어줘' · "
        "'금각사 빼줘' · '청수사 맨 앞으로' · '방금 거 취소'")


def call(name, **args):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": "call_" + uuid.uuid4().hex[:8]}])


class RuleModel:
    """Claude 대신 쓰는 테스트용 모델. 마지막 유저 말 + 그 뒤에 실행된 도구 수(step)를 보고 다음 행동을 정한다.
    delay = Claude 응답 시간 흉내(초) — 12교시 부하 테스트에서 쓴다."""

    def __init__(self, delay=0.0):
        self.delay = delay

    async def decide(self, state):
        await asyncio.sleep(self.delay)                      # ✅ await — 기다리는 동안 다른 요청 처리
        msgs = state["messages"]
        last_human = max(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage))
        text = msgs[last_human].content
        step = sum(isinstance(m, ToolMessage) for m in msgs[last_human:])
        names = [ja for ko, ja in KO.items() if ko in text]
        tray = state["tray"]

        if "노을" in text:
            if step == 0:
                return call("search_curated", keywords=["노을"])
            if step == 1:
                return call("add_spots", names=["円山公園", "八坂神社", "祇園白川"], mode="suggest")
        elif "취소" in text or "되돌" in text:
            if step == 0:
                return call("undo_last")
        elif "빼" in text and names:
            if step == 0:
                return call("remove_spot", names=names)
        elif "맨 앞" in text and names:
            if step == 0:
                return call("reorder_spot", name=names[0], position=1)
        elif ("넣어" in text or "담아" in text or "추가" in text) and names:
            if step == 0:
                return call("verify_places", names=names)
            if step == 1:
                return call("add_spots", names=names, mode="confirm")
        else:
            return AIMessage(HELP)
        last_tool = next(m for m in reversed(msgs) if isinstance(m, ToolMessage)).content
        if last_tool.startswith("오류") or "되돌릴 편집이 없어요" in last_tool or "아무것도" in last_tool:
            return AIMessage(last_tool)                        # 도구가 못 한 일은 그대로 전달
        return AIMessage(f"반영했어요. 지금 트레이: {', '.join(tray) or '(비어 있음)'}")


SYSTEM = (
    "너는 교토 여행 어드바이저야. 트레이 편집과 스팟 담기는 반드시 도구로만 해.\n"
    "- 유저가 취향만 말하면: search_curated → (없으면) verify_places → 확인된 곳만 add_spots(mode='suggest').\n"
    "- 유저가 장소를 콕 집으면: verify_places로 정식 명칭 확인 → add_spots(mode='confirm').\n"
    "- 말이 너무 모호하면 도구 없이 짧게 되물어.\n"
    "현재 트레이: {tray} / 여행 날짜: {date}"
)


def make_model():
    """MODEL=sonnet이면 진짜 Claude, 아니면 테스트용 규칙 모델."""
    if os.getenv("MODEL") == "sonnet":
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model="claude-sonnet-5-5", max_tokens=1024).bind_tools(TOOLS, parallel_tool_calls=False)

        async def decide(state):
            sys_msg = SystemMessage(SYSTEM.format(tray=state["tray"], date=state["date"]))
            return await llm.ainvoke([sys_msg] + state["messages"])   # ✅ ainvoke
        return decide
    return RuleModel(delay=float(os.getenv("FAKE_DELAY", "0.5"))).decide


# ── ⑤ 그래프 ─────────────────────────────────────────────
def build_graph(checkpointer):
    decide = make_model()

    async def agent(state: ChatState):
        resp = await decide(state)
        out = {"messages": [resp]}
        if isinstance(state["messages"][-1], HumanMessage):     # 새 요청 시작 → 지금 트레이 기억
            out["req_start_tray"] = state["tray"]
        return out

    builder = StateGraph(ChatState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": "__end__"})
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


FIRST_STATE = {"tray": ["清水寺"], "prev_tray": None, "req_start_tray": [], "date": "2026-10-18", "miss_count": 0}

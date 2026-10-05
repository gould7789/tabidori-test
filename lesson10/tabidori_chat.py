"""10교시 — 채팅 편집 에이전트 그래프 (트레이를 state에서 빼고 DB 테이블로)

9교시와 달라진 점
  1) state에서 tray · prev_tray · req_start_tray · date를 뺐다.
     트레이·날짜·되돌리기 지점은 tray_items · plans 테이블에 있다 (tray_db.py).
     체크포인트에는 이 그래프가 직접 진행하는 대화 상태만 남는다: 대화 기록, 확인 카드(interrupt), miss_count.
  2) 도구는 config로 '지금 어느 plan인지(thread_id)'를 받아 DB를 읽고 쓴다.
     config 인자는 Claude에게 보이는 도구 설명에서 숨겨진다 → Claude가 plan을 지어낼 수 없다.
  3) 되돌리기 단위 = 유저 말 한 마디. 그 말(HumanMessage)의 id를 turn 번호로 쓴다.
     확인 카드에 답해서 이어지는 실행도 같은 말이라 같은 turn → "빼고 넣어줘"도 되돌리기 한 번에 취소.
  4) add_spots는 interrupt를 먼저, DB 쓰기를 뒤에. 재개하면 이 도구가 첫 줄부터 다시 실행되기 때문
     (interrupt 앞에서 쓰면 두 번 쓰게 됨).
"""
import asyncio
import os
import uuid
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import MessagesState, START, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode, tools_condition
from langgraph.types import Command, interrupt

import tray_db

load_dotenv()


# ── ① state(서류철) — 대화에 관한 것만 ─────────────────────────
class ChatState(MessagesState):
    miss_count: int                      # verify_places '못 찾음' 누적 (대화 안에서만 의미 있음)


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


def plan_of(config):
    return config["configurable"]["thread_id"]           # thread_id = plan_id


def turn_of(messages):
    """지금 처리 중인 유저 말의 id = 되돌리기 단위(turn)."""
    return next(m.id for m in reversed(messages) if isinstance(m, HumanMessage))


# ── ③ 도구 ─────────────────────────────────────────────────
class ToolError(Exception):
    """편집이 불가능할 때 — 트랜잭션을 취소(ROLLBACK)하고 Claude에게 이유를 돌려준다."""


@tool
def search_curated(keywords: list[str]):
    """큐레이션 DB에서 취향 키워드(노을·야경·전통·자연 등)로 스팟을 찾는다. 후보를 찾을 때 항상 이것부터 쓴다."""
    hits = [s["name"] for s in CURATED if set(keywords) & set(s["tags"])]
    return f"큐레이션 DB 결과: {hits}" if hits else "큐레이션 DB에 맞는 곳 없음"


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
        update["miss_count"] = (miss or 0) + 1
    return Command(update=update)


@tool
async def add_spots(names: list[str], config: RunnableConfig,
                    messages: Annotated[list, InjectedState("messages")],
                    mode: str = "confirm"):
    """트레이에 스팟을 추가한다. 항상 정보 카드로 유저 확인을 받은 뒤 고른 곳만 담는다.
    mode: "confirm" = 유저가 콕 집은 곳(체크된 채로) / "suggest" = 네가 찾은 후보(빈 채로)."""
    plan = await tray_db.read_plan(plan_of(config))                  # 읽기는 두 번 해도 괜찮음
    tray, date = plan["tray"], plan["date"]
    already = [n for n in names if n in tray]
    cards = [{"name": n, "closed": bool(date and (n, date) in CLOSED), "checked": mode == "confirm"}
             for n in names if n not in tray]
    if not cards:
        return f"{already}은(는) 이미 트레이에 있어요"

    selected = interrupt({"type": "spot_" + mode, "date": date, "cards": cards})   # ⏸️ 여기서 멈춤
    # ── 여기부터는 유저가 답한 뒤(재개 때)에만 실행된다. DB 쓰기는 반드시 이 아래에 ──
    picked = [c["name"] for c in cards if c["name"] in selected]
    skipped = [c["name"] for c in cards if c["name"] not in selected]
    if not picked:
        return f"유저가 아무것도 담지 않기로 했어요 ({skipped})"
    new = await tray_db.edit_tray(plan_of(config), turn_of(messages),
                                  lambda t: t + [p for p in picked if p not in t])
    return f"유저가 고름: {picked} / 안 고름: {skipped} → {new}"


@tool
async def remove_spot(names: list[str], config: RunnableConfig,
                      messages: Annotated[list, InjectedState("messages")]):
    """트레이에서 스팟을 뺀다. 여러 개면 목록으로 한 번에."""
    def change(tray):
        missing = [n for n in names if n not in tray]
        if missing:
            raise ToolError(f"오류: {missing}은(는) 트레이에 없어요. 현재 트레이: {tray}")
        return [s for s in tray if s not in names]
    try:
        new = await tray_db.edit_tray(plan_of(config), turn_of(messages), change)
    except ToolError as e:
        return str(e)
    return f"{names} 뺌 → {new}"


@tool
async def reorder_spot(name: str, position: int, config: RunnableConfig,
                       messages: Annotated[list, InjectedState("messages")]):
    """스팟의 방문 순서를 바꾼다. position: 1부터 시작."""
    def change(tray):
        if name not in tray:
            raise ToolError(f"오류: {name}은(는) 트레이에 없어요. 현재 트레이: {tray}")
        return move(tray, name, position)
    try:
        new = await tray_db.edit_tray(plan_of(config), turn_of(messages), change)
    except ToolError as e:
        return str(e)
    return f"{name}을(를) {position}번째로 → {new}"


@tool
async def undo_last(config: RunnableConfig):
    """직전 행동의 편집을 되돌린다. '다시 넣어줘', '방금 거 취소'에 사용."""
    restored = await tray_db.undo(plan_of(config))
    return "되돌릴 편집이 없어요" if restored is None else f"직전 편집을 되돌림 → {restored}"


def move(tray, name, position):
    """순서 변경 계산 — 채팅 도구와 버튼이 같이 쓴다."""
    rest = [s for s in tray if s != name]
    pos = max(1, min(position, len(tray)))
    return rest[:pos - 1] + [name] + rest[pos - 1:]


TOOLS = [search_curated, verify_places, add_spots, remove_spot, reorder_spot, undo_last]


# ── ④ 테스트용 모델: 정해진 문장만 알아듣는 '규칙 모델' ──────────
KO = {"금각사": "金閣寺", "청수사": "清水寺", "니시키": "錦市場", "은각사": "銀閣寺", "철학의 길": "哲学の道",
      "남선사": "南禅寺", "후시미": "伏見稲荷大社", "마루야마": "円山公園", "야사카": "八坂神社", "기온": "祇園白川"}
HELP = ("테스트용 모델이라 이런 문장만 알아들어요: '노을 보기 좋은 데 있을까?' · '니시키랑 은각사 넣어줘' · "
        "'금각사 빼줘' · '청수사 맨 앞으로' · '은각사 빼고 니시키 맨 앞으로' · '방금 거 취소'")


def call(tool_name, **args):
    return AIMessage("", tool_calls=[{"name": tool_name, "args": args, "id": "call_" + uuid.uuid4().hex[:8]}])


def names_in(text):
    """문장에 나온 순서대로 스팟 이름(일본어)을 뽑는다."""
    found = sorted((text.find(ko), ja) for ko, ja in KO.items() if ko in text)
    return [ja for _, ja in found]


class RuleModel:
    """Claude 대신 쓰는 테스트용 모델. 마지막 유저 말 + 그 뒤에 실행된 도구 수(step)를 보고 다음 행동을 정한다.
    delay = Claude 응답 시간 흉내(초) — 12교시 부하 테스트에서 쓴다."""

    def __init__(self, delay=0.0):
        self.delay = delay

    async def decide(self, state, tray, date):
        await asyncio.sleep(self.delay)
        msgs = state["messages"]
        last_human = max(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage))
        text = msgs[last_human].content
        step = sum(isinstance(m, ToolMessage) for m in msgs[last_human:])
        names = names_in(text)

        if "노을" in text:
            if step == 0:
                return call("search_curated", keywords=["노을"])
            if step == 1:
                return call("add_spots", names=["円山公園", "八坂神社", "祇園白川"], mode="suggest")
        elif "취소" in text or "되돌" in text:
            if step == 0:
                return call("undo_last")
        elif "빼" in text and "맨 앞" in text and len(names) >= 2:    # 한 마디에 편집 두 번
            if step == 0:
                return call("remove_spot", names=[names[0]])
            if step == 1:
                return call("reorder_spot", name=names[1], position=1)
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
            return AIMessage(last_tool)
        return AIMessage(f"반영했어요. 지금 트레이: {', '.join(tray) or '(비어 있음)'}")


SYSTEM = (
    "너는 교토 여행 어드바이저야. 트레이 편집과 스팟 담기는 반드시 도구로만 해.\n"
    "- 유저가 취향만 말하면: search_curated → (없으면) verify_places → 확인된 곳만 add_spots(mode='suggest').\n"
    "- 유저가 장소를 콕 집으면: verify_places로 정식 명칭 확인 → add_spots(mode='confirm').\n"
    "- '다시 넣어줘'·'취소'는 add_spots가 아니라 undo_last.\n"
    "- 말이 너무 모호하면 도구 없이 짧게 되물어.\n"
    "현재 트레이: {tray} / 여행 날짜: {date}"
)


def make_model():
    """MODEL=sonnet이면 진짜 Claude, 아니면 테스트용 규칙 모델."""
    if os.getenv("MODEL") == "sonnet":
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model="claude-sonnet-5-5", max_tokens=1024).bind_tools(TOOLS, parallel_tool_calls=False)

        async def decide(state, tray, date):
            sys_msg = SystemMessage(SYSTEM.format(tray=tray, date=date))
            return await llm.ainvoke([sys_msg] + state["messages"])
        return decide
    return RuleModel(delay=float(os.getenv("FAKE_DELAY", "0.5"))).decide


# ── ⑤ 그래프 ─────────────────────────────────────────────
def build_graph(checkpointer):
    decide = make_model()

    async def agent(state: ChatState, config: RunnableConfig):
        plan = await tray_db.read_plan(plan_of(config))    # 매 바퀴 DB에서 지금 트레이를 읽어 Claude에게 줌
        return {"messages": [await decide(state, plan["tray"], plan["date"])]}

    builder = StateGraph(ChatState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": "__end__"})
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


FIRST_TRAY = ["清水寺"]
FIRST_DATE = "2026-10-18"

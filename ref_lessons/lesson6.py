"""6교시: FastAPI로 그래프 감싸기 — 담기 + "그래도 담을까요?" (interrupt)를 HTTP 요청 두 번으로

실행:  uvicorn lesson6:app --reload      → 브라우저에서 http://127.0.0.1:8000/docs
(API 키 불필요 — 휴무 조회는 가짜 함수)
"""
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver


# ════════════════════════════════════════════════════════
# [LangGraph 런타임 영역] — 3교시 그래프 거의 그대로
# ════════════════════════════════════════════════════════
class PlanState(TypedDict):
    tray: list                 # 담은 스팟들 (교체 방식 — 빼기·순서변경이 있으니까)
    spot: str                  # 지금 담으려는 스팟
    date: Optional[str]        # 여행 날짜 (없을 수도 있음)
    closed: bool
    message: str               # 프론트에 보여줄 안내 문구


CLOSED = {("錦市場", "2026-10-18")}          # 가짜 정기휴무 데이터 (실서비스: Places API)


def check_closure(state):
    closed = (state["spot"], state["date"]) in CLOSED
    print(f"  [check_closure] {state['spot']} {state['date']} → {'휴무' if closed else '영업'}")
    return {"closed": closed}


def ask_force(state):
    answer = interrupt(f"{state['date']}은 {state['spot']} 휴무예요 · 그래도 담을까요?")
    return {"message": answer}


def add_to_tray(state):
    return {"tray": state["tray"] + [{"spot": state["spot"], "closed": False}],
            "message": f"{state['spot']} 담았어요"}


def add_forced(state):
    return {"tray": state["tray"] + [{"spot": state["spot"], "closed": True}],
            "message": f"{state['spot']} 그래도 담았어요. 확정하기 전에 한 번 더 알려드릴게요"}


def reject(state):
    return {"message": f"{state['spot']}은(는) 담지 않았어요"}


def route_after_check(state):
    return "휴무" if state["closed"] else "영업"


def route_after_ask(state):
    return "예" if state["message"] == "예" else "아니오"


builder = StateGraph(PlanState)
for name, fn in [("check_closure", check_closure), ("ask_force", ask_force),
                 ("add_to_tray", add_to_tray), ("add_forced", add_forced), ("reject", reject)]:
    builder.add_node(name, fn)
builder.add_edge(START, "check_closure")
builder.add_conditional_edges("check_closure", route_after_check, {"휴무": "ask_force", "영업": "add_to_tray"})
builder.add_conditional_edges("ask_force", route_after_ask, {"예": "add_forced", "아니오": "reject"})
for n in ("add_to_tray", "add_forced", "reject"):
    builder.add_edge(n, END)

graph = builder.compile(checkpointer=InMemorySaver())   # 서버가 켜질 때 한 번만 만든다


# ════════════════════════════════════════════════════════
# [FastAPI 백엔드 영역] — 요청을 받아 그래프를 부르고, 결과를 JSON으로 돌려줌
# ════════════════════════════════════════════════════════
app = FastAPI(title="타비도리 6교시")


class AddRequest(BaseModel):          # 프론트가 보내는 요청 양식 (FastAPI가 자동 검사)
    spot: str
    date: Optional[str] = None


class AnswerRequest(BaseModel):
    answer: str                       # "예" 또는 "아니오" (칩 버튼)


def cfg(plan_id: str):
    return {"configurable": {"thread_id": plan_id}, "recursion_limit": 20}


def to_response(result: dict) -> dict:
    """그래프 결과(서류철) → 프론트에 줄 JSON. 멈췄으면 질문을, 끝났으면 트레이를."""
    if "__interrupt__" in result:
        return {"status": "확인필요", "question": result["__interrupt__"][0].value,
                "chips": ["예", "아니오"]}
    return {"status": "완료", "message": result["message"], "tray": result["tray"]}


@app.post("/plans/{plan_id}/spots")
async def add_spot(plan_id: str, req: AddRequest):
    """요청 1: 스팟 담기. 휴무면 그래프가 interrupt에서 멈추고 질문을 돌려준다."""
    state = await graph.aget_state(cfg(plan_id))
    if state.next:                                             # 아직 답을 안 한 질문이 있으면 거절
        raise HTTPException(409, "먼저 위 질문(그래도 담을까요?)에 답해 주세요")
    first = not state.values                                   # 이 plan의 첫 요청인가?
    inp = {"spot": req.spot, "date": req.date, "message": ""}
    if first:
        inp["tray"] = []
    result = await graph.ainvoke(inp, cfg(plan_id))
    return to_response(result)


@app.post("/plans/{plan_id}/answer")
async def answer(plan_id: str, req: AnswerRequest):
    """요청 2: 칩 버튼 답. 멈춘 자리(ask_force)에서 이어간다."""
    state = await graph.aget_state(cfg(plan_id))
    if not state.next:                                         # 기다리는 질문이 없는데 답이 오면 거절
        raise HTTPException(409, "지금 답을 기다리는 질문이 없어요")
    result = await graph.ainvoke(Command(resume=req.answer), cfg(plan_id))
    return to_response(result)


@app.get("/plans/{plan_id}")
async def get_plan(plan_id: str):
    """보관함(체크포인터)에 저장된 이 plan의 서류철 조회."""
    state = await graph.aget_state(cfg(plan_id))
    return {"tray": state.values.get("tray", []), "waiting_for": list(state.next)}

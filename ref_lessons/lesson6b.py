"""6교시 후반: 오래 걸리는 Loop A를 뒤에서 돌리기 — 담기는 즉시, 카드엔 ⏳, 끝나면 배지 교체

실행:  uvicorn lesson6b:app --reload   → http://127.0.0.1:8000/docs
(API 키 불필요 — Loop A는 몇 초 기다렸다 정해진 답을 주는 가짜)
"""
import asyncio
import random
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver


# ════════════════════════════════════════════════════════
# [LangGraph 런타임] ① 담기 그래프 (plan마다 서류철 하나, 체크포인터 보관)
# ════════════════════════════════════════════════════════
class PlanState(TypedDict):
    tray: list                            # [{"spot":..., "closed":...}] — 교체 방식
    spot: str
    date: Optional[str]
    closed: bool
    message: str


CLOSED = {("錦市場", "2026-10-18")}


def check_closure(state):
    return {"closed": (state["spot"], state["date"]) in CLOSED}


def ask_force(state):
    return {"message": interrupt(f"{state['date']}은 {state['spot']} 휴무예요 · 그래도 담을까요?")}


def add_to_tray(state):
    return {"tray": state["tray"] + [{"spot": state["spot"], "closed": False}], "message": f"{state['spot']} 담았어요"}


def add_forced(state):
    return {"tray": state["tray"] + [{"spot": state["spot"], "closed": True}], "message": f"{state['spot']} 그래도 담았어요"}


def reject(state):
    return {"message": f"{state['spot']}은(는) 담지 않았어요"}


b = StateGraph(PlanState)
for n, f in [("check_closure", check_closure), ("ask_force", ask_force), ("add_to_tray", add_to_tray),
             ("add_forced", add_forced), ("reject", reject)]:
    b.add_node(n, f)
b.add_edge(START, "check_closure")
b.add_conditional_edges("check_closure", lambda s: "휴무" if s["closed"] else "영업",
                        {"휴무": "ask_force", "영업": "add_to_tray"})
b.add_conditional_edges("ask_force", lambda s: "예" if s["message"] == "예" else "아니오",
                        {"예": "add_forced", "아니오": "reject"})
for n in ("add_to_tray", "add_forced", "reject"):
    b.add_edge(n, END)
plan_graph = b.compile(checkpointer=InMemorySaver())


# ════════════════════════════════════════════════════════
# [LangGraph 런타임] ② Loop A 그래프 (가짜 — 실제로는 5교시 lesson5_v2의 graph)
#    스팟 하나를 받아 배지 하나를 돌려주는 독립 그래프. plan 서류철을 모름(스테이트리스).
# ════════════════════════════════════════════════════════
class LoopAState(TypedDict):
    spot: str
    trip_date: str
    badge: str


FAKE = {"清水寺": "🔵 특별 개관·연장", "金閣寺": "🟢 공식 공지 확인 · 변동 없음", "錦市場": "🟡 예외 공지 없음 (검색 기준)"}


async def fake_loop_a(state):
    await asyncio.sleep(random.uniform(2, 5))                 # 공식 사이트 열람·검색하는 척
    return {"badge": FAKE.get(state["spot"], "⚪ 확인불가")}


la = StateGraph(LoopAState)
la.add_node("loop_a", fake_loop_a)
la.add_edge(START, "loop_a")
la.add_edge("loop_a", END)
loop_a_graph = la.compile()                                    # 체크포인터 없음 — 한 번 돌고 끝


# ════════════════════════════════════════════════════════
# [FastAPI 백엔드]
# ════════════════════════════════════════════════════════
app = FastAPI(title="타비도리 6교시 후반")

# Loop A 결과 보관소 — plan 서류철(체크포인터)이 아니라 별도 저장소. 실서비스: DB 테이블 (plan_id, spot, date, badge)
# 서류철에 넣지 않는 이유: 질문 대기 중인 서류철을 뒤에서 고치면 대기 중인 질문 기록이 사라짐(실측, 설명 참고)
LOOP_A_RESULTS: dict = {}          # {plan_id: {"清水寺|2026-10-18": "🔵 ...", ...}}


class AddRequest(BaseModel):
    spot: str
    date: Optional[str] = None


class AnswerRequest(BaseModel):
    answer: str


def cfg(plan_id):
    return {"configurable": {"thread_id": plan_id}, "recursion_limit": 20}


def key(spot, date):
    return f"{spot}|{date}"                                    # Loop A 결과는 (스팟, 날짜) 쌍에 묶는다 (7-3절)


async def run_loop_a(plan_id: str, spot: str, date: str):
    """백그라운드 작업: Loop A를 돌리고, 결과를 별도 보관소에 한 줄 추가한다."""
    out = await loop_a_graph.ainvoke({"spot": spot, "trip_date": date, "badge": ""}, {"recursion_limit": 12})
    LOOP_A_RESULTS.setdefault(plan_id, {})[key(spot, date)] = out["badge"]   # 행 하나 추가 — 다른 결과를 안 건드림
    print(f"  [Loop A 완료] {plan_id} {spot} → {out['badge']}")


async def view(plan_id: str, result: Optional[dict] = None) -> dict:
    """프론트용 JSON. 트레이 카드마다 배지를 붙여서 준다 (결과가 아직 없으면 ⏳)."""
    st = await plan_graph.aget_state(cfg(plan_id))
    v = st.values
    date, done = v.get("date"), LOOP_A_RESULTS.get(plan_id, {})
    cards = []
    for item in v.get("tray", []):
        if not date:
            badge = "날짜를 정하면 확인해요"
        else:
            badge = done.get(key(item["spot"], date), "⏳ 확인 중")
        cards.append({**item, "badge": badge})
    resp = {"tray": cards}
    if st.next:                                                # 질문 대기 중이면 칩도 같이
        resp.update(status="확인필요", question=st.tasks[0].interrupts[0].value, chips=["예", "아니오"])
    else:
        resp.update(status="완료", message=v.get("message", ""))
    return resp


def schedule_if_added(bg: BackgroundTasks, plan_id: str, before: int, result: dict, date: Optional[str]):
    """트레이가 늘었고 날짜가 있으면 Loop A 예약 (강행으로 담은 스팟도 — 7-1절)."""
    tray = result.get("tray", [])
    if date and len(tray) > before:
        bg.add_task(run_loop_a, plan_id, tray[-1]["spot"], date)


@app.post("/plans/{plan_id}/spots")
async def add_spot(plan_id: str, req: AddRequest, bg: BackgroundTasks):
    st = await plan_graph.aget_state(cfg(plan_id))
    if st.next:
        raise HTTPException(409, "먼저 위 질문(그래도 담을까요?)에 답해 주세요")
    before = len(st.values.get("tray", []))
    inp = {"spot": req.spot, "date": req.date, "message": ""}
    if not st.values:
        inp["tray"] = []
    result = await plan_graph.ainvoke(inp, cfg(plan_id))
    schedule_if_added(bg, plan_id, before, result, req.date)   # 응답을 보낸 '뒤에' 실행됨
    return await view(plan_id)


@app.post("/plans/{plan_id}/answer")
async def answer(plan_id: str, req: AnswerRequest, bg: BackgroundTasks):
    st = await plan_graph.aget_state(cfg(plan_id))
    if not st.next:
        raise HTTPException(409, "지금 답을 기다리는 질문이 없어요")
    before = len(st.values.get("tray", []))
    result = await plan_graph.ainvoke(Command(resume=req.answer), cfg(plan_id))
    schedule_if_added(bg, plan_id, before, result, st.values.get("date"))
    return await view(plan_id)


@app.get("/plans/{plan_id}")
async def get_plan(plan_id: str):
    """프론트가 몇 초마다 부르는 조회(폴링). ⏳가 배지로 바뀌었는지 확인."""
    return await view(plan_id)

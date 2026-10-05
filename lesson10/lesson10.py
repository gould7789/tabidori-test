"""10교시 — 트레이를 tray_items 테이블로, 되돌리기 지점을 plans.undo_snapshot으로

실행 (README 참고)
  1) docker compose up -d db               ← proto 폴더 맨 위에서 (DB만)
  2) cd lesson10 && uvicorn lesson10:app
  3) 브라우저 http://127.0.0.1:8000        (다른 터미널에서 python3 lesson10_check.py)

편집 진입점 여러 개, 공용 함수 두 개
  채팅 "은각사 빼줘"   → 채팅 그래프 → remove_spot 도구 ─┐
  트레이 ✕ 버튼        → POST /edit ─────────────────────┤
  트레이 ⠿ 드래그      → POST /reorder ──────────────────┼→ tray_db.edit_tray()  (잠금 + 지점 저장 + 교체, 한 트랜잭션)
  채팅 "방금 거 취소"  → undo_last 도구 ─────────────────┐
  [↶ 되돌리기] 버튼    → POST /undo ─────────────────────┴→ tray_db.undo()
"""
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import tray_db
from tabidori_chat import FIRST_DATE, FIRST_TRAY, build_graph

DB_URL = os.getenv("DATABASE_URL", "postgresql://tabidori:tabidori@localhost:5433/tabidori")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = AsyncConnectionPool(
        conninfo=DB_URL, max_size=10, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()                       # 체크포인트 테이블 (LangGraph가 관리)
    tray_db.set_pool(pool)
    await tray_db.setup()                     # plans · tray_items 테이블 (우리가 관리)
    app.state.graph = build_graph(saver)
    app.state.saver = saver
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan)


def cfg(plan_id: str):
    return {"configurable": {"thread_id": plan_id}, "recursion_limit": 20}


async def pending_card(request: Request, plan_id: str):
    snap = await request.app.state.graph.aget_state(cfg(plan_id))
    return snap, (snap.interrupts[0].value if snap.interrupts else None)


async def guard(request: Request, plan_id: str):
    """확인 카드 대기 중엔 이 plan을 바꾸는 요청 전부 409 (채팅·버튼 편집·되돌리기)."""
    snap, card = await pending_card(request, plan_id)
    if card:
        raise HTTPException(409, "먼저 위 확인 카드에 답해 주세요")
    return snap


async def view(request: Request, plan_id: str, error: str | None = None):
    snap, card = await pending_card(request, plan_id)
    bubbles = []
    for m in (snap.values or {}).get("messages", []):
        if isinstance(m, HumanMessage):
            bubbles.append({"who": "user", "text": m.content})
        elif isinstance(m, AIMessage) and m.content and not m.tool_calls:
            bubbles.append({"who": "ai", "text": m.content})
    plan = await tray_db.read_plan(plan_id)                    # 트레이는 이제 체크포인트가 아니라 테이블에서
    return {"request": request, "plan_id": plan_id, "bubbles": bubbles, "pending": card,
            "tray": plan["tray"], "date": plan["date"], "can_undo": plan["can_undo"],
            "error": error, "pid": os.getpid()}


def render(request, ctx):
    return templates.TemplateResponse(request, "_main.html", ctx)


@app.get("/")
async def root():
    return RedirectResponse("/plans/demo")


@app.get("/plans/{plan_id}", response_class=HTMLResponse)
async def page(request: Request, plan_id: str):
    await tray_db.ensure_plan(plan_id, FIRST_DATE, FIRST_TRAY)
    return templates.TemplateResponse(request, "page.html", await view(request, plan_id))


@app.post("/plans/{plan_id}/chat", response_class=HTMLResponse)
async def chat(request: Request, plan_id: str, message: str = Form(...)):
    snap = await guard(request, plan_id)
    await tray_db.ensure_plan(plan_id, FIRST_DATE, FIRST_TRAY)
    inp = {"messages": [HumanMessage(message, id=str(uuid.uuid4()))]}   # 이 말의 id = 되돌리기 단위(turn)
    if not snap.values:
        inp["miss_count"] = 0
    error = None
    try:
        await request.app.state.graph.ainvoke(inp, cfg(plan_id))
    except GraphRecursionError:
        error = "요청이 너무 길어요. 나눠서 말씀해 주세요."
    return render(request, await view(request, plan_id, error))


@app.post("/plans/{plan_id}/answer", response_class=HTMLResponse)
async def answer(request: Request, plan_id: str, selected: list[str] = Form(default=[])):
    _, card = await pending_card(request, plan_id)
    if not card:
        raise HTTPException(409, "답할 질문이 없어요")
    await request.app.state.graph.ainvoke(Command(resume=selected), cfg(plan_id))
    return render(request, await view(request, plan_id))


@app.post("/plans/{plan_id}/edit", response_class=HTMLResponse)
async def edit(request: Request, plan_id: str, action: str = Form(...), spot: str = Form(...)):
    """트레이 ✕ 버튼 (빼기). 버튼 한 번 = 행동 하나 = 새 turn."""
    await guard(request, plan_id)
    if action != "remove":
        raise HTTPException(400, "알 수 없는 편집")
    await tray_db.edit_tray(plan_id, "btn-" + uuid.uuid4().hex,
                            lambda tray: [s for s in tray if s != spot])   # 이미 없으면 변화 없음 (더블클릭 등)
    return render(request, await view(request, plan_id))


@app.post("/plans/{plan_id}/reorder", response_class=HTMLResponse)
async def reorder(request: Request, plan_id: str, order: list[str] = Form(...)):
    """트레이 드래그 (⠿ 손잡이). 드래그가 끝나면 화면의 새 순서 전체가 온다. 드래그 한 번 = 새 turn."""
    await guard(request, plan_id)

    def change(tray):
        if sorted(order) != sorted(tray):     # 그 사이 다른 탭·채팅이 트레이를 바꿨으면 이 화면은 낡은 것
            return tray                       # → 무시하고 지금 트레이를 다시 그려 보냄
        return order
    await tray_db.edit_tray(plan_id, "drag-" + uuid.uuid4().hex, change)
    return render(request, await view(request, plan_id))


@app.post("/plans/{plan_id}/undo", response_class=HTMLResponse)
async def undo(request: Request, plan_id: str):
    await guard(request, plan_id)
    await tray_db.undo(plan_id)
    return render(request, await view(request, plan_id))


@app.post("/plans/{plan_id}/reset", response_class=HTMLResponse)
async def reset(request: Request, plan_id: str):
    await request.app.state.saver.adelete_thread(plan_id)    # 대화·확인 카드 (체크포인트)
    await tray_db.delete_plan(plan_id)                        # 트레이·되돌리기 지점 (우리 테이블)
    await tray_db.ensure_plan(plan_id, FIRST_DATE, FIRST_TRAY)
    return render(request, await view(request, plan_id))

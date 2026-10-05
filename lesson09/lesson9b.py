"""9교시 후반 — 채팅 그래프를 웹 서버에 붙이고, 체크포인터를 PostgreSQL로

실행 순서 (README 참고)
  1) docker compose up -d db                    ← proto 폴더 맨 위에서. PostgreSQL 컨테이너만 시작
  2) cd lesson09 && uvicorn lesson9b:app        ← 웹 서버
  3) 브라우저 http://127.0.0.1:8000

요청 흐름
  브라우저(htmx) ─POST /plans/{id}/chat─▶ 웹 서버(FastAPI) ─await ainvoke─▶ 채팅 그래프(LangGraph)
                                                 ▲                              │ 체크포인트 저장·읽기
                                                 └──── HTML 조각 ◀──────────────┘    ▼
                                                                              PostgreSQL
"""
import os
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

from tabidori_chat import FIRST_STATE, build_graph

# 로컬 개발용 DB 주소 — docker-compose.yml과 같은 값. 실서비스에선 환경변수로만 넣는다.
DB_URL = os.getenv("DATABASE_URL", "postgresql://tabidori:tabidori@localhost:5433/tabidori")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버(프로세스)가 켜질 때 한 번: DB 연결 풀을 열고, 체크포인트 테이블을 만들고, 그래프를 compile."""
    pool = AsyncConnectionPool(
        conninfo=DB_URL, max_size=10, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},  # PostgresSaver가 요구하는 설정
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()                       # checkpoints 등 테이블 생성 (이미 있으면 그대로)
    app.state.graph = build_graph(saver)
    app.state.saver = saver
    yield                                     # ← 여기서 서버가 요청을 받는 동안 대기
    await pool.close()                        # 서버가 꺼질 때


app = FastAPI(lifespan=lifespan)


def cfg(plan_id: str):
    return {"configurable": {"thread_id": plan_id}, "recursion_limit": 20}


async def view(request: Request, plan_id: str, error: str | None = None):
    """체크포인트에서 지금 상태를 읽어 화면에 필요한 것만 뽑는다 (서류철을 그대로 내보내지 않음)."""
    snap = await request.app.state.graph.aget_state(cfg(plan_id))
    values = snap.values or {}
    bubbles = []
    for m in values.get("messages", []):
        if isinstance(m, HumanMessage):
            bubbles.append({"who": "user", "text": m.content})
        elif isinstance(m, AIMessage) and m.content and not m.tool_calls:
            bubbles.append({"who": "ai", "text": m.content})
    pending = snap.interrupts[0].value if snap.interrupts else None   # ⏸️ 대기 중인 확인 카드
    return {"request": request, "plan_id": plan_id, "bubbles": bubbles, "pending": pending,
            "tray": values.get("tray", FIRST_STATE["tray"]), "date": values.get("date", FIRST_STATE["date"]),
            "error": error, "pid": os.getpid()}


@app.get("/")
async def root():
    return RedirectResponse("/plans/demo")


@app.get("/plans/{plan_id}", response_class=HTMLResponse)
async def page(request: Request, plan_id: str):
    return templates.TemplateResponse(request, "page.html", await view(request, plan_id))


@app.post("/plans/{plan_id}/chat", response_class=HTMLResponse)
async def chat(request: Request, plan_id: str, message: str = Form(...)):
    graph = request.app.state.graph
    snap = await graph.aget_state(cfg(plan_id))
    if snap.interrupts:                                    # 서버 가드: 확인 카드 대기 중엔 거절 (6교시 409)
        raise HTTPException(409, "먼저 위 확인 카드에 답해 주세요")
    inp = {"messages": [HumanMessage(message)]}
    if not snap.values:                                    # 이 plan의 첫 메시지 → 초기 트레이·날짜 넣기
        inp |= FIRST_STATE
    error = None
    try:
        await graph.ainvoke(inp, cfg(plan_id))             # ✅ await ainvoke — 다른 유저 요청을 막지 않음
    except GraphRecursionError:
        error = "요청이 너무 길어요. 나눠서 말씀해 주세요."
    return templates.TemplateResponse(request, "_main.html", await view(request, plan_id, error))


@app.post("/plans/{plan_id}/answer", response_class=HTMLResponse)
async def answer(request: Request, plan_id: str, selected: list[str] = Form(default=[])):
    graph = request.app.state.graph
    snap = await graph.aget_state(cfg(plan_id))
    if not snap.interrupts:                                # 대기 중인 질문이 없는데 답이 옴 (더블클릭 등)
        raise HTTPException(409, "답할 질문이 없어요")
    await graph.ainvoke(Command(resume=selected), cfg(plan_id))   # 멈춘 add_spots부터 이어감
    return templates.TemplateResponse(request, "_main.html", await view(request, plan_id))


@app.post("/plans/{plan_id}/reset", response_class=HTMLResponse)
async def reset(request: Request, plan_id: str):
    await request.app.state.saver.adelete_thread(plan_id)  # 이 plan의 체크포인트 전부 삭제
    return templates.TemplateResponse(request, "_main.html", await view(request, plan_id))

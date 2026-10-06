"""11교시 — Loop A 그래프 (테스트용)

진짜 Loop A(공식 사이트 열람 → 웹검색 → 판정, ref_lessons/lesson5_v2.py)는 Claude 호출·검색 비용이 든다.
이번 교시의 목적은 '워커 + 작업표'가 제대로 도는지라서, 정해진 시간 동안 기다렸다가 정해진 배지를 돌려주는
테스트용 그래프를 쓴다. 그래프 모양(스팟 하나 → 배지 하나, 체크포인터 없음)은 진짜와 같다.

  LOOPA_SECONDS=5   한 스팟 확인에 걸리는 시간(초) 흉내. 기본 5초
  LOOPA_FAIL=錦市場  이 스팟은 일부러 오류를 낸다 (재시도·실패 실험용, 쉼표로 여러 개)
"""
import asyncio
import os
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from tabidori_chat import CLOSED

SECONDS = float(os.getenv("LOOPA_SECONDS", "5"))
FAIL = set(filter(None, os.getenv("LOOPA_FAIL", "").split(",")))

# 진짜 Loop A의 배지 표(lesson5_v2.BADGE)와 같은 문구
FAKE = {
    "清水寺": "🔵 특별 개관·연장",
    "金閣寺": "🟢 공식 공지 확인 · 변동 없음",
    "銀閣寺": "🟢 공식 공지 확인 · 변동 없음",
    "錦市場": "🟡 예외 공지 없음 (검색 기준)",
}


class LoopAState(TypedDict):
    spot: str
    trip_date: str
    badge: str


async def check(state: LoopAState):
    await asyncio.sleep(SECONDS)                          # ✅ await — 기다리는 동안 같은 워커가 다른 작업도 진행
    if state["spot"] in FAIL:
        raise RuntimeError(f"테스트용 오류: {state['spot']}")
    if (state["spot"], state["trip_date"]) in CLOSED:
        return {"badge": "🔴 휴관"}
    return {"badge": FAKE.get(state["spot"], "⚪ 확인불가")}


g = StateGraph(LoopAState)
g.add_node("check", check)
g.add_edge(START, "check")
g.add_edge("check", END)
loop_a_graph = g.compile()                                # 체크포인터 없음 — 한 번 돌고 끝 (스테이트리스)

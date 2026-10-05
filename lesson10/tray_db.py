"""10교시 — 트레이와 되돌리기 지점을 우리 테이블에 둔다

테이블 두 개
  plans       plan 하나 = 한 줄. 날짜, 되돌리기 지점(undo_snapshot, JSONB 한 칸)
  tray_items  트레이의 스팟 하나 = 한 줄. position = 몇 번째인지 (1부터)

  - 트레이는 스팟마다 따로 빠지고 순서가 바뀌므로 한 줄씩 저장(정규화).
  - 되돌리기 지점은 '그 순간 트레이의 사본'이라 통째로 저장·복원만 하므로 JSONB 한 칸.
  - 프로토타입에선 스팟을 이름(text)으로 저장. 실서비스에선 spot_id(큐레이션 FK) 또는 place_id.

편집 규칙
  1) plans 줄을 SELECT ... FOR UPDATE로 잠근다 → 같은 plan의 다른 편집은 끝날 때까지 기다림
  2) 지금 트레이를 읽고, 새 트레이를 파이썬에서 계산
  3) 되돌리기 지점 저장 + 트레이 교체를 한 트랜잭션으로 → 둘 다 되거나 둘 다 안 됨
  되돌리기 단위 = 유저의 행동 하나(turn). 같은 turn 안의 두 번째 편집은 되돌리기 지점을 덮어쓰지 않는다.
"""
import asyncio
import json
import os

# 실험용 스위치 (기본값 = 실서비스 동작)
#   EDIT_DELAY=0.5  읽기와 쓰기 사이에 0.5초 쉼 → 다른 탭이 끼어들 틈을 일부러 만듦
#   LOCK=off        FOR UPDATE를 빼고 그냥 SELECT → 갱신 손실이 실제로 일어나는지 확인
EDIT_DELAY = float(os.getenv("EDIT_DELAY", "0"))
LOCK = "" if os.getenv("LOCK") == "off" else " FOR UPDATE"

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id        text PRIMARY KEY,
    date           date,
    undo_snapshot  jsonb,          -- 되돌리기 지점: 편집 직전 트레이 (없으면 NULL)
    undo_turn      text,           -- 그 지점을 만든 행동(turn) 번호
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tray_items (
    plan_id   text NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
    position  int  NOT NULL,       -- 트레이에서 몇 번째인지 (1부터)
    spot      text NOT NULL,
    PRIMARY KEY (plan_id, position),
    UNIQUE (plan_id, spot)         -- 같은 스팟 두 번 담기 방지
);
"""

POOL = None                      # 웹 서버가 켜질 때 lifespan에서 넣어 줌 (set_pool)


def set_pool(pool):
    global POOL
    POOL = pool


async def setup():
    async with POOL.connection() as conn:
        for stmt in SCHEMA.split(";"):                      # 문장 하나씩 실행 (psycopg는 한 번에 여러 문장 불가)
            if stmt.strip():
                await conn.execute(stmt)


async def ensure_plan(plan_id, date, first_tray):
    """plan이 없으면 만든다 (프로토타입용 시작 데이터)."""
    async with POOL.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "INSERT INTO plans (plan_id, date) VALUES (%s, %s) ON CONFLICT DO NOTHING", [plan_id, date])
        if cur.rowcount:                                     # 방금 새로 만든 경우만
            await _write_tray(conn, plan_id, first_tray)


async def read_plan(plan_id):
    """화면·Claude에게 줄 값: 트레이(순서대로), 날짜, 되돌릴 게 있는지."""
    async with POOL.connection() as conn:
        plan = await (await conn.execute(
            "SELECT date, undo_snapshot IS NOT NULL AS can_undo FROM plans WHERE plan_id = %s", [plan_id])).fetchone()
        tray = await _read_tray(conn, plan_id)
    if plan is None:
        return {"tray": [], "date": None, "can_undo": False}
    return {"tray": tray, "date": plan["date"].isoformat() if plan["date"] else None, "can_undo": plan["can_undo"]}


async def edit_tray(plan_id, turn_id, change):
    """트레이 편집 공용 함수 — 채팅 도구와 버튼이 둘 다 이걸 부른다.
    change: 지금 트레이(list)를 받아 새 트레이(list)를 돌려주는 함수."""
    async with POOL.connection() as conn, conn.transaction():          # BEGIN … COMMIT
        plan = await (await conn.execute(
            "SELECT undo_turn FROM plans WHERE plan_id = %s" + LOCK, [plan_id])).fetchone()   # 🔒 잠금
        before = await _read_tray(conn, plan_id)
        if EDIT_DELAY:
            await asyncio.sleep(EDIT_DELAY)
        after = change(before)
        if after == before:                                  # 바뀐 게 없으면 되돌리기 지점도 그대로
            return before
        if plan["undo_turn"] != turn_id:                     # 이 행동의 첫 편집일 때만 지점 저장
            await conn.execute("UPDATE plans SET undo_snapshot = %s, undo_turn = %s WHERE plan_id = %s",
                               [json.dumps(before, ensure_ascii=False), turn_id, plan_id])
        await _write_tray(conn, plan_id, after)
        return after
    # with 블록을 빠져나올 때 COMMIT. 중간에 예외가 나면 ROLLBACK → 지점·트레이 둘 다 원래대로


async def undo(plan_id):
    """되돌리기 — 지점의 트레이로 복원하고 지점을 비운다(1단계). 되돌릴 게 없으면 None."""
    async with POOL.connection() as conn, conn.transaction():
        plan = await (await conn.execute(
            "SELECT undo_snapshot FROM plans WHERE plan_id = %s FOR UPDATE", [plan_id])).fetchone()
        if plan is None or plan["undo_snapshot"] is None:
            return None
        await _write_tray(conn, plan_id, plan["undo_snapshot"])
        await conn.execute("UPDATE plans SET undo_snapshot = NULL, undo_turn = NULL WHERE plan_id = %s", [plan_id])
        return plan["undo_snapshot"]


async def delete_plan(plan_id):
    async with POOL.connection() as conn:
        await conn.execute("DELETE FROM plans WHERE plan_id = %s", [plan_id])   # tray_items도 같이 (CASCADE)


async def _read_tray(conn, plan_id):
    rows = await (await conn.execute(
        "SELECT spot FROM tray_items WHERE plan_id = %s ORDER BY position", [plan_id])).fetchall()
    return [r["spot"] for r in rows]


async def _write_tray(conn, plan_id, tray):
    """트레이 통째 교체: 지우고 position 1, 2, 3 … 으로 다시 넣는다 (반드시 트랜잭션 안에서 부를 것)."""
    await conn.execute("DELETE FROM tray_items WHERE plan_id = %s", [plan_id])
    if tray:
        async with conn.cursor() as cur:
            await cur.executemany("INSERT INTO tray_items (plan_id, position, spot) VALUES (%s, %s, %s)",
                                  [(plan_id, i, s) for i, s in enumerate(tray, start=1)])

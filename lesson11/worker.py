"""11교시 — 워커: 작업표(loop_a_jobs)에서 일을 꺼내 Loop A 그래프를 돌리고 결과를 같은 줄에 적는다

실행
  python3 worker.py                          ← 웹 서버와 다른 터미널에서. 끌 때는 Ctrl+C
  WORKER_CONCURRENCY=3 python3 worker.py     ← 이 워커 하나가 동시에 맡는 작업 수

워커는 요청을 받지 않는다. 켜져 있는 동안 계속 돌면서 스스로 작업표를 확인한다.

  ┌─▶ 대기 작업 하나 꺼내기 (FOR UPDATE SKIP LOCKED) ── 없음 ─▶ POLL초 쉬고 다시
  │        │ 있음
  │        ▼
  │   Loop A 그래프 실행 (수십 초)
  │        │
  │        ▼
  └── 결과를 같은 줄에 기록 ('완료') → 쉬지 않고 바로 다음 작업 확인

워커가 죽어도 작업은 안 사라진다
  작업 줄은 DB에 '실행 중'으로 남는다. 살아 있는 워커가 STALE초마다 확인해서,
  STALE초 넘게 '실행 중'인 줄을 '대기'로 되돌린다 → 누군가 다시 꺼내 간다.
  시간 기준인 이유: 워커가 여러 개일 때 다른 워커가 멀쩡히 하고 있는 작업을 뺏지 않으려고.
  → STALE은 Loop A가 정상적으로 걸리는 최대 시간보다 길어야 한다.
"""
import asyncio
import os
import socket
import sys

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import tray_db
from loop_a import loop_a_graph

DB_URL = os.getenv("DATABASE_URL", "postgresql://tabidori:tabidori@localhost:5433/tabidori")
POLL = float(os.getenv("WORKER_POLL", "0.5"))              # 작업이 없을 때 쉬는 시간(초) — 잠정값, 부하 테스트로 확정
CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "2"))    # 이 워커가 동시에 맡는 작업 수
STALE = int(os.getenv("WORKER_STALE", "60"))               # 이 시간(초) 넘게 '실행 중'이면 죽은 작업으로 본다
MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "3"))  # 이만큼 실패하면 '실패'로 끝냄
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

CLAIM = """
UPDATE loop_a_jobs
   SET status = 'running', started_at = now(), attempts = attempts + 1, worker = %s
 WHERE id = (SELECT id FROM loop_a_jobs
              WHERE status = 'queued'
              ORDER BY id
              FOR UPDATE SKIP LOCKED      -- 다른 워커가 방금 잡은 줄은 건너뛰고 다음 줄
              LIMIT 1)
RETURNING id, plan_id, spot, date, attempts
"""
# '실행 중' 줄을 내가 맡은 경우에만 고친다 (worker = 나). 내가 너무 오래 걸려 남에게 넘어갔다면 덮어쓰지 않음.
DONE = """UPDATE loop_a_jobs SET status = 'done', badge = %s, finished_at = now()
           WHERE id = %s AND worker = %s AND status = 'running'"""
FAIL = """UPDATE loop_a_jobs SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'queued' END,
                                 badge = CASE WHEN attempts >= %s THEN '⚪ 확인불가' ELSE NULL END,
                                 finished_at = CASE WHEN attempts >= %s THEN now() END
           WHERE id = %s AND worker = %s AND status = 'running'
       RETURNING status"""
RECOVER = """UPDATE loop_a_jobs SET status = 'queued'
              WHERE status = 'running' AND started_at < now() - make_interval(secs => %s)
          RETURNING id, spot, worker"""


def log(*args):
    print(f"[워커 {WORKER_ID}]", *args, flush=True)


async def run_one(pool, job):
    """작업 하나: Loop A 실행 → 결과 기록. 오류면 다시 '대기'로(최대 MAX_ATTEMPTS번)."""
    log(f"시작  #{job['id']} {job['spot']} {job['date']} ({job['attempts']}번째)")
    try:
        out = await loop_a_graph.ainvoke({"spot": job["spot"], "trip_date": job["date"].isoformat(), "badge": ""},
                                         {"recursion_limit": 12})
    except Exception as e:                                   # Loop A가 실패해도 워커는 죽지 않는다
        async with pool.connection() as conn:
            row = await (await conn.execute(FAIL, [MAX_ATTEMPTS] * 3 + [job["id"], WORKER_ID])).fetchone()
        log(f"오류  #{job['id']} {job['spot']}: {e} → {row['status'] if row else '이미 다른 워커에게 넘어감'}")
        return
    async with pool.connection() as conn:
        cur = await conn.execute(DONE, [out["badge"], job["id"], WORKER_ID])
    log(f"완료  #{job['id']} {job['spot']} → {out['badge']}" + ("" if cur.rowcount else " (이미 다른 워커에게 넘어가 기록 안 함)"))


async def slot(pool, n):
    """작업 칸 하나: 꺼내기 → 실행을 끝없이 반복. CONCURRENCY만큼 동시에 돈다."""
    while True:
        async with pool.connection() as conn:
            job = await (await conn.execute(CLAIM, [WORKER_ID])).fetchone()   # autocommit: 이 한 문장이 곧 트랜잭션
        if job is None:
            await asyncio.sleep(POLL)                        # 할 일 없음 → 잠깐 쉬고 다시 확인
            continue
        await run_one(pool, job)                             # 일이 있었으면 쉬지 않고 바로 다음 확인


async def recover_loop(pool):
    """멈춘 작업 되살리기: 켜질 때 한 번 + 그 뒤로 주기적으로."""
    while True:
        async with pool.connection() as conn:
            rows = await (await conn.execute(RECOVER, [STALE])).fetchall()
        for r in rows:
            log(f"복구  #{r['id']} {r['spot']} — {STALE}초 넘게 '실행 중'(맡았던 워커 {r['worker']}) → '대기'로 되돌림")
        await asyncio.sleep(max(1, STALE / 4))


async def main():
    pool = AsyncConnectionPool(conninfo=DB_URL, min_size=1, max_size=CONCURRENCY + 2, open=False,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    await pool.open()
    tray_db.set_pool(pool)
    await tray_db.setup()                                    # 웹 서버보다 먼저 켜져도 테이블이 준비되게
    log(f"켜짐 — 동시 {CONCURRENCY}개, 빈 작업표면 {POLL}초마다 확인, {STALE}초 넘게 멈춘 작업은 복구")
    try:
        await asyncio.gather(recover_loop(pool), *(slot(pool, n) for n in range(CONCURRENCY)))
    finally:
        await pool.close()


if __name__ == "__main__":
    # 윈도우의 기본 이벤트 루프(Proactor)에서는 psycopg 비동기 연결이 동작하지 않아 Selector 루프를 쓴다.
    # (docker compose로 띄우면 컨테이너는 리눅스라 상관없음)
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:    # = asyncio.run(main()) + 루프 종류 지정
            runner.run(main())
    except KeyboardInterrupt:
        log("꺼짐 (실행 중이던 작업은 DB에 '실행 중'으로 남고, 복구 규칙이 되살림)")

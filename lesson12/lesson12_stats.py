"""12교시 — 부하 테스트가 끝난 뒤 작업표(loop_a_jobs)에서 워커 쪽 숫자 보기

  python3 lesson12_stats.py          이번 시험(plan 이름이 lt-로 시작)의 작업 통계
  python3 lesson12_stats.py clear    lt- plan 전부 지우기 (다음 시험 전에)

Locust는 "브라우저가 본 응답 시간"만 잰다. 작업이 표에서 얼마나 기다렸는지는 DB 시각으로 따로 본다.
  집기까지   = started_at - created_at   (등록 → 워커가 꺼내 감)  ← 워커 수가 모자라면 여기가 늘어난다
  실행       = finished_at - started_at  (Loop A 실행 시간)       ← 테스트용 Loop A면 LOOPA_SECONDS 근처
"""
import os
import sys

import psycopg
from psycopg.rows import dict_row

DB_URL = os.getenv("DATABASE_URL", "postgresql://tabidori:tabidori@localhost:5433/tabidori")

with psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True) as conn:
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        n = conn.execute("DELETE FROM plans WHERE plan_id LIKE 'lt-%'").rowcount
        conn.execute("DELETE FROM checkpoint_writes WHERE thread_id LIKE 'lt-%'")
        conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id LIKE 'lt-%'")
        conn.execute("DELETE FROM checkpoints WHERE thread_id LIKE 'lt-%'")
        print(f"lt- plan {n}개와 그 대화 기록을 지웠어요.")
        sys.exit()

    by_status = conn.execute("""SELECT status, count(*) AS n FROM loop_a_jobs
                                 WHERE plan_id LIKE 'lt-%' GROUP BY status ORDER BY status""").fetchall()
    print("작업 상태:", ", ".join(f"{r['status']} {r['n']}" for r in by_status) or "(없음)")

    row = conn.execute("""
        SELECT count(*)                                                                         AS n,
               count(*) FILTER (WHERE attempts > 1)                                             AS retried,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM started_at - created_at))  AS wait_p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM started_at - created_at))  AS wait_p95,
               max(extract(epoch FROM started_at - created_at))                                 AS wait_max,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)) AS run_p50,
               extract(epoch FROM max(finished_at) - min(created_at))                           AS span
          FROM loop_a_jobs
         WHERE plan_id LIKE 'lt-%' AND status IN ('done', 'failed')""").fetchone()
    if not row["n"]:
        print("끝난 작업이 없어요.")
        sys.exit()
    f = lambda x: f"{x:.2f}초" if x is not None else "-"
    print(f"끝난 작업 {row['n']}개  (두 번 이상 실행 {row['retried']}개 — 0이어야 정상)")
    print(f"집기까지  p50 {f(row['wait_p50'])} · p95 {f(row['wait_p95'])} · 최대 {f(row['wait_max'])}")
    print(f"실행      p50 {f(row['run_p50'])}")
    print(f"처리량    {row['n'] / row['span']:.2f}개/초  (첫 등록 ~ 마지막 완료 {f(row['span'])})")

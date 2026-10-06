"""11교시 — 코드로 확인 (웹 서버 + 워커를 켠 상태에서)

  python3 lesson11_check.py           기본 흐름: 담기 → ⏳ → 워커가 처리 → 폴링이 배지를 가져오고 286으로 멈춤
  python3 lesson11_check.py burst     작업 18개를 한꺼번에 → 워커가 몇 개든 작업마다 딱 한 번씩만 실행됐는지
  python3 lesson11_check.py jobs      작업표 지금 상태 보기 (실험하면서 수시로)
"""
import os
import re
import sys
import time

import httpx
import psycopg
from psycopg.rows import dict_row

BASE = "http://127.0.0.1:8000"
DB_URL = os.getenv("DATABASE_URL", "postgresql://tabidori:tabidori@localhost:5433/tabidori")


def tray(html):
    """트레이 조각에서 (스팟, 배지) 목록."""
    return re.findall(r'<span class="spot">(.*?)</span>\s*<span class="badge">(.*?)</span>', html, re.S)


def db(sql, args=()):
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        return conn.execute(sql, args).fetchall()


def add_two(c, plan):
    """니시키·은각사를 담는다 (채팅 → 확인 카드 → 둘 다 골라 답함)."""
    c.post(f"/plans/{plan}/chat", data={"message": "니시키랑 은각사 넣어줘"})
    return c.post(f"/plans/{plan}/answer", data={"selected": ["錦市場", "銀閣寺"]}).text


def poll_until_done(c, plan, every=1.0, limit=60):
    """브라우저가 하는 것과 똑같이 GET /tray를 주기적으로 부른다. 286이 오면 끝."""
    t0, n = time.time(), 0
    while time.time() - t0 < limit:
        r = c.get(f"/plans/{plan}/tray")
        n += 1
        if r.status_code == 286:
            return r.text, n, time.time() - t0
        time.sleep(every)
    raise SystemExit("시간 안에 끝나지 않았어요 — 워커가 켜져 있나요? (python3 worker.py)")


def show_jobs(where="", args=()):
    rows = db(f"""SELECT id, plan_id, spot, status, attempts, worker, badge,
                         round(extract(epoch FROM finished_at - started_at)::numeric, 1) AS secs
                    FROM loop_a_jobs {where} ORDER BY id""", args)
    for r in rows:
        print(f"   #{r['id']:<4} {r['plan_id']:<10} {r['spot']:<6} {r['status']:<8} 시도 {r['attempts']}  "
              f"{r['worker'] or '-':<22} {r['badge'] or '':<28} {r['secs'] or '':>5}")
    return rows


mode = sys.argv[1] if len(sys.argv) > 1 else "basic"

if mode == "jobs":
    show_jobs()

elif mode == "basic":
    P = "check11"
    with httpx.Client(base_url=BASE, timeout=30) as c:
        html = c.post(f"/plans/{P}/reset").text
        print("① 시작:", tray(html), "| 폴링 켜짐:", 'hx-trigger="every 1s"' in html)
        html, n, secs = poll_until_done(c, P)
        print(f"   워커가 처리 → {tray(html)}  (폴링 {n}번, {secs:.1f}초, 마지막 응답 286)")

        html = add_two(c, P)
        print("② 니시키·은각사 담음:", tray(html))
        html, n, secs = poll_until_done(c, P)
        print(f"   → {tray(html)}  (폴링 {n}번, {secs:.1f}초)")
        print("   폴링 멈춘 뒤 화면에 폴링 속성 남았나:", 'hx-trigger="every 1s"' in html)

        before = len(db("SELECT 1 FROM loop_a_jobs WHERE plan_id = %s", [P]))
        c.post(f"/plans/{P}/edit", data={"action": "remove", "spot": "銀閣寺"})
        html = c.post(f"/plans/{P}/undo").text
        after = len(db("SELECT 1 FROM loop_a_jobs WHERE plan_id = %s", [P]))
        print("③ 은각사 빼고 되돌리기 →", tray(html), f"| 작업 수 {before} → {after} (재확인 없이 기존 배지)")

        print("④ 작업표:")
        show_jobs("WHERE plan_id = %s", [P])

elif mode == "burst":
    plans = [f"burst{i}" for i in range(6)]
    with httpx.Client(base_url=BASE, timeout=60) as c:
        for p in plans:
            c.post(f"/plans/{p}/reset")
            add_two(c, p)
        t0 = time.time()
        print(f"작업 {len(plans) * 3}개 등록 (plan {len(plans)}개 × 스팟 3곳). 끝날 때까지 기다리는 중…")
        for p in plans:
            poll_until_done(c, p, every=0.5, limit=300)
        print(f"전부 끝: {time.time() - t0:.1f}초")
    rows = db("""SELECT worker, count(*) AS n, max(attempts) AS max_attempts
                   FROM loop_a_jobs WHERE plan_id LIKE 'burst%%' GROUP BY worker ORDER BY worker""")
    for r in rows:
        print(f"   {r['worker']:<24} {r['n']}개 처리")
    dup = db("SELECT count(*) AS n FROM loop_a_jobs WHERE plan_id LIKE 'burst%%' AND attempts > 1")[0]["n"]
    print("두 번 이상 실행된 작업:", dup, "(0이어야 정상 — SKIP LOCKED 덕분에 같은 줄을 두 워커가 잡지 않음)")

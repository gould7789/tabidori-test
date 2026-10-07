"""12교시 — 진짜 Loop A 소요 시간 재기 (Claude API 사용 — 비용 발생, 약 $0.3~0.5)

  python3 measure_loop_a.py

5교시 Loop A(ref_lessons/lesson5_v2.py)를 '전부 Sonnet' 모드(8절 결정)로 5곳에 한 번씩 돌리고
스팟마다 전체 시간 + 노드(단계)별 시간 + 비용을 적는다. API 키는 proto/.env에서 읽는다(lesson5_v2의 load_dotenv).

이 숫자로 정하는 것
  - 하트비트 판정 시간(WORKER_DEAD)이 Loop A보다 짧아도 되는지 확인 (하트비트 방식이라 원래 상관없어야 함)
  - 워커 수: 워커가 1초에 끝내는 작업 수 = 동시 실행 수 ÷ Loop A 평균 시간
  - 사용자가 ⏳를 보는 시간

주의: 9/30에 정한 "검색 뒤 공식 페이지 다시 열람"(8-6절)은 이 코드에 아직 없다 → 검색 경로는 실제로 더 길어질 수 있음.
"""
import importlib.util
import statistics
import sys
import time
from pathlib import Path

# ../ref_lessons/lesson5_v2.py를 파일 경로로 직접 불러온다.
# (그냥 `import lesson5_v2`라고 쓰면 다른 폴더라서 파이썬도, 편집기(VS Code)도 찾지 못한다)
LESSON5 = Path(__file__).resolve().parent.parent / "ref_lessons" / "lesson5_v2.py"
if not LESSON5.exists():
    sys.exit(f"Loop A 코드를 찾을 수 없어요: {LESSON5}")
sys.argv = [sys.argv[0], "sonnet"]                                   # lesson5_v2는 불러올 때 모드를 sys.argv에서 읽는다
spec = importlib.util.spec_from_file_location("lesson5_v2", LESSON5)
la = importlib.util.module_from_spec(spec)
spec.loader.exec_module(la)                                          # 이 순간 lesson5_v2의 load_dotenv()가 proto/.env를 읽음

# (스팟, 여행 날짜, 공식 URL, 정기 정보) — 처음 4곳은 9/30에 공식 사이트를 확인한 곳
SPOTS = [
    ("京都国立博物館", "2026-10-18", "https://www.kyohaku.go.jp/", "9:30~17:00, 월요일 휴관"),
    ("金閣寺", "2026-10-18", "https://www.shokoku-ji.jp/kinkakuji/", "9:00~17:00, 연중무휴"),
    ("清水寺", "2026-11-25", "https://www.kiyomizudera.or.jp/", "6:00~18:00 (구글맵 현재 시즌 기준)"),
    ("伏見稲荷大社", "2026-10-18", None, "24시간 개방"),
    ("錦市場", "2026-10-18", None, "점포마다 다름 (대략 9:00~18:00)"),
]


def cost_of(usage):
    c = 0.0
    for u in usage:
        pin, pout = la.PRICES[u["model"]]
        c += u["in"] / 1e6 * pin + u["out"] / 1e6 * pout + u["searches"] * la.SEARCH_PRICE
    return c


rows = []
tok_rows = []
for spot, date, url, hours in SPOTS:
    print(f"=== {spot} ({date}) ===", flush=True)
    state = {"spot": spot, "trip_date": date, "url": url, "regular_hours": hours, "material": "", "evidence": "",
             "kind": "", "quote": "", "rejected_quote": "", "path": [], "usage": []}
    t0 = last = time.perf_counter()
    steps, final = [], dict(state)
    # stream: 노드 하나가 끝날 때마다 그 노드의 결과가 온다 → 노드별 시간을 잴 수 있음
    for update in la.graph.stream(state, {"recursion_limit": 12}, stream_mode="updates"):
        now = time.perf_counter()
        for node, out in update.items():
            steps.append((node, now - last))
            for k, v in (out or {}).items():
                final[k] = final[k] + v if k in ("path", "usage") else v
        last = now
    total = time.perf_counter() - t0
    cost = cost_of(final["usage"])
    rows.append((spot, total, cost, steps, la.badge_of(final)))
    print("  단계: " + " → ".join(f"{n} {s:.1f}초" for n, s in steps if s >= 0.05))
    # 단계별 토큰 — 어디서 입력 토큰(=비용)이 나가는지
    print("  토큰: " + " · ".join(f"{u['step']} 입력 {u['in']:,}/출력 {u['out']:,}" + (f"/검색 {u['searches']}회" if u['searches'] else "")
                                  for u in final["usage"]))
    tok_rows.extend(final["usage"])
    print(f"  배지: {la.badge_of(final)}   전체 {total:.1f}초   약 ${cost:.4f}\n", flush=True)

times = [r[1] for r in rows]
print("──── 요약 ────")
for spot, total, cost, steps, badge in rows:
    print(f"  {spot:<8} {total:5.1f}초  ${cost:.4f}  {badge}")
print(f"  평균 {statistics.mean(times):.1f}초 · 가장 빠름 {min(times):.1f}초 · 가장 느림 {max(times):.1f}초 · 합계 ${sum(r[2] for r in rows):.4f}")
by_node = {}
for *_, steps, _ in rows:
    for n, s in steps:
        by_node.setdefault(n, []).append(s)
print("  단계별 평균: " + ", ".join(f"{n} {statistics.mean(v):.1f}초({len(v)}회)" for n, v in by_node.items() if max(v) >= 0.05))
by_step = {}
for u in tok_rows:
    by_step.setdefault(u["step"], []).append(u)
print("  단계별 평균 토큰: " + ", ".join(
    f"{k} 입력 {statistics.mean(x['in'] for x in v):,.0f}/출력 {statistics.mean(x['out'] for x in v):,.0f}({len(v)}회)"
    for k, v in by_step.items()))
total_in = sum(u["in"] for u in tok_rows)
print(f"  스팟당 평균 입력 {total_in / len(rows):,.0f}토큰 · 출력 {sum(u['out'] for u in tok_rows) / len(rows):,.0f}토큰")

"""9교시 실험 1 — 측정용 클라이언트. 서버를 띄운 뒤 다른 터미널에서 실행."""
import asyncio
import time

import httpx

BASE = "http://127.0.0.1:8000"


async def burst(path, n=5):
    """같은 주소로 요청 n개를 '동시에' 보내고 전부 끝날 때까지 걸린 시간을 잰다."""
    async with httpx.AsyncClient(timeout=60) as c:
        t = time.perf_counter()
        rs = await asyncio.gather(*[c.get(BASE + path) for _ in range(n)])
        sec = time.perf_counter() - t
    pids = sorted({r.json()["pid"] for r in rs})
    print(f"{path:12s} 동시 {n}개 → {sec:4.1f}초  (처리한 프로세스 PID: {pids})")


async def main():
    # 1) 프로세스마다 메모리가 따로인가?
    print("── /whoami 10번 (요청마다 새 연결로, 하나씩 차례로)")
    for _ in range(10):
        async with httpx.AsyncClient() as c:  # 새 연결 → 운영체제가 어느 프로세스에 줄지 고름
            r = (await c.get(BASE + "/whoami")).json()
        print(f"   PID {r['pid']}  이 프로세스의 카운터 = {r['count_in_this_process']}")
    # 2) 막히는 코드 vs 기다리는 코드
    print("── 2초짜리 작업 5개를 동시에")
    for path in ["/sync", "/async-bad", "/async-good"]:
        await burst(path)


asyncio.run(main())

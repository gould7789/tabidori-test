"""12교시 — 부하 테스트 시나리오 (Locust)

Locust: 파이썬으로 "가상 사용자 한 명이 하는 일"을 적어 두면, 그 사용자를 N명 동시에 띄워
요청마다 응답 시간·실패 수를 재 주는 부하 테스트 도구. (https://locust.io)

가상 사용자 한 명이 반복하는 일 (한 바퀴 = 새 plan 하나)
  1) 화면 열기            GET  /plans/{id}           → 처음 트레이 [清水寺] + Loop A 작업 1개
  2) 채팅으로 담기         POST /chat "니시키랑 은각사 넣어줘" → 확인 카드
  3) 카드에 답하기         POST /answer (둘 다 선택)  → 작업 2개 추가, 화면에 ⏳
  4) 드래그로 순서 바꾸기   POST /reorder
  5) ✕ 빼기 → 되돌리기     POST /edit, POST /undo
  그동안 브라우저처럼 ⏳가 있는 동안 POLL초마다 GET /tray (286이 오면 멈춤) — 위 단계와 동시에 돈다.
  단계 사이엔 사람이 읽고 누르는 시간(THINK초)을 둔다.

따로 재는 값 (Locust 표에 함께 나옴)
  "⏳→배지 전부"  카드에 답한 순간부터 폴링이 286을 받을 때까지 = 사용자가 배지를 기다린 시간

실행 (웹 서버 · 워커 · DB를 켠 상태에서)
  locust -f locustfile.py --host http://127.0.0.1:8000                        ← 브라우저 http://localhost:8089 에서 사용자 수 입력
  locust -f locustfile.py --host http://127.0.0.1:8000 --headless -u 20 -r 5 -t 60s --csv out/u20
         (-u 동시 사용자 수, -r 초당 몇 명씩 늘릴지, -t 시험 시간, --csv 결과 파일)

  POLL=1  THINK=2  환경 변수로 바꿔 가며 비교
"""
import os
import time
import uuid

import gevent
from locust import HttpUser, between, events, task

POLL = float(os.getenv("POLL", "1"))       # 브라우저 폴링 주기 (초) — 지금 화면과 같은 1초
THINK = float(os.getenv("THINK", "2"))     # 단계 사이 사람이 보는 시간 (초)


class Traveler(HttpUser):
    wait_time = between(1, 3)              # 한 바퀴 끝나고 다음 plan을 시작하기 전 쉬는 시간

    def poll_until_done(self, url):
        """브라우저 폴링 흉내: ⏳가 없어질(286) 때까지 POLL초마다. 끝난 시각을 돌려준다."""
        while True:
            with self.client.get(url, name="GET /plans/[id]/tray (폴링)", catch_response=True) as r:
                if r.status_code == 286:
                    r.success()                # 286은 htmx의 "폴링 멈춰" 신호라 실패가 아님
                    return time.time()
                if r.status_code != 200:
                    r.failure(f"폴링 {r.status_code}")
                    return None
            gevent.sleep(POLL)

    @task
    def one_plan(self):
        plan = "lt-" + uuid.uuid4().hex[:10]
        base = f"/plans/{plan}"

        self.client.get(base, name="GET /plans/[id]")
        poller = gevent.spawn(self.poll_until_done, f"{base}/tray")   # 첫 스팟 확인 중 → 폴링 시작
        gevent.sleep(THINK)

        self.client.post(f"{base}/chat", data={"message": "니시키랑 은각사 넣어줘"}, name="POST /chat")
        gevent.sleep(THINK)                                            # 카드 읽고 고르는 시간
        r = self.client.post(f"{base}/answer", data={"selected": ["錦市場", "銀閣寺"]}, name="POST /answer")
        if r.status_code != 200:
            return
        t_added = time.time()
        poller.join()                                                  # 앞의 폴링이 끝났으면 새로 시작(htmx와 같음)
        poller = gevent.spawn(self.poll_until_done, f"{base}/tray")
        gevent.sleep(THINK)

        self.client.post(f"{base}/reorder", data={"order": ["銀閣寺", "清水寺", "錦市場"]}, name="POST /reorder")
        gevent.sleep(THINK)
        self.client.post(f"{base}/edit", data={"action": "remove", "spot": "清水寺"}, name="POST /edit")
        gevent.sleep(THINK)
        self.client.post(f"{base}/undo", name="POST /undo")

        t_done = poller.get()                                          # 배지가 다 나올 때까지 (폴링이 끝날 때까지)
        if t_done:
            events.request.fire(request_type="측정", name="⏳→배지 전부", response_time=(t_done - t_added) * 1000,
                                response_length=0, exception=None, context={})

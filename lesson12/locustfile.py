"""12교시 — 부하 테스트 시나리오 (Locust)

Locust: 파이썬으로 "가상 사용자 한 명이 하는 일"을 적어 두면, 그 사용자를 N명 동시에 띄워
요청마다 응답 시간·실패 수를 재 주는 부하 테스트 도구. (https://locust.io)

가상 사용자 두 종류 (run_experiment.py는 SCENARIO 환경 변수로 고름, 기본 real)

  Realistic (SCENARIO=real) — 현우 가정(10/6): 한 사람이 1분에 1곳쯤 담는다
    1) 새 plan 화면 열기 (처음 트레이 [清水寺] → Loop A 1개)
    2) SPOT_EVERY초(±50%)마다 채팅으로 "○○ 넣어줘" → 확인 카드 → 담기 (Loop A 1개)
       담을 때마다 드래그로 순서도 한 번 바꿈
    3) 6곳을 다 담으면(약 6~7분) 잠깐 쉬고 새 plan
    → 사용자 1명이 넣는 작업 ≈ 1분에 1개 남짓

  Heavy (SCENARIO=heavy) — 앞 실험(①②③)에서 쓴 것: 약 40초마다 새 plan + 스팟 3곳 (실제보다 훨씬 자주 담음)

공통: ⏳가 있는 동안 POLL초마다 GET /tray (286이 오면 멈춤) — 위 단계와 따로 돈다.
"⏳→배지 전부" = 담은 순간부터 폴링이 286을 받을 때까지 = 사용자가 배지를 기다린 시간

환경 변수: POLL=1 · THINK=2 · SPOT_EVERY=60 (Realistic) · GAP=30 (Heavy)
"""
import os
import random
import time
import uuid

import gevent
from locust import HttpUser, between, events, task

POLL = float(os.getenv("POLL", "1"))
THINK = float(os.getenv("THINK", "2"))
GAP = float(os.getenv("GAP", "30"))
SPOT_EVERY = float(os.getenv("SPOT_EVERY", "60"))
# 테스트용 채팅 모델이 알아듣는 스팟 (한국어 → 확인 카드에 뜨는 일본어 이름)
SPOTS = {"금각사": "金閣寺", "은각사": "銀閣寺", "니시키": "錦市場", "철학의 길": "哲学の道", "남선사": "南禅寺", "후시미": "伏見稲荷大社"}


class Polling:
    """두 사용자가 같이 쓰는 폴링 흉내."""

    def poll_until_done(self, url, t_added=None):
        """브라우저 폴링 흉내: ⏳가 없어질(286) 때까지 POLL초마다. t_added가 있으면 기다린 시간을 기록."""
        while True:
            with self.client.get(url, name="GET /plans/[id]/tray (폴링)", catch_response=True) as r:
                if r.status_code == 286:
                    r.success()                # 286은 htmx의 "폴링 멈춰" 신호라 실패가 아님
                    if t_added:
                        events.request.fire(request_type="측정", name="⏳→배지 전부",
                                            response_time=(time.time() - t_added) * 1000,
                                            response_length=0, exception=None, context={})
                    return
                if r.status_code != 200:
                    r.failure(f"폴링 {r.status_code}")
                    return
            gevent.sleep(POLL)



class Heavy(Polling, HttpUser):
    """앞 실험용: 약 40초마다 새 plan + 스팟 3곳."""
    wait_time = between(GAP * 0.5, GAP * 1.5)

    @task
    def one_plan(self):
        plan = "lt-" + uuid.uuid4().hex[:10]
        base = f"/plans/{plan}"

        self.client.get(base, name="GET /plans/[id]")
        poller = gevent.spawn(self.poll_until_done, f"{base}/tray")        # 첫 스팟 확인 중 → 폴링 시작
        gevent.sleep(THINK)

        self.client.post(f"{base}/chat", data={"message": "니시키랑 은각사 넣어줘"}, name="POST /chat")
        gevent.sleep(THINK)                                                 # 카드 읽고 고르는 시간
        r = self.client.post(f"{base}/answer", data={"selected": ["錦市場", "銀閣寺"]}, name="POST /answer")
        if r.status_code != 200:
            return
        poller.kill()                                                       # 화면이 새로 그려지면 폴링도 새로(htmx와 같음)
        gevent.spawn(self.poll_until_done, f"{base}/tray", time.time())     # 끝까지 기다리지 않고 따로 돈다
        gevent.sleep(THINK)

        self.client.post(f"{base}/reorder", data={"order": ["銀閣寺", "清水寺", "錦市場"]}, name="POST /reorder")
        gevent.sleep(THINK)
        self.client.post(f"{base}/edit", data={"action": "remove", "spot": "清水寺"}, name="POST /edit")
        gevent.sleep(THINK)
        self.client.post(f"{base}/undo", name="POST /undo")


class Realistic(Polling, HttpUser):
    """한 사람이 1분에 1곳쯤 담는 사용자 (SPOT_EVERY초 ±50%)."""
    wait_time = between(5, 15)                         # plan 하나 다 짜고 다음 plan까지

    @task
    def one_session(self):
        plan = "lt-" + uuid.uuid4().hex[:10]
        base = f"/plans/{plan}"
        tray = ["清水寺"]
        self.client.get(base, name="GET /plans/[id]")
        poller = gevent.spawn(self.poll_until_done, f"{base}/tray")
        for ko, ja in random.sample(list(SPOTS.items()), len(SPOTS)):
            gevent.sleep(SPOT_EVERY * random.uniform(0.5, 1.5))        # 다음 스팟을 고르는 시간
            self.client.post(f"{base}/chat", data={"message": f"{ko} 넣어줘"}, name="POST /chat")
            gevent.sleep(THINK)
            r = self.client.post(f"{base}/answer", data={"selected": [ja]}, name="POST /answer")
            if r.status_code != 200:
                continue
            tray.append(ja)
            poller.kill()
            poller = gevent.spawn(self.poll_until_done, f"{base}/tray", time.time())
            gevent.sleep(THINK)
            random.shuffle(tray)                                       # 드래그로 순서 바꾸기
            self.client.post(f"{base}/reorder", data={"order": tray}, name="POST /reorder")
        poller.join(timeout=60)

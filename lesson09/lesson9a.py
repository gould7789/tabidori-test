"""9교시 실험 1 — 프로세스 · 스레드 · 비동기를 눈으로 확인하기

실행:  uvicorn lesson9a:app --workers 2
측정:  python3 lesson9a_client.py   (다른 터미널에서)

엔드포인트 4개
  /whoami      : 이 요청을 처리한 프로세스 번호(PID)와, 그 프로세스 메모리에만 있는 카운터
  /sync        : def + time.sleep(2)          — 평범한 함수, 2초 막힘
  /async-bad   : async def + time.sleep(2)    — async인데 안에서 막는 함수를 부름 (실수 예시)
  /async-good  : async def + asyncio.sleep(2) — async답게 기다림
"""
import asyncio
import os
import time

from fastapi import FastAPI

app = FastAPI()

counter = {"n": 0}  # 프로세스 메모리 안의 값 — 프로세스마다 따로 생긴다


@app.get("/whoami")
def whoami():
    counter["n"] += 1
    return {"pid": os.getpid(), "count_in_this_process": counter["n"]}


@app.get("/sync")
def sync_sleep():
    time.sleep(2)  # 2초 동안 이 스레드가 멈춤
    return {"pid": os.getpid(), "kind": "sync"}


@app.get("/async-bad")
async def async_bad():
    time.sleep(2)  # ❌ async 함수 안에서 막는 호출 — 이벤트 루프 전체가 멈춤
    return {"pid": os.getpid(), "kind": "async-bad"}


@app.get("/async-good")
async def async_good():
    await asyncio.sleep(2)  # ✅ 기다리는 동안 다른 요청을 처리
    return {"pid": os.getpid(), "kind": "async-good"}

"""12교시 — 부하 실험 한 번을 명령 하나로

  python3 run_experiment.py 사용자수 워커수 워커당동시실행수
  예) python3 run_experiment.py 10 1 2      ← 실험 ① 사용자 10명, 워커 1개 × 동시 2
      python3 run_experiment.py 10 2 8      ← 실험 ② 사용자 10명, 워커 2개 × 동시 8
      python3 run_experiment.py 30 2 8      ← 실험 ③ 사용자 30명, 워커 2개 × 동시 8

미리 켜 둘 것: DB만 (proto 폴더에서 docker compose up -d db). 웹 서버·워커는 이 스크립트가 켜고 끈다.
(따로 켜 둔 uvicorn·worker.py가 있으면 먼저 Ctrl+C로 꺼 주세요 — 포트 8000이 겹치고 워커 수가 달라짐)

하는 일
  1) 지난 실험 데이터(lt- plan) 지우기
  2) 웹 서버 1개 + 워커 N개 켜기 (테스트용 Loop A = 맥 실측 시간 분포, API 비용 없음)
  3) Locust로 가상 사용자 U명이 5분 동안 사이트를 쓰게 함 (DURATION 환경 변수로 변경 가능)
     가상 사용자 종류: SCENARIO=real(기본, 1분에 1곳쯤 담음) / SCENARIO=heavy(앞 실험 ①②③, 40초마다 3곳)
  4) 남은 작업이 마저 끝나도록 30초 기다림
  5) 결과 출력 → 전부 끄기
"""
import os
import subprocess
import sys
import time
import urllib.request

if len(sys.argv) != 4:
    sys.exit(__doc__)
users, workers, conc = (int(x) for x in sys.argv[1:])
scenario = os.getenv("SCENARIO", "real")             # real = 1분에 1곳쯤 담는 사용자, heavy = 앞 실험(①②③)의 사용자
user_class = {"real": "Realistic", "heavy": "Heavy"}[scenario]
duration = os.getenv("DURATION", "5m" if scenario == "real" else "3m")
here = os.path.dirname(os.path.abspath(__file__))
py = sys.executable                                   # 지금 켜진 가상환경의 파이썬
env = dict(os.environ, LOOPA_SECONDS="real", WORKER_CONCURRENCY=str(conc))

try:
    import locust  # noqa: F401  — 부하 테스트 도구가 이 가상환경에 있는지 먼저 확인
except ImportError:
    sys.exit("Locust가 설치되어 있지 않아요. 이 가상환경에서 먼저:  pip install locust==2.46.7")

try:
    urllib.request.urlopen("http://127.0.0.1:8000/", timeout=1)
    sys.exit("포트 8000에 이미 웹 서버가 켜져 있어요. 그 터미널에서 Ctrl+C로 끄고 다시 실행해 주세요.")
except OSError:
    pass

print(f"\n===== 실험({user_class}): 사용자 {users}명 · 워커 {workers}개 × 동시 {conc} (= 최대 {workers * conc}개 동시 실행) =====")
subprocess.run([py, "lesson12_stats.py", "clear"], cwd=here, check=True)

logs = open(os.path.join(here, "experiment.log"), "w")
procs = [subprocess.Popen([py, "-m", "uvicorn", "lesson12:app", "--port", "8000"], cwd=here, env=env,
                          stdout=logs, stderr=logs)]
procs += [subprocess.Popen([py, "worker.py"], cwd=here, env=env, stdout=logs, stderr=logs) for _ in range(workers)]
try:
    for _ in range(30):                               # 웹 서버가 뜰 때까지 최대 15초
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/plans/ping", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    else:
        sys.exit("웹 서버가 뜨지 않았어요. experiment.log를 확인해 주세요 (DB가 켜져 있나요?)")

    print(f"가상 사용자 {users}명이 {duration} 동안 사용 중… (Locust 결과 표가 끝나면 30초 더 기다려요)")
    lt = subprocess.run([py, "-m", "locust", "-f", "locustfile.py", "--host", "http://127.0.0.1:8000", "--headless",
                    "-u", str(users), "-r", "2", "-t", duration, "--only-summary", user_class], cwd=here, env=env)
    if lt.returncode != 0:                            # Locust는 실패한 요청이 하나라도 있으면 1로 끝남 → 결과는 그대로 봄
        print(f"⚠ Locust 종료 코드 {lt.returncode} — 실패한 요청이 있었거나 오류가 났어요. 위 표의 # fails를 확인해 주세요.")
    time.sleep(30)
    print(f"\n----- 작업표 결과 (사용자 {users}명 · 워커 {workers}개 × 동시 {conc}) -----")
    subprocess.run([py, "lesson12_stats.py"], cwd=here)
finally:
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait()
    logs.close()
    print("웹 서버·워커를 껐어요.\n")

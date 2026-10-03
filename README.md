# 타비도리 프로토타입 — 학습 폴더

10/9 재검사까지 실무 수준 아키텍처를 확인하는 미니 프로토타입을 단계별로 만드는 곳.

## 구성

| 폴더 | 내용 |
|---|---|
| `lesson09/` | 9교시 — 전반: 프로세스·비동기 실험 (`lesson9a.py`, `lesson9a_client.py`) · 후반: 채팅 그래프 웹 서버 (`lesson9b.py`, `tabidori_chat.py`, `templates/`, `lesson9b_check.py`) |
| `docker-compose.yml` | PostgreSQL 컨테이너 (9교시~) |
| `ref_lessons/` | 프로토타입에 다시 쓰는 이전 수업 코드 (아래 표) |

| 이전 수업 파일 | 내용 | 프로토타입에서 |
|---|---|---|
| `lesson5_v2.py` | Loop A 그래프 (공식 사이트 열람 → 웹검색 → 배지) | 11교시 워커가 실행 |
| `lesson6.py` | FastAPI + interrupt (담기 · 확인 칩 · 409) | 엔드포인트 골격 참고 |
| `lesson6b.py` | 백그라운드 Loop A + 폴링 | 11교시에서 워커 + 작업표로 교체 |
| `lesson7.py` | 채팅 편집 에이전트 (도구 · 되돌리기) | 9교시 후반에 웹 서버에 붙임 |
| `lesson8.py` | 스팟 탐색 도구 추가 (`lesson7.py`를 import — 같은 폴더에 둘 것) | 9교시 후반에 웹 서버에 붙임 |

## 준비

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`.env`(이 폴더 맨 위)에 `ANTHROPIC_API_KEY=` — `.gitignore`에 들어 있음. 채팅·코드·로그에 키를 붙이지 않는다.
테스트용 모델(정해진 답을 내는 모델)로 돌리는 수업은 키가 필요 없다.

## 9교시 후반 실행

```bash
docker compose up -d                 # 이 폴더 맨 위에서 — PostgreSQL 시작
cd lesson09
uvicorn lesson9b:app                 # 웹 서버 → 브라우저 http://127.0.0.1:8000
python3 lesson9b_check.py            # (다른 터미널) 코드로 흐름 확인
```
진짜 Claude로: `MODEL=sonnet uvicorn lesson9b:app` (.env의 키 사용, 비용 발생).

## 일정

| 수업 | 날짜 | 내용 |
|---|---|---|
| 9교시 | 10/3 | 프로세스·스레드·비동기 → 채팅 그래프를 웹 서버에 붙이기, PostgresSaver |
| 10교시 | 10/4 | 확인 카드 재개 흐름 HTML, 409 잠금, `plans.undo_snapshot` |
| 11교시 | 10/5 | 워커 + `loop_a_jobs` 작업표, htmx 폴링 |
| 12교시 | 10/6 | 부하 테스트 (Locust) |
| 13교시 | 10/7 | 컨텍스트 · MCP · 서브그래프 · 배포 판단 |
| — | 10/8 | 아키텍처 문서 |

"""9교시 후반 — 브라우저 대신 코드로 흐름 확인 (서버를 켠 상태에서 실행)

  python3 lesson9b_check.py            1단계: 대화 → 확인 카드 → 답 → 트레이 확인
  python3 lesson9b_check.py after      2단계: 서버를 껐다 켠 뒤 실행 — 대화·트레이가 남아 있는지
"""
import re
import sys

import httpx

BASE = "http://127.0.0.1:8000"
PLAN = "check-plan"


def tray(html):
    return re.findall(r"<li>(.*?)</li>", html)


def bubbles(html):
    return re.findall(r'class="bubble (?:user|ai)">(.*?)</div>', html)


with httpx.Client(base_url=BASE, timeout=30) as c:
    if len(sys.argv) > 1 and sys.argv[1] == "after":
        html = c.get(f"/plans/{PLAN}").text
        print("재시작 후 말풍선:", bubbles(html))
        print("재시작 후 트레이:", tray(html))
        sys.exit()

    c.post(f"/plans/{PLAN}/reset")
    print("① 시작 트레이:", tray(c.get(f"/plans/{PLAN}").text))

    html = c.post(f"/plans/{PLAN}/chat", data={"message": "니시키랑 은각사 넣어줘"}).text
    print("② 채팅 후 — 확인 카드가 떴나?", "선택한 곳 담기" in html, "/ 휴무 표시:", "휴무" in html)

    r = c.post(f"/plans/{PLAN}/chat", data={"message": "금각사 빼줘"})
    print("③ 카드 대기 중 다른 채팅 →", r.status_code, r.json().get("detail"))

    html = c.post(f"/plans/{PLAN}/answer", data={"selected": ["銀閣寺"]}).text   # 은각사만 고름
    print("④ 은각사만 골라 답함 → 트레이:", tray(html))
    print("   마지막 말풍선:", bubbles(html)[-1])

    r = c.post(f"/plans/{PLAN}/answer", data={"selected": []})
    print("⑤ 질문 없는데 또 답함 →", r.status_code, r.json().get("detail"))

    html = c.post(f"/plans/{PLAN}/chat", data={"message": "방금 거 취소"}).text
    print("⑥ 되돌리기 → 트레이:", tray(html))

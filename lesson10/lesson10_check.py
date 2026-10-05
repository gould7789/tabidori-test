"""10교시 — 브라우저 대신 코드로 확인 (서버를 켠 상태에서 실행)

  python3 lesson10_check.py
"""
import asyncio
import html as H
import re

import httpx

BASE = "http://127.0.0.1:8000"
PLAN = "check10"


def tray(html):
    return re.findall(r'<span class="spot">(.*?)</span>', html)


def can_undo(html):
    return "↶ 되돌리기" in html


def last_bubble(html):
    return H.unescape(re.findall(r'class="bubble (?:user|ai)">(.*?)</div>', html)[-1])


def chat(c, msg):
    return c.post(f"/plans/{PLAN}/chat", data={"message": msg})


def edit(c, action, spot):
    return c.post(f"/plans/{PLAN}/edit", data={"action": action, "spot": spot})


with httpx.Client(base_url=BASE, timeout=30) as c:
    html = c.post(f"/plans/{PLAN}/reset").text
    print("① 시작 트레이:", tray(html), "/ 되돌리기 버튼:", can_undo(html))

    html = chat(c, "니시키랑 은각사 넣어줘").text
    print("② 확인 카드 뜸:", "선택한 곳 담기" in html)
    r = edit(c, "remove", "清水寺")
    print("   카드 대기 중 버튼 편집 →", r.status_code, r.json()["detail"])
    r = c.post(f"/plans/{PLAN}/undo")
    print("   카드 대기 중 되돌리기 →", r.status_code, r.json()["detail"])
    html = c.post(f"/plans/{PLAN}/answer", data={"selected": ["錦市場", "銀閣寺"]}).text
    print("   둘 다 골라 답함 → 트레이:", tray(html), "(재개 때 add_spots가 다시 돌았지만 한 번만 담김)")

    html = chat(c, "은각사 빼줘").text
    print("③ 채팅으로 빼기 →", tray(html))
    html = c.post(f"/plans/{PLAN}/undo").text
    print("   [↶ 되돌리기] 버튼 →", tray(html), "/ 되돌리기 버튼 남음:", can_undo(html))

    html = edit(c, "remove", "清水寺").text
    print("④ 버튼으로 빼기 →", tray(html))
    html = chat(c, "방금 거 취소").text
    print("   채팅 '방금 거 취소' →", tray(html))
    html = chat(c, "방금 거 취소").text
    print("   한 번 더 →", last_bubble(html))

    html = chat(c, "은각사 빼고 니시키 맨 앞으로").text
    print("⑤ 한 마디에 편집 두 번 →", tray(html))
    html = c.post(f"/plans/{PLAN}/undo").text
    print("   되돌리기 한 번 →", tray(html), "(두 편집이 통째로 취소)")

    html = chat(c, "금각사 빼줘").text
    print("⑥ 없는 스팟 빼기 →", last_bubble(html), "/ 트레이 그대로:", tray(html))

    html = c.post(f"/plans/{PLAN}/reorder", data={"order": ["銀閣寺", "清水寺", "錦市場"]}).text
    print("⑦ 드래그로 은각사를 맨 위로 →", tray(html))
    html = c.post(f"/plans/{PLAN}/undo").text
    print("   되돌리기 →", tray(html))
    html = c.post(f"/plans/{PLAN}/reorder", data={"order": ["錦市場", "清水寺"]}).text
    print("   낡은 화면에서 드래그(그 사이 다른 탭이 바꾼 경우) →", tray(html), "(무시하고 지금 트레이를 다시 보여줌)")


async def concurrent():
    """탭 두 개가 동시에 서로 다른 스팟을 빼면 둘 다 반영되는가 (FOR UPDATE)"""
    with httpx.Client(base_url=BASE, timeout=30) as c:                       # 트레이를 3곳으로 준비
        c.post(f"/plans/{PLAN}/reset")
        chat(c, "니시키랑 은각사 넣어줘")
        c.post(f"/plans/{PLAN}/answer", data={"selected": ["錦市場", "銀閣寺"]})
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        await asyncio.gather(
            c.post(f"/plans/{PLAN}/edit", data={"action": "remove", "spot": "錦市場"}),
            c.post(f"/plans/{PLAN}/edit", data={"action": "remove", "spot": "銀閣寺"}))
        html = (await c.get(f"/plans/{PLAN}")).text
    print("⑧ 동시에 두 탭에서 각각 빼기 →", tray(html), "(둘 다 빠져야 정상)")


asyncio.run(concurrent())

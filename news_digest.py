"""Fetch Korea (Newsis) + Japan (NHK) headlines, rank them with Gemini, publish a digest
page to GitHub Pages, and send a KakaoTalk "memo to self" teaser linking to it.

Run twice a day via GitHub Actions (see .github/workflows/news-digest.yml).
Requires env vars: GEMINI_API_KEY, KAKAO_CLIENT_ID, KAKAO_CLIENT_SECRET,
KAKAO_REFRESH_TOKEN, PAGE_BASE_URL
"""
import html
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import requests
from google import genai
from google.genai import types

KST = ZoneInfo("Asia/Seoul")

# category label -> feed url
KOREA_FEEDS = {
    "정치": "https://newsis.com/RSS/politics.xml",
    "경제": "https://newsis.com/RSS/economy.xml",
    "사회": "https://newsis.com/RSS/society.xml",
    "국제": "https://newsis.com/RSS/international.xml",
}
JAPAN_FEEDS = {
    "정치": "https://www.nhk.or.jp/rss/news/cat4.xml",
    "경제": "https://www.nhk.or.jp/rss/news/cat5.xml",
    "사회": "https://www.nhk.or.jp/rss/news/cat1.xml",
    "국제": "https://www.nhk.or.jp/rss/news/cat6.xml",
}
ITEMS_PER_CATEGORY = 6


def fetch_items(feed_url: str, limit: int) -> list[dict]:
    parsed = feedparser.parse(feed_url)
    items = []
    for entry in parsed.entries[:limit]:
        items.append(
            {
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", "").strip(),
                "summary": entry.get("summary", entry.get("description", "")).strip(),
            }
        )
    return items


def collect_source(feeds: dict[str, str]) -> dict[str, list[dict]]:
    return {category: fetch_items(url, ITEMS_PER_CATEGORY) for category, url in feeds.items()}


def build_prompt(korea: dict, japan: dict, time_slot: str, date_str: str) -> str:
    payload = {"korea_raw": korea, "japan_raw": japan}
    return f"""너는 한국어 뉴스 다이제스트 편집자야. 아래 JSON은 한국(뉴시스)과 일본(NHK) 뉴스 원문(제목/링크/요약)이야.
일본 기사는 일본어 그대로 들어있으니 한국어로 번역해줘. 한국 기사는 이미 한국어니까 번역하지 마.

각 기사에 대해 다음을 해줘:
1. importance: "red"(반드시 알아야 할 주요 뉴스) / "orange"(알아두면 좋음) / "white"(일반 동향) 중 하나로 판단
2. headline: 한국어 헤드라인 (일본 기사는 번역, 한국 기사는 원문 유지, 너무 길면 자연스럽게 축약)
3. context: importance가 "red"인 기사만, 왜 중요한지/배경을 1문장으로 작성. red가 아니면 빈 문자열("")
4. link: 원문 링크 그대로

중요도 판단 기준: 사상자·인명피해·대형 재난, 정상회담·전쟁·외교 갈등, 급격한 금융시장 변동, 정책 대전환 등은 red. 정부기관 일상 발표, 통계, 스포츠, 소규모 이슈는 white에 가깝게. 애매하면 orange.

카테고리 순서와 기사 순서는 원본 순서를 유지해줘. 각 카테고리에서 중복되거나 너무 사소한 기사는 제외해도 좋지만 카테고리당 최소 2개는 남겨줘.

아래 형식의 JSON만 출력해줘. 다른 설명 텍스트는 절대 붙이지 마.

{{
  "date": "{date_str}",
  "time_slot": "{time_slot}",
  "korea": {{"정치": [{{"importance": "red", "headline": "...", "context": "...", "link": "..."}}], "경제": [...], "사회": [...], "국제": [...]}},
  "japan": {{"정치": [...], "경제": [...], "사회": [...], "국제": [...]}}
}}

원본 데이터:
{json.dumps(payload, ensure_ascii=False)}
"""


def call_gemini(prompt: str) -> dict:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)


ICONS = {"red": "🔴", "orange": "🟠", "white": "⚪"}


def render_category(name: str, items: list[dict]) -> str:
    lines = [f"[{name}]"]
    for item in items:
        icon = ICONS.get(item.get("importance"), "⚪")
        lines.append(f"{icon} {item.get('headline', '').strip()}")
        if item.get("importance") == "red" and item.get("context"):
            lines.append(f"→ {item['context'].strip()}")
        if item.get("link"):
            lines.append(item["link"])
        lines.append("")
    return "\n".join(lines).rstrip()


def render_digest(data: dict) -> str:
    header = f"📰 {data['time_slot']} 뉴스 요약 · {data['date']}"
    parts = [header, "", "🇰🇷 한국 (뉴시스)", ""]
    for cat in ["정치", "경제", "사회", "국제"]:
        items = data.get("korea", {}).get(cat, [])
        if items:
            parts.append(render_category(cat, items))
            parts.append("")
    parts.append("🇯🇵 일본 (NHK)")
    parts.append("")
    for cat in ["정치", "경제", "사회", "국제"]:
        items = data.get("japan", {}).get(cat, [])
        if items:
            parts.append(render_category(cat, items))
            parts.append("")
    return "\n".join(parts).strip()


def count_importance(data: dict) -> dict:
    counts = {"red": 0, "orange": 0, "white": 0}
    for source in (data.get("korea", {}), data.get("japan", {})):
        for items in source.values():
            for item in items:
                counts[item.get("importance", "white")] = counts.get(item.get("importance", "white"), 0) + 1
    return counts


def render_html_category(name: str, items: list[dict]) -> str:
    rows = []
    for item in items:
        icon = ICONS.get(item.get("importance"), "⚪")
        headline = html.escape(item.get("headline", "").strip())
        link = html.escape(item.get("link", ""), quote=True)
        context_html = ""
        if item.get("importance") == "red" and item.get("context"):
            context_html = f'<div class="context">→ {html.escape(item["context"].strip())}</div>'
        rows.append(
            f'<li><div class="headline">{icon} '
            f'<a href="{link}" target="_blank" rel="noopener">{headline}</a></div>{context_html}</li>'
        )
    return f'<h3>{html.escape(name)}</h3><ul>{"".join(rows)}</ul>'


def render_html(data: dict) -> str:
    counts = count_importance(data)
    sections = []
    for label, source_key in (("🇰🇷 한국 (뉴시스)", "korea"), ("🇯🇵 일본 (NHK)", "japan")):
        cats_html = "".join(
            render_html_category(cat, data.get(source_key, {}).get(cat, []))
            for cat in ["정치", "경제", "사회", "국제"]
            if data.get(source_key, {}).get(cat)
        )
        sections.append(f'<section><h2>{label}</h2>{cats_html}</section>')

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>{html.escape(data['time_slot'])} 뉴스 요약 · {html.escape(data['date'])}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, "Malgun Gothic", sans-serif; max-width: 640px; margin: 0 auto; padding: 20px 16px 60px; line-height: 1.5; color: #1a1a1a; background: #fff; }}
h1 {{ font-size: 1.3rem; }}
h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 2px solid #eee; padding-bottom: 6px; }}
h3 {{ font-size: 1rem; color: #444; margin-bottom: 4px; }}
ul {{ list-style: none; padding: 0; margin: 0 0 1.2rem; }}
li {{ padding: 8px 0; border-bottom: 1px solid #f0f0f0; }}
.headline a {{ color: #1a1a1a; text-decoration: none; }}
.headline a:hover {{ text-decoration: underline; }}
.context {{ color: #666; font-size: 0.9rem; margin-top: 2px; padding-left: 1.4rem; }}
.summary {{ color: #666; font-size: 0.95rem; }}
</style></head>
<body>
<h1>📰 {html.escape(data['time_slot'])} 뉴스 요약 · {html.escape(data['date'])}</h1>
<p class="summary">🔴 주요 {counts['red']}건 · 🟠 중요 {counts['orange']}건 · ⚪ 일반 {counts['white']}건</p>
{"".join(sections)}
</body></html>"""


def get_kakao_access_token() -> str:
    # Kakao access tokens only last ~6h, so mint a fresh one each run from the
    # long-lived (~60 day) refresh token instead of storing a static token.
    resp = requests.post(
        "https://kauth.kakao.com/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["KAKAO_CLIENT_ID"],
            "client_secret": os.environ["KAKAO_CLIENT_SECRET"],
            "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_kakao(token: str, title: str, description: str, page_url: str) -> None:
    template_object = {
        "object_type": "feed",
        "content": {
            "title": title,
            "description": description,
            "link": {"web_url": page_url, "mobile_web_url": page_url},
        },
        "buttons": [
            {"title": "전체 뉴스 보기", "link": {"web_url": page_url, "mobile_web_url": page_url}}
        ],
    }
    resp = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data={"template_object": json.dumps(template_object, ensure_ascii=False)},
        timeout=30,
    )
    resp.raise_for_status()


def main() -> None:
    now = datetime.now(KST)
    time_slot = "아침" if now.hour < 12 else "저녁"
    date_str = now.strftime("%Y.%m.%d(%a) %H:%M")

    korea = collect_source(KOREA_FEEDS)
    japan = collect_source(JAPAN_FEEDS)

    prompt = build_prompt(korea, japan, time_slot, date_str)
    data = call_gemini(prompt)

    print(render_digest(data))

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(render_html(data))

    counts = count_importance(data)
    page_url = os.environ["PAGE_BASE_URL"].rstrip("/") + "/"
    teaser = f"🔴 주요 {counts['red']}건 · 🟠 중요 {counts['orange']}건 · ⚪ 일반 {counts['white']}건 도착"

    kakao_token = get_kakao_access_token()
    send_kakao(kakao_token, f"📰 {time_slot} 뉴스 요약 · {date_str}", teaser, page_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # surface failures clearly in Actions logs
        print(f"news-digest failed: {exc}", file=sys.stderr)
        raise

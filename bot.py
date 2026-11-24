"""
X(트위터) 특정 계정의 새 글을 텔레그램으로 보내는 봇
- 원문 링크 / 요약 / 원문 / 번역
- GitHub Actions에서 10분마다 실행됨

설정은 모두 GitHub 저장소의 Secrets / Variables에서 합니다 (코드 수정 불필요).
"""
import os
import re
import json
from datetime import datetime, timezone, timedelta

import requests
import anthropic
import trafilatura

# ───────── 설정 (GitHub Secrets / Variables에서 읽어옴) ─────────
X_BEARER = os.environ["X_BEARER_TOKEN"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

# 지켜볼 계정: "abc, def" 처럼 쉼표로 구분 (@는 있어도 없어도 됨)
USERNAMES = [u.strip().lstrip("@") for u in os.environ.get("X_USERNAMES", "").split(",") if u.strip()]
# 트윗 속 기사 링크 본문까지 가져올지 (기본: 가져옴)
FETCH_LINKS = os.environ.get("FETCH_LINKS", "").strip().lower() != "false"
# 답글(스레드 포함)도 받을지 (기본: 안 받음)
INCLUDE_REPLIES = os.environ.get("INCLUDE_REPLIES", "").strip().lower() == "true"
MODEL = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5"

STATE_FILE = "state.json"
X_API = "https://api.x.com/2"
KST = timezone(timedelta(hours=9))
X_DOMAINS = ("x.com", "twitter.com", "t.co")


# ───────── 상태 저장 (이미 보낸 글을 다시 보내지 않도록) ─────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "since": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ───────── X API ─────────
def x_get(path, params=None):
    r = requests.get(
        f"{X_API}{path}",
        headers={"Authorization": f"Bearer {X_BEARER}"},
        params=params,
        timeout=30,
    )
    if r.status_code == 429:
        raise RuntimeError("X API 요청 한도 초과(429). 다음 실행 때 다시 시도합니다.")
    if r.status_code >= 400:
        raise RuntimeError(f"X API 오류 {r.status_code}: {r.text[:300]}")
    return r.json()


def get_user_id(username, state):
    key = username.lower()
    if key in state["users"]:
        return state["users"][key]
    data = x_get(f"/users/by/username/{username}")
    if "data" not in data:
        raise RuntimeError(f"@{username} 계정을 찾을 수 없습니다: {data}")
    state["users"][key] = data["data"]["id"]
    save_state(state)
    return data["data"]["id"]


def fetch_new_posts(user_id, since_id):
    exclude = "retweets" if INCLUDE_REPLIES else "retweets,replies"
    params = {
        "max_results": 100 if since_id else 5,
        "tweet.fields": "created_at,entities,note_tweet",
        "exclude": exclude,
    }
    if since_id:
        params["since_id"] = since_id
    data = x_get(f"/users/{user_id}/tweets", params)
    posts = data.get("data", [])
    posts.sort(key=lambda p: int(p["id"]))  # 오래된 글부터
    return posts


def post_text_and_links(post):
    """긴 글(note_tweet)까지 고려해 본문을 얻고, t.co 링크를 실제 주소로 바꾼다."""
    note = post.get("note_tweet") or {}
    text = note.get("text") or post.get("text", "")
    entities = note.get("entities") or post.get("entities") or {}

    links = []
    for u in entities.get("urls", []):
        real = u.get("unwound_url") or u.get("expanded_url") or u.get("url")
        is_media = bool(real) and ("/photo/" in real or "/video/" in real)
        if u.get("url") and real:
            text = text.replace(u["url"], "" if is_media else real)
        host = re.sub(r"^https?://(www\.)?", "", real or "").split("/")[0].lower()
        if real and not any(host == d or host.endswith("." + d) for d in X_DOMAINS):
            if real not in links:
                links.append(real)
    # 본문 끝에 남은 사진/영상용 t.co 링크 제거
    text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text).strip()
    return text, links


# ───────── 링크 본문 추출 ─────────
def fetch_article(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return text[:8000] if text else None
    except Exception as e:
        print(f"  링크 본문 추출 실패 ({url}): {e}")
        return None


# ───────── Claude로 요약 + 번역 ─────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def summarize_and_translate(post_text, article_text=None, article_url=None):
    content = f"<post>\n{post_text}\n</post>"
    if article_text:
        content += f'\n<article url="{article_url}">\n{article_text}\n</article>'

    prompt = (
        "아래는 X(트위터) 게시글" + (" 과 게시글에 링크된 웹페이지 본문" if article_text else "") + "이다.\n\n"
        "1) <summary> 태그 안에 핵심 내용을 한국어 불릿(•) 3줄 이내로 요약해라."
        + (" 웹페이지 본문 내용도 반영해라." if article_text else "") + "\n"
        "2) <translation> 태그 안에 게시글(<post>) 원문만 자연스러운 한국어로 번역해라. "
        "원문이 이미 한국어라면 '(원문이 한국어입니다)'라고만 써라.\n"
        "태그 밖에는 아무것도 쓰지 마라.\n\n" + content
    )
    msg = claude.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    summary = re.search(r"<summary>(.*?)</summary>", out, re.S)
    translation = re.search(r"<translation>(.*?)</translation>", out, re.S)
    return (
        summary.group(1).strip() if summary else out.strip(),
        translation.group(1).strip() if translation else "",
    )


# ───────── 텔레그램 ─────────
def send_telegram(text):
    chunks, buf = [], ""
    for line in text.split("\n"):
        while len(line) > 4000:  # 아주 긴 한 줄은 잘라서
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:4000])
            line = line[4000:]
        if len(buf) + len(line) + 1 > 4000:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)

    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": chunk},
            timeout=30,
        )
        res = r.json()
        if not res.get("ok"):
            raise RuntimeError(f"텔레그램 전송 실패: {res.get('description')}")


def build_message(username, post, text, links, summary, translation):
    created = post.get("created_at", "")
    try:
        t = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(KST)
        when = t.strftime("%m/%d %H:%M")
    except ValueError:
        when = ""
    lines = [
        f"🔔 @{username}" + (f" · {when}" if when else ""),
        f"🔗 https://x.com/{username}/status/{post['id']}",
    ]
    for link in links:
        lines.append(f"📰 {link}")
    lines += ["", "📝 요약", summary, "", "📄 원문", text]
    if translation:
        lines += ["", "🇰🇷 번역", translation]
    return "\n".join(lines)


# ───────── 실행 ─────────
def main():
    if not USERNAMES:
        raise SystemExit("X_USERNAMES 변수가 비어 있습니다. GitHub Variables에 계정을 넣어주세요.")

    state = load_state()
    for username in USERNAMES:
        print(f"@{username} 확인 중...")
        user_id = get_user_id(username, state)
        since_id = state["since"].get(user_id)
        posts = fetch_new_posts(user_id, since_id)

        if not since_id and posts:
            # 첫 실행: 옛날 글 폭탄을 막기 위해 가장 최근 글 1개만 보냄
            posts = posts[-1:]
        print(f"  새 글 {len(posts)}개")

        for post in posts:
            text, links = post_text_and_links(post)
            article_text, article_url = None, None
            if FETCH_LINKS and links:
                article_url = links[0]
                article_text = fetch_article(article_url)
            try:
                summary, translation = summarize_and_translate(text, article_text, article_url)
            except Exception as e:
                print(f"  요약 실패: {e}")
                summary, translation = "(요약 실패)", ""

            send_telegram(build_message(username, post, text, links, summary, translation))
            state["since"][user_id] = post["id"]
            save_state(state)  # 한 개 보낼 때마다 저장 → 중복 전송 방지
            print(f"  전송 완료: {post['id']}")

    save_state(state)


if __name__ == "__main__":
    main()

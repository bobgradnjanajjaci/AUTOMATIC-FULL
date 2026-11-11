import os
import re
import time
import threading
import unicodedata
import requests
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

# =====================================================================
# 🔧 KONFIGURACIJA ZA LAJKOVE (GodOfPanel)
# =====================================================================
DEFAULT_APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
DEFAULT_PANEL_URL = os.environ.get("PANEL_URL", "https://godofpanel.com/api/v2")
DEFAULT_API_KEY = os.environ.get("API_KEY", "ff9e106148468ef7b5639e9b46e2d2f4")
DEFAULT_LIKE_SERVICE_ID = os.environ.get("LIKE_SERVICE_ID", "8771")  # TikTok Comment Like na GodOfPanel

# Pravila za lajkove
MULTIPLIER = 1.30           # Šalje 1.3x više lajkova od top komentara
MAX_TOP_LIKES_LIMIT = 12000 # Ako prvi komentar ima 12.000 ili više lajkova, ne šalje ništa
FALLBACK_MIN_LIKES = 25     # Ako top komentar ima 0 lajkova, pošalji 25
MAX_PAGES_PER_VIDEO = 12    # Maksimalno stranica po videu


def calculate_target_likes(top_likes: int) -> int:
    # 1. Sigurnosni limit - 12.000 ili više lajkova -> ne šalje ništa
    if top_likes >= 12000:
        return 0

    # 2. Mali komentari (0 - 199): minimalno 150 ili trenutni + 80
    if top_likes < 200:
        return max(150, top_likes + 80)

    # 3. Srednji komentari (200 - 999): dodaj 35% prednosti
    if top_likes < 1000:
        return int(top_likes * 1.35)

    # 4. Jači komentari (1.000 - 2.999): dodaj 600 lajkova prednosti
    if top_likes < 3000:
        return top_likes + 600

    # 5. Vrlo jaki komentari (3.000 - 11.999): dodaj 1.000 lajkova prednosti (npr. 10k -> 11k)
    return top_likes + 1000


# Korijeni riječi (nakon čišćenja nevidljivih znakova, tačkica i ćirilice)
CLEAN_ROOT_PATTERNS = [
    "alverset",      # Hvata: Alversett, Alverset, AIversett, Alver.sett, Alversettte, Alversette...
    "noraalver",     # Hvata: Nora Alversett, Nora.Alversett, N. Alversett...
    "norraalver",    # Hvata: Norra Alversett, Norra A...
    "neraalver",     # Hvata: Nera Alversett...
    "alversetefect", # Hvata: Alversett Efect, Alverset Effect...
]

DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

queue_lock = threading.Lock()
order_queue = []
log_lines = []
is_running = False
stats = {"scanned": 0, "ordered": 0, "skipped": 0, "failed": 0}


def add_log(msg: str):
    now = datetime.now().strftime("%H:%M:%S")
    line = f"[{now}] {msg}"
    print(line, flush=True)
    with queue_lock:
        log_lines.insert(0, line)
        if len(log_lines) > 400:
            log_lines.pop()


def normalize_camouflaged_text(text: str) -> str:
    """Skida sve nevidljive (zero-width) znakove, tačkice i pretvara ćirilicu/homoglife u latinicu."""
    if not text:
        return ""
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    homoglyphs = {
        "е": "e", "Е": "e", "ё": "e", "Ё": "e",
        "а": "a", "А": "a", "α": "a", "Α": "a",
        "о": "o", "О": "o", "ο": "o", "Ο": "o", "0": "o",
        "с": "c", "С": "c",
        "і": "i", "І": "i", "ї": "i", "1": "l", "I": "l",
        "р": "p", "Р": "p",
        "х": "x", "Х": "x",
        "у": "y", "У": "y",
        "ѕ": "s", "Ѕ": "s",
        "т": "t", "Т": "t",
        "н": "h", "Н": "h",
        "в": "b", "В": "b",
        "к": "k", "К": "k",
        "м": "m", "М": "m",
    }
    for fake, real in homoglyphs.items():
        text = text.replace(fake, real)

    return re.sub(r"[^a-z]", "", text.lower())


def is_our_comment(text: str) -> bool:
    clean_compact = normalize_camouflaged_text(text)
    return any(root in clean_compact for root in CLEAN_ROOT_PATTERNS)


def resolve_tiktok_url_and_id(raw_url: str):
    url = (raw_url or "").strip()
    if not url:
        return None, ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    pc_match = re.search(r"tiktok\.com/(@[^/?#]+/(?:video|photo)/(\d+))", url)
    if pc_match:
        return pc_match.group(2), f"https://www.tiktok.com/{pc_match.group(1)}"

    resolved_url = url
    try:
        resp = requests.get(url, headers=DESKTOP_HEADERS, allow_redirects=True, timeout=12, stream=True)
        resolved_url = resp.url
        resp.close()
    except Exception as e:
        add_log(f"⚠️ Redirect greška za {url}: {e}")

    match_std = re.search(r"tiktok\.com/(@[^/?#]+/(?:video|photo)/(\d+))", resolved_url)
    if match_std:
        return match_std.group(2), f"https://www.tiktok.com/{match_std.group(1)}"

    match_id = re.search(r"(?:m\.tiktok\.com/v/|tiktok\.com/(?:video|photo)/|/v/)(\d+)", resolved_url)
    if match_id:
        vid = match_id.group(1)
        return vid, f"https://www.tiktok.com/@tiktok/video/{vid}"

    return None, resolved_url.split("?")[0]


def extract_comments_from_nested_json(data):
    """Rekurzivno izvlači listu komentara iz bilo kojeg JSON odgovora."""
    found = []
    has_more = False
    next_cursor = None

    if isinstance(data, list):
        for item in data:
            c_list, hm, nc = extract_comments_from_nested_json(item)
            found.extend(c_list)
            if hm:
                has_more = True
            if nc is not None:
                next_cursor = nc
    elif isinstance(data, dict):
        if ("text" in data or "comment" in data) and (
            "digg_count" in data or "diggCount" in data or "cid" in data or "user" in data
        ):
            found.append(data)

        if data.get("has_more") in (1, True) or data.get("hasMore") is True:
            has_more = True
        if "cursor" in data and isinstance(data["cursor"], int):
            next_cursor = data["cursor"]
        elif "nextCursor" in data and isinstance(data["nextCursor"], int):
            next_cursor = data["nextCursor"]

        for key in ("comments", "data", "items", "result", "results", "comment_list"):
            if key in data and isinstance(data[key], (list, dict)):
                c_list, hm, nc = extract_comments_from_nested_json(data[key])
                found.extend(c_list)
                if hm:
                    has_more = True
                if nc is not None:
                    next_cursor = nc

    return found, has_more, next_cursor


def fetch_comments_multi_engine(video_id: str, video_url: str, apify_token: str):
    """
    1. Poziva službeni ScrapTik Comments Scraper API ('scraptik~tiktok-comments-scraper-api')
       sa poljem 'listComments_awemeId'.
    2. Ako nije dostupan, proba glavni 'scraptik~tiktok-api'.
    3. Ako Apify vrati grešku, automatski povlači preko TikWM API-ja.
    """
    all_comments = []

    if apify_token:
        actors_to_try = [
            "scraptik~tiktok-comments-scraper-api",
            "scraptik~tiktok-api",
        ]
        for actor_id in actors_to_try:
            cursor = 0
            actor_success = False

            for page in range(1, MAX_PAGES_PER_VIDEO + 1):
                endpoint = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
                payload = {
                    "listComments_awemeId": str(video_id),
                    "listComments_count": 50,
                    "listComments_cursor": cursor,
                    "comments_getComments": True,
                    "comments_awemeId": str(video_id),
                    "comments_count": 50,
                    "comments_cursor": cursor,
                }
                try:
                    res = requests.post(
                        endpoint,
                        params={"token": apify_token},
                        json=payload,
                        timeout=60
                    )
                    if res.status_code == 401:
                        add_log("❌ [APIFY GREŠKA 401]: Apify token je nevažeći ili ugašen!")
                        break
                    if res.status_code not in (200, 201):
                        if page == 1:
                            add_log(f"ℹ️ Apify ({actor_id}) status {res.status_code}: {res.text[:140]}")
                        break

                    raw_json = res.json()
                    page_comments, has_more, next_cursor = extract_comments_from_nested_json(raw_json)

                    if not page_comments:
                        break

                    actor_success = True
                    all_comments.extend(page_comments)

                    # Early-Stop: čim nađemo komentar s keywordom i replyem, prekidamo daljnje trošenje requestova!
                    found_with_reply = any(
                        is_our_comment(str(c.get("text", c.get("comment", "")) or "")) and
                        int(c.get("reply_comment_total", c.get("reply_total", c.get("replyCount", 0))) or 0) > 0
                        for c in all_comments
                    )
                    if found_with_reply:
                        return all_comments, f"Apify ({actor_id})"

                    if not has_more:
                        break
                    cursor = next_cursor if (next_cursor is not None and next_cursor > cursor) else (cursor + 50)

                except Exception as e:
                    add_log(f"⚠️ Greška pri pozivu {actor_id}: {e}")
                    break

            if actor_success and all_comments:
                return all_comments, f"Apify ({actor_id})"

    # --- REZERVNI SKENER (TikWM API) ---
    add_log("🔄 Prebacujem na direktni TikWM skener komentara...")
    cursor = 0
    for page in range(1, MAX_PAGES_PER_VIDEO + 1):
        try:
            res = requests.get(
                "https://www.tikwm.com/api/comment/list",
                params={"url": video_url, "count": 50, "cursor": cursor},
                headers=DESKTOP_HEADERS,
                timeout=15
            )
            data = res.json()
            c_data = data.get("data") or {}
            page_comments = c_data.get("comments") or []

            if not page_comments:
                break

            all_comments.extend(page_comments)

            found_with_reply = any(
                is_our_comment(str(c.get("text", "") or "")) and
                int(c.get("reply_total", c.get("reply_comment_total", 0)) or 0) > 0
                for c in all_comments
            )
            if found_with_reply:
                break

            if not c_data.get("has_more"):
                break
            cursor = c_data.get("cursor", cursor + 50)
            time.sleep(1.1)
        except Exception as e:
            add_log(f"⚠️ TikWM greška: {e}")
            break

    return all_comments, "TikWM Engine"


def send_likes_order(panel_url: str, api_key: str, service_id: str, video_link: str, username: str, quantity: int):
    payload = {
        "key": api_key,
        "action": "add",
        "service": int(service_id),
        "link": video_link,
        "username": username,
        "quantity": int(quantity),
    }
    try:
        r = requests.post(panel_url, data=payload, timeout=20)
        data = r.json()
        if "order" in data:
            return True, f"Order #{data['order']}"
        return False, f"{data}"
    except Exception as e:
        return False, f"Exception: {e}"


def worker_loop():
    global is_running

    while True:
        task = None
        with queue_lock:
            if order_queue:
                task = order_queue.pop(0)
                is_running = True
            else:
                is_running = False

        if not task:
            time.sleep(0.5)
            continue

        raw_link = task["link"]
        apify_token = task["apify_token"]
        panel_url = task["panel_url"]
        api_key = task["api_key"]
        service_id = task["service_id"]
        test_mode = task["test_mode"]

        video_id, clean_url = resolve_tiktok_url_and_id(raw_link)
        if not video_id:
            add_log(f"❌ Ne mogu izvući Video ID iz linka: {raw_link}")
            with queue_lock:
                stats["failed"] += 1
            continue

        add_log(f"🔍 Skeniram: {clean_url} (ID: {video_id})")
        comments, source_used = fetch_comments_multi_engine(video_id, clean_url, apify_token)

        with queue_lock:
            stats["scanned"] += 1

        if not comments:
            add_log(f"❌ Nema učitanih komentara za {clean_url}.")
            with queue_lock:
                stats["failed"] += 1
            continue

        add_log(f"📥 Uspješno učitano {len(comments)} komentara [{source_used}]. Tražim Alversett...")

        max_likes = 0
        top_user = "nepoznato"
        keyword_with_reply = None
        first_keyword_no_reply = None

        for c in comments:
            text = str(c.get("text", c.get("comment", "")) or "")
            likes = int(c.get("digg_count", c.get("diggCount", c.get("likes", 0))) or 0)
            replies = int(c.get("reply_comment_total", c.get("reply_total", c.get("replyCount", 0))) or 0)

            user_obj = c.get("user", {})
            if isinstance(user_obj, dict):
                username = user_obj.get("unique_id", user_obj.get("uniqueId", user_obj.get("nickname", "")))
            else:
                username = c.get("unique_id", c.get("user_unique_id", c.get("author", "")))
            username = str(username or "").lstrip("@")

            if likes > max_likes:
                max_likes = likes
                top_user = username or "nepoznato"

            if is_our_comment(text):
                parsed_c = {
                    "username": username,
                    "text": text,
                    "likes": likes,
                    "replies": replies,
                }
                if replies > 0 and keyword_with_reply is None:
                    keyword_with_reply = parsed_c
                elif first_keyword_no_reply is None:
                    first_keyword_no_reply = parsed_c

        # 1. PRAVILO: Ako najjači komentar ima 12.000 ili više lajkova -> NE ŠALJI NIŠTA
        desired_total = calculate_target_likes(max_likes)
        if desired_total == 0:
            add_log(
                f"⏭️ [PRESKOČENO] Top komentar (@{top_user}) ima {max_likes} lajkova "
                f"(limit je {MAX_TOP_LIKES_LIMIT}). Ne šaljem ništa."
            )
            with queue_lock:
                stats["skipped"] += 1
            continue

        # 2. PRAVILO: Prioritet ima komentar s replyem; ako nema replya, šalje na prvi pronađeni s keywordom
        target = None
        match_type = ""
        if keyword_with_reply:
            target = keyword_with_reply
            match_type = f"SA REPLYEM ({target['replies']} reply)"
        elif first_keyword_no_reply:
            target = first_keyword_no_reply
            match_type = "BEZ REPLYA (Prvi pronađeni s keywordom)"
        else:
            add_log(f"⚠️ Nije pronađen nijedan Alversett komentar među {len(comments)} učitanih komentara.")
            with queue_lock:
                stats["skipped"] += 1
            continue

        my_user = target["username"]
        my_likes = target["likes"]
        my_text_short = (target["text"][:55] + "...") if len(target["text"]) > 55 else target["text"]

        # 3. PRAVILO: Izračunaj koliko lajkova treba poslati do ciljanog broja
        likes_to_send = desired_total - my_likes

        add_log(
            f"🎯 Pronađen @{my_user} [{match_type}] | Trenutno: {my_likes} lajkova | "
            f"Top kom (@{top_user}): {max_likes} lajkova | Cilj: {desired_total}"
        )

        if likes_to_send <= 0:
            add_log(f"✅ Komentar @{my_user} već ima {my_likes} lajkova (više od ciljanih {desired_total}). Preskačem.")
            with queue_lock:
                stats["skipped"] += 1
            continue

        if test_mode:
            add_log(f"🧪 [TEST MOD] Poslalo bi se: {likes_to_send} lajkova -> @{my_user} | \"{my_text_short}\"")
            with queue_lock:
                stats["ordered"] += 1
        else:
            ok, msg = send_likes_order(panel_url, api_key, service_id, clean_url, my_user, likes_to_send)
            if ok:
                add_log(f"🚀 [POSLANO NA GODOFPANEL] +{likes_to_send} lajkova za @{my_user} -> {msg}")
                with queue_lock:
                    stats["ordered"] += 1
            else:
                add_log(f"❌ [GREŠKA PANELA] @{my_user} -> {msg}")
                with queue_lock:
                    stats["failed"] += 1

        time.sleep(1.5)


threading.Thread(target=worker_loop, daemon=True).start()


HTML_TEMPLATE = """
<!doctype html>
<html lang="bs">
<head>
  <title>🔥 TikTok Auto Comment Liker — GodOfPanel</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * { box-sizing: border-box; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; padding: 0; background: #050816; color: #f9fafb; display: flex; justify-content: center; min-height: 100vh; }
    .container { max-width: 940px; width: 100%; padding: 24px 16px 48px; }
    .card { background: rgba(15, 23, 42, 0.95); border-radius: 18px; padding: 22px; box-shadow: 0 20px 45px rgba(0, 0, 0, 0.6); border: 1px solid rgba(139, 92, 246, 0.4); }
    h1 { font-size: 22px; margin: 0 0 6px; text-align: center; color: #e0e7ff; }
    .subtitle { text-align: center; font-size: 13px; color: #9ca3af; margin-bottom: 18px; }
    .status-banner { background: rgba(30, 41, 59, 0.9); border: 1px solid rgba(99, 102, 241, 0.4); border-radius: 12px; padding: 12px 16px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 13px; }
    .badge { padding: 4px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; }
    .badge-active { background: rgba(234, 179, 8, 0.2); color: #facc15; border: 1px solid rgba(234, 179, 8, 0.5); }
    .badge-idle { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.5); }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
    label { font-size: 12px; font-weight: 600; color: #cbd5e1; margin-bottom: 5px; display: block; }
    textarea, input[type="text"], input[type="password"] { width: 100%; background: rgba(15, 23, 42, 0.9); border-radius: 10px; border: 1px solid rgba(55, 65, 81, 0.9); padding: 10px 12px; color: #e5e7eb; font-size: 13px; outline: none; }
    textarea:focus, input:focus { border-color: #8b5cf6; }
    textarea { min-height: 150px; resize: vertical; font-family: monospace; }
    .rules-box { background: rgba(15, 23, 42, 0.7); border: 1px dashed rgba(139, 92, 246, 0.4); border-radius: 10px; padding: 10px 14px; font-size: 12px; color: #a5b4fc; margin-bottom: 14px; line-height: 1.5; }
    .btn-row { display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center; margin: 16px 0 10px; }
    button { border: none; border-radius: 999px; padding: 10px 22px; font-size: 13px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; }
    .btn-primary { background: linear-gradient(135deg, #8b5cf6, #ec4899); color: white; box-shadow: 0 10px 25px rgba(139, 92, 246, 0.4); }
    .btn-danger { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.5); }
    .log { margin-top: 10px; font-size: 12px; font-family: monospace; white-space: pre-wrap; background: rgba(10, 15, 30, 0.95); border-radius: 10px; padding: 14px; border: 1px solid rgba(55,65,81,0.9); height: 320px; overflow-y: auto; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>🔥 TikTok Auto Comment Liker (x1.3) — GodOfPanel</h1>
      <div class="subtitle">
        ScrapTik API + TikWM Fallback • Dekodira kamuflažu • Šalje 1.3x na GodOfPanel (Service #8771)
      </div>

      <div class="status-banner">
        <div>
          Status: 
          <span id="status-badge" class="badge badge-idle">✅ SPREMNO</span>
        </div>
        <div>U redu: <b id="q-count">0</b></div>
        <div>
          Skenirano: <b id="st-scanned" style="color:#38bdf8;">0</b> |
          Poslano: <b id="st-ordered" style="color:#4ade80;">0</b> |
          Preskočeno: <b id="st-skipped" style="color:#facc15;">0</b> |
          Fail: <b id="st-failed" style="color:#f87171;">0</b>
        </div>
      </div>

      <div class="rules-box">
        📌 <b>Aktivna pravila:</b> 
        1. Čita kroz sve nevidljive (zero-width) znakove, tačkice i ćirilične homoglife • 
        2. Prioritet ima komentar sa replyem; ako nema replya, šalje na prvi pronađeni sa keywordom • 
        3. Dinamičko računanje cilja (0-199: min 150 ili +80 | 200-999: ×1.35 | 1k-3k: +600 | 3k-12k: +1000) • 
        4. Ako Top komentar ima <b>12.000 ili više lajkova</b>, automatski preskače video.
      </div>

      <div class="grid-2">
        <div>
          <label>Apify Personal API Token (apify_api_...):</label>
          <input type="password" id="apify_token" value="{{ default_apify }}" placeholder="Zalijepi svoj Apify token...">
        </div>
        <div>
          <label>Service ID za TikTok Comment Likes (GodOfPanel):</label>
          <input type="text" id="service_id" value="{{ default_service }}">
        </div>
      </div>

      <div class="grid-2">
        <div>
          <label>SMM Panel API URL (GodOfPanel):</label>
          <input type="text" id="panel_url" value="{{ default_panel_url }}">
        </div>
        <div>
          <label>SMM Panel API Ključ (GodOfPanel):</label>
          <input type="password" id="api_key" value="{{ default_api_key }}">
        </div>
      </div>

      <label>TikTok Video linkovi (jedan ispod drugog):</label>
      <textarea id="links" placeholder="https://vt.tiktok.com/...&#10;https://www.tiktok.com/@user/video/..."></textarea>

      <div class="btn-row">
        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color:#fbbf24; font-size:13px; margin:0;">
          <input type="checkbox" id="test_mode" style="width:17px; height:17px;">
          🧪 <b>TEST MOD</b> (Samo skeniraj i izračunaj u logu, ne šalji narudžbe na GodOfPanel)
        </label>

        <div style="display:flex; gap:10px;">
          <button class="btn-primary" onclick="startQueue()">🚀 POKRENI SKENIRANJE I LAJKOVE</button>
          <button class="btn-danger" onclick="clearQueue()">🛑 Zaustavi / Očisti red</button>
        </div>
      </div>

      <div style="font-size: 12px; color: #9ca3af; margin-top: 8px;"><b>Live Log (Najnovije na vrhu):</b></div>
      <div class="log" id="log-box">Čekam zadatke...</div>
    </div>
  </div>

  <script>
    async function startQueue() {
      const payload = {
        apify_token: document.getElementById('apify_token').value.trim(),
        panel_url: document.getElementById('panel_url').value.trim(),
        api_key: document.getElementById('api_key').value.trim(),
        service_id: document.getElementById('service_id').value.trim(),
        test_mode: document.getElementById('test_mode').checked,
        links: document.getElementById('links').value
      };
      const res = await fetch('/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.error) {
        alert(data.error);
      } else {
        document.getElementById('links').value = '';
      }
    }

    async function clearQueue() {
      if (!confirm('Sigurno želiš isprazniti red čekanja?')) return;
      await fetch('/clear', {method: 'POST'});
    }

    setInterval(async () => {
      try {
        const res = await fetch('/status');
        const data = await res.json();
        document.getElementById('q-count').innerText = data.queue_count;
        document.getElementById('st-scanned').innerText = data.stats.scanned;
        document.getElementById('st-ordered').innerText = data.stats.ordered;
        document.getElementById('st-skipped').innerText = data.stats.skipped;
        document.getElementById('st-failed').innerText = data.stats.failed;
        document.getElementById('log-box').innerText = data.log || 'Čekam zadatke...';

        const badge = document.getElementById('status-badge');
        if (data.is_running || data.queue_count > 0) {
          badge.className = 'badge badge-active';
          badge.innerText = '⚡ SKENIRANJE U TOKU';
        } else {
          badge.className = 'badge badge-idle';
          badge.innerText = '✅ SPREMNO';
        }
      } catch (e) {}
    }, 1500);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        default_apify=DEFAULT_APIFY_TOKEN,
        default_panel_url=DEFAULT_PANEL_URL,
        default_api_key=DEFAULT_API_KEY,
        default_service=DEFAULT_LIKE_SERVICE_ID,
    )


@app.route("/add", methods=["POST"])
def add_tasks():
    data = request.json or {}
    apify_token = data.get("apify_token", "").strip()
    panel_url = data.get("panel_url", "").strip()
    api_key = data.get("api_key", "").strip()
    service_id = data.get("service_id", "").strip()
    test_mode = bool(data.get("test_mode", False))
    raw_links = data.get("links", "")

    links = [l.strip() for l in raw_links.splitlines() if l.strip()]

    if not links:
        return jsonify({"error": "Unesi barem jedan TikTok link!"})
    if not test_mode and (not api_key or not service_id):
        return jsonify({"error": "Unesi GodOfPanel API ključ i Service ID (8771)!"})

    with queue_lock:
        for link in links:
            order_queue.append({
                "link": link,
                "apify_token": apify_token,
                "panel_url": panel_url,
                "api_key": api_key,
                "service_id": service_id,
                "test_mode": test_mode,
            })

    add_log(f"📥 Dodano {len(links)} linkova u red za skeniranje (Mod: {'TEST' if test_mode else 'LIVE SLANJE'}).")
    return jsonify({"ok": True})


@app.route("/clear", methods=["POST"])
def clear_tasks():
    with queue_lock:
        count = len(order_queue)
        order_queue.clear()
    add_log(f"🛑 Red čekanja je ispražnjen (obrisano {count} linkova).")
    return jsonify({"ok": True})


@app.route("/status")
def status():
    with queue_lock:
        return jsonify({
            "queue_count": len(order_queue),
            "is_running": is_running,
            "stats": stats,
            "log": "\n".join(log_lines),
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

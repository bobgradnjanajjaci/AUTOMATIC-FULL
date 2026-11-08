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
# 🔧 DEFAULT KONFIGURACIJA
# =====================================================================
DEFAULT_APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "apify_api_envhGdMaylNtuuVIfDHGHc946c7uOu1pBsiC")
DEFAULT_PANEL_URL = os.environ.get("PANEL_URL", "https://godofpanel.com/api/v2")
DEFAULT_API_KEY = os.environ.get("API_KEY", "c4868df4a78299b800d222cd9478ba17")
DEFAULT_LIKE_SERVICE_ID = os.environ.get("LIKE_SERVICE_ID", "8771")

# Pravila za lajkove
MULTIPLIER = 1.30           # Šalje 1.3x više lajkova od top komentara
MAX_TOP_LIKES_LIMIT = 15000 # Ako prvi komentar ima preko 15.000 lajkova, ne šalje ništa
FALLBACK_MIN_LIKES = 25     # Ako top komentar ima 0 lajkova, pošalji 25
MAX_PAGES_PER_VIDEO = 10    # Maksimalno stranica po videu (do 500 komentara)

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
    """
    Rekurzivno pronalazi sve objekte komentara bez obzira kako ih je Apify actor zapakovao.
    """
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
        # Ako je sam dict zapravo komentar
        if ("text" in data or "comment" in data) and (
            "digg_count" in data or "diggCount" in data or "cid" in data or "user" in data
        ):
            found.append(data)

        if data.get("has_more") in (1, True) or data.get("hasMore") is True:
            has_more = True
        if "cursor" in data and isinstance(data["cursor"], int):
            next_cursor = data["cursor"]

        # Pretraži pod-ključeve
        for key in ("comments", "data", "items", "result", "results", "comment_list"):
            if key in data and isinstance(data[key], (list, dict)):
                c_list, hm, nc = extract_comments_from_nested_json(data[key])
                found.extend(c_list)
                if hm:
                    has_more = True
                if nc is not None:
                    next_cursor = nc

    return found, has_more, next_cursor


def fetch_comments_via_apify_or_fallback(video_id: str, video_url: str, apify_token: str):
    """
    1. Pokušava Apify ScrapTik (sa svim varijantama input polja: comments_getComments=True, aweme_id, videoUrl).
    2. Ako Apify vrati prazno (npr. pogrešan actor ili potrošen free limit), automatski prelazi na
       direktni TikWM API kako nijedan video ne bi propao!
    """
    all_comments = []
    cursor = 0

    # --- KORAK 1: APIFY SCRAPTIK ---
    if apify_token:
        actors_to_try = [
            "scraptik~tiktok-comments-scraper",
            "scraptik~tiktok-api",
        ]
        for actor_id in actors_to_try:
            cursor = 0
            actor_worked = False
            for page in range(1, MAX_PAGES_PER_VIDEO + 1):
                endpoint = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
                # Šaljemo sva polja koja različite verzije ScrapTik actora traže
                payload = {
                    "comments_getComments": True,
                    "comments_awemeId": str(video_id),
                    "comments_count": 50,
                    "comments_cursor": cursor,
                    "aweme_id": str(video_id),
                    "video_id": str(video_id),
                    "videoId": str(video_id),
                    "url": video_url,
                    "videoUrl": video_url,
                    "postURLs": [video_url],
                    "count": 50,
                    "maxComments": 200,
                    "cursor": cursor,
                }
                try:
                    res = requests.post(
                        endpoint,
                        params={"token": apify_token},
                        json=payload,
                        timeout=55
                    )
                    if res.status_code not in (200, 201):
                        if page == 1:
                            add_log(f"ℹ️ Apify ({actor_id}) status {res.status_code}: {res.text[:140]}")
                        break

                    raw_json = res.json()
                    page_comments, has_more, next_cursor = extract_comments_from_nested_json(raw_json)

                    if not page_comments:
                        if page == 1 and raw_json:
                            add_log(f"ℹ️ Apify ({actor_id}) vratio neočekivan format: {str(raw_json)[:160]}")
                        break

                    actor_worked = True
                    all_comments.extend(page_comments)

                    # Early-Stop: ako smo već našli naš komentar SA REPLYEM, stani odmah!
                    found_with_reply = any(
                        is_our_comment(str(c.get("text", c.get("comment", "")) or "")) and
                        int(c.get("reply_comment_total", c.get("reply_total", c.get("replyCount", 0))) or 0) > 0
                        for c in all_comments
                    )
                    if found_with_reply:
                        return all_comments, f"Apify ({actor_id})"

                    # Ako je actor već vratio 100+ komentara u jednom cugu (npr. comments-scraper)
                    if len(page_comments) > 55 or not has_more:
                        break
                    cursor = next_cursor if next_cursor is not None else (cursor + 50)

                except Exception as e:
                    add_log(f"⚠️ Apify ({actor_id}) greška: {e}")
                    break

            if actor_worked and all_comments:
                return all_comments, f"Apify ({actor_id})"

    # --- KORAK 2: AUTOMATSKI REZERVNI ENGINE (TikWM Public API) ---
    # Ako Apify vrati 0 komentara, ovaj rezervni sistem besplatno izvlači komentare da skripta nikad ne stane!
    add_log("🔄 Apify nije vratio komentare — aktiviram rezervni direktni skener (TikWM)...")
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
            time.sleep(1.1)  # Poštujemo 1 req/sec limit besplatnog TikWM-a
        except Exception as e:
            add_log(f"⚠️ Greška na rezervnom skeneru: {e}")
            break

    return all_comments, "TikWM Fallback"


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
        comments, source_used = fetch_comments_via_apify_or_fallback(video_id, clean_url, apify_token)

        with queue_lock:
            stats["scanned"] += 1

        if not comments:
            add_log(f"❌ Nema učitanih komentara za {clean_url} (video možda nema komentara ili je zaključan).")
            with queue_lock:
                stats["failed"] += 1
            continue

        add_log(f"📥 Učitano {len(comments)} komentara (Izvor: {source_used}). Analiziram...")

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

        # 1. PRAVILO: Ako najjači komentar ima preko 15.000 lajkova -> NE ŠALJI NIŠTA
        if max_likes > MAX_TOP_LIKES_LIMIT:
            add_log(
                f"⏭️ [PRESKOČENO] Top komentar (@{top_user}) ima {max_likes} lajkova "
                f"(preko limita od {MAX_TOP_LIKES_LIMIT}). Ne šaljem ništa."
            )
            with queue_lock:
                stats["skipped"] += 1
            continue

        # 2. PRAVILO: Prioritet ima komentar sa replyem; ako nema replya, šalje na prvi sa keywordom
        target = None
        match_type = ""
        if keyword_with_reply:
            target = keyword_with_reply
            match_type = f"SA REPLYEM ({target['replies']} reply)"
        elif first_keyword_no_reply:
            target = first_keyword_no_reply
            match_type = "BEZ REPLYA (Prvi pronađeni sa keywordom)"
        else:
            add_log(f"⚠️ Nije pronađen nijedan Alversett komentar među {len(comments)} učitanih komentara.")
            with queue_lock:
                stats["skipped"] += 1
            continue

        my_user = target["username"]
        my_likes = target["likes"]
        my_text_short = (target["text"][:55] + "...") if len(target["text"]) > 55 else target["text"]

        # 3. PRAVILO: Top lajkovi * 1.3
        if max_likes == 0:
            desired_total = FALLBACK_MIN_LIKES
        else:
            desired_total = int(round(max_likes * MULTIPLIER))
            if desired_total <= max_likes:
                desired_total = max_likes + 10

        likes_to_send = desired_total - my_likes

        add_log(
            f"🎯 Pronađen @{my_user} [{match_type}] | Trenutno: {my_likes} lajkova | "
            f"Top kom (@{top_user}): {max_likes} lajkova | Cilj (x1.3): {desired_total}"
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
                add_log(f"🚀 [POSLANO] +{likes_to_send} lajkova za @{my_user} -> {msg}")
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
  <title>🔥 TikTok Auto Comment Liker (x1.3) — Dual Engine</title>
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
      <h1>🔥 TikTok Auto Comment Liker (x1.3) — Dual Engine</h1>
      <div class="subtitle">
        Apify ScrapTik + Automatski TikWM Fallback • Čita kroz nevidljive znakove i ćirilicu • Šalje 1.3x od Top komentara
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
        1. Čita kroz sve nevidljive (zero-width) znakove, tačkice i ćirilične homoglife (<i>Alversett, AIversett, Alver.sett, Norra, Efect...</i>) • 
        2. Prioritet ima komentar sa replyem; ako nema replya, šalje na prvi pronađeni sa keywordom • 
        3. Računa <b>Top Komentar × 1.3</b> (umanjeno za trenutne lajkove tvog komentara) • 
        4. Ako Top komentar ima <b>preko 15.000 lajkova</b>, automatski preskače video.
      </div>

      <div class="grid-2">
        <div>
          <label>Apify API Token (apify_api_... ili ostavi prazno za direktni TikWM):</label>
          <input type="password" id="apify_token" value="{{ default_apify }}" placeholder="Zalijepi svoj Apify Personal API Token...">
        </div>
        <div>
          <label>Service ID za TikTok Comment Likes (SMMFrog):</label>
          <input type="text" id="service_id" value="{{ default_service }}" placeholder="npr. 4125 (upiši ID servisa za lajkove komentara)">
        </div>
      </div>

      <div class="grid-2">
        <div>
          <label>SMM Panel API URL:</label>
          <input type="text" id="panel_url" value="{{ default_panel_url }}">
        </div>
        <div>
          <label>SMM Panel API Ključ:</label>
          <input type="password" id="api_key" value="{{ default_api_key }}">
        </div>
      </div>

      <label>TikTok Video linkovi (jedan ispod drugog):</label>
      <textarea id="links" placeholder="https://vt.tiktok.com/...&#10;https://www.tiktok.com/@user/video/..."></textarea>

      <div class="btn-row">
        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color:#fbbf24; font-size:13px; margin:0;">
          <input type="checkbox" id="test_mode" style="width:17px; height:17px;">
          🧪 <b>TEST MOD</b> (Samo skeniraj i izračunaj u logu, ne šalji narudžbe na SMMFrog)
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
        return jsonify({"error": "Unesi SMMFrog API ključ i Service ID za Comment Likes (ili uključi Test Mod)!"})

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

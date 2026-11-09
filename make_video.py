import os, json, re, datetime, subprocess, asyncio, textwrap, time
import html as hl
from pathlib import Path
import requests
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from playwright.sync_api import sync_playwright

WORK = Path("work")
WORK.mkdir(exist_ok=True)
VOICE = os.environ.get("VOICE", "en-US-GuyNeural")
PRIVACY = os.environ.get("PRIVACY", "private")  # पहले "private" रखें, जाँच के बाद "public"
MODEL = "gemini-3.8-flash"
W, H = 1280, 720
DISCLAIMER = ("This video is for general education only. It is not financial advice. "
              "Speak with a qualified advisor before making financial decisions.")


def pick_topic():
    lines = Path("topics.txt").read_text(encoding="utf-8").splitlines()
    topics = [t.strip() for t in lines if t.strip()]
    idx = datetime.date.today().timetuple().tm_yday % len(topics)
    return topics[idx]


def post_with_retry(url, payload, tries=8):
    r = None
    for attempt in range(tries):
        try:
            r = requests.post(url, json=payload, timeout=300)
        except requests.exceptions.RequestException as e:
            print(f"Request error: {e}")
            r = None
            if attempt == tries - 1:
                raise
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return r
        wait = min(15 * (2 ** attempt), 300)
        print(f"Gemini busy/timeout, retry {attempt + 1}/{tries} in {wait}s")
        time.sleep(wait)
    return r


def gemini_content(topic):
    key = os.environ["GEMINI_API_KEY"]
    prompt = ("You are writing an 8 to 10 minute educational YouTube video for a faceless "
              "personal finance channel aimed at a global English audience.\n"
              "Topic: " + topic + "\n"
              "Rules:\n"
              "- Accurate, general educational information only. No investment advice, "
              "no guaranteed returns, no get-rich claims.\n"
              "- Plain English, friendly and clear tone.\n"
              "- Make 12 to 15 slides. Each narration is 4 to 6 sentences.\n"
              "- Each slide has an html field: a complete standalone HTML document sized "
              "1280x720, dark background #121826, accent color #00aa78, all CSS and JS inline, "
              "no external links, scripts or images. Show an animated visual of the concept "
              "(for example a candlestick chart, a growing bar chart, a coin stack or a flow "
              "diagram). The animation must finish within 10 seconds.\n"
              "- Return ONLY valid JSON, no markdown, in this shape: "
              '{"title": "max 70 characters", "description": "two short paragraphs", '
              '"tags": ["8 short tags"], "slides": [{"heading": "max 6 words", '
              '"narration": "...", "html": "..."}]}\n')
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + MODEL + ":generateContent?key=" + key)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "maxOutputTokens": 16000}}
    r = post_with_retry(url, body)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    text = re.sub(r"^```(json)?|```$", "", text).strip()
    return json.loads(text)


def template_html(heading, body):
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:{W}px;height:{H}px;background:#121826;color:#fff;
font-family:DejaVu Sans,Arial,sans-serif;overflow:hidden}}
.bar{{height:12px;background:#00aa78}}
h1{{font-size:64px;margin:90px 80px 30px;animation:fade 1s ease-out}}
p{{font-size:34px;line-height:1.4;margin:0 80px;color:#d2dce6;animation:fade 1.5s .5s both}}
@keyframes fade{{from{{opacity:0;transform:translateY(20px)}}to{{opacity:1;transform:none}}}}
</style></head><body><div class="bar"></div>
<h1>{hl.escape(heading)}</h1><p>{hl.escape(body)}</p></body></html>"""


def write_slide_html(s, path):
    h = s.get("html") or ""
    if "<html" not in h.lower() and "<body" not in h.lower():
        h = template_html(s["heading"], s["narration"])
    path.write_text(h, encoding="utf-8")


async def _audio_all(items):
    for text, path in items:
        await edge_tts.Communicate(text, VOICE).save(str(path))


def audio_length(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.decode().strip())


def render_webm(browser, html_path, dur):
    ctx = browser.new_context(viewport={"width": W, "height": H},
                              record_video_dir=str(WORK / "rec"),
                              record_video_size={"width": W, "height": H})
    page = ctx.new_page()
    page.goto(html_path.resolve().as_uri(), wait_until="load")
    page.wait_for_timeout(int(dur * 1000))
    video = page.video
    ctx.close()
    return Path(video.path())


def mux(webm, audio, dur, out):
    subprocess.run(["ffmpeg", "-y", "-i", str(webm), "-i", str(audio),
                    "-map", "0:v", "-map", "1:a", "-vf", "fps=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-t", f"{dur:.2f}", str(out)], check=True)


def load_font(size):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_slide_image(heading, narration, path):
    img = Image.new("RGB", (W, H), (18, 24, 38))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 12], fill=(0, 170, 120))
    d.text((80, 110), heading, font=load_font(56), fill=(255, 255, 255))
    d.multiline_text((80, 250), textwrap.fill(narration, 60),
                     font=load_font(34), fill=(210, 220, 230), spacing=12)
    img.save(path)


def static_clip(img, audio, dur, out):
    subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-i", str(audio),
                    "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "160k",
                    "-pix_fmt", "yuv420p", "-t", f"{dur:.2f}", "-shortest", str(out)],
                   check=True)


def make_thumbnail(browser, title, out):
    tpath = WORK / "thumb.html"
    tpath.write_text(template_html(title[:60], "Money Made Simple"), encoding="utf-8")
    page = browser.new_page(viewport={"width": W, "height": H})
    page.goto(tpath.resolve().as_uri(), wait_until="load")
    page.wait_for_timeout(1500)
    page.screenshot(path=str(out))
    page.close()


def youtube_service():
    info = json.loads(os.environ["YOUTUBE_TOKEN"])
    creds = Credentials.from_authorized_user_info(
        info, ["https://www.googleapis.com/auth/youtube.upload"])
    if not creds.valid:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload(path, title, description, tags):
    yt = youtube_service()
    body = {"snippet": {"title": title, "description": description,
                        "tags": tags, "categoryId": "27"},  # 27 = Education
            "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(path), chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Uploaded video id:", resp["id"])
    return resp["id"], yt


def main():
    topic = pick_topic()
    print("Topic:", topic)
    data = gemini_content(topic)
    slides = data["slides"]
    slides.append({"heading": "Disclaimer", "narration": DISCLAIMER, "html": ""})

    slides_dir = WORK / "slides"
    slides_dir.mkdir(exist_ok=True)
    (WORK / "rec").mkdir(exist_ok=True)

    html_paths, auds = [], []
    for i, s in enumerate(slides):
        hp = slides_dir / f"{i:02d}.html"
        write_slide_html(s, hp)
        html_paths.append(hp)
        auds.append(slides_dir / f"{i:02d}.mp3")
    asyncio.run(_audio_all([(s["narration"], a) for s, a in zip(slides, auds)]))
    durs = [audio_length(a) + 0.8 for a in auds]

    clips = []
    thumb = WORK / "thumb.png"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for i, s in enumerate(slides):
            clip = slides_dir / f"{i:02d}.mp4"
            try:
                webm = render_webm(browser, html_paths[i], durs[i])
                mux(webm, auds[i], durs[i], clip)
            except Exception as e:
                print(f"Slide {i} animation failed, using static image: {e}")
                img = slides_dir / f"{i:02d}.png"
                make_slide_image(s["heading"], s["narration"], img)
                static_clip(img, auds[i], durs[i], clip)
            clips.append(clip)
        try:
            make_thumbnail(browser, data["title"], thumb)
        except Exception as e:
            print(f"Thumbnail failed: {e}")
            thumb = None
        browser.close()

    lst = WORK / "list.txt"
    lst.write_text("\n".join(f"file '{c.resolve()}'" for c in clips), encoding="utf-8")
    final = WORK / "final.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(final)],
                   check=True)

    description = data["description"] + "\n\nFor general education only. Not financial advice."
    vid, yt = upload(final, data["title"][:100], description, data["tags"])

    if thumb and thumb.exists():
        try:
            yt.thumbnails().set(videoId=vid,
                                media_body=MediaFileUpload(str(thumb), mimetype="image/png")
                                ).execute()
            print("Thumbnail set")
        except Exception as e:
            print(f"Thumbnail upload failed (channel verification needed?): {e}")


if __name__ == "__main__":
    main()

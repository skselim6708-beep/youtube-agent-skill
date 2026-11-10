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
MODEL = "thinkingmachines/inkling-2-small:free"
W, H = 1280, 720
FPS = 25
DISCLAIMER = ("This video is for general education only. It is not financial advice. "
              "Speak with a qualified advisor before making financial decisions.")

STYLE = """
html,body{margin:0;width:1280px;height:720px;background:#121826;color:#fff;
font-family:DejaVu Sans,Arial,sans-serif;overflow:hidden;position:relative}
.bar{height:12px;background:#00aa78}
h1{font-size:58px;margin:60px 80px 16px;max-width:1120px;animation:fade .9s ease-out both}
p{font-size:28px;line-height:1.4;margin:0 0 0 80px;max-width:520px;color:#d2dce6;animation:fade 1.2s .4s both}
.art{position:absolute;right:80px;bottom:60px;width:560px;height:300px}
.chart{display:flex;align-items:flex-end;gap:22px;justify-content:flex-end}
.col{width:60px;height:0;background:#00aa78;animation:grow 1.2s ease-out forwards}
.art polyline{fill:none;stroke:#00aa78;stroke-width:8;stroke-linecap:round;
stroke-dasharray:1400;stroke-dashoffset:1400;animation:draw 2.5s ease-out .5s forwards}
.coin{position:absolute;top:-80px;width:70px;height:70px;border-radius:50%;background:#f5b841;
animation:drop 1.4s ease-in forwards}
@keyframes fade{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}
@keyframes grow{to{height:var(--h)}}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes drop{to{transform:translateY(400px)}}
"""


def pick_topic():
    lines = Path("topics.txt").read_text(encoding="utf-8").splitlines()
    topics = [t.strip() for t in lines if t.strip()]
    idx = datetime.date.today().timetuple().tm_yday % len(topics)
    return topics[idx]


def post_with_retry(url, payload, headers=None, tries=5):
    last = ""
    for attempt in range(tries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=180)
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            last = "HTTP " + str(r.status_code)
        except requests.exceptions.RequestException as e:
            last = str(e)
        wait = min(15 * (2 ** attempt), 120)
        print("Model retry " + str(attempt + 1) + "/" + str(tries) + " (" + last + "), waiting " + str(wait) + "s")
        time.sleep(wait)
    raise RuntimeError("Model failed after " + str(tries) + " tries: " + last)


def model_outline(topic):
    key = os.environ["OPENROUTER_API_KEY"]
    prompt = (
        "You are writing an educational YouTube video script for a faceless personal finance "
        "channel aimed at a global English audience.\n"
        "Topic: " + topic + "\n"
        "Rules:\n"
        "- Accurate, general educational information only. No investment advice, no guaranteed "
        "returns, no get-rich claims.\n"
        "- Plain English, friendly and clear tone.\n"
        "- Make 8 to 10 slides.\n"
        "- Each heading is at most 6 words.\n"
        "- Each narration is 3 to 4 sentences.\n"
        "- Each visual is one of: bars, line, coins.\n"
        "- Return ONLY valid JSON, no markdown, in this exact shape:\n"
        '{"title": "max 70 characters", "description": "two short paragraphs", '
        '"tags": ["8 short tags"], "slides": [{"heading": "...", "narration": "...", "visual": "bars"}]}\n'
    )
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000}
    for attempt in range(2):
        r = post_with_retry(url, body, headers=headers)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(json)?|```$", "", text).strip()
        try:
            data = json.loads(text)
            if data.get("slides"):
                return data
        except json.JSONDecodeError as e:
            print("Bad JSON from model, attempt " + str(attempt + 1) + ": " + str(e))
    raise RuntimeError("Model did not return valid outline JSON")


def scene_html(heading, narration, visual):
    short = narration
    if len(short) > 260:
        short = short[:257].rsplit(" ", 1)[0] + "..."
    if visual == "line":
        art = ('<div class="art"><svg width="560" height="300" viewBox="0 0 560 300">'
               '<polyline points="0,260 80,220 160,240 240,170 320,190 400,120 480,140 560,40"/>'
               '</svg></div>')
    elif visual == "coins":
        coins = "".join(
            '<div class="coin" style="left:' + str(x) + 'px;animation-delay:' + str(d) + 's"></div>'
            for x, d in [(300, 0.0), (380, 0.3), (460, 0.6), (340, 0.9), (420, 1.2), (500, 1.5)])
        art = '<div class="art">' + coins + '</div>'
    else:
        heights = [120, 180, 150, 240, 210, 300, 280]
        cols = "".join(
            '<div class="col" style="--h:' + str(h) + 'px;animation-delay:' + str(round(i * 0.15, 2)) + 's"></div>'
            for i, h in enumerate(heights))
        art = '<div class="art chart">' + cols + '</div>'
    return ('<!doctype html><html><head><meta charset="utf-8"><style>' + STYLE +
            '</style></head><body><div class="bar"></div>'
            '<h1>' + hl.escape(heading) + '</h1>'
            '<p>' + hl.escape(short) + '</p>' + art + '</body></html>')


def thumb_html(title):
    return ('<!doctype html><html><head><meta charset="utf-8"><style>' + STYLE +
            '.big{font-size:84px;font-weight:bold;margin:150px 80px 0;line-height:1.1}'
            '.sub{font-size:36px;color:#00aa78;margin:30px 80px 0}'
            '</style></head><body><div class="bar"></div>'
            '<div class="big">' + hl.escape(title[:60]) + '</div>'
            '<div class="sub">Money Made Simple</div></body></html>')


async def make_voice(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))


def audio_length(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.decode().strip())


def record_slide(browser, html_path, dur):
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
                    "-map", "0:v", "-map", "1:a", "-vf", "fps=" + str(FPS),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-t", "{:.2f}".format(dur), str(out)], check=True)


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
                    "-pix_fmt", "yuv420p", "-t", "{:.2f}".format(dur), "-shortest", str(out)],
                   check=True)


def make_thumbnail(browser, title, out):
    tpath = WORK / "thumb.html"
    tpath.write_text(thumb_html(title), encoding="utf-8")
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
    data = model_outline(topic)
    slides = data["slides"][:10]
    slides.append({"heading": "Disclaimer", "narration": DISCLAIMER, "visual": "bars"})

    slides_dir = WORK / "slides"
    slides_dir.mkdir(exist_ok=True)
    (WORK / "rec").mkdir(exist_ok=True)

    auds = []
    for i, s in enumerate(slides):
        aud = slides_dir / (str(i).zfill(2) + ".mp3")
        asyncio.run(make_voice(s["narration"], aud))
        auds.append(aud)
    durs = [audio_length(a) + 0.8 for a in auds]

    clips = []
    thumb = WORK / "thumb.png"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for i, s in enumerate(slides):
            html_path = slides_dir / (str(i).zfill(2) + ".html")
            html_path.write_text(scene_html(s["heading"], s["narration"], s.get("visual", "bars")),
                                 encoding="utf-8")
            clip = slides_dir / (str(i).zfill(2) + ".mp4")
            try:
                webm = record_slide(browser, html_path, durs[i])
                mux(webm, auds[i], durs[i], clip)
            except Exception as e:
                print("Slide " + str(i) + " animation failed, using static image: " + str(e))
                img = slides_dir / (str(i).zfill(2) + ".png")
                make_slide_image(s["heading"], s["narration"], img)
                static_clip(img, auds[i], durs[i], clip)
            clips.append(clip)
        try:
            make_thumbnail(browser, data["title"], thumb)
        except Exception as e:
            print("Thumbnail failed: " + str(e))
            thumb = None
        browser.close()

    lst = WORK / "list.txt"
    lst.write_text("\n".join("file '" + str(c.resolve()) + "'" for c in clips), encoding="utf-8")
    final = WORK / "final.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    str(final)], check=True)

    description = data["description"] + "\n\nFor general education only. Not financial advice."
    vid, yt = upload(final, data["title"][:100], description, data["tags"])

    if thumb and thumb.exists():
        try:
            yt.thumbnails().set(videoId=vid,
                                media_body=MediaFileUpload(str(thumb), mimetype="image/png")
                                ).execute()
            print("Thumbnail set")
        except Exception as e:
            print("Thumbnail upload failed (channel verification needed?): " + str(e))


if __name__ == "__main__":
    main()

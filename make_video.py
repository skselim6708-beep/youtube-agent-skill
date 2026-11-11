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
GEMINI_MODEL = "gemini-3.8-flash"
OPENROUTER_MODELS = [
    "liquid/lfm-3-1.7b:free",
    "thinkingmachines/inkling-2-small:free",
]
W, H = 1280, 720
FPS = 25
THREE_URL = "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"
DISCLAIMER = ("This video is for general education only. It is not financial advice. "
              "Speak with a qualified advisor before making financial decisions.")

STYLE = """
html,body{margin:0;width:1280px;height:720px;background:#0b1020;color:#fff;
font-family:DejaVu Sans,Arial,sans-serif;overflow:hidden;position:relative}
#scene{position:absolute;left:0;top:0;width:1280px;height:720px}
.bar{position:absolute;left:0;top:0;width:1280px;height:12px;background:#00aa78;z-index:2}
h1{position:absolute;left:80px;top:50px;font-size:56px;margin:0;max-width:1120px;z-index:2;
text-shadow:0 0 18px rgba(0,255,200,.6);animation:fade .9s ease-out both}
p{position:absolute;left:80px;bottom:60px;font-size:26px;line-height:1.4;margin:0;max-width:700px;
color:#d2f5ec;z-index:2;background:rgba(10,20,30,.55);padding:14px 18px;border-radius:10px;
animation:fade 1.2s .4s both}
@keyframes fade{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}
"""


def pick_topic():
    lines = Path("topics.txt").read_text(encoding="utf-8").splitlines()
    topics = [t.strip() for t in lines if t.strip()]
    idx = datetime.date.today().timetuple().tm_yday % len(topics)
    return topics[idx]


def build_prompt(topic):
    return (
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
        "- Each visual is one of: bars, line, coins, globe.\n"
        "- Return ONLY valid JSON in this exact shape, no markdown:\n"
        '{"title": "max 70 characters", "description": "two short paragraphs", '
        '"tags": ["8 short tags"], "slides": [{"heading": "...", "narration": "...", "visual": "bars"}]}\n'
    )


def parse_outline(text):
    text = re.sub(r"^```(json)?|```$", "", text.strip()).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("slides"):
        return None
    return data


def ask_gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + GEMINI_MODEL + ":generateContent?key=" + key)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "maxOutputTokens": 6000}}
    r = requests.post(url, json=body, timeout=180)
    if r.status_code != 200:
        print("Gemini HTTP " + str(r.status_code) + ": " + r.text[:200])
        return None
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError):
        print("Gemini gave no text")
        return None
    return parse_outline(text)


def ask_openrouter(prompt):
    key = os.environ["OPENROUTER_API_KEY"]
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    for model in OPENROUTER_MODELS:
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 6000}
        try:
            r = requests.post(url, json=body, headers=headers, timeout=180)
        except requests.exceptions.RequestException as e:
            print("OpenRouter network error on " + model + ": " + str(e))
            continue
        if r.status_code != 200:
            print("OpenRouter " + model + " HTTP " + str(r.status_code))
            continue
        try:
            text = r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            print("OpenRouter " + model + " gave no text")
            continue
        if not text or not str(text).strip():
            print("OpenRouter " + model + " gave empty text")
            continue
        data = parse_outline(str(text))
        if data is None:
            print("OpenRouter " + model + " gave invalid JSON")
            continue
        print("Using OpenRouter model: " + model)
        return data
    return None


def get_outline(topic):
    prompt = build_prompt(topic)
    if os.environ.get("GEMINI_API_KEY"):
        data = ask_gemini(prompt)
        if data is not None:
            print("Using Gemini model: " + GEMINI_MODEL)
            return data
        print("Gemini failed, trying OpenRouter")
    data = ask_openrouter(prompt)
    if data is not None:
        return data
    raise RuntimeError("No model produced a valid script (Gemini and OpenRouter both failed)")


def three_scene_js(visual):
    # हर visual के लिए एक 3D दृश्य। सब Three.js से बनते हैं।
    return """
var scene = new THREE.Scene();
var camera = new THREE.PerspectiveCamera(45, 1280/720, 0.1, 100);
camera.position.set(0, 1.2, 9);
var renderer = new THREE.WebGLRenderer({antialias:true, alpha:true, preserveDrawingBuffer:true});
renderer.setSize(1280, 720);
renderer.setClearColor(0x000000, 0);
document.getElementById('scene').appendChild(renderer.domElement);
scene.add(new THREE.AmbientLight(0x88ffdd, 0.6));
var light = new THREE.DirectionalLight(0x00ffcc, 1.0);
light.position.set(3, 5, 4);
scene.add(light);
var group = new THREE.Group();
scene.add(group);
var mat = new THREE.MeshPhongMaterial({color:0x00aa78, emissive:0x003322, shininess:80,
  transparent:true, opacity:0.92});
var wire = new THREE.MeshBasicMaterial({color:0x00ffcc, wireframe:true, transparent:true, opacity:0.35});
var visual = '""" + visual + """';
if (visual === 'coins') {
  for (var i = 0; i < 7; i++) {
    var c = new THREE.Mesh(new THREE.CylinderGeometry(0.6, 0.6, 0.15, 40),
      new THREE.MeshPhongMaterial({color:0xf5b841, emissive:0x553300, shininess:100}));
    c.rotation.x = Math.PI / 2;
    c.position.set(-3 + i * 1.0, Math.sin(i) * 0.6, 0);
    group.add(c);
  }
} else if (visual === 'line' || visual === 'globe') {
  var g = new THREE.SphereGeometry(2.2, 32, 32);
  var s = new THREE.Mesh(g, mat);
  var w = new THREE.Mesh(new THREE.SphereGeometry(2.25, 24, 24), wire);
  group.add(s);
  group.add(w);
} else {
  var heights = [1.0, 1.8, 1.4, 2.6, 2.1, 3.2, 2.8];
  for (var j = 0; j < heights.length; j++) {
    var b = new THREE.Mesh(new THREE.BoxGeometry(0.7, heights[j], 0.7), mat);
    b.position.set(-3 + j * 1.0, heights[j] / 2 - 1.5, 0);
    group.add(b);
    var e = new THREE.Mesh(new THREE.BoxGeometry(0.72, heights[j], 0.72), wire);
    e.position.copy(b.position);
    group.add(e);
  }
}
var t = 0;
function animate() {
  t += 0.02;
  group.rotation.y = t * 0.6;
  group.rotation.x = Math.sin(t * 0.5) * 0.15;
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();
"""


def scene_html(heading, narration, visual):
    short = narration
    if len(short) > 260:
        short = short[:257].rsplit(" ", 1)[0] + "..."
    js = three_scene_js(visual)
    return ('<!doctype html><html><head><meta charset="utf-8"><style>' + STYLE +
            '</style><script src="' + THREE_URL + '"></script></head><body>'
            '<div id="scene"></div>'
            '<div class="bar"></div>'
            '<h1>' + hl.escape(heading) + '</h1>'
            '<p>' + hl.escape(short) + '</p>'
            '<script>' + js + '</script></body></html>')


def thumb_html(title):
    return ('<!doctype html><html><head><meta charset="utf-8"><style>' + STYLE +
            '.big{position:absolute;left:80px;top:300px;font-size:84px;font-weight:bold;line-height:1.1;'
            'text-shadow:0 0 22px rgba(0,255,200,.7)}'
            '.sub{position:absolute;left:80px;top:500px;font-size:36px;color:#00ffcc}'
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
    data = get_outline(topic)
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
        browser = p.chromium.launch(args=["--no-sandbox", "--use-gl=swiftshader",
                                          "--enable-webgl", "--ignore-gpu-blocklist"])
        for i, s in enumerate(slides):
            html_path = slides_dir / (str(i).zfill(2) + ".html")
            html_path.write_text(scene_html(s["heading"], s["narration"], s.get("visual", "bars")),
                                 encoding="utf-8")
            clip = slides_dir / (str(i).zfill(2) + ".mp4")
            try:
                webm = record_slide(browser, html_path, durs[i])
                mux(webm, auds[i], durs[i], clip)
            except Exception as e:
                print("Slide " + str(i) + " 3D failed, using static image: " + str(e))
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

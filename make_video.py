import os, json, re, datetime, subprocess, asyncio, textwrap
from pathlib import Path
import requests
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

WORK = Path("work")
WORK.mkdir(exist_ok=True)
VOICE = os.environ.get("VOICE", "en-US-AriaNeural")
PRIVACY = os.environ.get("PRIVACY", "private")  # पहले "private" रखें, जाँच के बाद "public"
W, H = 1280, 720


def pick_topic():
    lines = Path("topics.txt").read_text(encoding="utf-8").splitlines()
    topics = [t.strip() for t in lines if t.strip()]
    idx = datetime.date.today().timetuple().tm_yday % len(topics)
    return topics[idx]


def gemini_script(topic):
    key = os.environ["GEMINI_API_KEY"]
    prompt = f"""You are writing a 5 to 7 minute educational YouTube script for a faceless personal finance channel aimed at a global audience.
Topic: {topic}
Rules:
- Accurate, general educational information only. No investment advice, no guaranteed returns, no get-rich claims.
- Plain English, friendly and clear tone.
- Split into 8 to 12 slides. Each slide has a heading (max 6 words) and narration (2 to 4 sentences).
- Return ONLY valid JSON with keys: "title" (max 70 characters), "description" (two short paragraphs), "tags" (list of 8 strings), "slides" (list of objects with "heading" and "narration").
"""
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.0-flash:generateContent?key={key}")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    r = requests.post(url, json=body, timeout=180)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    text = re.sub(r"^```(json)?|```$", "", text).strip()
    data = json.loads(text)
    data["slides"].append({
        "heading": "Disclaimer",
        "narration": "This video is for general education only. It is not financial advice. "
                     "Speak with a qualified advisor before making financial decisions."})
    return data


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


async def make_voice(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))


def audio_length(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.decode().strip())


def make_clip(img, audio, out):
    dur = audio_length(audio) + 0.5
    subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-i", str(audio),
                    "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac",
                    "-b:a", "160k", "-pix_fmt", "yuv420p",
                    "-t", f"{dur:.2f}", "-shortest", str(out)], check=True)


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


def main():
    topic = pick_topic()
    print("Topic:", topic)
    data = gemini_script(topic)

    slides_dir = WORK / "slides"
    slides_dir.mkdir(exist_ok=True)
    clips = []
    for i, s in enumerate(data["slides"]):
        img = slides_dir / f"{i:02d}.png"
        aud = slides_dir / f"{i:02d}.mp3"
        clip = slides_dir / f"{i:02d}.mp4"
        make_slide_image(s["heading"], s["narration"], img)
        asyncio.run(make_voice(s["narration"], aud))
        make_clip(img, aud, clip)
        clips.append(clip)

    lst = WORK / "list.txt"
    lst.write_text("\n".join(f"file '{c.resolve()}'" for c in clips), encoding="utf-8")
    final = WORK / "final.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(final)], check=True)

    description = data["description"] + "\n\nFor general education only. Not financial advice."
    upload(final, data["title"][:100], description, data["tags"])


if __name__ == "__main__":
    main()

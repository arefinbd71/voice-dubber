"""ডাবিংয়ের মূল লজিক (Streamlit ছাড়া, আলাদা করে টেস্ট করা যায়)।

ধারণা: প্রতিটি বক্তব্য-অংশ (segment) মূল অডিওর ঠিক যে সেকেন্ডে শুরু হয়েছিল,
বাংলা অডিও সেই সেকেন্ডেই শুরু হয়। বাংলা ক্লিপ মূল অংশের দৈর্ঘ্যে মেলাতে
গতি সামান্য কমানো/বাড়ানো হয়। ফাঁকা জায়গা (বিরতি) নীরবই থাকে, তাই মোট
দৈর্ঘ্য ও বিরতির অবস্থান মূলের মতোই থাকে।
"""
import json
import shutil
import subprocess
import time
import wave

import requests

SR = 44100
EL_BASE = "https://api.elevenlabs.io/v1"
ENDERS = tuple("।.!?؟۔…")
BN_CHARS_PER_SEC = 13  # বাংলা বলার গড় গতি (আনুমানিক), অনুবাদের দৈর্ঘ্য-নির্দেশনায় ব্যবহৃত

# ক্লিপের শুরু ও শেষের নীরবতা ছেঁটে ফেলে, যাতে সময় মেলানো নিখুঁত হয়
_TRIM = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,"
         "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse")


# ------------------------------------------------------------ ffmpeg
def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(cmd, data=None) -> bytes:
    p = subprocess.run(cmd, input=data, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg ত্রুটি: " + p.stderr.decode("utf-8", "ignore")[-400:])
    return p.stdout


def probe_duration(path: str) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", path])
    return float(out.decode().strip())


def extract_audio(src: str, out_mp3: str) -> None:
    _run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", str(SR),
          "-c:a", "libmp3lame", "-b:a", "96k", out_mp3])


def cut_sample(src: str, out_mp3: str, start: float = 0.0, length: float = 150.0) -> None:
    """ভয়েস ক্লোনের জন্য ছোট, হালকা স্যাম্পল (মনো, ~১৫০ সেকেন্ড পর্যন্ত)।"""
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(start, 0):.2f}", "-t", f"{length:.2f}",
          "-i", src, "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "libmp3lame",
          "-b:a", "96k", out_mp3])


# ------------------------------------------------------------ ElevenLabs
def _el(key: str) -> dict:
    return {"xi-api-key": key}


def transcribe(key: str, path: str):
    """ওয়ার্ড-লেভেল টাইমস্ট্যাম্পসহ ট্রান্সক্রিপশন। ফেরত: (words, language, text)"""
    with open(path, "rb") as f:
        r = requests.post(
            f"{EL_BASE}/speech-to-text",
            headers=_el(key),
            data={"model_id": "scribe_v1", "timestamps_granularity": "word",
                  "tag_audio_events": "false", "diarize": "false"},
            files={"file": ("audio.mp3", f, "audio/mpeg")},
            timeout=1800,
        )
    r.raise_for_status()
    j = r.json()
    return j.get("words", []), j.get("language_code", "unknown"), j.get("text", "")


def clone_voice(key: str, sample_path: str, label: str = "dub") -> str:
    with open(sample_path, "rb") as f:
        r = requests.post(
            f"{EL_BASE}/voices/add",
            headers=_el(key),
            data={"name": f"dub-{label}-{int(time.time())}"[:60],
                  "remove_background_noise": "true"},
            files=[("files", ("sample.mp3", f, "audio/mpeg"))],
            timeout=600,
        )
    r.raise_for_status()
    return r.json()["voice_id"]


def delete_voice(key: str, voice_id: str) -> None:
    try:
        requests.delete(f"{EL_BASE}/voices/{voice_id}", headers=_el(key), timeout=60)
    except Exception:
        pass


def tts(key: str, voice_id: str, text: str, stability: float, model: str = "eleven_v3") -> bytes:
    r = None
    for attempt in range(5):
        r = requests.post(
            f"{EL_BASE}/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={**_el(key), "Content-Type": "application/json"},
            json={"text": text, "model_id": model,
                  "voice_settings": {"stability": stability, "similarity_boost": 0.85}},
            timeout=600,
        )
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.content
    r.raise_for_status()
    return r.content


# ------------------------------------------------------------ সেগমেন্টিং
def build_segments(words, pause_thr: float = 0.5, max_seg: float = 20.0):
    """শব্দের মাঝের ফাঁক >= pause_thr হলে নতুন অংশ। বিরতিগুলো তাই অংশের সীমানা হয়ে থাকে।"""
    ws = [w for w in words
          if w.get("type", "word") == "word" and str(w.get("text", "")).strip()]
    segs, cur = [], None
    for w in ws:
        s, e, t = float(w["start"]), float(w["end"]), str(w["text"]).strip()
        if cur is None:
            cur = {"start": s, "end": e, "src": t}
            continue
        gap = s - cur["end"]
        sentence_end = cur["src"].endswith(ENDERS)
        too_long = (e - cur["start"]) > max_seg and sentence_end
        if gap >= pause_thr or too_long or (sentence_end and gap >= pause_thr * 0.6):
            segs.append(cur)
            cur = {"start": s, "end": e, "src": t}
        else:
            cur["src"] += " " + t
            cur["end"] = e
    if cur:
        segs.append(cur)
    return segs


# ------------------------------------------------------------ অনুবাদ
def _gemini_json(key: str, model: str, prompt: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r = requests.post(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.3,
                                   "responseMimeType": "application/json",
                                   "responseSchema": {"type": "ARRAY",
                                                      "items": {"type": "STRING"}}}},
        timeout=300,
    )
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return json.loads("".join(p.get("text", "") for p in parts))


def translate_segments(key: str, segs, tone: str, model: str = "gemini-2.5-flash",
                       batch: int = 30):
    """প্রতিটি অংশের বাংলা অনুবাদ, মূল অংশের সময়ের কাছাকাছি দৈর্ঘ্যে।"""
    out = []
    for i in range(0, len(segs), batch):
        chunk = segs[i:i + batch]
        items = [{"text": s["src"],
                  "seconds": round(s["end"] - s["start"], 2),
                  "max_chars": max(8, int((s["end"] - s["start"]) * BN_CHARS_PER_SEC))}
                 for s in chunk]
        prompt = (
            "তুমি ভিডিও ডাবিংয়ের পেশাদার অনুবাদক। নিচের JSON তালিকার প্রতিটি আইটেম একটি "
            "ধারাবাহিক বক্তৃতার একেকটি অংশ। প্রতিটির স্বাভাবিক, শুদ্ধ, বলার উপযোগী বাংলা "
            "অনুবাদ দাও। অনুবাদটি জোরে পড়তে প্রায় 'seconds' সেকেন্ড লাগা উচিত, তাই "
            "'max_chars' অক্ষরের মধ্যে রাখার চেষ্টা করো, তবে মূল অর্থ বাদ দিও না। "
            f"স্টাইল: {tone}। কোনো ব্যাখ্যা বা মন্তব্য নয়। আউটপুট হবে ঠিক {len(chunk)}টি "
            "স্ট্রিংয়ের একটি JSON অ্যারে, একই ক্রমে।\n\n"
            + json.dumps(items, ensure_ascii=False)
        )
        res = _gemini_json(key, model, prompt)
        if len(res) != len(chunk):  # সংখ্যা না মিললে একটা একটা করে
            res = []
            for it in chunk:
                one = _gemini_json(key, model, prompt.split("\n\n")[0].replace(
                    f"ঠিক {len(chunk)}টি", "ঠিক ১টি") + "\n\n" + json.dumps(
                    [{"text": it["src"], "seconds": round(it["end"] - it["start"], 2),
                      "max_chars": max(8, int((it["end"] - it["start"]) * BN_CHARS_PER_SEC))}],
                    ensure_ascii=False))
                res.append(one[0] if one else "")
        out.extend(str(x).strip() for x in res)
    return out


# ------------------------------------------------------------ সময় মেলানো
def decode_pcm(mp3: bytes, tempo: float = 1.0) -> bytes:
    filt = _TRIM
    if abs(tempo - 1.0) > 0.005:
        filt += f",atempo={tempo:.4f}"
    return _run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-af", filt, "-ac", "1",
                 "-ar", str(SR), "-f", "s16le", "pipe:1"], data=mp3)


def fit_segment(mp3: bytes, start: float, end: float, next_start, total: float,
                min_tempo: float = 0.85, max_tempo: float = 1.35, hard_max: float = 1.9):
    """বাংলা ক্লিপকে মূল অংশের [start, end] স্লটে বসানো।

    - স্বাভাবিক ক্ষেত্রে গতি min_tempo..max_tempo এর মধ্যে বদলে স্লটের দৈর্ঘ্যে আনা হয়।
    - তাতেও পরের অংশের শুরু ছাড়িয়ে গেলে hard_max পর্যন্ত গতি বাড়ানো হয়।
    - তবুও না মিললে ক্লিপ ছেঁটে দেওয়া হয় (cut=True), যাতে পরের বিরতি/অংশ সরে না যায়।
    """
    pcm = decode_pcm(mp3)
    dur = len(pcm) / 2 / SR
    if dur <= 0:
        return b"", {"tts": 0.0, "slot": round(end - start, 2), "tempo": 1.0,
                     "final": 0.0, "cut": False}
    target = max(end - start, 0.2)
    avail = (next_start - start - 0.08) if next_start is not None else (total - start)
    avail = max(avail, 0.2)
    tempo = min(max(dur / target, min_tempo), max_tempo)
    if dur / tempo > avail:
        tempo = min(dur / avail, hard_max)
    if abs(tempo - 1.0) > 0.005:
        pcm = decode_pcm(mp3, tempo)
    new_dur = len(pcm) / 2 / SR
    cut = new_dur > avail + 0.02
    if cut:
        pcm = pcm[: int(avail * SR) * 2]
        new_dur = avail
    return pcm, {"tts": round(dur, 2), "slot": round(target, 2), "tempo": round(tempo, 2),
                 "final": round(new_dur, 2), "cut": cut}


def build_timeline(total: float, placed) -> bytes:
    """মোট দৈর্ঘ্যের নীরব ট্র্যাকে প্রতিটি ক্লিপ তার মূল শুরুর সেকেন্ডে বসানো।"""
    buf = bytearray(int(round(total * SR)) * 2)
    for start, pcm in placed:
        off = int(round(start * SR)) * 2
        end = min(off + len(pcm), len(buf))
        if end > off:
            buf[off:end] = pcm[: end - off]
    return bytes(buf)


def write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def wav_to_mp3(wav: str, out_mp3: str) -> None:
    _run(["ffmpeg", "-y", "-v", "error", "-i", wav, "-c:a", "libmp3lame", "-b:a", "192k", out_mp3])


def mux_video(video: str, wav: str, out_mp4: str, total: float) -> None:
    """মূল ভিডিও (ছবি অপরিবর্তিত) + নতুন বাংলা অডিও। দৈর্ঘ্য মূলের সমান।"""
    base = ["ffmpeg", "-y", "-v", "error", "-i", video, "-i", wav,
            "-map", "0:v:0", "-map", "1:a:0"]
    tail = ["-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}",
            "-movflags", "+faststart", out_mp4]
    try:
        _run(base + ["-c:v", "copy"] + tail)
    except RuntimeError:
        _run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                     "-pix_fmt", "yuv420p"] + tail)

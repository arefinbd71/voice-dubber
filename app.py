"""Voice Dubber BN - অডিও/ভিডিওর ন্যারেটর ভয়েস একই ভয়েসে বাংলায়।

ধাপ: অডিও বের করা -> ওয়ার্ড-টাইমস্ট্যাম্পসহ ট্রান্সক্রিপশন -> বিরতি ধরে অংশ ভাগ ->
বাংলা অনুবাদ -> ভয়েস ক্লোন -> প্রতি অংশের বাংলা অডিও -> মূল সময়ে বসানো (বিরতি ও
মোট দৈর্ঘ্য অক্ষুণ্ণ) -> MP3 / MP4।
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

import dubbing as d

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TTS_MODEL = os.getenv("TTS_MODEL", "eleven_v3")


def secret(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name, default)


def check_password() -> bool:
    pw = secret("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return True
    entered = st.text_input("টিম পাসওয়ার্ড", type="password")
    if entered:
        if entered == pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("পাসওয়ার্ড ভুল")
    return False


def fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:05.2f}"


def show_error(e: Exception) -> None:
    detail = ""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        detail = f"\n\n{e.response.status_code}: {e.response.text[:400]}"
    st.error(f"সমস্যা হয়েছে: {e}{detail}")


# ---------------------------------------------------------------- UI
st.set_page_config(page_title="Voice Dubber BN", page_icon="🎙️", layout="wide")
st.title("🎙️ Voice Dubber BN")
st.caption("অডিও বা ভিডিওর ন্যারেটর ভয়েস → একই ভয়েসে বাংলা, একই দৈর্ঘ্য, বিরতি একই জায়গায়")

if not check_password():
    st.stop()

if not d.have_ffmpeg():
    st.error("ffmpeg পাওয়া যায়নি। Streamlit Cloud/Hugging Face-এ `packages.txt` ফাইলে "
             "`ffmpeg` লেখা থাকতে হবে (প্যাকেজের সঙ্গে দেওয়া আছে)।")
    st.stop()

S = st.session_state
S.setdefault("gen", 0)

with st.sidebar:
    st.header("সেটিংস")
    el_key = secret("ELEVENLABS_API_KEY") or st.text_input("ElevenLabs API Key", type="password")
    gem_key = secret("GEMINI_API_KEY") or st.text_input("Gemini API Key", type="password")
    tone = st.selectbox("অনুবাদের স্টাইল",
                        ["সংবাদ/ডকুমেন্টারি ন্যারেশন", "সহজ কথ্য বাংলা", "আনুষ্ঠানিক"])
    pause_thr = st.slider("কত সেকেন্ডের ফাঁককে 'বিরতি' ধরব", 0.3, 1.5, 0.5, 0.1,
                          help="এর চেয়ে বড় ফাঁকে নতুন অংশ শুরু হয়। বিরতির অবস্থান ও দৈর্ঘ্য "
                               "বাংলা ডাবেও একই থাকে।")
    max_tempo = st.slider("সর্বোচ্চ গতি বাড়ানো", 1.0, 1.6, 1.35, 0.05,
                          help="বাংলা অনুবাদ লম্বা হলে এই সীমা পর্যন্ত দ্রুত পড়ে স্লটে বসানো হয়।")
    stability = st.slider("ভয়েস স্টেবিলিটি", 0.0, 1.0, 0.5, 0.5,
                          help="কম = বেশি আবেগ, বেশি = স্থির ও একরকম")
    keep_voice = st.checkbox("ক্লোন ভয়েস ElevenLabs-এ রেখে দাও", value=False,
                             help="বন্ধ থাকলে কাজ শেষে মুছে যায় (ভয়েস-স্লট বাঁচে)")

if not el_key or not gem_key:
    st.info("বাঁদিকের সাইডবারে দুটি API Key দিন (অথবা secrets-এ সেট করুন)।")
    st.stop()

up = st.file_uploader("অডিও বা ভিডিও আপলোড করুন",
                      type=["mp3", "wav", "m4a", "ogg", "flac", "aac",
                            "mp4", "mov", "mkv", "webm", "avi", "m4v"])
sample_up = st.file_uploader("ভয়েস স্যাম্পল (ঐচ্ছিক)",
                             type=["mp3", "wav", "m4a", "ogg", "flac", "aac"],
                             help="খালি রাখলে মূল ফাইল থেকেই ভয়েস ক্লোন হবে। ব্যাকগ্রাউন্ড "
                                  "মিউজিক ছাড়া, একজন বক্তার ১–৫ মিনিটের পরিষ্কার অডিও সেরা।")

# ---------------------------------------------------------------- ধাপ ১
if up and st.button("১) বিশ্লেষণ ও বাংলা অনুবাদ", type="primary"):
    try:
        work = tempfile.mkdtemp(prefix="dub_")
        ext = os.path.splitext(up.name)[1].lower() or ".mp4"
        src = os.path.join(work, "input" + ext)
        with open(src, "wb") as f:
            f.write(up.getbuffer())
        audio = os.path.join(work, "audio.mp3")
        with st.spinner("অডিও আলাদা করা হচ্ছে…"):
            d.extract_audio(src, audio)
            total = d.probe_duration(src)
        with st.spinner("ট্রান্সক্রিপশন চলছে (সময় লাগতে পারে)…"):
            words, lang, _ = d.transcribe(el_key, audio)
        segs = d.build_segments(words, pause_thr)
        if not segs:
            st.error("কোনো কথা শনাক্ত হয়নি।")
        else:
            with st.spinner(f"{len(segs)}টি অংশ বাংলায় অনুবাদ হচ্ছে…"):
                bn = d.translate_segments(gem_key, segs, tone, GEMINI_MODEL)
            for s, b in zip(segs, bn):
                s["bn"] = b
            S.update(segs=segs, work=work, src=src, audio=audio, total=total, lang=lang,
                     is_video=ext in VIDEO_EXT, gen=S["gen"] + 1, result=None)
    except Exception as e:
        show_error(e)

# ---------------------------------------------------------------- অনুবাদ এডিট
if S.get("segs"):
    segs = S["segs"]
    st.success(f"ভাষা: {S['lang']} · মোট দৈর্ঘ্য {fmt(S['total'])} · {len(segs)}টি অংশ")
    st.subheader("বাংলা অনুবাদ (প্রয়োজনে এডিট করুন)")
    st.caption("অংশ বড় হয়ে গেলে ডাবিংয়ে গতি বাড়াতে হয়। তাই বাংলা টেক্সট মূলের চেয়ে অনেক "
               "লম্বা হলে ছোট করে দিন।")
    df = pd.DataFrame([{"#": i + 1, "শুরু": fmt(s["start"]), "শেষ": fmt(s["end"]),
                        "মূল টেক্সট": s["src"], "বাংলা": s["bn"]}
                       for i, s in enumerate(segs)])
    edited = st.data_editor(df, hide_index=True, key=f"editor_{S['gen']}",
                            disabled=["#", "শুরু", "শেষ", "মূল টেক্সট"])
    for i, row in edited.iterrows():
        segs[i]["bn"] = str(row["বাংলা"] or "")

    # ------------------------------------------------------------ ধাপ ২
    if st.button("২) বাংলা ডাব তৈরি করো", type="primary"):
        vid = None
        try:
            work, total = S["work"], S["total"]
            jobs = [i for i, s in enumerate(segs) if s["bn"].strip()]

            with st.spinner("ভয়েস ক্লোন হচ্ছে…"):
                sample_src = S["audio"]
                if sample_up:
                    sample_src = os.path.join(work, "sample_in" +
                                              os.path.splitext(sample_up.name)[1])
                    with open(sample_src, "wb") as f:
                        f.write(sample_up.getbuffer())
                sample = os.path.join(work, "sample.mp3")
                d.cut_sample(sample_src, sample,
                             0.0 if sample_up else segs[0]["start"])
                vid = d.clone_voice(el_key, sample)

            bar = st.progress(0.0, text="বাংলা ভয়েস তৈরি হচ্ছে…")
            clips = {}
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = {ex.submit(d.tts, el_key, vid, segs[i]["bn"].strip(),
                                  stability, TTS_MODEL): i for i in jobs}
                for n, fu in enumerate(as_completed(futs), 1):
                    clips[futs[fu]] = fu.result()
                    bar.progress(n / len(jobs), text=f"ভয়েস তৈরি হচ্ছে… {n}/{len(jobs)}")

            bar.progress(1.0, text="মূল সময়ে বসানো হচ্ছে…")
            placed, report = [], []
            for i in jobs:
                nxt = segs[i + 1]["start"] if i + 1 < len(segs) else None
                pcm, info = d.fit_segment(clips[i], segs[i]["start"], segs[i]["end"],
                                          nxt, total, max_tempo=max_tempo)
                placed.append((segs[i]["start"], pcm))
                report.append({"#": i + 1, "মূল স্লট (সে.)": info["slot"],
                               "বাংলা (সে.)": info["tts"], "গতি": info["tempo"],
                               "ফাইনাল (সে.)": info["final"],
                               "ছাঁটা হয়েছে": "হ্যাঁ" if info["cut"] else ""})

            wav = os.path.join(work, "dub.wav")
            d.write_wav(wav, d.build_timeline(total, placed))
            out_mp3 = os.path.join(work, "bangla_dub.mp3")
            d.wav_to_mp3(wav, out_mp3)
            out_mp4 = None
            if S["is_video"]:
                bar.progress(1.0, text="ভিডিওতে অডিও বসানো হচ্ছে…")
                out_mp4 = os.path.join(work, "bangla_dub.mp4")
                d.mux_video(S["src"], wav, out_mp4, total)
            bar.empty()
            S["result"] = {"mp3": out_mp3, "mp4": out_mp4, "report": report,
                           "dur_in": total, "dur_out": d.probe_duration(out_mp4 or out_mp3)}
        except Exception as e:
            show_error(e)
        finally:
            if vid and not keep_voice:
                d.delete_voice(el_key, vid)

# ---------------------------------------------------------------- ফলাফল
res = S.get("result")
if res:
    st.success(f"তৈরি! মূল দৈর্ঘ্য {res['dur_in']:.2f} সে. · ডাব {res['dur_out']:.2f} সে.")
    if res["mp4"]:
        st.video(res["mp4"])
        with open(res["mp4"], "rb") as f:
            st.download_button("⬇️ বাংলা ভিডিও (MP4)", f.read(), "bangla_dub.mp4", "video/mp4")
    st.audio(res["mp3"])
    with open(res["mp3"], "rb") as f:
        st.download_button("⬇️ বাংলা অডিও (MP3)", f.read(), "bangla_dub.mp3", "audio/mpeg")

    cut = [r for r in res["report"] if r["ছাঁটা হয়েছে"]]
    fast = [r for r in res["report"] if r["গতি"] > 1.3 and not r["ছাঁটা হয়েছে"]]
    if cut:
        st.warning(f"{len(cut)}টি অংশের বাংলা টেক্সট এত লম্বা যে শেষটুকু ছাঁটা পড়েছে। ওই "
                   "অংশগুলোর বাংলা ছোট করে আবার ডাব করুন: " + ", ".join(str(r["#"]) for r in cut))
    if fast:
        st.info(f"{len(fast)}টি অংশ বেশ দ্রুত পড়া হয়েছে: " + ", ".join(str(r["#"]) for r in fast))
    with st.expander("অংশ-ভিত্তিক সময়ের রিপোর্ট"):
        st.dataframe(pd.DataFrame(res["report"]), hide_index=True)

import os
import threading
import smtplib
import json
import re
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, request
from faster_whisper import WhisperModel
from twilio.twiml.voice_response import VoiceResponse

load_dotenv()
app = Flask(__name__)

DATA_DIR = Path(os.getenv("VOICEMAIL_DIR", "./voicemails")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

INBOX_DIR = Path.home() / ".simple_voicemail_transcriber"
INBOX_DIR.mkdir(parents=True, exist_ok=True)
INBOX_DB = INBOX_DIR / "messages.json"
INBOX_LOCK = threading.Lock()

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "voicemail@localhost")
LOCAL_SMTP_HOST = os.getenv("LOCAL_SMTP_HOST", "127.0.0.1")
LOCAL_SMTP_PORT = int(os.getenv("LOCAL_SMTP_PORT", "25"))

active_calls = {}
active_calls_lock = threading.Lock()

@app.route("/voice", methods=["GET", "POST"])
def voice():
    call_sid = request.form.get("CallSid", "")
    caller = request.form.get("From", "Unknown caller")
    called = request.form.get("To", "")
    if call_sid:
        with active_calls_lock:
            active_calls[call_sid] = {
                "caller": caller,
                "called": called,
                "started": datetime.now().isoformat(),
            }
    print(f"Incoming call: {caller} -> {called} ({call_sid})")
    response = VoiceResponse()
    response.say(
        "Thank you for calling McCombs Supply. Please leave your name, phone number, "
        "and message after the beep. Please include the part number and order number if possible. When you are finished, you may hang up."
    )
    response.record(
        action="/finished",
        method="POST",
        max_length=180,
        play_beep=True,
        trim="trim-silence",
        recording_status_callback="/recording-ready",
        recording_status_callback_method="POST",
        recording_status_callback_event="completed",
    )
    response.say("Thank you. Goodbye.")
    response.hangup()
    return str(response), 200, {"Content-Type": "text/xml"}

@app.route("/finished", methods=["GET", "POST"])
def finished():
    call_sid = request.form.get("CallSid", "")
    caller = request.form.get("From", "")
    if call_sid and caller:
        with active_calls_lock:
            active_calls.setdefault(call_sid, {})["caller"] = caller
    response = VoiceResponse()
    response.say("Thank you. Your message has been recorded. Goodbye.")
    response.hangup()
    return str(response), 200, {"Content-Type": "text/xml"}

@app.route("/recording-ready", methods=["POST"])
def recording_ready():
    status = request.form.get("RecordingStatus", "")
    recording_url = request.form.get("RecordingUrl", "")
    recording_sid = request.form.get("RecordingSid", "")
    call_sid = request.form.get("CallSid", "")
    caller = request.form.get("From", "")
    if not caller and call_sid:
        with active_calls_lock:
            caller = active_calls.get(call_sid, {}).get("caller", "")
    caller = caller or "Unknown caller"
    print(f"Recording callback: status={status} recording={recording_sid} call={call_sid} caller={caller}")
    if status != "completed":
        return ("OK", 200)
    if not recording_url:
        return ("Missing RecordingUrl", 400)
    threading.Thread(
        target=process_voicemail,
        args=(recording_url, recording_sid, call_sid, caller),
        daemon=True,
    ).start()
    return ("OK", 200)

def process_voicemail(recording_url, recording_sid, call_sid, caller):
    try:
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
        created = now.strftime("%Y-%m-%d %H:%M:%S")
        base = f"voicemail_{stamp}_{recording_sid or call_sid}"
        wav_path = DATA_DIR / f"{base}.wav"
        txt_path = DATA_DIR / f"{base}.txt"
        json_path = DATA_DIR / f"{base}.json"

        wav_url = recording_url if recording_url.lower().endswith(".wav") else recording_url + ".wav"
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

        print(f"Downloading recording: {wav_url}")
        r = requests.get(wav_url, auth=auth, timeout=60)
        r.raise_for_status()
        wav_path.write_bytes(r.content)
        print(f"Saved WAV: {wav_path}")

        print("Transcribing on CPU...")
        segments, info = whisper_model.transcribe(
            str(wav_path),
            beam_size=5,
            vad_filter=True,
            language="en",
        )
        lines = [s.text.strip() for s in segments if s.text.strip()]
        transcript = "\n".join(lines).strip() or "[No speech was detected.]"
        language = getattr(info, "language", "unknown")
        txt_path.write_text(transcript + "\n", encoding="utf-8")
        print(f"Saved transcript: {txt_path}")

        print(f"Analyzing transcript with {OLLAMA_MODEL}...")
        analysis = analyze_with_qwen(transcript, caller)
        json_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
        print(f"Saved AI analysis: {json_path}")

        add_to_inbox({
            "id": recording_sid or f"{stamp}_{call_sid}",
            "created": created,
            "caller": caller,
            "caller_name": analysis.get("caller_name"),
            "callback_number": analysis.get("callback_number"),
            "category": analysis.get("category"),
            "priority": analysis.get("priority"),
            "order_number": analysis.get("order_number"),
            "part_number": analysis.get("part_number"),
            "action_required": analysis.get("action_required"),
            "summary": analysis.get("summary"),
            "call_sid": call_sid,
            "recording_sid": recording_sid,
            "audio_path": str(wav_path),
            "transcript_path": str(txt_path),
            "analysis_path": str(json_path),
            "filename": wav_path.name,
            "transcript": transcript,
            "language": language,
            "status": "Analyzed",
        })

        if EMAIL_TO:
            send_email(caller, transcript, analysis, wav_path, recording_sid)
        else:
            print("EMAIL_TO is not configured. Email skipped.")
    except Exception as exc:
        print(f"ERROR processing voicemail: {exc}")
    finally:
        if call_sid:
            with active_calls_lock:
                active_calls.pop(call_sid, None)

def analyze_with_qwen(transcript, caller_id):
    fallback = {
        "caller_name": None,
        "callback_number": caller_id if caller_id and caller_id != "Unknown caller" else None,
        "category": "Other",
        "priority": "normal",
        "order_number": None,
        "part_number": None,
        "action_required": None,
        "summary": transcript[:300] if transcript else None,
    }

    system_prompt = """You process voicemail transcripts for a small business.
Extract only information supported by the transcript or supplied caller ID.
Never invent names, numbers, order numbers, part numbers, urgency, or facts.

Return ONLY valid JSON with exactly these keys:
{
  "caller_name": string or null,
  "callback_number": string or null,
  "category": string,
  "priority": "low" | "normal" | "high" | "urgent",
  "order_number": string or null,
  "part_number": string or null,
  "action_required": string or null,
  "summary": string
}

Use a short useful category such as New order, Existing order, Return, Wrong item,
Damaged item, Product question, Billing, Callback request, Spam, or Other.
If the caller states a callback number, prefer that. Otherwise use caller ID if valid.
Do not infer order or part numbers from unrelated numbers.
Use normal priority unless urgency is clearly stated.
Keep the summary to 1-3 concise sentences.
No markdown or extra commentary."""

    user_prompt = f"Caller ID:\n{caller_id}\n\nVoicemail transcript:\n{transcript}"

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0.1},
    }

    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "").strip()
        if not content:
            raise RuntimeError("Ollama returned an empty response.")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if not match:
                raise
            parsed = json.loads(match.group(0))

        result = fallback.copy()
        for key in result:
            if key in parsed:
                result[key] = parsed[key]

        if result["priority"] not in {"low", "normal", "high", "urgent"}:
            result["priority"] = "normal"
        if not isinstance(result["category"], str) or not result["category"].strip():
            result["category"] = "Other"
        if not isinstance(result["summary"], str) or not result["summary"].strip():
            result["summary"] = fallback["summary"]
        return result
    except Exception as exc:
        print(f"WARNING: Qwen analysis failed: {exc}")
        return fallback

def add_to_inbox(message):
    with INBOX_LOCK:
        messages = []
        if INBOX_DB.exists():
            try:
                messages = json.loads(INBOX_DB.read_text(encoding="utf-8"))
            except Exception:
                messages = []
        messages = [m for m in messages if m.get("id") != message["id"]]
        messages.insert(0, message)
        temp = INBOX_DB.with_suffix(".tmp")
        temp.write_text(json.dumps(messages, indent=2), encoding="utf-8")
        temp.replace(INBOX_DB)

def clean(value):
    return "Not provided" if value is None or (isinstance(value, str) and not value.strip()) else str(value)

def send_email(caller, transcript, analysis, wav_path, recording_sid):
    msg = EmailMessage()
    subject_identity = analysis.get("caller_name") or caller
    msg["Subject"] = f"New voicemail - {analysis.get('category', 'Other')} - {subject_identity}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.set_content(f"""New voicemail

AI SUMMARY
----------
Caller ID: {caller}
Caller name: {clean(analysis.get("caller_name"))}
Callback number: {clean(analysis.get("callback_number"))}
Category: {clean(analysis.get("category"))}
Priority: {clean(analysis.get("priority"))}
Order number: {clean(analysis.get("order_number"))}
Part number: {clean(analysis.get("part_number"))}
Action required: {clean(analysis.get("action_required"))}

Summary:
{clean(analysis.get("summary"))}

RAW TRANSCRIPT
--------------
{transcript}

Recording SID: {recording_sid}

The original WAV recording is attached.
""")

    msg.add_attachment(
        wav_path.read_bytes(),
        maintype="audio",
        subtype="wav",
        filename=wav_path.name,
    )

    with smtplib.SMTP(LOCAL_SMTP_HOST, LOCAL_SMTP_PORT, timeout=30) as smtp:
        smtp.send_message(msg)

    print(f"Email submitted to Postfix for {EMAIL_TO}")

if __name__ == "__main__":
    print(f"Voicemail directory: {DATA_DIR}")
    print(f"GUI inbox database: {INBOX_DB}")
    print(f"Whisper model: {WHISPER_MODEL} (CPU / INT8)")
    print(f"Ollama model: {OLLAMA_MODEL}")
    print(f"Ollama URL: {OLLAMA_URL}")
    print(f"Email destination: {EMAIL_TO or '[not configured]'}")
    app.run(host="0.0.0.0", port=5000, threaded=True)

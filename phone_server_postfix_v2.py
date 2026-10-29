import os
import threading
import smtplib
import json
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

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "voicemail@localhost")

LOCAL_SMTP_HOST = os.getenv("LOCAL_SMTP_HOST", "127.0.0.1")
LOCAL_SMTP_PORT = int(os.getenv("LOCAL_SMTP_PORT", "25"))

# CallSid -> caller metadata
active_calls = {}
active_calls_lock = threading.Lock()

# CPU only
whisper_model = WhisperModel(
    WHISPER_MODEL,
    device="cpu",
    compute_type="int8",
)


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
        "You're up. "
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
            entry = active_calls.setdefault(call_sid, {})
            entry["caller"] = caller

    response = VoiceResponse()
    response.say("Thank you. Your message has been recorded. Goodbye.")
    response.hangup()
    return str(response), 200, {"Content-Type": "text/xml"}


@app.route("/recording-ready", methods=["POST"])
def recording_ready():
    recording_status = request.form.get("RecordingStatus", "")
    recording_url = request.form.get("RecordingUrl", "")
    recording_sid = request.form.get("RecordingSid", "")
    call_sid = request.form.get("CallSid", "")

    caller = request.form.get("From", "")
    if not caller and call_sid:
        with active_calls_lock:
            caller = active_calls.get(call_sid, {}).get("caller", "")

    caller = caller or "Unknown caller"

    print(
        f"Recording callback: status={recording_status} "
        f"recording={recording_sid} call={call_sid} caller={caller}"
    )

    if recording_status != "completed":
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

        wav_url = recording_url
        if not wav_url.lower().endswith(".wav"):
            wav_url += ".wav"

        auth = None
        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

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

        lines = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                lines.append(text)

        transcript = "\n".join(lines).strip() or "[No speech was detected.]"
        language = getattr(info, "language", "unknown")

        txt_path.write_text(transcript + "\n", encoding="utf-8")

        add_to_inbox(
            {
                "id": recording_sid or f"{stamp}_{call_sid}",
                "created": created,
                "caller": caller,
                "call_sid": call_sid,
                "recording_sid": recording_sid,
                "audio_path": str(wav_path),
                "transcript_path": str(txt_path),
                "filename": wav_path.name,
                "transcript": transcript,
                "language": language,
                "status": "Transcribed",
            }
        )

        print(f"Added to GUI inbox: {caller}")
        print("Transcript:")
        print(transcript)

        if EMAIL_TO:
            send_email(caller, transcript, wav_path, recording_sid)
        else:
            print("EMAIL_TO is not configured. Email skipped.")

    except Exception as exc:
        print(f"ERROR processing voicemail: {exc}")
    finally:
        if call_sid:
            with active_calls_lock:
                active_calls.pop(call_sid, None)


def add_to_inbox(message):
    with INBOX_LOCK:
        messages = []

        if INBOX_DB.exists():
            try:
                messages = json.loads(INBOX_DB.read_text(encoding="utf-8"))
            except Exception:
                messages = []

        # Prevent duplicates if Twilio retries a callback.
        messages = [m for m in messages if m.get("id") != message["id"]]
        messages.insert(0, message)

        temp_file = INBOX_DB.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps(messages, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(INBOX_DB)


def send_email(caller, transcript, wav_path, recording_sid):
    msg = EmailMessage()
    msg["Subject"] = f"New voicemail from {caller}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.set_content(
        f"""New voicemail

Caller ID: {caller}
Recording SID: {recording_sid}

Transcript:

{transcript}

The original WAV recording is attached.
"""
    )

    msg.add_attachment(
        wav_path.read_bytes(),
        maintype="audio",
        subtype="wav",
        filename=wav_path.name,
    )

    with smtplib.SMTP(
        LOCAL_SMTP_HOST,
        LOCAL_SMTP_PORT,
        timeout=30,
    ) as smtp:
        smtp.send_message(msg)

    print(f"Email submitted to Postfix for {EMAIL_TO}")


if __name__ == "__main__":
    print(f"Voicemail directory: {DATA_DIR}")
    print(f"GUI inbox database: {INBOX_DB}")
    print(f"Whisper model: {WHISPER_MODEL} (CPU / INT8)")
    print(f"Email destination: {EMAIL_TO or '[not configured]'}")
    app.run(host="0.0.0.0", port=5000, threaded=True)

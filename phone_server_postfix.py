import os
import threading
import smtplib
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

# -----------------------------
# Configuration
# -----------------------------

DATA_DIR = Path(os.getenv("VOICEMAIL_DIR", "./voicemails")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "voicemail@localhost")

# Local Postfix / sendmail-compatible SMTP server
LOCAL_SMTP_HOST = os.getenv("LOCAL_SMTP_HOST", "127.0.0.1")
LOCAL_SMTP_PORT = int(os.getenv("LOCAL_SMTP_PORT", "25"))

# CPU only
whisper_model = WhisperModel(
    WHISPER_MODEL,
    device="cpu",
    compute_type="int8",
)


@app.route("/voice", methods=["GET", "POST"])
def voice():
    response = VoiceResponse()

    response.say(
        "Thank you for calling. "
        "Please leave your name, phone number, and message after the beep. "
        "When you are finished, you may hang up."
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
    caller = request.form.get("From", "Unknown caller")

    print(
        f"Recording callback: status={recording_status} "
        f"recording={recording_sid} call={call_sid}"
    )

    if recording_status != "completed":
        return ("OK", 200)

    if not recording_url:
        print("ERROR: RecordingUrl missing.")
        return ("Missing RecordingUrl", 400)

    threading.Thread(
        target=process_voicemail,
        args=(recording_url, recording_sid, call_sid, caller),
        daemon=True,
    ).start()

    return ("OK", 200)


def process_voicemail(recording_url, recording_sid, call_sid, caller):
    try:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
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

        r = requests.get(
            wav_url,
            auth=auth,
            timeout=60,
        )
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

        transcript_lines = []

        for segment in segments:
            text = segment.text.strip()
            if text:
                transcript_lines.append(text)

        transcript = "\n".join(transcript_lines).strip()

        if not transcript:
            transcript = "[No speech was detected.]"

        txt_path.write_text(
            transcript + "\n",
            encoding="utf-8"
        )

        print(f"Saved transcript: {txt_path}")
        print()
        print("----- TRANSCRIPT -----")
        print(transcript)
        print("----------------------")
        print()

        if EMAIL_TO:
            send_email(
                caller=caller,
                transcript=transcript,
                wav_path=wav_path,
                recording_sid=recording_sid,
            )
        else:
            print("EMAIL_TO is not configured. Email skipped.")

    except Exception as exc:
        print(f"ERROR processing voicemail: {exc}")


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

    print(
        f"Handing email to local mail server "
        f"{LOCAL_SMTP_HOST}:{LOCAL_SMTP_PORT}..."
    )

    with smtplib.SMTP(
        LOCAL_SMTP_HOST,
        LOCAL_SMTP_PORT,
        timeout=30,
    ) as smtp:
        smtp.send_message(msg)

    print(f"Email submitted for delivery to {EMAIL_TO}")


if __name__ == "__main__":
    print(f"Voicemail directory: {DATA_DIR}")
    print(f"Whisper model: {WHISPER_MODEL} (CPU / INT8)")
    print(f"Email destination: {EMAIL_TO or '[not configured]'}")
    print(f"Local mail server: {LOCAL_SMTP_HOST}:{LOCAL_SMTP_PORT}")

    app.run(
        host="0.0.0.0",
        port=5000,
        threaded=True,
    )

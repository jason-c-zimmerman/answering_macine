from faster_whisper import WhisperModel
from pathlib import Path
import sys


# CPU ONLY
model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8"
)


def transcribe(audio_file):
    audio_path = Path(audio_file)

    if not audio_path.exists():
        print(f"Error: File not found: {audio_path}")
        sys.exit(1)

    print(f"Transcribing: {audio_path.name}")

    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True
    )

    transcript = []

    for segment in segments:
        text = segment.text.strip()

        if text:
            print(text)
            transcript.append(text)

    full_transcript = "\n".join(transcript)

    output_path = audio_path.with_suffix(".txt")
    output_path.write_text(full_transcript, encoding="utf-8")

    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python transcribe.py message.wav")
        sys.exit(1)

    transcribe(sys.argv[1])

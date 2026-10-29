from flask import Flask
from twilio.twiml.voice_response import VoiceResponse

app = Flask(__name__)


@app.route("/voice", methods=["GET", "POST"])
def voice():
    response = VoiceResponse()

    response.say(
        "Thank you for calling. "
        "Please leave a message after the beep."
    )

    response.record(
        action="/finished",
        method="POST",
        max_length=120,
        play_beep=True
    )

    return str(response)


@app.route("/finished", methods=["GET", "POST"])
def finished():
    response = VoiceResponse()

    response.say(
        "Thank you. Your message has been recorded. Goodbye."
    )

    response.hangup()

    return str(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

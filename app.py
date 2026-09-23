import os
import itertools
from flask import Flask, request
import requests

app = Flask(__name__)

# Environment variables configured in Vercel
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

# Rotates between 3 public comment reply variations
PUBLIC_REPLIES = itertools.cycle([
    "Naisend ko na sa Messenger inbox mo ang libreng tool link, Ka-OFW! Check your messages or message requests. 📩",
    "Check your inbox, Kabayan! Naisend ko na ang link ng aming libreng Agency Finder tool. ⚡",
    "Naitabi ko na sa Messenger mo! Siguraduhing mag-Legit Check muna bago mag-submit ng requirements. 🛡️"
])

PRIVATE_MESSAGE = (
    "Hello Kabayan! 👋 Narito ang link para sa aming libreng Agency Finder tool:\n\n"
    "https://yourwebsite.com/agency-finder\n\n"
    "Gamitin ito para mag-verify ng DMW-licensed agencies in seconds!"
)

TRIGGER_KEYWORDS = ["free", "libre", "link", "agency", "tool", "nurse", "welder", "factory"]

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    data = request.get_json()

    if data and data.get("object") == "page":
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if change.get("field") == "feed" and value.get("item") == "comment" and value.get("verb") == "add":
                    comment_id = value.get("comment_id")
                    user_message = value.get("message", "").lower()
                    sender_id = value.get("from", {}).get("id")

                    if sender_id == page_id:
                        continue

                    if not TRIGGER_KEYWORDS or any(kw in user_message for kw in TRIGGER_KEYWORDS):
                        send_comment_replies(comment_id)

        return "EVENT_RECEIVED", 200
    return "Not Found", 404

def send_comment_replies(comment_id):
    next_variation = next(PUBLIC_REPLIES)
    requests.post(
        f"https://graph.facebook.com/v20.0/{comment_id}/comments",
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={"message": next_variation}
    )

    requests.post(
        f"https://graph.facebook.com/v20.0/{comment_id}/private_replies",
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={"message": PRIVATE_MESSAGE}
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

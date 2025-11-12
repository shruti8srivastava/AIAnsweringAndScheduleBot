import os, json, re, base64
from datetime import datetime as dt, timedelta
from flask import Flask, request, Response
import pytz

# -------------------- Globals --------------------
_initialized = False
twilio_client = None
db = None
llm = None
calendar_service = None
gmail_service = None
LOCAL_TZ = None
DEFAULT_TZ = None
OWNER_EMAIL = None
BUSINESS_HOURS = (9, 18)
MEETING_DURATION_MIN = 30

app = Flask(__name__)

# -------------------- Health --------------------
@app.route("/")
@app.route("/healthz")
def healthz():
    return "ok", 200


# -------------------- Lazy init --------------------
@app.before_request
def init_once():
    """Load secrets and init clients once per container."""
    global _initialized
    global LOCAL_TZ, twilio_client, db, llm, calendar_service, gmail_service, OWNER_EMAIL
    if _initialized:
        return
    _initialized = True
    print("🔥 Warming up dependencies...")

    from google.cloud import secretmanager, firestore
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from twilio.rest import Client as TwilioClient
    from langchain_openai import ChatOpenAI

    sm = secretmanager.SecretManagerServiceClient()
    proj = os.environ["GOOGLE_CLOUD_PROJECT"]

    def get_secret(name):
        resp = sm.access_secret_version(
            request={"name": f"projects/{proj}/secrets/{name}/versions/latest"}
        )
        return resp.payload.data.decode()

    # Secrets
    OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
    OWNER_EMAIL = get_secret("OWNER_EMAIL")
    DEFAULT_TZ = get_secret("DEFAULT_TZ") or "America/New_York"
    TWILIO_ACCOUNT_SID = get_secret("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = get_secret("TWILIO_AUTH_TOKEN")
    TWILIO_NUMBER = get_secret("TWILIO_NUMBER")
    OAUTH_TOKEN_JSON = json.loads(get_secret("OAUTH_TOKEN_JSON"))
    CLIENT_JSON = json.loads(get_secret("OAUTH_CLIENT_JSON"))

    os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
    LOCAL_TZ = pytz.timezone(DEFAULT_TZ)

    SCOPES = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    creds = Credentials.from_authorized_user_info(
        {
            "token": OAUTH_TOKEN_JSON.get("token"),
            "refresh_token": OAUTH_TOKEN_JSON.get("refresh_token"),
            "token_uri": OAUTH_TOKEN_JSON.get("token_uri"),
            "client_id": OAUTH_TOKEN_JSON.get("client_id"),
            "client_secret": OAUTH_TOKEN_JSON.get("client_secret"),
            "scopes": OAUTH_TOKEN_JSON.get("scopes"),
        },
        scopes=SCOPES,
    )

    calendar_service = build("calendar", "v3", credentials=creds)
    gmail_service = build("gmail", "v1", credentials=creds)
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    db = firestore.Client()
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    print(f"✅ Warm-up complete. OWNER_EMAIL={OWNER_EMAIL}")


# -------------------- Helpers --------------------
def say_and_gather(text: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech" action="/voice" method="POST" language="en-US" speechTimeout="auto">
    <Say>{text}</Say>
  </Gather>
  <Say>Goodbye.</Say>
</Response>"""


def say_and_hangup(text: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>{text}</Say>
  <Hangup/>
</Response>"""


def load_session(call_sid: str) -> dict:
    ref = db.collection("callsessions").document(call_sid)
    doc = ref.get()
    if doc.exists:
        return doc.to_dict()
    s = {"stage": "greet", "history": [], "proposals": []}
    ref.set(s)
    return s


def save_session(call_sid: str, s: dict):
    db.collection("callsessions").document(call_sid).set(s)


def freebusy_busy_ranges(day_local: dt):
    start = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, 0, 0, 0)).astimezone(pytz.UTC)
    end = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, 23, 59, 59)).astimezone(pytz.UTC)
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": DEFAULT_TZ,
        "items": [{"id": "primary"}],
    }
    fb = calendar_service.freebusy().query(body=body).execute()
    busy = fb["calendars"]["primary"].get("busy", [])
    ranges = []
    for b in busy:
        s = dt.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        e = dt.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        ranges.append((s, e))
    return ranges


def is_free(candidate: dt, duration_min: int, busy_ranges):
    end = candidate + timedelta(minutes=duration_min)
    for (bs, be) in busy_ranges:
        if not (end <= bs or candidate >= be):
            return False
    return True


def next_business_slots(day_local: dt, max_slots=8):
    slots = []
    day_start = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, BUSINESS_HOURS[0], 0, 0))
    day_end = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, BUSINESS_HOURS[1], 0, 0))
    now_local = LOCAL_TZ.localize(dt.now())
    t = max(now_local, day_start)
    while t + timedelta(minutes=MEETING_DURATION_MIN) <= day_end and len(slots) < max_slots:
        slots.append(t)
        t += timedelta(minutes=MEETING_DURATION_MIN)
    return slots


def propose_slots_from_preference(utterance: str, max_slots=3):
    text = (utterance or "").lower()
    today = LOCAL_TZ.localize(dt.now())
    if "tomorrow" in text:
        pref = today + timedelta(days=1)
    else:
        wmap = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
        target = None
        for k, v in wmap.items():
            if k in text:
                target = v
                break
        if target is not None:
            diff = (target - today.weekday()) % 7
            pref = today + timedelta(days=diff)
        else:
            pref = today

    proposals = []
    for d in [pref, pref + timedelta(days=1)]:
        busy = freebusy_busy_ranges(d)
        for c in next_business_slots(d, max_slots=24):
            if is_free(c, MEETING_DURATION_MIN, busy):
                proposals.append(c)
                if len(proposals) >= max_slots:
                    return proposals
    return proposals


def create_event(start_local: dt, caller_number: str, subject="Call with Shruti"):
    end_local = start_local + timedelta(minutes=MEETING_DURATION_MIN)
    event = {
        "summary": subject,
        "description": f"Auto-scheduled by AI assistant.\nCaller: {caller_number}",
        "start": {"dateTime": start_local.isoformat(), "timeZone": DEFAULT_TZ},
        "end": {"dateTime": end_local.isoformat(), "timeZone": DEFAULT_TZ},
    }
    return calendar_service.events().insert(calendarId="primary", body=event).execute()


def send_email(to_email: str, subject: str, body: str):
    from email.mime.text import MIMEText
    msg = MIMEText(body)
    msg["to"] = to_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


def extract_intent(utterance: str) -> str:
    prompt = f"""Text: {utterance}
Classify intent as one of: SCHEDULE, MESSAGE, EXIT.
Answer with one token."""
    out = llm.invoke(prompt).content.strip().upper()
    if "EXIT" in out or any(k in utterance.lower() for k in ["bye", "goodbye", "no thanks", "nothing"]):
        return "EXIT"
    if "MESSAGE" in out:
        return "MESSAGE"
    return "SCHEDULE"


def choose_slot_from_reply(reply: str, proposals):
    r = (reply or "").lower()
    for s in proposals:
        lab = s.strftime("%-I:%M %p").lower()
        dow = s.strftime("%A").lower()
        if lab in r or dow in r:
            return s
    m = re.search(r"\b([0-1]?\d)\s*(am|pm)\b", r)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(2) == "pm":
            hour += 12
        for s in proposals:
            if s.hour == hour:
                return s
    return None


# -------------------- Twilio Voice webhook --------------------
@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.form.get("CallSid", "")
    from_num = request.form.get("From", "")
    speech = (request.form.get("SpeechResult") or "").strip()

    print(f"\n📞 CallSid={call_sid} From={from_num}")
    print(f"🗣  SpeechResult: {speech}")

    s = load_session(call_sid)
    stage_before = s.get("stage", "greet")
    print(f"🔄 Previous stage: {stage_before}")

    s["caller"] = from_num
    s["history"].append({"user": speech})

    intent = extract_intent(speech)
    print(f"🎯 Intent: {intent}")

    if intent == "EXIT" or s.get("stage") == "done":
        s["stage"] = "done"
        save_session(call_sid, s)
        print("👋 Exit detected.")
        return Response(say_and_hangup("Goodbye!"), mimetype="text/xml")

    if s["stage"] == "greet" and not speech:
        s["stage"] = "main"
        save_session(call_sid, s)
        return Response(
            say_and_gather("Hi, this is Shruti's AI assistant. Would you like to schedule a meeting or leave a message?"),
            mimetype="text/xml",
        )

    if s["stage"] == "main":
        s["intent"] = intent
        if intent == "SCHEDULE":
            s["stage"] = "propose"
        elif intent == "MESSAGE":
            s["stage"] = "record"
            save_session(call_sid, s)
            print("📝 Switching to message mode.")
            return Response(say_and_gather("Sure. Please state your message and I will email Shruti."), mimetype="text/xml")
        elif intent == "EXIT":
            s["stage"] = "done"
            save_session(call_sid, s)
            return Response(say_and_hangup("Goodbye!"), mimetype="text/xml")
        save_session(call_sid, s)
        print(f"➡️ New stage: {s['stage']}")

    if s["stage"] == "propose":
        # Only generate proposals once
        if not s.get("proposals"):
            props = propose_slots_from_preference(speech, max_slots=3)
            s["proposals"] = [p.isoformat() for p in props]
            if not props:
                reply = "I didn’t find open times then. Would another day work?"
            else:
                human = ", ".join(p.strftime("%A %-I:%M %p") for p in props)
                reply = f"These times are open: {human}. Which one should I book?"
            save_session(call_sid, s)
            print(f"📅 Offered slots: {s['proposals']}")
            return Response(say_and_gather(reply), mimetype="text/xml")

        proposals = [dt.fromisoformat(p).astimezone(LOCAL_TZ) for p in s["proposals"]]
        chosen = choose_slot_from_reply(speech, proposals)
        if not chosen:
            s["stage"] = "confirm"
            save_session(call_sid, s)
            print("❓ No matching slot, asking again.")
            return Response(
                say_and_gather("No problem—please pick one of the offered times or suggest another day."),
                mimetype="text/xml",
            )

        event = create_event(chosen, s.get("caller", ""))
        when = chosen.strftime("%A %-I:%M %p")
        s["stage"] = "after_booking"
        s["event_link"] = event.get("htmlLink", "")
        save_session(call_sid, s)

        try:
            send_email(
                OWNER_EMAIL,
                "AI Assistant: Meeting booked",
                f"Caller: {s.get('caller')}\nTime: {when} ({DEFAULT_TZ})\nEvent: {s['event_link']}\n",
            )
        except Exception as e:
            print(f"Email error: {e}")

        print(f"✅ Booked {when}")
        return Response(
            say_and_gather(f"Done! I’ve booked {when}. I’ll email Shruti a confirmation. Would you like to do anything else?"),
            mimetype="text/xml",
        )

    if s["stage"] == "record":
        try:
            send_email(OWNER_EMAIL, "AI Assistant: New message", f"Caller: {s.get('caller')}\nMessage: {speech}")
            print("💌 Sent message email.")
        except Exception as e:
            print(f"Email error: {e}")
        s["stage"] = "after_record"
        save_session(call_sid, s)
        return Response(
            say_and_gather("Thanks! I’ve sent your message. Would you like to do anything else?"), mimetype="text/xml"
        )

    if s["stage"] in ("after_booking", "after_record"):
        if intent == "EXIT":
            s["stage"] = "done"
            save_session(call_sid, s)
            print("👋 User ended conversation.")
            return Response(say_and_hangup("Alright, goodbye!"), mimetype="text/xml")
        else:
            s["stage"] = "main"
            save_session(call_sid, s)
            print("🔁 Restarting main flow.")
            return Response(
                say_and_gather("Sure, would you like to schedule another meeting or leave a message?"), mimetype="text/xml"
            )

    print("⚠️ Unrecognized state — restarting.")
    s["stage"] = "main"
    save_session(call_sid, s)
    return Response(say_and_gather("Sorry, could you repeat that?"), mimetype="text/xml")


# -------------------- Entry --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

import os, json, re, base64
from datetime import datetime as dt, timedelta
from typing import List, Dict, Any
from flask import Flask, request, Response, jsonify
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
    global LOCAL_TZ, twilio_client, db, llm, calendar_service, gmail_service, OWNER_EMAIL, DEFAULT_TZ
    if _initialized:
        return
    _initialized = True
    print("🔥 Warming up dependencies (agentic) ...")

    from google.cloud import secretmanager, firestore
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from twilio.rest import Client as TwilioClient
    from langchain_openai import ChatOpenAI

    sm = secretmanager.SecretManagerServiceClient()
    proj = os.environ["GOOGLE_CLOUD_PROJECT"]

    def get_secret(name: str) -> str:
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
    OAUTH_TOKEN_JSON = json.loads(get_secret("OAUTH_TOKEN_JSON"))
    _ = json.loads(get_secret("OAUTH_CLIENT_JSON"))  # not used directly here

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

    # Clients
    calendar_client = build("calendar", "v3", credentials=creds)
    gmail_client = build("gmail", "v1", credentials=creds)
    tw_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    firestore_client = firestore.Client()
    controller = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

    # Set globals
    calendar_service = calendar_client
    gmail_service = gmail_client
    twilio_client = tw_client
    db = firestore_client
    llm = controller

    print(f"✅ Warm-up complete. OWNER_EMAIL={OWNER_EMAIL}, TZ={DEFAULT_TZ}")


# -------------------- TwiML helpers --------------------
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


# -------------------- Session helpers --------------------
def load_session(call_sid: str) -> Dict[str, Any]:
    ref = db.collection("callsessions").document(call_sid)
    doc = ref.get()
    if doc.exists:
        return doc.to_dict()
    s = {"stage": "agentic", "history": [], "proposals": [], "caller": "", "last_tool_result": ""}
    ref.set(s)
    return s


def save_session(call_sid: str, s: Dict[str, Any]):
    db.collection("callsessions").document(call_sid).set(s, merge=True)


# -------------------- Calendar + Gmail primitives --------------------
def freebusy_busy_ranges(day_local: dt):
    start = dt(day_local.year, day_local.month, day_local.day, 0, 0, 0, tzinfo=LOCAL_TZ).astimezone(pytz.UTC)
    end = dt(day_local.year, day_local.month, day_local.day, 23, 59, 59, tzinfo=LOCAL_TZ).astimezone(pytz.UTC)
    body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": DEFAULT_TZ, "items": [{"id": "primary"}]}
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
    day_start = dt(day_local.year, day_local.month, day_local.day, BUSINESS_HOURS[0], 0, 0, tzinfo=LOCAL_TZ)
    day_end = dt(day_local.year, day_local.month, day_local.day, BUSINESS_HOURS[1], 0, 0, tzinfo=LOCAL_TZ)
    now_local = dt.now(LOCAL_TZ)
    t = max(now_local, day_start)
    while t + timedelta(minutes=MEETING_DURATION_MIN) <= day_end and len(slots) < max_slots:
        slots.append(t)
        t += timedelta(minutes=MEETING_DURATION_MIN)
    return slots


def propose_slots(date_hint: str, max_slots=3) -> List[dt]:
    text = (date_hint or "").lower()
    today = dt.now(LOCAL_TZ)
    if "tomorrow" in text:
        pref = today + timedelta(days=1)
    else:
        wmap = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
        target = next((v for k, v in wmap.items() if k in text), None)
        pref = today + timedelta(days=((target - today.weekday()) % 7)) if target is not None else today

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


def gmail_send(to_email: str, subject: str, body: str):
    from email.mime.text import MIMEText
    msg = MIMEText(body)
    msg["to"] = to_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


# -------------------- Agent tools --------------------
def make_tools_for_call(call_sid: str):
    from langchain.agents import Tool

    def tool_get_free_slots(input_str: str) -> str:
        try:
            payload = json.loads(input_str) if input_str else {}
        except Exception:
            payload = {"date_hint": input_str}
        date_hint = payload.get("date_hint", "")
        slots = propose_slots(date_hint, max_slots=3)
        session = load_session(call_sid)
        session["proposals"] = [s.isoformat() for s in slots]
        save_session(call_sid, session)
        out = {"slots": [{"iso": s.isoformat(), "speak": s.strftime("%A %I:%M %p").lstrip("0")} for s in slots]}
        return json.dumps(out)

    def tool_book_meeting(input_str: str) -> str:
        try:
            payload = json.loads(input_str) if input_str else {}
        except Exception:
            payload = {}
        session = load_session(call_sid)
        props = [dt.fromisoformat(p).astimezone(LOCAL_TZ) for p in session.get("proposals", [])]
        chosen = None
        if "iso" in payload:
            try:
                chosen = dt.fromisoformat(payload["iso"]).astimezone(LOCAL_TZ)
            except Exception:
                pass
        elif "choice" in payload and props:
            idx = int(payload["choice"]) - 1
            if 0 <= idx < len(props):
                chosen = props[idx]
        if not chosen:
            return json.dumps({"ok": False, "error": "No valid slot to book"})
        event = create_event(chosen, session.get("caller", ""))
        when = chosen.strftime("%A %I:%M %p").lstrip("0")
        session["last_tool_result"] = {"event_link": event.get("htmlLink", ""), "when": when}
        save_session(call_sid, session)
        try:
            gmail_send(OWNER_EMAIL, "AI Assistant: Meeting booked",
                       f"Caller: {session.get('caller')}\nTime: {when} ({DEFAULT_TZ})\nEvent: {event.get('htmlLink','')}\n")
        except Exception as e:
            print(f"Email error: {e}")
        return json.dumps({"ok": True, "when": when, "event_link": event.get("htmlLink", "")})

    def tool_send_owner_email(input_str: str) -> str:
        try:
            payload = json.loads(input_str) if input_str else {}
        except Exception:
            payload = {"body": input_str}
        sub = payload.get("subject", "AI Assistant: Caller update")
        body = payload.get("body", "")
        session = load_session(call_sid)
        body = f"Caller: {session.get('caller')}\n\n{body}"
        try:
            gmail_send(OWNER_EMAIL, sub, body)
            return json.dumps({"ok": True})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    def tool_summarize_call(_input: str) -> str:
        session = load_session(call_sid)
        transcript = "\n".join([f"User: {h.get('user','')}\nAI: {h.get('ai','')}" for h in session.get("history", [])])
        prompt = f"Summarize this call in 3 sentences:\n{transcript}"
        summary = llm.invoke(prompt).content
        return summary

    return [
        Tool(
            name="get_free_slots",
            func=tool_get_free_slots,
            description="Get available meeting time slots using a date hint like 'tomorrow' or 'Monday'.",
        ),
        Tool(
            name="book_meeting",
            func=tool_book_meeting,
            description="Book one of the offered meeting slots using a numeric choice or ISO datetime string.",
        ),
        Tool(
            name="send_owner_email",
            func=tool_send_owner_email,
            description="Send an email to the owner with caller details and message content.",
        ),
        Tool(
            name="summarize_call",
            func=tool_summarize_call,
            description="Summarize the call so far for logging or email.",
        ),
    ]


# -------------------- Agent wrapper --------------------
AGENT_SYSTEM_PROMPT = """You are Shruti's AI phone receptionist. Speak concisely and professionally.
Your goals:
1) Help callers schedule meetings (find availability, propose times, confirm, then book).
2) If they want to leave a message, ask for it and email the owner.
3) If the proposed times don't work, offer alternatives.
4) After booking, confirm and ask if they need anything else.
5) If they indicate they are done, end the call.
Return only what should be SPOKEN to the caller (no JSON)."""

def run_agent_turn(call_sid: str, user_text: str) -> str:
    from langchain.agents import initialize_agent, AgentType
    tools = make_tools_for_call(call_sid)
    s = load_session(call_sid)
    history_lines = []
    for h in s.get("history", [])[-6:]:
        if h.get("user"):
            history_lines.append(f"User: {h['user']}")
        if h.get("ai"):
            history_lines.append(f"Assistant: {h['ai']}")
    context = "\n".join(history_lines)
    prompt = f"{AGENT_SYSTEM_PROMPT}\nConversation so far:\n{context}\nUser just said: {user_text}"
    agent = initialize_agent(tools, llm, agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION, verbose=True,handle_parsing_errors=True)
    try:
        result = agent.invoke({"input": prompt})
        spoken = result.get("output", result) if isinstance(result, dict) else result
    except Exception as e:
        print(f"Agent error: {e}")
        spoken = "Sorry, something went wrong while processing that. Could you repeat?"
    s["history"].append({"user": user_text, "ai": spoken})
    save_session(call_sid, s)
    return spoken


# -------------------- Voice webhook --------------------
@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.form.get("CallSid", request.json.get("CallSid", "test")) if request.is_json else request.form.get("CallSid", "test")
    from_num = request.form.get("From", request.json.get("From", "postman")) if request.is_json else request.form.get("From", "postman")
    speech = (request.form.get("SpeechResult") or (request.json.get("SpeechResult") if request.is_json else "") or "").strip()

    print(f"\n📞 CallSid={call_sid} From={from_num}")
    print(f"🗣  SpeechResult: {speech}")

    s = load_session(call_sid)
    s["caller"] = from_num
    if speech:
        s["history"].append({"user": speech})
        save_session(call_sid, s)

    if not speech and len(s.get("history", [])) == 0:
        greeting = "Hi, this is Shruti's AI assistant. How can I help you today — schedule a meeting or leave a message?"
        s["history"].append({"ai": greeting})
        save_session(call_sid, s)
        return jsonify({"response": greeting})

    spoken = run_agent_turn(call_sid, speech or "")
    print(f"🗣  Agent says: {spoken}")

    if re.search(r"\b(goodbye|bye|that's all|nothing|no thanks)\b", spoken.lower()):
        return jsonify({"response": spoken, "end": True})
    return jsonify({"response": spoken})


# -------------------- Entry --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

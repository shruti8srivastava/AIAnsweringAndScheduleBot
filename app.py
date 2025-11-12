import os, sys, json, re, base64
from datetime import datetime as dt, timedelta
from typing import List, Dict, Any
from flask import Flask, request, jsonify
import pytz

print("🪴 Container booting... Python:", sys.version)
sys.stdout.flush()

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
print("✅ Flask app object created")
sys.stdout.flush()

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
    print("🚀 init_once() called")
    sys.stdout.flush()

    try:
        from google.cloud import secretmanager, firestore
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from twilio.rest import Client as TwilioClient
        from langchain_openai import ChatOpenAI
        print("📦 Imports successful")
        sys.stdout.flush()
    except Exception as e:
        print(f"❌ Import error: {e}")
        sys.stdout.flush()
        return

    try:
        sm = secretmanager.SecretManagerServiceClient()
        proj = os.environ["GOOGLE_CLOUD_PROJECT"]
        print(f"🔑 Using project: {proj}")
        sys.stdout.flush()

        def get_secret(name: str) -> str:
            resp = sm.access_secret_version(
                request={"name": f"projects/{proj}/secrets/{name}/versions/latest"}
            )
            return resp.payload.data.decode()

        print("📡 Accessing secrets...")
        sys.stdout.flush()
        OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
        OWNER_EMAIL = get_secret("OWNER_EMAIL")
        DEFAULT_TZ = get_secret("DEFAULT_TZ") or "America/New_York"
        TWILIO_ACCOUNT_SID = get_secret("TWILIO_ACCOUNT_SID")
        TWILIO_AUTH_TOKEN = get_secret("TWILIO_AUTH_TOKEN")
        OAUTH_TOKEN_JSON = json.loads(get_secret("OAUTH_TOKEN_JSON"))
        _ = json.loads(get_secret("OAUTH_CLIENT_JSON"))
        print("✅ Secrets loaded")
        sys.stdout.flush()
    except Exception as e:
        print(f"❌ Secret Manager error: {e}")
        sys.stdout.flush()
        return

    try:
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        LOCAL_TZ = pytz.timezone(DEFAULT_TZ)
        SCOPES = [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/gmail.send",
        ]
        from google.oauth2.credentials import Credentials
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

        from google.cloud import firestore
        calendar_client = build("calendar", "v3", credentials=creds)
        gmail_client = build("gmail", "v1", credentials=creds)
        tw_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        firestore_client = firestore.Client()
        controller = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)

        calendar_service = calendar_client
        gmail_service = gmail_client
        twilio_client = tw_client
        db = firestore_client
        llm = controller
        print(f"✅ Warm-up complete. OWNER_EMAIL={OWNER_EMAIL}, TZ={DEFAULT_TZ}")
        sys.stdout.flush()
    except Exception as e:
        print(f"❌ Client setup error: {e}")
        sys.stdout.flush()
        return


# -------------------- Helpers --------------------
def safe_json_loads(s):
    """Parse as JSON if possible, otherwise treat as plain text."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {"input_text": s}


def load_session(call_sid: str) -> Dict[str, Any]:
    ref = db.collection("callsessions").document(call_sid)
    doc = ref.get()
    if doc.exists:
        return doc.to_dict()
    s = {"history": [], "proposals": [], "caller": "", "last_tool_result": ""}
    ref.set(s)
    return s


def save_session(call_sid: str, s: Dict[str, Any]):
    db.collection("callsessions").document(call_sid).set(s, merge=True)


# -------------------- Calendar + Gmail --------------------
def freebusy_busy_ranges(day_local: dt):
    start = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, 0, 0)).astimezone(pytz.UTC)
    end = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, 23, 59, 59)).astimezone(pytz.UTC)
    body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": DEFAULT_TZ, "items": [{"id": "primary"}]}
    fb = calendar_service.freebusy().query(body=body).execute()
    busy = fb["calendars"]["primary"].get("busy", [])
    return [(dt.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(LOCAL_TZ),
             dt.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)) for b in busy]


def is_free(candidate: dt, duration_min: int, busy_ranges):
    end = candidate + timedelta(minutes=duration_min)
    return all(end <= bs or candidate >= be for bs, be in busy_ranges)


def next_business_slots(day_local: dt, max_slots=8):
    slots, now_local = [], dt.now(LOCAL_TZ)
    start = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, BUSINESS_HOURS[0], 0))
    end = LOCAL_TZ.localize(dt(day_local.year, day_local.month, day_local.day, BUSINESS_HOURS[1], 0))
    t = max(start, now_local)
    while t + timedelta(minutes=MEETING_DURATION_MIN) <= end and len(slots) < max_slots:
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
        pref = today
        for k, v in wmap.items():
            if k in text:
                diff = (v - today.weekday()) % 7
                pref = today + timedelta(days=diff)
                break
    proposals = []
    for d in [pref, pref + timedelta(days=1)]:
        busy = freebusy_busy_ranges(d)
        for c in next_business_slots(d, 24):
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
    msg["to"], msg["subject"] = to_email, subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


# -------------------- Tools --------------------
def make_tools_for_call(call_sid: str):
    from langchain.agents import Tool

    def record_message(input_str: str) -> str:
        payload = safe_json_loads(input_str)
        msg = payload.get("message") or payload.get("input_text", "")
        session = load_session(call_sid)
        session["last_tool_result"] = {"message": msg}
        save_session(call_sid, session)
        gmail_send(OWNER_EMAIL, "AI Assistant: Caller message",
                   f"Caller: {session.get('caller')}\n\nMessage: {msg}")
        print("📩 record_message: sent")
        return json.dumps({"ok": True})

    def get_free_slots(input_str: str) -> str:
        payload = safe_json_loads(input_str)
        date_hint = payload.get("date_hint") or payload.get("input_text", "")
        print(f"🧭 get_free_slots called with hint={date_hint}")
        slots = propose_slots(date_hint, max_slots=3)
        session = load_session(call_sid)
        session["proposals"] = [s.isoformat() for s in slots]
        save_session(call_sid, session)
        return json.dumps({"slots": [{"iso": s.isoformat(), "speak": s.strftime('%A %-I:%M %p')} for s in slots]})

    def book_meeting(input_str: str) -> str:
        payload = safe_json_loads(input_str)
        choice = payload.get("choice")
        session = load_session(call_sid)
        props = [dt.fromisoformat(p).astimezone(LOCAL_TZ) for p in session.get("proposals", [])]
        chosen = None
        if choice and props:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(props):
                    chosen = props[idx]
            except Exception:
                pass
        if not chosen and props:
            chosen = props[0]
        if not chosen:
            return json.dumps({"ok": False})
        event = create_event(chosen, session.get("caller", ""))
        gmail_send(OWNER_EMAIL, "AI Assistant: Meeting booked",
                   f"Caller: {session.get('caller')}\nWhen: {chosen.strftime('%A %-I:%M %p')}\n{event.get('htmlLink')}")
        return json.dumps({"ok": True, "when": chosen.strftime('%A %-I:%M %p')})

    return [
        Tool("record_message", record_message, "Record a caller voicemail message and email Shruti."),
        Tool("get_free_slots", get_free_slots, "Find available meeting times for today or another day."),
        Tool("book_meeting", book_meeting, "Book one of the previously offered meeting slots."),
    ]


# -------------------- Agent --------------------

# -------------------- Agent system prompt --------------------

AGENT_SYSTEM_PROMPT = """You are Shruti's AI phone receptionist — an automated answering assistant.
Your sole job is to speak concisely and professionally to callers.

Your behavior rules:
1) If the caller mentions scheduling, booking, or an appointment:
   - Immediately call `get_free_slots` (start with "today" or "tomorrow" as a hint).
   - NEVER ask "when would you like to meet" or "who should be invited".
   - Always respond with available options, e.g.:
     "Here are the next available times — Option 1: Thursday 10 AM, Option 2: 2 PM, Option 3: 4 PM. Which works for you?"
   - After the caller chooses a slot, call `book_meeting` and confirm.
2) If the caller wants to leave a message, capture it using `record_message` and email Shruti.
3) After booking or recording, confirm and ask if they need anything else.
4) If they say no, goodbye, or thank you, end politely.
5) Never ask about attendees, topics, or duration — always assume this is a one-on-one meeting with Shruti.

Always return only what should be spoken to the caller (no JSON, no explanations).

Format your reasoning when using tools as:
Thought: your reasoning
Action: tool name
Action Input: JSON input
Observation: tool output

If no tool is needed (like a greeting as hi or hello ), reply directly "
"Hi! This is Shruti's AI assistant. Would you like to schedule a meeting or leave a message?".

"""



# -------------------- Agent turn runner --------------------
def run_agent_turn(call_sid: str, user_text: str) -> str:
    """Run one conversational turn safely, preserving recent call context."""
    from langchain.agents import initialize_agent, AgentType

    print(f"🧩 run_agent_turn invoked — CallSid={call_sid}, text={user_text[:80]}")
    sys.stdout.flush()

    tools = make_tools_for_call(call_sid)
    s = load_session(call_sid)

    # ---- Build conversation memory from Firestore ----
    history = s.get("history", [])
    recent_history = history[-6:] if len(history) > 0 else []
    history_lines = []
    for h in recent_history:
        if h.get("user"):
            history_lines.append(f"User: {h['user']}")
        if h.get("ai"):
            history_lines.append(f"Assistant: {h['ai']}")
    context = "\n".join(history_lines)

    # ---- Compose contextualized prompt ----
    prompt_text = (
        f"{AGENT_SYSTEM_PROMPT}\n\n"
        f"You are continuing the same phone call with the same caller.\n"
        f"Caller number: {s.get('caller')}\n\n"
        f"Conversation so far:\n{context}\n\n"
        f"The caller just said: {user_text}\n"
        f"Interpret their reply in context of the previous conversation.\n"
        f"If they pick an option or refer to a time you mentioned earlier, use it.\n"
        f"Always respond with what to SAY next."
    )

    # ---- Force proactive scheduling behavior ----
    if re.search(r"\b(schedule|meeting|appointment|book|call)\b", user_text.lower()):
        prompt_text += "\n\nThe user mentioned scheduling; immediately call `get_free_slots`."

    try:
        # ---- Initialize conversational agent ----
        agent = initialize_agent(
            tools,
            llm,
            agent_type=AgentType.CHAT_CONVERSATIONAL_REACT_DESCRIPTION,
            verbose=False,
            handle_parsing_errors=True,
            max_iterations=20,
            max_execution_time=90,
        )

        # ---- Invoke the agent ----
        print("🚀 Invoking agent with contextual memory...")
        sys.stdout.flush()
        result = agent.invoke({"input": prompt_text})

        # ---- Extract final text ----
        spoken = (
            result.get("output", result)
            if isinstance(result, dict)
            else str(result)
        ).strip()

        # ---- Handle time/iteration limits ----
        if not spoken or "Agent stopped" in spoken:
            print("⚠️ Agent fallback triggered; using direct LLM response.")
            spoken = llm.invoke(
                f"The caller said '{user_text}' after this prior context:\n{context}\n"
                "Respond politely and concisely as Shruti's phone assistant."
            ).content.strip()

    except Exception as e:
        print(f"⚠️ Agent error: {e}")
        spoken = "Sorry, I had trouble understanding that. Could you repeat?"

    if not spoken:
        spoken = "I'm sorry, I didn’t catch that. Could you say it again?"

    # ---- Save conversation for next turn ----
    try:
        s["history"].append({"user": user_text, "ai": spoken})
        save_session(call_sid, s)
        print(f"💾 Session updated for {call_sid}")
    except Exception as e:
        print(f"⚠️ Firestore save failed: {e}")

    print(f"🧠 Final response: {spoken}")
    sys.stdout.flush()
    return spoken


# -------------------- Voice endpoint --------------------
@app.route("/voice", methods=["POST"])
def voice():
    data = request.get_json(silent=True) or request.form
    call_sid = data.get("CallSid", "test")
    from_num = data.get("From", "postman")
    speech = (data.get("SpeechResult") or "").strip()

    print(f"📞 CallSid={call_sid} From={from_num} Speech={speech}")
    s = load_session(call_sid)
    s["caller"] = from_num

    # First greeting
    if not speech and not s.get("history"):
        greeting = (
            "Hi, this is Shruti's AI assistant. "
            "Would you like to schedule a meeting or leave a voicemail message?"
        )
        s["history"].append({"ai": greeting})
        save_session(call_sid, s)
        return jsonify({"response": greeting})

    spoken = run_agent_turn(call_sid, speech)
    print(f"🗣  Agent says: {spoken}")

    if re.search(r"\b(goodbye|bye|no thanks|nothing)\b", spoken.lower()):
        return jsonify({"response": spoken, "end": True})
    return jsonify({"response": spoken})


# -------------------- Entry --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🌐 Starting Flask on 0.0.0.0:{port}")
    sys.stdout.flush()
    app.run(host="0.0.0.0", port=port)

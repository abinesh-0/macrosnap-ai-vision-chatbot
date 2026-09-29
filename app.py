import json
import time

import streamlit as st
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

from prompts import SUMMARY_REQUEST_PROMPT, SYSTEM_PROMPT, WELCOME_MESSAGE_TEMPLATE

MODEL_NAME = "gemini-3.5-flash-lite"
st.set_page_config(page_title="MacroSnap", page_icon="🥗")

GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
TWILIO_ACCOUNT_SID = st.secrets["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = st.secrets["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM = st.secrets["TWILIO_WHATSAPP_FROM"]
TWILIO_CONTENT_SID = st.secrets["TWILIO_CONTENT_SID"]

# Follow-up chat reliability tuning for Streamlit Community Cloud.
# Transient model/API failures (HTTP 500/503, overloaded, timeouts) are
# retried a bounded number of times with exponential backoff (1s, 2s, 4s).
# Follow-up requests are stateless: instead of reusing one long-lived chat
# session (which can go stale and resend ever-growing history), every
# request carries a compact text summary of the conversation so far.
# The UI, session history, model, and image-analysis flow are unchanged.
RETRY_DELAYS_SECONDS = (1, 2, 4)
MAX_MEAL_CONTEXT_CHARS = 6000
FRIENDLY_ERROR_MESSAGE = (
    "I'm having trouble reaching the nutrition service right now. "
    "Please try your question again in a moment - "
    "your meal analysis above is still saved."
)


@st.cache_resource
def get_gemini_client():
    return genai.Client(api_key=GEMINI_API_KEY)


@st.cache_resource
def get_twilio_client():
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


gemini_client = get_gemini_client()
twilio_client = get_twilio_client()


def render_message(message):
    with st.chat_message(message["role"]):
        if message["kind"] == "text":
            st.write(message["content"])
        elif message["kind"] == "image":
            st.image(message["content"])


def add_message(role, kind, content):
    st.session_state.messages.append({"role": role, "kind": kind, "content": content})
    render_message(st.session_state.messages[-1])


def _is_transient_error(error):
    """Return True for transient 500/503-style failures worth retrying."""
    text = str(error).lower()
    markers = (
        "500",
        "502",
        "503",
        "504",
        "internal error",
        "unavailable",
        "overloaded",
        "temporarily",
        "timeout",
        "timed out",
        "try again",
        "please retry",
        "deadline exceeded",
        "resource exhausted",
        "connection reset",
        "connection aborted",
    )
    return any(marker in text for marker in markers)


def _meal_context():
    """Compact text history so follow-ups keep the analyzed meal context."""
    lines = []
    messages = st.session_state.messages if "messages" in st.session_state else []
    for message in messages:
        kind = message.get("kind")
        role = message.get("role")
        if kind == "image" and role == "user":
            lines.append("User: [shared a photo of their meal]")
        elif kind == "text":
            content = (message.get("content") or "").strip()
            if not content:
                continue
            who = "User" if role == "user" else "MacroSnap"
            lines.append(f"{who}: {content}")
    context = "\n".join(lines).strip()
    if len(context) > MAX_MEAL_CONTEXT_CHARS:
        context = context[-MAX_MEAL_CONTEXT_CHARS:]
    return context


def _send_with_retry(contents):
    """Call Gemini with bounded retries and exponential backoff (1s, 2s, 4s)."""
    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
    last_error = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS_SECONDS):
        if attempt > 0:
            time.sleep(delay)
        try:
            return gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=config,
            ).text
        except Exception as error:  # never crash the chat UI on API errors
            last_error = error
            if not _is_transient_error(error):
                break
    # Server logs only - the UI gets a clean message without raw API details.
    print(f"MacroSnap Gemini request failed: {last_error!r}")
    return None


def ask_gemini(parts):
    if isinstance(parts, str):
        parts = [parts]
    history = _meal_context()
    contents = []
    if history:
        contents.append(
            "Conversation so far - use it as context for follow-up questions:\n"
            + history
        )
    contents.extend(parts)
    answer = _send_with_retry(contents)
    if answer:
        return answer
    return FRIENDLY_ERROR_MESSAGE


def clean_whatsapp_text(text):
    if not text:
        return "No nutrition summary available."
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text[:1500] + "..." if len(text) > 1500 else text


def send_whatsapp(to_number, user_name, summary):
    try:
        content_variables = json.dumps(
            {"1": user_name, "2": clean_whatsapp_text(summary)},
            ensure_ascii=False
        )

        message = twilio_client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_FROM}",
            to=f"whatsapp:{to_number}",
            content_sid=TWILIO_CONTENT_SID,
            content_variables=content_variables,
        )

        return True, message.sid

    except Exception as error:
        return False, str(error)

# Step 1: onboarding
if "onboarded" not in st.session_state:
    st.title("🥗 MacroSnap")
    st.caption("Snap it. Track it. Text yourself the results.")
    with st.form("onboarding_form"):
        name = st.text_input("Your name")
        whatsapp_number = st.text_input(
            "WhatsApp number (with country code)",
            placeholder="+91XXXXXXXXXX",
            help="This is the number MacroSnap will text your summary to.",
        )
        submitted = st.form_submit_button("Let's go 🚀")
    if submitted:
        if not name.strip() or not whatsapp_number.strip():
            st.warning("Please fill in both your name and WhatsApp number.")
        else:
            st.session_state.name = name.strip()
            st.session_state.whatsapp_number = whatsapp_number.strip()
            st.session_state.messages = []
            st.session_state.onboarded = True
            st.rerun()
    st.stop()

# Step 2: chat interface
header_col, button_col = st.columns([5, 2], vertical_alignment="center")

with header_col:
    st.title("🥗 MacroSnap")

with button_col:
    send_disabled = len(st.session_state.messages) <= 1
    if st.button("📤 Send to WhatsApp", disabled=send_disabled, use_container_width=True):
        with st.spinner("Summarizing your day..."):
            summary = ask_gemini([SUMMARY_REQUEST_PROMPT])
        success, info = send_whatsapp(st.session_state.whatsapp_number, st.session_state.name, summary)
        if success:
            st.success("Sent! Check your WhatsApp 📲")
        else:
            st.error(f"Couldn't send that: {info}")

st.caption(f"Logged in as {st.session_state.name} - updates go to {st.session_state.whatsapp_number}")

if not st.session_state.messages:
    add_message("assistant", "text", WELCOME_MESSAGE_TEMPLATE.format(name=st.session_state.name))
else:
    for message in st.session_state.messages:
        render_message(message)

user_input = st.chat_input(
    "Ask a question, or attach a photo of your meal",
    accept_file=True,
    file_type=["jpg", "jpeg", "png"],
)

if user_input:
    photo = user_input.files[0] if user_input.files else None
    text = user_input.text
    parts = []

    if photo is not None:
        photo_bytes = photo.getvalue()
        add_message("user", "image", photo_bytes)
        parts.append(types.Part.from_bytes(data=photo_bytes, mime_type=photo.type))
    if text:
        add_message("user", "text", text)
        parts.append(text)
    elif photo is not None:
        parts.append("What is this meal? Give me the calories and macros.")

    with st.spinner("Crunching the numbers..."):
        answer = ask_gemini(parts)
    add_message("assistant", "text", answer)

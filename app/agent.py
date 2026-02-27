import json
import logging
from datetime import datetime, timezone
from typing import Generator

from openai import OpenAI

from app.config import NEBIUS_BASE_URL, NEBIUS_API_KEY, LLM_MODEL
from app.tools import TOOL_DEFINITIONS, execute_tool
from app.db import refresh_db

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are a helpful WhatsApp assistant. You can search contacts, read messages, \
explore group info, find information across the user's WhatsApp chats, send messages, \
check incoming messages, and schedule messages for later delivery.

Current date and time: {{current_time}}

IMPORTANT GUIDELINES:
1. When the user asks about a contact, ALWAYS use search_contacts first to find the right JID.
   - If multiple matches are found, present the options and ask the user to choose.
   - If only one strong match is found, proceed with it.

2. When fetching messages:
   - Start with a reasonable time window (e.g., last 24 hours for "recent", last 7 days for general).
   - If the conversation seems to start abruptly or the user wants more context, automatically \
fetch earlier messages by calling get_messages again with an earlier 'after' date.
   - Always use ISO 8601 format for dates.

3. For groups, you can use get_group_info to see members and get_messages with the group JID.

4. Present messages in a clean, readable format with timestamps and sender names.

5. You can search across all messages using search_messages — great for finding specific topics.

6. If a tool returns no results, try broadening your search (wider date range, different name spelling).

7. Be concise but thorough. Summarize long conversations when appropriate.

8. NEVER fabricate messages or contacts — only report what the tools return.

SENDING MESSAGES:
9. Before sending any message, ALWAYS call check_whatsapp_status first.
   - If status is "qr_pending": tell the user they need to scan the QR code in the web UI first.
   - If status is "bridge_offline" or "disconnected": tell the user the bridge is not running.
   - Only proceed with sending if status is "connected".

10. **CRITICAL — ALWAYS get explicit user confirmation before sending.** When the user asks to \
send a message, you MUST:
    a. Search for the contact using search_contacts to get the correct JID and name.
    b. Note the EXACT jid and name from the search result — you will need both.
    c. Draft the message and show it to the user like this:
       **Draft message to [Contact Name] ([JID]):**
       > [message text]
       Shall I send this?
    d. Wait for the user to confirm (e.g., "yes", "send it", "go ahead").
    e. Only AFTER confirmation, call send_message with recipient_jid, recipient_name, and message.
       Use the EXACT jid and name from the search_contacts result. Do NOT modify or substitute them.
    NEVER call send_message without showing the draft and receiving confirmation first.

11. After successfully sending, confirm to the user that the message was delivered.

INCOMING MESSAGES & CATCH-UP:
12. When the user asks "what did I miss", "catch me up", "any new messages", or similar:
    - Use get_unread_summary to get all unread chats with previews.
    - Present a clean summary organized by chat, with the most important/active ones first.
    - For each chat, briefly summarize the unread messages.
    - Offer to dive deeper into any specific chat.

13. Use get_incoming_messages to check for real-time messages received through the bridge.
    This shows messages received since the bridge started, even if not yet reflected in the local DB.

SCHEDULED MESSAGES:
14. When the user asks to schedule a message (e.g., "remind me to text X tomorrow at 9am"):
    - Same rules as sending: search contact, draft, get confirmation.
    - Use schedule_message with the send_at time in ISO 8601 UTC format.
    - Convert relative times ("tomorrow at 9am", "in 2 hours") to absolute UTC datetimes.
    - After scheduling, confirm the time and recipient.

15. Use list_scheduled_messages to show pending scheduled messages.
    Use cancel_scheduled_message to cancel one by ID.

VOICE / TTS:
16. The user may interact via voice. Your full response is always shown in the chat UI, \
but a text-to-speech engine may read part of it aloud. When your response is long \
(message lists, detailed data, tables, multi-line content), wrap ONLY the brief \
conversational summary in <tts> tags. The TTS engine will speak just that part. \
For example:
    • List of messages here...
    <tts>You had 3 messages in the Campers group — mostly a late-night plan to meet up at MMM.</tts>
Rules for <tts>:
  - Keep it short and natural, like you're talking to a friend (1-3 sentences).
  - Only use <tts> when the full response is too long to speak comfortably.
  - For short responses (confirmations, simple answers), do NOT use <tts> — the whole response will be spoken.
  - If the user explicitly asks to hear specific messages read aloud, speak those in <tts>.

CONTEXT:
17. You have access to the full conversation history including your previous tool calls \
and their results. Use this context to avoid redundant tool calls. For example, if you \
already searched for a contact in a previous turn, you don't need to search again unless \
the user asks about a different contact.

18. Each user request is independent unless it explicitly references a previous one. \
When the user asks about a DIFFERENT contact or chat, ALWAYS call the tools fresh — \
do NOT reuse data from a previous request about a different person.

Available tools: search_contacts, list_recent_chats, get_messages, get_group_info, \
search_messages, get_starred_messages, get_chat_statistics, check_whatsapp_status, \
send_message, get_incoming_messages, get_unread_summary, schedule_message, \
list_scheduled_messages, cancel_scheduled_message"""

MAX_TOOL_ROUNDS = 10
MAX_TURNS_IN_CONTEXT = 15
RECENT_TURNS_FULL = 3
TOOL_RESULT_TRUNCATE_CHARS = 500


def _get_client() -> OpenAI:
    return OpenAI(base_url=NEBIUS_BASE_URL, api_key=NEBIUS_API_KEY)


def _build_system_prompt() -> str:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return SYSTEM_PROMPT.replace("{current_time}", now)


def _split_into_turns(messages: list[dict]) -> list[list[dict]]:
    """Group messages into turns. Each turn starts with a user message."""
    turns: list[list[dict]] = []
    current: list[dict] = []
    for msg in messages:
        if msg.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return turns


def prepare_context(messages: list[dict]) -> list[dict]:
    """
    Manage context window by:
    1. Limiting total turns to MAX_TURNS_IN_CONTEXT
    2. Truncating tool results in older turns to save tokens
    """
    turns = _split_into_turns(messages)

    if len(turns) > MAX_TURNS_IN_CONTEXT:
        turns = turns[-MAX_TURNS_IN_CONTEXT:]

    result = []
    for i, turn in enumerate(turns):
        turns_from_end = len(turns) - i - 1
        for msg in turn:
            if msg.get("role") == "tool" and turns_from_end >= RECENT_TURNS_FULL:
                content = msg.get("content", "")
                if len(content) > TOOL_RESULT_TRUNCATE_CHARS:
                    result.append(
                        {**msg, "content": content[:TOOL_RESULT_TRUNCATE_CHARS] + "\n...[truncated]..."}
                    )
                else:
                    result.append(msg)
            else:
                result.append(msg)
    return result


def _clean_assistant_message(msg_dict: dict) -> dict:
    """Strip model_dump() extras down to fields needed for persistence/replay."""
    cleaned: dict = {"role": "assistant", "content": msg_dict.get("content")}
    if msg_dict.get("tool_calls"):
        cleaned["tool_calls"] = [
            {"id": tc["id"], "type": tc["type"], "function": tc["function"]}
            for tc in msg_dict["tool_calls"]
        ]
    return cleaned


def chat(messages: list[dict], conversation_id: str | None = None) -> Generator[dict, None, None]:
    """
    Run the agent loop. Yields events:
      {"type": "tool_call", "name": ..., "arguments": ...}
      {"type": "tool_result", "name": ..., "result": ...}
      {"type": "message", "content": ...}
      {"type": "error", "content": ...}
      {"type": "persist", "message": ...}   ← for caller to save to store
    """
    refresh_db()
    client = _get_client()

    managed = prepare_context(messages)
    full_messages = [{"role": "system", "content": _build_system_prompt()}] + managed

    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=full_messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
            )
        except Exception as e:
            yield {"type": "error", "content": f"LLM API error: {str(e)}"}
            return

        choice = response.choices[0]
        message = choice.message

        if message.tool_calls:
            raw_msg = message.model_dump()
            full_messages.append(raw_msg)

            cleaned = _clean_assistant_message(raw_msg)
            yield {"type": "persist", "message": cleaned}

            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                try:
                    func_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    func_args = {}

                yield {"type": "tool_call", "name": func_name, "arguments": func_args}

                result = execute_tool(func_name, func_args)

                yield {"type": "tool_result", "name": func_name, "result": result}

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
                full_messages.append(tool_msg)
                yield {"type": "persist", "message": tool_msg}

            continue

        content = message.content or ""
        yield {"type": "message", "content": content}
        return

    yield {
        "type": "message",
        "content": "I've reached the maximum number of tool calls. "
        "Here's what I found so far — please try a more specific question if you need more details.",
    }


def chat_sync(messages: list[dict]) -> dict:
    """
    Synchronous version that collects all events and returns the final result.
    Returns {"response": str, "tool_calls": list, "persist_messages": list}
    """
    tool_calls = []
    final_response = ""
    persist_messages = []

    for event in chat(messages):
        if event["type"] == "persist":
            persist_messages.append(event["message"])
        elif event["type"] == "tool_call":
            tool_calls.append({"name": event["name"], "arguments": event["arguments"]})
        elif event["type"] == "tool_result":
            for tc in tool_calls:
                if tc["name"] == event["name"] and "result" not in tc:
                    tc["result"] = event["result"]
                    break
        elif event["type"] == "message":
            final_response = event["content"]
        elif event["type"] == "error":
            final_response = event["content"]

    return {
        "response": final_response,
        "tool_calls": tool_calls,
        "persist_messages": persist_messages,
    }

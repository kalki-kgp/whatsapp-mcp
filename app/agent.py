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

Available tools: search_contacts, list_recent_chats, get_messages, get_group_info, \
search_messages, get_starred_messages, get_chat_statistics, check_whatsapp_status, \
send_message, get_incoming_messages, get_unread_summary, schedule_message, \
list_scheduled_messages, cancel_scheduled_message"""

MAX_TOOL_ROUNDS = 10  # Safety limit on agentic loops


def _get_client() -> OpenAI:
    return OpenAI(base_url=NEBIUS_BASE_URL, api_key=NEBIUS_API_KEY)


def _build_system_prompt() -> str:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return SYSTEM_PROMPT.replace("{current_time}", now)


def chat(messages: list[dict], conversation_id: str | None = None) -> Generator[dict, None, None]:
    """
    Run the agent loop. Yields events:
      {"type": "tool_call", "name": ..., "arguments": ...}
      {"type": "tool_result", "name": ..., "result": ...}
      {"type": "message", "content": ...}
      {"type": "error", "content": ...}
    """
    # Refresh DB copies at the start of each conversation turn
    refresh_db()

    client = _get_client()

    # Prepend system message
    full_messages = [{"role": "system", "content": _build_system_prompt()}] + messages

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

        # If the model wants to call tools
        if message.tool_calls:
            # Add assistant message with tool calls
            full_messages.append(message.model_dump())

            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                try:
                    func_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    func_args = {}

                yield {"type": "tool_call", "name": func_name, "arguments": func_args}

                # Execute the tool
                result = execute_tool(func_name, func_args)

                yield {"type": "tool_result", "name": func_name, "result": result}

                # Add tool result to conversation
                full_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

            # Continue the loop — the model may want to call more tools or give a final answer
            continue

        # No tool calls — this is the final response
        content = message.content or ""
        yield {"type": "message", "content": content}
        return

    # Exceeded max rounds
    yield {"type": "message", "content": "I've reached the maximum number of tool calls. Here's what I found so far — please try a more specific question if you need more details."}


def chat_sync(messages: list[dict]) -> dict:
    """
    Synchronous version that collects all events and returns the final result.
    Returns {"response": str, "tool_calls": list}
    """
    tool_calls = []
    final_response = ""

    for event in chat(messages):
        if event["type"] == "tool_call":
            tool_calls.append({"name": event["name"], "arguments": event["arguments"]})
        elif event["type"] == "tool_result":
            # Find matching tool call and attach result
            for tc in tool_calls:
                if tc["name"] == event["name"] and "result" not in tc:
                    tc["result"] = event["result"]
                    break
        elif event["type"] == "message":
            final_response = event["content"]
        elif event["type"] == "error":
            final_response = event["content"]

    return {"response": final_response, "tool_calls": tool_calls}

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
explore group info, and find information across the user's WhatsApp chats.

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

Available tools: search_contacts, list_recent_chats, get_messages, get_group_info, \
search_messages, get_starred_messages, get_chat_statistics"""

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

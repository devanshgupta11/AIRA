"""Step 1 of Phase B: prove a Google ADK agent can run against our existing local Ollama model.

Nothing AIRA-specific. If this does not work, every later step is built on sand.

ADK defaults to Gemini. The route to a local model is its LiteLlm wrapper
(google/adk/models/lite_llm.py), which forwards to LiteLLM - and LiteLLM speaks to Ollama.
We must NOT pull a second model: the laptop has ~1.4 GB of spare VRAM and the 7B model already
occupies 5.6 GB, so ADK has to share the model CrewAI is already using.

Usage:  python adk_smoke_test.py
"""

import asyncio
import os
import time

# Keep ADK fully local and quiet: no telemetry, no cloud project lookups.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "0")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

OLLAMA_URL = os.getenv("AIRA_OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("AIRA_OLLAMA_MODEL", "qwen2.5:7b-instruct")

from google.adk.agents import Agent                    # noqa: E402
from google.adk.models.lite_llm import LiteLlm         # noqa: E402
from google.adk.runners import Runner                  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types                         # noqa: E402

APP = "aira-smoke"
USER = "aira"


def build_agent() -> Agent:
    """An ADK agent pointed at the local Ollama model.

    'ollama_chat/<model>' is LiteLLM's provider prefix for Ollama's chat endpoint; api_base
    tells it where Ollama lives. Both are passed straight through LiteLlm to LiteLLM.
    """
    return Agent(
        name="smoke_agent",
        model=LiteLlm(model=f"ollama_chat/{MODEL}", api_base=OLLAMA_URL),
        instruction="You answer in one short sentence. Never add commentary.",
    )


async def ask(prompt: str) -> tuple[str, float]:
    runner = Runner(agent=build_agent(), app_name=APP, session_service=InMemorySessionService())
    session = await runner.session_service.create_session(app_name=APP, user_id=USER)
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    t0 = time.perf_counter()
    chunks: list[str] = []
    async for event in runner.run_async(user_id=USER, session_id=session.id, new_message=message):
        content = getattr(event, "content", None)
        for part in (getattr(content, "parts", None) or []):
            if getattr(part, "text", None):
                chunks.append(part.text)
    return "".join(chunks).strip(), time.perf_counter() - t0


async def main() -> int:
    import google.adk as adk

    print(f"google-adk {adk.__version__}")
    print(f"model      ollama_chat/{MODEL} via {OLLAMA_URL}")
    print("class      google.adk.models.lite_llm.LiteLlm\n")

    for prompt in ("Reply with exactly the word: ready",
                   "In one sentence, what does a high error rate on a service usually mean?"):
        print(f"> {prompt}")
        try:
            text, secs = await ask(prompt)
        except Exception as e:  # noqa: BLE001 - report honestly, do not mask
            print(f"  FAILED after error: {type(e).__name__}: {e}")
            return 1
        if not text:
            print("  FAILED: the agent returned no text")
            return 1
        print(f"  {text}")
        print(f"  ({secs:.1f}s)\n")

    print("ADK can reach the local model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

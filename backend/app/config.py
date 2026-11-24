"""Runtime settings for the AIRA backend (overridable with environment variables)."""

import os
from pathlib import Path

from dotenv import load_dotenv

# backend/.env holds the Gemini API keys and the model name. Loaded first, by path rather than from
# the working directory, so the backend, the RCA agent's process and the scripts all read the same
# file wherever they are started from. Variables already set in the environment win, so a cloud
# deployment can supply them as secrets with no .env file at all.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

# Keep CrewAI fully local: no anonymous telemetry, no trace upload. Must be set before crewai is
# imported. These two variables are what stop CrewAI reporting anything outward.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
# OTEL_SDK_DISABLED is deliberately NOT set. It used to be, as belt-and-braces alongside the two
# above, and it silently turned every one of AIRA's own spans into a no-op: the tracing layer
# initialised, reported itself healthy, and recorded nothing, because that variable disables the
# whole OpenTelemetry SDK for the process rather than just a framework's use of it. The CrewAI
# opt-outs above are what actually keep telemetry local; AIRA now needs the SDK for its own traces.

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("AIRA_DATA_DIR", BACKEND_DIR / "data"))
DB_PATH = Path(os.getenv("AIRA_DB_PATH", DATA_DIR / "aira.db"))
LOG_DIR = Path(os.getenv("AIRA_LOG_DIR", BACKEND_DIR.parent / "logs"))

# The installable-app (PWA) files: manifest, service worker, icons, mobile stylesheet. They live in
# the repository's pwa/ folder; a container sets AIRA_PWA_DIR to wherever it copied them. If the
# folder is missing the console still works exactly as before, it just cannot be installed.
PWA_DIR = Path(os.getenv("AIRA_PWA_DIR", BACKEND_DIR.parent / "pwa"))

PROMETHEUS_URL = os.getenv("AIRA_PROMETHEUS_URL", "http://localhost:9090")
ALERTMANAGER_URL = os.getenv("AIRA_ALERTMANAGER_URL", "http://localhost:9093")
# The model every agent uses, in both frameworks, through the Gemini API (Google AI Studio keys).
# The keys themselves are read by llm_keys.py, which also fails over between them.
GEMINI_MODEL = os.getenv("AIRA_GEMINI_MODEL", "gemini-3.5-flash")
# The name recorded against each incident and agent run.
LLM_MODEL = f"gemini/{GEMINI_MODEL}"

# ---------------------------------------------------------------- incident memory
#
# Qdrant runs embedded (a local storage directory, no server and no container): the corpus is a
# few hundred vectors, and the machine has under 4 GB of RAM free, so a thirtieth container would
# cost more than it returns. The client API is identical either way, so moving to a server later
# is a change of this one setting, not of the code.
#
# Embeddings run on the CPU through onnxruntime. The 7B model occupies 5.6 GB of 8 GB of VRAM, and
# a second model on the GPU would make Ollama evict it, adding a reload to the next agent call.
QDRANT_PATH = Path(os.getenv("AIRA_QDRANT_PATH", BACKEND_DIR.parent / "qdrant"))
FASTEMBED_CACHE = Path(os.getenv("AIRA_FASTEMBED_CACHE", BACKEND_DIR.parent / ".cache" / "fastembed"))
EMBED_MODEL = os.getenv("AIRA_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIM = int(os.getenv("AIRA_EMBED_DIM", "384"))
MEMORY_ENABLED = os.getenv("AIRA_MEMORY_ENABLED", "1").lower() not in ("0", "false", "no")

# ---------------------------------------------------------------- deployment mode
#
# "full"    everything: the monitoring stack, the Gemini keys, the MCP server and the RCA agent are all
#           reachable, and AIRA can investigate live incidents. This is the laptop.
# "archive" the agentic layer and the console are running, but the stack they investigate is not
#           part of this deployment. Recorded incidents, their findings, traces and the memory
#           store are all served; nothing new can be investigated.
#
# The distinction exists so a cloud deployment can be honest. Pointing a hosted console at a
# monitoring stack that does not exist would show five red dependencies and look broken; claiming
# they were healthy would be a lie. Naming the mode lets /health say the true thing: these are not
# part of this deployment.
MODE = os.getenv("AIRA_MODE", "full").lower()
ARCHIVE = MODE == "archive"

# Seconds the agent may take before the incident is marked as an error.
AGENT_TIMEOUT_S = float(os.getenv("AIRA_AGENT_TIMEOUT_S", "180"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

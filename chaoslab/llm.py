"""Optional language-model access for the planner and the analyst.

One OpenAI-compatible chat client. DeepSeek is the default; Groq, OpenRouter, a local
Ollama or LM Studio work the same way by changing the base URL and model:

    export DEEPSEEK_API_KEY=...                       # or CHAOSLAB_LLM_API_KEY
    export CHAOSLAB_LLM_MODEL=deepseek-flash          # default
    export CHAOSLAB_LLM_BASE_URL=https://api.deepseek.com
    export CHAOSLAB_LLM=off                           # never call a model

The model only proposes. Every call returns None on any failure (no key, network,
timeout, bad JSON), so the deterministic path always has the final say and nothing
the model writes can raise a safety limit.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _local(base_url: str) -> bool:
    return urlparse(base_url).hostname in ("localhost", "127.0.0.1", "::1")


def extract_json(text: str | None) -> dict | None:
    """The first JSON object in a model reply, tolerating think blocks and code fences."""
    if not text:
        return None
    text = _THINK.sub("", text).strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@dataclass
class LLM:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = ""
    timeout_s: float = 60.0
    enabled: bool = True
    last_error: str = ""
    calls: int = field(default=0)

    def available(self) -> bool:
        if not self.enabled:
            self.last_error = "model disabled"
            return False
        if not self.api_key and not _local(self.base_url):
            self.last_error = "no API key: set DEEPSEEK_API_KEY or CHAOSLAB_LLM_API_KEY"
            return False
        return True

    def describe(self) -> str:
        return f"{self.model} via {urlparse(self.base_url).hostname}"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice=None,
             json_mode: bool = False, max_tokens: int = 1200) -> dict | None:
        """One chat turn. Returns the assistant message ({content, tool_calls}) or None."""
        if not self.available():
            return None
        body: dict = {"model": self.model, "messages": messages, "temperature": 0.2,
                      "max_tokens": max_tokens, "stream": False}
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url.rstrip("/") + "/chat/completions"
        for attempt in range(2):   # one retry for rate limits and server errors
            self.calls += 1
            try:
                with urlopen(Request(url, data=json.dumps(body).encode(), headers=headers),
                             timeout=self.timeout_s) as resp:
                    data = json.load(resp)
                message = data["choices"][0]["message"]
                return {"content": message.get("content") or "", "tool_calls": message.get("tool_calls") or []}
            except HTTPError as err:
                detail = err.read(300).decode(errors="replace") if hasattr(err, "read") else ""
                self.last_error = f"HTTP {err.code}: {detail}".strip()
                if err.code in (429, 500, 502, 503) and attempt == 0:
                    time.sleep(min(float(err.headers.get("Retry-After") or 2), 10))
                    continue
                return None
            except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as err:
                self.last_error = f"{type(err).__name__}: {err}"
                return None
        return None

    def chat_json(self, system: str, user: str, max_tokens: int = 900) -> dict | None:
        reply = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                          json_mode=True, max_tokens=max_tokens)
        data = extract_json(reply["content"]) if reply else None
        if reply and data is None:
            self.last_error = "reply was not a JSON object"
        return data


def from_env(enabled: bool = True) -> LLM:
    switch = os.environ.get("CHAOSLAB_LLM", "on").lower() not in ("off", "0", "false", "no")
    return LLM(base_url=os.environ.get("CHAOSLAB_LLM_BASE_URL", DEFAULT_BASE_URL),
               model=os.environ.get("CHAOSLAB_LLM_MODEL", DEFAULT_MODEL),
               api_key=os.environ.get("CHAOSLAB_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", ""),
               timeout_s=float(os.environ.get("CHAOSLAB_LLM_TIMEOUT_S", "60")),
               enabled=enabled and switch)

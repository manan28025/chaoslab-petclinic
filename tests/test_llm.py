import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from chaoslab import llm


class FakeAPI(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint: rate-limits the first call, then answers with a tool call."""
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if len(FakeAPI.seen) == 1:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        reply = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "submit_plan", "arguments": "{}"}}]}}]}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LLMTest(unittest.TestCase):
    def test_json_is_extracted_from_think_blocks_and_fences(self):
        self.assertEqual(llm.extract_json('<think>{"no": 1}</think> ok ```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(llm.extract_json('prefix {"b": [1]} suffix'), {"b": [1]})
        self.assertIsNone(llm.extract_json("no json here"))
        self.assertIsNone(llm.extract_json("[1, 2]"))

    def test_configuration_comes_from_the_environment(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k", "CHAOSLAB_LLM_MODEL": "m"}, clear=True):
            client = llm.from_env()
            self.assertEqual((client.api_key, client.model, client.base_url), ("k", "m", llm.DEFAULT_BASE_URL))
            self.assertTrue(client.available())
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k", "CHAOSLAB_LLM": "off"}, clear=True):
            self.assertFalse(llm.from_env().available())
        with patch.dict(os.environ, {}, clear=True):
            client = llm.from_env()
            self.assertFalse(client.available())
            self.assertIn("no API key", client.last_error)
            self.assertIsNone(client.chat([{"role": "user", "content": "hi"}]))
        with patch.dict(os.environ, {"CHAOSLAB_LLM_BASE_URL": "http://localhost:11434/v1"}, clear=True):
            self.assertTrue(llm.from_env().available(), "a local model needs no key")

    def test_chat_retries_a_rate_limit_and_returns_tool_calls(self):
        FakeAPI.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = llm.LLM(base_url=f"http://127.0.0.1:{server.server_port}", model="m", api_key="secret")
        reply = client.chat([{"role": "user", "content": "plan"}], tools=[{"type": "function"}], tool_choice="auto")
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "submit_plan")
        self.assertEqual(reply["content"], "")
        self.assertEqual(len(FakeAPI.seen), 2)
        sent = FakeAPI.seen[-1]
        self.assertEqual(sent["path"], "/chat/completions")
        self.assertEqual(sent["auth"], "Bearer secret")
        self.assertEqual((sent["body"]["model"], sent["body"]["tool_choice"]), ("m", "auto"))


if __name__ == "__main__":
    unittest.main()

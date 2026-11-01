from contextlib import contextmanager
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import unittest
from unittest.mock import patch

from banking_agent.llm_client import APIConfig, LLMError, OpenAICompatibleClient, PROVIDERS


KEY = "sk-private-transport-test-key"


def reply(message, finish_reason="stop"):
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


@contextmanager
def provider_server(response=None, status=200, headers=None, raw=None):
    records = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            records.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            content = raw if raw is not None else json.dumps(response or reply({"role": "assistant", "content": "收到"}), ensure_ascii=False).encode()
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class LLMClientTests(unittest.TestCase):
    def client(self, base_url="http://127.0.0.1:8888/v1"):
        return OpenAICompatibleClient(APIConfig("custom", base_url, "test-model", KEY))

    def test_presets_and_normalized_endpoints_hide_key(self):
        for name in ("deepseek", "qwen", "openai"):
            preset = PROVIDERS[name]
            config = APIConfig(name, preset["base_url"] + "/", preset["model"], KEY)
            self.assertTrue(config.endpoint.endswith("/chat/completions"))
            self.assertNotIn(KEY, repr(config))
            self.assertNotIn(KEY, json.dumps(config.public_dict()))
            self.assertTrue(config.public_dict()["configured"])
        config = APIConfig("custom", "https://example.com/v1/chat/completions/", "model", KEY)
        self.assertEqual(config.endpoint, "https://example.com/v1/chat/completions")

    def test_reject_insecure_and_credential_urls(self):
        for url in ("http://example.com/v1", "https://me:secret@example.com/v1", "https://example.com/v1?key=secret", "https://example.com/#key", "file:///tmp/x", "https://example.com/%0d%0aX", "https://example.com\\@evil.test"):
            with self.subTest(url=url), self.assertRaises(LLMError):
                APIConfig("custom", url, "model", KEY)
        APIConfig("custom", "http://localhost:1234/v1", "model", KEY)
        APIConfig("custom", "http://[::1]:1234/v1", "model", KEY)

    def test_invalid_key_model_provider_timeout(self):
        cases = ({"api_key": ""}, {"api_key": "abc\r\nInjected: yes"}, {"model": ""}, {"provider": []}, {"timeout": True}, {"timeout": 0}, {"timeout": float("nan")})
        for changes in cases:
            values = {"provider": "custom", "base_url": "https://example.com/v1", "model": "model", "api_key": KEY}
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(LLMError):
                APIConfig(**values)

    def test_real_http_request_auth_unicode_and_optional_tools(self):
        tools = [{"type": "function", "function": {"name": "query_bank", "parameters": {"type": "object"}}}]
        with provider_server() as (url, records):
            client = self.client(url + "/v1")
            answer = client.complete([{"role": "user", "content": "最近花了多少钱"}], tools)
            self.assertEqual(answer["content"], "收到")
            self.assertEqual(records[0]["path"], "/v1/chat/completions")
            self.assertEqual(records[0]["headers"]["Authorization"], "Bearer " + KEY)
            self.assertEqual(records[0]["body"]["messages"][0]["content"], "最近花了多少钱")
            self.assertEqual(records[0]["body"]["tools"], tools)
            self.assertEqual(records[0]["body"]["tool_choice"], "auto")
            client.test_connection()
            self.assertNotIn("tools", records[1]["body"])
            self.assertNotIn("最近花", json.dumps(records[1]["body"], ensure_ascii=False))

    def test_tool_calls_and_reasoning_can_continue_provider_turn(self):
        call = {"id": "call_1", "type": "function", "function": {"name": "submit_operation", "arguments": '{"operation":"balance","payload":{}}'}}
        message = {"role": "assistant", "content": None, "tool_calls": [call], "reasoning_content": "内部思考"}
        with provider_server(reply(message, "tool_calls")) as (url, _):
            result = self.client(url).complete([{"role": "user", "content": "查余额"}])
            self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"])["operation"], "balance")
            self.assertEqual(result["reasoning_content"], "内部思考")

    def test_provider_echoed_key_is_scrubbed_in_text_and_arguments(self):
        message = {"role": "assistant", "content": "密钥=" + KEY, "reasoning_content": KEY, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "query_bank", "arguments": json.dumps({"note": KEY})}, "private_echo": KEY}]}
        with provider_server(reply(message)) as (url, _):
            result = self.client(url).complete([{"role": "user", "content": "hello"}])
            self.assertNotIn(KEY, json.dumps(result))
            self.assertNotIn("private_echo", result["tool_calls"][0])

    def test_http_errors_are_redacted_and_not_retried(self):
        errors = set()
        for status in (401, 403, 404, 429, 503):
            with self.subTest(status=status), provider_server(status=status, raw=("error echo " + KEY).encode()) as (url, records):
                with self.assertRaises(LLMError) as captured:
                    self.client(url).complete([{"role": "user", "content": "hello"}])
                self.assertNotIn(KEY, str(captured.exception))
                self.assertEqual(len(records), 1)
                errors.add(str(captured.exception))
        self.assertEqual(len(errors), 5)

    def test_redirect_is_never_followed(self):
        with provider_server(status=307, headers={"Location": "http://127.0.0.1:9/leak"}) as (url, records):
            with self.assertRaisesRegex(LLMError, "重定向"):
                self.client(url).complete([{"role": "user", "content": "hello"}])
            self.assertEqual(len(records), 1)

    def test_truncated_or_filtered_model_result_cannot_execute(self):
        for reason in ("length", "content_filter"):
            with self.subTest(reason=reason), provider_server(reply({"role": "assistant", "content": "partial"}, reason)) as (url, _):
                with self.assertRaises(LLMError):
                    self.client(url).complete([{"role": "user", "content": "hello"}])

    def test_malformed_response_and_tool_arguments_fail_closed(self):
        call = {"id": "c1", "type": "function", "function": {"name": "submit_operation", "arguments": '{"amount":NaN}'}}
        malformed = (b"not-json", b'{"choices":[]}', b'{"choices":[{"message":{"role":"user","content":"bad"}}]}', json.dumps(reply({"role": "assistant", "content": None, "tool_calls": [call]})).encode())
        for content in malformed:
            with self.subTest(content=content[:70]), provider_server(raw=content) as (url, _):
                with self.assertRaises(LLMError):
                    self.client(url).complete([{"role": "user", "content": "hello"}])

    def test_duplicate_tool_ids_are_rejected(self):
        call = {"id": "duplicate", "type": "function", "function": {"name": "query_bank", "arguments": "{}"}}
        with provider_server(reply({"role": "assistant", "content": None, "tool_calls": [call, call]})) as (url, _):
            with self.assertRaises(LLMError):
                self.client(url).complete([{"role": "user", "content": "hello"}])

    def test_response_size_limit_is_enforced(self):
        with provider_server(raw=b"x" * 100) as (url, _), patch("banking_agent.llm_client.MAX_RESPONSE_BYTES", 50):
            with self.assertRaisesRegex(LLMError, "过长"):
                self.client(url).complete([{"role": "user", "content": "hello"}])

    def test_timeout_and_incomplete_read_are_safe_errors(self):
        for failure in (socket.timeout("echo " + KEY), IncompleteRead(("echo " + KEY).encode(), 100)):
            client = self.client()
            with self.subTest(failure=type(failure).__name__), patch.object(client._opener, "open", side_effect=failure):
                with self.assertRaises(LLMError) as captured:
                    client.complete([{"role": "user", "content": "hello"}])
                self.assertNotIn(KEY, str(captured.exception))

    def test_deeply_nested_provider_json_is_safe_error(self):
        with provider_server(raw=b"[" * 1500 + b"]" * 1500) as (url, _):
            with self.assertRaises(LLMError):
                self.client(url).complete([{"role": "user", "content": "hello"}])

    def test_request_size_and_nonfinite_values_are_rejected_before_http(self):
        client = self.client()
        with patch.object(client._opener, "open") as open_request:
            with patch("banking_agent.llm_client.MAX_REQUEST_BYTES", 40), self.assertRaises(LLMError):
                client.complete([{"role": "user", "content": "hello"}])
            with self.assertRaises(LLMError):
                client.complete([{"role": "user", "content": float("nan")}])
            open_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

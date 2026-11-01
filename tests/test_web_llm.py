"""Full local HTTP path: browser session -> compatible model -> bank tools."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from banking_agent.web_app import LocalBankServer


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append((self.path, dict(self.headers), body))
        message = self.server.responses.pop(0) if self.server.responses else {"role": "assistant", "content": "连接正常"}
        data = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ModelWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        self.provider.responses, self.provider.received = [], []
        self.provider_thread = threading.Thread(target=self.provider.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.provider_thread.start()
        self.server = LocalBankServer(audit_dir=self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.state = {}
        self.status, self.state = self.request("GET", "/api/state")

    def tearDown(self):
        for server, thread in ((self.server, self.thread), (self.provider, self.provider_thread)):
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        values = {"Cookie": self.cookie, "Origin": self.server.origin, "Content-Type": "application/json", "X-CSRF-Token": self.state.get("csrf", "")}
        values.update(headers or {})
        connection.request(method, path, None if body is None else json.dumps(body).encode(), values)
        response = connection.getresponse()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def post(self, path, **fields):
        body = {"request_id": str(uuid.uuid4()), "revision": self.state["revision"], **fields}
        status, result = self.request("POST", path, body)
        if status == 200:
            self.state = result
        return status, result, body

    def configure(self):
        status, result, body = self.post("/api/model", action="configure", payload={"provider": "custom", "base_url": f"http://127.0.0.1:{self.provider.server_port}/v1", "model": "local-test-model", "api_key": "secret-test-key-do-not-persist"})
        self.assertEqual(status, 200, result)
        return body

    @staticmethod
    def call(operation, payload):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "call_local_1", "type": "function", "function": {"name": "submit_operation", "arguments": json.dumps({"operation": operation, "payload": payload})}}]}

    def test_configuration_is_private_and_does_not_contact_provider(self):
        body = self.configure()
        self.assertTrue(self.state["llm"]["enabled"])
        self.assertEqual(self.provider.received, [])
        session = next(iter(self.server.sessions.values()))
        self.assertNotIn("secret-test-key-do-not-persist", json.dumps(self.state))
        self.assertNotIn("secret-test-key-do-not-persist", repr(session))
        self.assertNotIn("secret-test-key-do-not-persist", repr(session.model_requests))
        self.assertNotIn("secret-test-key-do-not-persist", "".join(p.read_text() for p in Path(self.temp.name).glob("*.jsonl")))
        self.assertEqual(self.request("POST", "/api/model", body)[0], 200)
        body["payload"]["model"] = "different"
        self.assertEqual(self.request("POST", "/api/model", body)[0], 409)

    def test_probe_is_explicit_harmless_and_replay_safe(self):
        self.configure()
        self.provider.responses.append({"role": "assistant", "content": "secret-test-key-do-not-persist"})
        status, result, body = self.post("/api/model", action="test")
        self.assertEqual(status, 200, result)
        self.assertIn("连接成功", result["model_test"])
        self.assertNotIn("secret-test-key-do-not-persist", json.dumps(result))
        self.assertEqual(self.request("POST", "/api/model", body)[0], 200)
        self.assertEqual(len(self.provider.received), 1)
        path, headers, payload = self.provider.received[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret-test-key-do-not-persist")
        self.assertNotIn("50000", json.dumps(payload))

    def test_free_chat_uses_real_compatible_transport_and_bank_tool(self):
        self.configure()
        self.provider.responses.extend([self.call("balance", {}), {"role": "assistant", "content": "这是你的虚拟账户概览。"}])
        status, result, _ = self.post("/api/message", source="chat", message="我手头还有多少钱可以花呀？")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["messages"][-1]["source"], "model")
        self.assertEqual(result["messages"][-1]["task"]["state"], "SUCCESS")
        self.assertEqual(result["messages"][-1]["task"]["operation"], "balance")
        self.assertGreaterEqual(len(self.provider.received), 1)
        self.assertIn("我手头还有多少钱可以花呀", json.dumps(self.provider.received[0][2], ensure_ascii=False))

    def test_direct_transfer_and_large_confirmation_keep_existing_guards(self):
        self.configure()
        self.provider.responses.extend([self.call("transfer", {"recipient_query": "小明", "amount": "2888"}), {"role": "assistant", "content": "请查看系统结果。"}])
        status, result, body = self.post("/api/message", source="chat", message="小明刚替我垫了两千八百八十八块，帮我还他吧")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["accounts"][0]["available_balance"], "47112.00")
        self.assertEqual(self.request("POST", "/api/message", body)[1]["accounts"][0]["available_balance"], "47112.00")
        self.provider.responses.extend([self.call("transfer", {"recipient_query": "小明", "amount": "3000"}), {"role": "assistant", "content": "请核对摘要。"}])
        status, result, _ = self.post("/api/message", source="chat", message="再给他三千块")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["task"]["state"], "AWAITING_CONFIRMATION")
        self.assertEqual(result["accounts"][0]["available_balance"], "47112.00")
        token = result["confirmation_token"]
        self.assertEqual(self.post("/api/message", source="chat", message="确认转账", confirmation_token="invalid")[0], 409)
        count = len(self.provider.received)
        status, result, _ = self.post("/api/message", source="chat", message="确认转账", confirmation_token=token)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["accounts"][0]["available_balance"], "44112.00")
        self.assertEqual(len(self.provider.received), count)

    def test_controls_remain_local_and_reset_preserves_connection(self):
        self.configure()
        self.assertEqual(self.post("/api/message", source="control", message="查看股票行情")[0], 200)
        self.assertEqual(self.provider.received, [])
        self.assertEqual(self.post("/api/message", action="reset")[0], 200)
        self.assertTrue(self.state["llm"]["enabled"])
        self.assertEqual(len(self.state["messages"]), 1)
        self.assertEqual(self.post("/api/model", action="disconnect")[0], 200)
        self.assertFalse(self.state["llm"]["enabled"])
        self.assertIsNone(next(iter(self.server.sessions.values())).model_config)

    def test_model_transfer_to_first_recipient_still_requires_identity_window(self):
        self.configure()
        self.provider.responses.extend([self.call("transfer", {"recipient_query": "张三", "amount": "2888"}), {"role": "assistant", "content": "请先核对收款人。"}])
        status, result, _ = self.post("/api/message", source="chat", message="把两千八百八十八块还给张三")
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["messages"][-1]["task"])
        self.assertEqual(result["pending_recipient"]["name"], "张三")
        self.assertEqual(result["accounts"][0]["available_balance"], "50000.00")
        count = len(self.provider.received)
        status, result, _ = self.post("/api/message", action="confirm_recipient", recipient_token=result["pending_recipient"]["token"], payload={"name": "张三", "account_last4": "7788"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["accounts"][0]["available_balance"], "47112.00")
        self.assertEqual(len(self.provider.received), count)

    def test_compound_draft_exposes_reviewable_fields_and_does_not_pay(self):
        self.configure()
        call = {"role": "assistant", "content": None, "tool_calls": [{"id": "draft-one", "type": "function", "function": {"name": "compose_workflow", "arguments": json.dumps({"steps": [{"operation": "transfer", "payload": {"recipient_query": "小明", "amount": "100"}}, {"operation": "balance", "payload": {}}]})}}]}
        self.provider.responses.extend([call, {"role": "assistant", "content": "请先查看计划草稿。"}])
        status, result, _ = self.post("/api/message", source="chat", message="先给小明一百块，然后看看余额")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["accounts"][0]["available_balance"], "50000.00")
        self.assertEqual(result["workflow"]["state"], "DRAFT")
        self.assertEqual(result["workflow"]["steps"][0]["payload"]["recipient_query"], "小明")
        self.assertEqual(result["workflow"]["steps"][0]["payload"]["amount"], "100")

    def test_model_endpoint_has_same_csrf_origin_revision_and_session_boundaries(self):
        body = {"action": "disconnect", "request_id": str(uuid.uuid4()), "revision": self.state["revision"]}
        self.assertEqual(self.request("POST", "/api/model", body, {"X-CSRF-Token": "bad"})[0], 403)
        self.assertEqual(self.request("POST", "/api/model", body, {"Origin": "https://example.com"})[0], 403)
        body["revision"] -= 1
        self.assertEqual(self.request("POST", "/api/model", body)[0], 409)
        self.configure()
        self.cookie = ""
        status, other = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(other["llm"]["enabled"])


if __name__ == "__main__":
    unittest.main()

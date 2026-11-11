"""Local OpenAI-compatible Provider for the installed desktop memory acceptance flow."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import urlparse


_requests = []
_requests_lock = Lock()


def _complete(messages):
    user_text = next(
        (item.get("content", "") for item in reversed(messages) if item.get("role") == "user"),
        "",
    )
    marker = "\nMESSAGES:\n"
    if marker in user_text:
        evidence = json.loads(user_text.split(marker, 1)[1])
        facts = []
        for item in evidence:
            text = item.get("text", "")
            if "记住" in text and ("偏好" in text or "喜欢" in text):
                facts.append({
                    "kind": "preference",
                    "topic_key": "response.style_language",
                    "content": "偏好简洁并使用中文回答",
                    "evidence_message_ids": [item["message_id"]],
                    "confidence": 1.0,
                    "self_reported": True,
                    "durable": True,
                })
            elif "我通常" in text or "我可能" in text:
                routine = "可能每周五整理收件箱" if "我可能" in text else "通常在上午处理邮件"
                facts.append({
                    "kind": "habit",
                    "topic_key": "work.routine.weekly" if "我可能" in text else "work.routine",
                    "content": routine,
                    "evidence_message_ids": [item["message_id"]],
                    "confidence": 0.8,
                    "self_reported": True,
                    "durable": True,
                })
        return json.dumps({"facts": facts}, ensure_ascii=False)
    if "Summarize only" in user_text:
        return json.dumps({"summary": "偏好简洁并使用中文回答"}, ensure_ascii=False)
    return "我会按你的偏好，用简洁的中文回答。"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if urlparse(self.path).path == "/__requests":
            with _requests_lock:
                body = json.dumps(_requests, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        messages = payload.get("messages", [])
        with _requests_lock:
            _requests.append({"model": payload.get("model"), "messages": messages,
                              "stream": payload.get("stream") is True})
        answer = _complete(messages)
        if payload.get("stream") is True:
            chunks = [answer[index:index + 24] for index in range(0, len(answer), 24)]
            body = "".join(
                "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}, ensure_ascii=False) + "\n\n"
                for chunk in chunks
            ) + "data: [DONE]\n\n"
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": answer}}]},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def main():
    server = ThreadingHTTPServer(("0.0.0.0", 18761), Handler)
    print("memory acceptance provider listening on port 18761", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

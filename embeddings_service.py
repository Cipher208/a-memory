"""Shared e5 embeddings indexer service for ariel-memory.

One process owns the sentence-transformers model (torch) and serves an
OpenAI-compatible POST /v1/embeddings on loopback. All ariel-memory
instances call it via shared/embeddings.py (config key embeddings.url).

Run: .venv-embeddings/bin/python embeddings_service.py  (port 8710 default)
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("ARIEL_EMBEDDINGS_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARIEL_EMBEDDINGS_PORT", "8710"))
MODEL_NAME = os.environ.get("ARIEL_EMBEDDINGS_MODEL", "intfloat/multilingual-e5-small")

print(f"loading {MODEL_NAME} ...", flush=True)
from sentence_transformers import SentenceTransformer

model = SentenceTransformer(MODEL_NAME)
_lock = threading.Lock()  # encode() is batched under one lock — e5-small is fast
print(f"ready, dim={model.get_sentence_embedding_dimension()}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "healthy", "model": MODEL_NAME})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/embeddings":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            texts = body.get("input") or []
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                self._json(400, {"error": "input must be a list of strings"})
                return
            # Texts arrive pre-prefixed ("query: " / "passage: ") from callers —
            # the service does NOT add e5 instruction prefixes itself.
            with _lock:
                vectors = model.encode(texts).tolist()
            self._json(
                200,
                {
                    "model": MODEL_NAME,
                    "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
                },
            )
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _json(self, code: int, payload: dict) -> None:
        out = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):  # silence per-request stderr noise
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"embeddings service on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()

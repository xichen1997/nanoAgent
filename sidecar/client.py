"""
sidecar/client.py — Python SDK for the Sidecar HTTP API.

This client allows Ring 3 applications (like agent/loop.py) to interact
with the Sidecar kernel using high-level Python methods, while maintaining
strict process isolation over HTTP.
"""
import json
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, List


class SidecarClient:
    """Client for the Sidecar HTTP API."""

    def __init__(self, base_url: str = "http://127.0.0.1:7878"):
        self.base_url = base_url.rstrip("/")
        self.session_id: Optional[str] = None

    def _post(self, path: str, data: Dict[str, Any], timeout: int = 180) -> Dict[str, Any]:
        """Send a JSON POST request to the Sidecar."""
        body = json.dumps(data, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            raise RuntimeError(f"Sidecar {path} HTTP {e.code}: {raw}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Sidecar unreachable at {self.base_url}: {e.reason}")

    def _get(self, path: str, timeout: int = 20) -> Dict[str, Any]:
        """Send a JSON GET request to the Sidecar."""
        req = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            raise RuntimeError(f"Sidecar {path} HTTP {e.code}: {raw}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Sidecar unreachable at {self.base_url}: {e.reason}")

    # ── Core API ──────────────────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        return self._get("/health")

    def start(self, resume_session_id: Optional[str] = None, fork_at: Optional[int] = None) -> Dict[str, Any]:
        payload = {}
        if resume_session_id:
            payload["resume_session_id"] = resume_session_id
            if fork_at is not None:
                payload["fork_at"] = fork_at
        
        info = self._post("/session/start", payload)
        self.session_id = info.get("session_id")
        return info

    def end(self) -> Dict[str, Any]:
        if not self.session_id:
            return {"ok": True}
        res = self._post("/session/end", {"session_id": self.session_id})
        self.session_id = None
        return res

    def llm_generate(self, messages: List[Dict], system: str = "") -> Dict[str, Any]:
        return self._post("/llm/generate", {"messages": messages, "system": system})

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any], tool_use_id: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        if tool_use_id:
            payload["tool_use_id"] = tool_use_id
        if self.session_id:
            payload["session_id"] = self.session_id
            
        return self._post("/tool/execute", payload)

    def revive_sandbox(self) -> Dict[str, Any]:
        return self._post("/session/revive", {})

    # ── Snapshot API ──────────────────────────────────────────────────────────

    def snapshot_take(self) -> Dict[str, Any]:
        return self._post("/snapshot/take", {})

    def snapshot_list(self) -> Dict[str, Any]:
        return self._get("/snapshot/list")

    # ── Trunk / Fork API ──────────────────────────────────────────────────────

    def trunk_init(self) -> Dict[str, Any]:
        return self._post("/trunk/init", {})

    def trunk_fork(self, trunk_id: Optional[str] = None) -> Dict[str, Any]:
        payload = {}
        if trunk_id:
            payload["trunk_id"] = trunk_id
        info = self._post("/trunk/fork", payload)
        self.session_id = info.get("session_id")
        return info

    def trunk_commit(self) -> Dict[str, Any]:
        return self._post("/trunk/commit", {}, timeout=300)

    def trunk_abort(self) -> Dict[str, Any]:
        return self._post("/trunk/abort", {})

    def trunk_status(self) -> Dict[str, Any]:
        return self._get("/trunk/status")

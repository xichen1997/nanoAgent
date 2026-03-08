"""
sidecar/gateway.py — Host-level URL fetcher (the only path to external internet).

Runs inside the Sidecar process. The Sandbox has no direct network egress.
All HTTP requests from the Agent or Sandbox must route through here.
"""
import urllib.request
import urllib.error
import urllib.parse
import json

MAX_RESPONSE_BYTES = 4000


def fetch_url(url: str, method: str = "GET", data: str | None = None) -> dict:
    """
    Perform an HTTP request from the Sidecar host process.
    Returns a dict with 'status', 'body', and optionally 'error'.
    """
    method = method.upper()
    try:
        encoded_data = data.encode("utf-8") if data else None
        req = urllib.request.Request(url, data=encoded_data, method=method)
        req.add_header("User-Agent", "NanoAgent-Sidecar/1.0")

        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError:
                body = raw.decode("latin-1")

            if len(body) > MAX_RESPONSE_BYTES:
                body = body[:MAX_RESPONSE_BYTES] + "\n...[Content Truncated]..."

            return {
                "status": resp.status,
                "body": body,
                "error": None,
            }

    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": str(e.reason), "error": f"HTTPError: {e.code} {e.reason}"}
    except urllib.error.URLError as e:
        return {"status": 0, "body": "", "error": f"URLError: {str(e.reason)}"}
    except Exception as e:
        return {"status": 0, "body": "", "error": f"GatewayError: {str(e)}"}

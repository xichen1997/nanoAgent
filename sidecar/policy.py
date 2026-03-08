"""
sidecar/policy.py — Tool Registry and Policy Engine.

The Tool Registry is the single source of truth for the effect type of every tool.
The agent loop just calls tools by name; it has ZERO knowledge about effect types.
"""
from enum import Enum


class Effect(str, Enum):
    NO_SIDE_EFFECTS = "no_side_effects"   # Safe to skip on replay (pure read)
    REPLAYABLE      = "replayable"        # Safe to re-execute on replay (reconstruct env)
    IRREVERSIBLE    = "irreversible"      # Must NEVER execute twice; use cached result


# ── Tool Registry ──────────────────────────────────────────────────────────────
# This is the ONLY place where effect type is declared.
# A tool's name here becomes the name used in the LLM's tool schema.
TOOL_REGISTRY: dict[str, dict] = {
    "bash": {
        "effect": Effect.IRREVERSIBLE,  # Default for unclassified bash — safe default
        "description": "Execute a bash command in the sandbox.",
    },
    "bash_read": {
        "effect": Effect.NO_SIDE_EFFECTS,
        "description": "[SAFE TO SKIP] Read-only bash command (cat, ls, find, echo).",
    },
    "bash_write": {
        "effect": Effect.REPLAYABLE,
        "description": "[SAFE TO REPLAY] Bash command that modifies sandbox state (mkdir, write file, apt install).",
    },
    "bash_run": {
        "effect": Effect.IRREVERSIBLE,
        "description": "[NEVER REPLAY] Bash command with external side effects (e.g. posting to API, dropping DB).",
    },
    "fetch_url": {
        "effect": Effect.IRREVERSIBLE,
        "description": "[NEVER REPLAY] Fetch a URL via the host Gateway (GET or POST calls to external APIs).",
    },
}

# ── L2 Dynamic Quota ────────────────────────────────────────────────────────
MAX_IRREVERSIBLE_PER_SESSION = 50   # Raise a warning if exceeded

# ── L3 Dangerous Command Blocklist ─────────────────────────────────────────
BLOCKLIST_PATTERNS = [
    "rm -rf /",
    "mkfs",
    ":(){:|:&};:",     # fork bomb
    "dd if=",
]


class PolicyViolation(Exception):
    pass


class PolicyEngine:
    def __init__(self):
        self._irreversible_count = 0

    def get_effect(self, tool_name: str) -> Effect:
        entry = TOOL_REGISTRY.get(tool_name)
        if not entry:
            raise PolicyViolation(f"Unknown tool: '{tool_name}'. Not in TOOL_REGISTRY.")
        return entry["effect"]

    def check(self, tool_name: str, command: str = "") -> Effect:
        """
        Run all policy levels and return the approved Effect type,
        or raise PolicyViolation if rejected.
        """
        # L1: Effect type from registry
        effect = self.get_effect(tool_name)

        # L3: Blocklist
        if command:
            for pattern in BLOCKLIST_PATTERNS:
                if pattern in command:
                    raise PolicyViolation(
                        f"L3 Block: command contains forbidden pattern '{pattern}'"
                    )

        # L2: Quota
        if effect == Effect.IRREVERSIBLE:
            self._irreversible_count += 1
            if self._irreversible_count > MAX_IRREVERSIBLE_PER_SESSION:
                raise PolicyViolation(
                    f"L2 Quota: Too many irreversible actions in this session "
                    f"({self._irreversible_count} > {MAX_IRREVERSIBLE_PER_SESSION})"
                )

        return effect


def build_llm_tool_schema() -> list[dict]:
    """
    Auto-generate the Anthropic-compatible tool schema from the TOOL_REGISTRY.
    The agent gets this schema and calls tools by name — no effect knowledge embedded.
    """
    schemas = []
    
    # fetch_url gets richer schema
    fetch_url_schema = {
        "name": "fetch_url",
        "description": TOOL_REGISTRY["fetch_url"]["description"],
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full HTTP/HTTPS URL to request."},
                "method": {"type": "string", "description": "HTTP method (GET, POST). Default: GET.", "default": "GET"},
                "data": {"type": "string", "description": "Optional body for POST/PUT requests."},
            },
            "required": ["url"],
        }
    }
    
    bash_tools = ["bash_read", "bash_write", "bash_run"]
    for name in bash_tools:
        schemas.append({
            "name": name,
            "description": TOOL_REGISTRY[name]["description"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute."}
                },
                "required": ["command"],
            }
        })
    schemas.append(fetch_url_schema)
    return schemas

"""
sidecar/policy.py — Tool Registry and Policy Engine.

The Tool Registry is the single source of truth for the effect type of every tool.
The agent loop just calls tools by name; it has ZERO knowledge about effect types.
"""
from enum import Enum


class Effect(str, Enum):
    NO_SIDE_EFFECTS      = "no_side_effects"   # Safe to skip on replay (pure read)
    REPLAYABLE_FAST      = "replayable_fast"   # Re-execute on replay (fast commands)
    REPLAYABLE_EXPENSIVE = "replayable_expensive"  # Re-execute + snapshot afterwards
    IRREVERSIBLE         = "irreversible"      # Must NEVER execute twice; use cached result

    # Backwards-compat alias used in existing effect_log rows
    REPLAYABLE = "replayable"  # treat as replayable_fast on read


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
        "effect": Effect.REPLAYABLE_FAST,
        "description": "[SAFE TO REPLAY] Quick bash command that modifies sandbox state (mkdir, write file, small installs).",
    },
    "bash_build": {
        "effect": Effect.REPLAYABLE_EXPENSIVE,
        "description": "[SAFE TO REPLAY, EXPENSIVE] Expensive bash command that modifies sandbox state and triggers a filesystem snapshot for fast recovery (compiling, pip install torch, apt install build-essential, pulling large datasets). Use this instead of bash_write for long-running setup commands.",
    },
    "bash_run": {
        "effect": Effect.IRREVERSIBLE,
        "description": "[NEVER REPLAY] Bash command with external side effects (e.g. posting to API, dropping DB, running a server, calling an internal localhost service).",
    },
    "fetch_url": {
        "effect": Effect.IRREVERSIBLE,
        "description": "[NEVER REPLAY] Fetch an EXTERNAL URL via the host Gateway (GET or POST to public APIs). Do NOT use for localhost — use bash_run curl instead.",
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

    @staticmethod
    def is_replayable(effect: Effect | str) -> bool:
        """True for any effect tier that replays filesystem writes."""
        # effect can be an Enum instance or a string loaded from DB
        val = effect.value if isinstance(effect, Effect) else str(effect)
        return val in (
            Effect.REPLAYABLE_FAST.value,
            Effect.REPLAYABLE_EXPENSIVE.value,
            Effect.REPLAYABLE.value,  # legacy rows
            str(Effect.REPLAYABLE_FAST),
            str(Effect.REPLAYABLE_EXPENSIVE),
            str(Effect.REPLAYABLE),
        )

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
    
    bash_tools = ["bash_read", "bash_write", "bash_build", "bash_run"]
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

"""
sidecar — Capability Gateway SDK

Public API:
    from sidecar import SidecarSession

    session = SidecarSession(debug=True)
    info    = await session.start()
    result  = await session.execute_tool("bash_read", {"command": "uname -a"})
    gen     = await session.llm_generate(messages, system="...")
    await session.end()
"""
from sidecar.session import SidecarSession

__all__ = ["SidecarSession"]

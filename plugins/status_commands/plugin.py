from plugins.base import Plugin, ToolRegistration, TurnResult
from tools.schema import function_tool


class StatusCommandsPlugin(Plugin):
    name = "status_commands"

    def tools(self) -> list[ToolRegistration]:
        return [
            ToolRegistration(
                schema=function_tool(
                    "runtime_status",
                    "Show runtime status, including plugin and session information.",
                    {},
                ),
                handler=self.runtime_status,
                risk="low",
                enabled_modes={"bot", "coding"},
                always_on=True,
                session_scoped=True,
                source="plugin:status_commands",
            )
        ]

    def before_turn(self, context):
        command = _normalize_command(context.inbound.content)
        if command not in {"/status", "/plugins"}:
            return None
        return TurnResult(abort=True, reply=self.runtime_status(_session=context.session))

    def runtime_status(self, *, _session=None) -> str:
        manager = self.context.tool_registry
        session_rows = []
        if self.context.sessions is not None:
            try:
                session_rows = self.context.sessions.list_sessions()
                user_id = (_session.metadata or {}).get("user_id") if _session else None
                if user_id:
                    session_rows = [
                        row for row in session_rows
                        if (row.get("metadata") or {}).get("user_id") == user_id
                    ]
            except Exception:
                session_rows = []

        lines = ["Runtime status"]
        lines.append("")
        lines.append("Plugin manager:")
        plugin_manager = getattr(self.context, "plugin_manager", None)
        if plugin_manager is not None:
            lines.append(plugin_manager.status_text())
        else:
            lines.append("- status_commands")

        visible_tools = []
        if hasattr(manager, "_tools"):
            visible_tools = sorted(manager._tools.keys())
        lines.append("")
        lines.append(f"Registered tools: {len(visible_tools)}")
        if visible_tools:
            lines.append(", ".join(visible_tools[:20]))

        lines.append("")
        lines.append(f"Sessions: {len(session_rows)}")
        for row in session_rows[:5]:
            lines.append(f"- {row['id']} ({row['current_mode']}) updated {row['updated_at']}")
        return "\n".join(lines)


def _normalize_command(content: str) -> str:
    parts = (content or "").strip().split(maxsplit=1)
    if not parts:
        return ""
    head = parts[0].lower()
    if "@" in head:
        head = head.split("@", 1)[0]
    return head

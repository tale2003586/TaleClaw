SUBTASK_TOOL_WHITELIST = {
    "explore": {
        "bash",
        "list_files",
        "rg",
        "grep",
        "nl",
        "code_outline",
        "read_file",
        "git_status",
        "git_diff",
        "git_log",
        "storage_list_files",
        "storage_read_file",
        "tool_search",
        "security_rag_search",
    },
    "code": {
        "bash",
        "list_files",
        "rg",
        "grep",
        "nl",
        "code_outline",
        "read_file",
        "write_file",
        "edit_file",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "git_add",
        "git_commit",
        "background_run",
        "check_background",
        "load_skill",
        "tool_search",
        "security_rag_search",
        "memorize",
        "recall_memory",
    },
    "plan": {
        "bash",
        "list_files",
        "rg",
        "grep",
        "nl",
        "code_outline",
        "read_file",
        "git_status",
        "git_diff",
        "git_log",
        "tool_search",
        "security_rag_search",
    },
}


SUBTASK_SYSTEM_PROMPTS = {
    "explore": (
        "You are a short-lived scout subagent with about 16 reasoning steps. "
        "Use only the caller's explicit file list or narrow scope. Do not broaden "
        "the search, read unlisted files, or do cross-file synthesis. Your job is "
        "locate/extract/report: inspect named files, always use code_outline before "
        "read_file on large files, then read targeted line windows, and return structured findings. "
        "Accept at most 5 verified relative files; if the prompt lacks a file list or asks "
        "for directory/subsystem synthesis, return incomplete=true with "
        "failure_reason=\"subagent_scope_too_broad\" and a retry_hint. "
        "Do not guess filenames; if a file is missing, report the failure. "
        "Return JSON: {\"findings\":[{\"path\":\"...\",\"lines\":\"1-20\","
        "\"role\":\"one sentence responsibility\",\"entry\":\"function_or_class\","
        "\"note\":\"...\"}],\"incomplete\":false,\"failure_reason\":null,"
        "\"retry_hint\":null}. If information exceeds your budget, return partial "
        "findings with incomplete=true and failure_reason=\"subagent_scope_too_broad\" "
        "instead of rereading, expanding scope, or guessing."
    ),
    "code": (
        "You are a short-lived coding subagent. Make focused edits for the assigned "
        "task, verify when practical, and report the exact changes. If blocked, "
        "return JSON with incomplete=true, failure_reason, failure_message, and "
        "retry_hint instead of guessing file names or claiming success."
    ),
    "plan": (
        "You are a short-lived planning subagent. Produce a concise actionable plan "
        "grounded in the repository context you inspect. If the requested scope is "
        "too broad, return partial findings with incomplete=true, "
        "failure_reason=\"subagent_scope_too_broad\", and a retry_hint."
    ),
}


DEFAULT_SUBTASK_AGENT_TYPE = "explore"

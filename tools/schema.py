"""Tool schemas for the team agent harness.

This file only describes what the model can call. The actual Python handlers
live in tools/handlers.py and are selected by identity: lead vs teammate.
"""


def function_tool(name: str, description: str, properties: dict,
                  required: list = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


BASE_TOOLS = [
    function_tool(
        "bash",
        "Run a shell command. Use for quick commands like ls, rg, git, python, cat, etc.",
        {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
        },
        ["command"],
    ),
    function_tool(
        "list_files",
        "List files and directories inside the current coding workspace. Use workspace-relative paths only; use '.' for the workspace root, never an absolute path.",
        {
            "path": {
                "type": "string",
                "description": "Optional workspace-relative directory path. Use '.' or omit for workspace root. Never pass an absolute path.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Recursively list files when true. Defaults to false.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Pagination offset for large listings. Use the next_offset returned by a truncated result.",
            },
        },
    ),
    function_tool(
        "rg",
        (
            "Search text in the current coding workspace using ripgrep. "
            "Use this instead of bash rg when you need fast, bounded search results. "
            "Paths must be workspace-relative; pass path='.' or omit it for the workspace root."
        ),
        {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for. Set literal=true for exact text.",
            },
            "path": {
                "type": "string",
                "description": "Optional workspace-relative file or directory to search. Defaults to workspace root.",
            },
            "glob": {
                "type": "string",
                "description": "Optional ripgrep glob, such as '*.py' or '!*.md'.",
            },
            "max_matches": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum output match lines to return. Defaults to 100, capped at 500.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive search when true. Defaults to true.",
            },
            "literal": {
                "type": "boolean",
                "description": "Treat pattern as literal text instead of a regex when true.",
            },
        },
        ["pattern"],
    ),
    function_tool(
        "grep",
        (
            "Search file contents in the current coding workspace with grep-like "
            "path:line:content output. Use workspace-relative paths only; pass "
            "path='.' or omit it for the workspace root."
        ),
        {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for. Set literal=true for exact text.",
            },
            "path": {
                "type": "string",
                "description": "Optional workspace-relative file or directory to search. Defaults to workspace root.",
            },
            "glob": {
                "type": "string",
                "description": "Optional file glob, such as '*.py' or '!*.md'.",
            },
            "max_matches": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum output match lines to return. Defaults to 100, capped at 500.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive search when true. Defaults to true.",
            },
            "literal": {
                "type": "boolean",
                "description": "Treat pattern as literal text instead of a regex when true.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Recursively search directories when true. Defaults to true.",
            },
        },
        ["pattern"],
    ),
    function_tool(
        "nl",
        (
            "Read a workspace file with stable one-based line numbers, similar to "
            "nl -ba. Use this when you need exact line references or targeted "
            "line windows. If the result is truncated, continue with the returned offset."
        ),
        {
            "path": {
                "type": "string",
                "description": "Workspace-relative path to the file. Never pass an absolute path.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based starting line offset. Use the offset suggested by a truncated result to continue.",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read. If omitted, read as much as fits in the tool result budget.",
            },
            "number_blank_lines": {
                "type": "boolean",
                "description": "Number blank lines when true, matching nl -ba behavior. Defaults to true.",
            },
            "width": {
                "type": "integer",
                "minimum": 1,
                "maximum": 12,
                "description": "Line number width. Defaults to 6.",
            },
        },
        ["path"],
    ),
    function_tool(
        "repo_map",
        (
            "Build a deterministic, paginated repository map for planning broad coding work. "
            "Use this before repository-wide architecture review or multi-subsystem fan-out. "
            "It returns directory aggregates and shallow file entries with optional line counts; "
            "drill down with path=... instead of repeatedly listing directories."
        ),
        {
            "path": {
                "type": "string",
                "description": "Optional workspace-relative subtree path. Use '.' or omit for workspace root.",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 0,
                "description": "Relative depth of directories/files to show. Defaults to the configured shallow overview.",
            },
            "include_lines": {
                "type": "boolean",
                "description": "Include inexpensive line counts for text files. Defaults to true.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Line offset for continuing a truncated repo_map result.",
            },
        },
    ),
    function_tool(
        "code_outline",
        (
            "Return a deterministic symbol outline for one source file. Use it for large files "
            "before reading full contents; it returns path, total_lines, and symbols with names, "
            "kinds, and line numbers."
        ),
        {
            "path": {
                "type": "string",
                "description": "Workspace-relative source file path. Never pass an absolute path.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Line offset for continuing a truncated code_outline result.",
            },
            "limit": {
                "type": "integer",
                "description": "Max output lines to return. If omitted, return as much as fits.",
            },
        },
        ["path"],
    ),
    function_tool(
        "read_file",
        "Read UTF-8 file contents from the current coding workspace. Use workspace-relative paths only; never an absolute path. If the result is truncated, continue with the returned offset.",
        {
            "path": {
                "type": "string",
                "description": "Workspace-relative path to the file. Never pass an absolute path.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Starting line offset. Use the offset suggested by a truncated read_file result to continue.",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read. If omitted, read the whole file.",
            },
        },
        ["path"],
    ),
    function_tool(
        "write_file",
        "Write content to a file in the current coding workspace. Use workspace-relative paths only; never an absolute path.",
        {
            "path": {
                "type": "string",
                "description": "Workspace-relative path for the file. Never pass an absolute path.",
            },
            "content": {
                "type": "string",
                "description": "Content to write.",
            },
        },
        ["path", "content"],
    ),
    function_tool(
        "edit_file",
        "Replace exact text in a workspace file. Use workspace-relative paths only; never an absolute path.",
        {
            "path": {
                "type": "string",
                "description": "Workspace-relative path to the file. Never pass an absolute path.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to find.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text.",
            },
        },
        ["path", "old_text", "new_text"],
    ),
]


GIT_TOOLS = [
    function_tool(
        "git_status",
        "Show git working tree status for the current coding workspace.",
        {
            "porcelain": {
                "type": "boolean",
                "description": "Return porcelain status when true. Defaults to false.",
            },
        },
    ),
    function_tool(
        "git_diff",
        "Show git diff for the current coding workspace. Supports staged or unstaged diff.",
        {
            "path": {
                "type": "string",
                "description": "Optional relative path to limit the diff.",
            },
            "staged": {
                "type": "boolean",
                "description": "Show staged diff when true. Defaults to false.",
            },
            "stat": {
                "type": "boolean",
                "description": "Show diff stat instead of full patch when true. Defaults to false.",
            },
        },
    ),
    function_tool(
        "git_log",
        "Show recent git commits for the current coding workspace.",
        {
            "max_count": {
                "type": "integer",
                "description": "Maximum commits to show. Defaults to 10, capped at 50.",
            },
        },
    ),
    function_tool(
        "git_branch",
        "Show the current git branch, or all local branches.",
        {
            "all": {
                "type": "boolean",
                "description": "List all local branches when true. Defaults to false.",
            },
        },
    ),
    function_tool(
        "git_add",
        "Stage workspace files for commit. Paths must be relative and stay inside the workspace.",
        {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Relative paths to stage.",
            },
        },
        ["paths"],
    ),
    function_tool(
        "git_commit",
        "Create a git commit for already staged changes in the current coding workspace.",
        {
            "message": {
                "type": "string",
                "description": "Commit message.",
            },
        },
        ["message"],
    ),
]


STORAGE_TOOLS = [
    function_tool(
        "storage_list_files",
        (
            "List files in the private storage area. Paths are relative to storage/, "
            "such as uploads or generated/reports."
        ),
        {
            "path": {
                "type": "string",
                "description": "Optional directory path relative to storage/. Defaults to the storage root.",
            },
        },
    ),
    function_tool(
        "storage_read_file",
        (
            "Read a UTF-8 text file from the private storage area. "
            "The path must be relative to storage/."
        ),
        {
            "path": {
                "type": "string",
                "description": "File path relative to storage/, such as uploads/notes.txt.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum number of lines to return.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Starting line offset. Use the offset suggested by a truncated result to continue.",
            },
        },
        ["path"],
    ),
    function_tool(
        "storage_write_file",
        (
            "Create a new UTF-8 text artifact in storage/generated/. "
            "Use this for reports, notes, and exports. Paths are relative to "
            "storage/generated/, and existing files are never overwritten."
        ),
        {
            "path": {
                "type": "string",
                "description": "New artifact path relative to storage/generated/, such as reports/ai-daily.md.",
            },
            "content": {
                "type": "string",
                "description": "UTF-8 text content to save.",
            },
        },
        ["path", "content"],
    ),
]


SANDBOX_TOOLS = [
    function_tool(
        "sandbox_list_files",
        (
            "List files in this conversation's private temporary sandbox. "
            "Use the sandbox for drafts and intermediate files before publishing a final artifact."
        ),
        {
            "path": {
                "type": "string",
                "description": "Optional directory path relative to this session's sandbox.",
            },
        },
    ),
    function_tool(
        "sandbox_read_file",
        "Read a UTF-8 text file from this conversation's private temporary sandbox.",
        {
            "path": {
                "type": "string",
                "description": "File path relative to this session's sandbox.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum number of lines to return.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Starting line offset. Use the offset suggested by a truncated result to continue.",
            },
        },
        ["path"],
    ),
    function_tool(
        "sandbox_write_file",
        (
            "Write a UTF-8 draft or intermediate file in this conversation's private "
            "temporary sandbox. Set overwrite=true only when intentionally revising a draft."
        ),
        {
            "path": {
                "type": "string",
                "description": "File path relative to this session's sandbox.",
            },
            "content": {
                "type": "string",
                "description": "UTF-8 text content to save.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Whether to replace an existing sandbox file. Defaults to false.",
            },
        },
        ["path", "content"],
    ),
    function_tool(
        "publish_artifact",
        (
            "Publish one finished sandbox file to storage/generated/. "
            "Existing generated files are never overwritten."
        ),
        {
            "source_path": {
                "type": "string",
                "description": "Finished file path relative to this session's sandbox.",
            },
            "destination_path": {
                "type": "string",
                "description": (
                    "Optional destination path relative to storage/generated/. "
                    "Defaults to the source path."
                ),
            },
        },
        ["source_path"],
    ),
]


SKILL_TOOLS = [
    function_tool(
        "load_skill",
        "Load a specialized skill by name before tackling unfamiliar work.",
        {
            "name": {
                "type": "string",
                "description": "Skill name to load.",
            },
        },
        ["name"],
    ),
]


TASK_TOOLS = [
    function_tool(
        "task_create",
        "Create a persistent task in the task system.",
        {
            "subject": {
                "type": "string",
                "description": "Task title or short subject.",
            },
            "description": {
                "type": "string",
                "description": "Optional detailed task description.",
            },
        },
        ["subject"],
    ),
    function_tool(
        "task_update",
        "Update a task's status or dependency blockers.",
        {
            "task_id": {
                "type": "integer",
                "description": "ID of the task to update.",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed"],
                "description": "New task status.",
            },
            "addBlockedBy": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Task IDs that this task should be blocked by.",
            },
            "removeBlockedBy": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Task IDs to remove from this task's blockers.",
            },
        },
        ["task_id"],
    ),
    function_tool(
        "task_list",
        "List all persistent tasks with status summary.",
        {},
    ),
    function_tool(
        "task_get",
        "Get full details of a task by ID.",
        {
            "task_id": {
                "type": "integer",
                "description": "ID of the task to inspect.",
            },
        },
        ["task_id"],
    ),
    function_tool(
        "claim_task",
        "Claim an available task by ID and mark it as your in-progress work.",
        {
            "task_id": {
                "type": "integer",
                "description": "ID of the task to claim.",
            },
        },
        ["task_id"],
    ),
]


BACKGROUND_TOOLS = [
    function_tool(
        "background_run",
        "Run a long shell command in a background thread. Returns a task_id immediately.",
        {
            "command": {
                "type": "string",
                "description": "The shell command to run in the background.",
            },
        },
        ["command"],
    ),
    function_tool(
        "check_background",
        "Check background task status. Omit task_id to list all background tasks.",
        {
            "task_id": {
                "type": "string",
                "description": "Optional background task ID to inspect.",
            },
        },
    ),
]


COMMUNICATION_TOOLS = [
    function_tool(
        "send_message",
        "Send a message to another teammate's inbox.",
        {
            "to": {
                "type": "string",
                "description": "Recipient teammate name.",
            },
            "content": {
                "type": "string",
                "description": "Message content to send.",
            },
            "msg_type": {
                "type": "string",
                "enum": [
                    "message",
                    "broadcast",
                    "shutdown_request",
                    "shutdown_response",
                    "plan_approval_request",
                    "plan_approval_response",
                ],
                "description": "Message type. Defaults to message.",
            },
        },
        ["to", "content"],
    ),
    function_tool(
        "read_inbox",
        "Read and drain your own inbox.",
        {},
    ),
]


TEAMMATE_PROTOCOL_TOOLS = [
    function_tool(
        "idle",
        "Indicate that your current work is done and enter the idle polling phase.",
        {},
    ),
    function_tool(
        "shutdown_response",
        "Respond to a shutdown_request from the lead.",
        {
            "request_id": {
                "type": "string",
                "description": "Shutdown request ID from the inbox message.",
            },
            "approve": {
                "type": "boolean",
                "description": "True if shutdown is accepted, false if it cannot be completed.",
            },
            "details": {
                "type": "string",
                "description": "Optional explanation or final status details.",
            },
        },
        ["request_id", "approve"],
    ),
    function_tool(
        "plan_approval_request",
        "Submit a plan to the lead for approval before major work.",
        {
            "plan": {
                "type": "string",
                "description": "Plan text to send to the lead for review.",
            },
        },
        ["plan"],
    ),
]


LEAD_ONLY_TOOLS = [
    function_tool(
        "task",
        """Spawn a short-lived subagent for one bounded subtask with fresh, isolated context.
Subagents have about 16 reasoning steps and a restricted tool set. They are best for locating, listing, or extracting local facts from explicit files. For large files, give them code_outline-first instructions and targeted line windows. They are not suited for broad cross-file synthesis, multi-round design work, or implementation that needs iteration; use spawn_teammate for that.""",
        {
            "prompt": {
                "type": "string",
                "description": "Detailed instructions for the subagent.",
            },
            "description": {
                "type": "string",
                "description": "Short task name for display.",
            },
            "agent_type": {
                "type": "string",
                "enum": ["explore", "code", "plan"],
                "description": "Subagent type.",
            },
            "scope": {
                "type": "object",
                "description": "Required bounded file scope for file-oriented subagents. files must come from repo_map/list_files/code_outline, use verified workspace-relative file paths only, and contain at most 5 files.",
                "properties": {
                    "files": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {"type": "string"},
                        "description": "Verified workspace-relative file paths. Do not guess file names or pass directories.",
                    },
                },
                "required": ["files"],
            },
            "objective": {
                "type": "string",
                "description": "Optional single-sentence objective for the subagent.",
            },
            "deliverable": {
                "type": "string",
                "description": "Optional expected output shape, such as findings with path/lines/note.",
            },
            "budget": {
                "type": "object",
                "description": "Optional budget hints such as max_files or max_steps.",
            },
        },
        ["prompt"],
    ),
    function_tool(
        "parallel_tasks",
        """Spawn several short-lived subagents in parallel for independent, bounded subtasks.
Each subagent has about 16 reasoning steps, isolated context, and restricted tools. Use only after repo_map or another deterministic step gives concrete scopes. Good uses: bounded locate/list/extract shards with explicit files; use code_outline for large files before targeted reads. Bad uses: broad directory-level architecture synthesis, writing code with iteration, or multi-round feedback; use spawn_teammate or the parent agent for those.""",
        {
            "tasks": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Detailed instructions for this subagent.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Short task name for display.",
                        },
                        "agent_type": {
                            "type": "string",
                            "enum": ["explore", "code", "plan"],
                            "description": "Subagent type.",
                        },
                        "scope": {
                            "type": "object",
                            "description": "Required bounded file scope for file-oriented subagents. files must come from repo_map/list_files/code_outline, use verified workspace-relative file paths only, and contain at most 5 files.",
                            "properties": {
                                "files": {
                                    "type": "array",
                                    "maxItems": 5,
                                    "items": {"type": "string"},
                                    "description": "Verified workspace-relative file paths. Do not guess file names or pass directories.",
                                },
                            },
                            "required": ["files"],
                        },
                        "objective": {
                            "type": "string",
                            "description": "Optional single-sentence objective for this subagent.",
                        },
                        "deliverable": {
                            "type": "string",
                            "description": "Optional expected output shape, such as findings with path/lines/note.",
                        },
                        "budget": {
                            "type": "object",
                            "description": "Optional budget hints such as max_files or max_steps.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            "max_workers": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "Optional parallel worker count.",
            },
        },
        ["tasks"],
    ),
    function_tool(
        "compact",
        "Manually compress the conversation history into a continuity summary.",
        {
            "focus": {
                "type": "string",
                "description": "Optional focus: details to preserve in the summary.",
            },
        },
    ),
    function_tool(
        "spawn_teammate",
        "Spawn a persistent teammate that runs in its own thread and communicates through inbox messages.",
        {
            "name": {
                "type": "string",
                "description": "Unique teammate name, such as alice or tester.",
            },
            "role": {
                "type": "string",
                "description": "Teammate role, such as coder, tester, reviewer, researcher, or planner.",
            },
            "prompt": {
                "type": "string",
                "description": "Initial task instructions for the teammate.",
            },
        },
        ["name", "role", "prompt"],
    ),
    function_tool(
        "list_teammates",
        "List all teammates with their names, roles, and current statuses.",
        {},
    ),
    function_tool(
        "broadcast",
        "Broadcast a message from the lead to all teammates.",
        {
            "content": {
                "type": "string",
                "description": "Broadcast message content.",
            },
        },
        ["content"],
    ),
    function_tool(
        "shutdown_request",
        "Ask a teammate to shut down gracefully and create a tracked shutdown request.",
        {
            "teammate": {
                "type": "string",
                "description": "Name of the teammate to ask to shut down.",
            },
        },
        ["teammate"],
    ),
    function_tool(
        "shutdown_status",
        "Check the tracked status of a shutdown request by request_id.",
        {
            "request_id": {
                "type": "string",
                "description": "Shutdown request ID to inspect.",
            },
        },
        ["request_id"],
    ),
    function_tool(
        "plan_approval",
        "Approve or reject a teammate's submitted plan.",
        {
            "request_id": {
                "type": "string",
                "description": "Plan approval request ID from the lead inbox.",
            },
            "approve": {
                "type": "boolean",
                "description": "True to approve the plan, false to reject it.",
            },
            "feedback": {
                "type": "string",
                "description": "Optional feedback to send back to the teammate.",
            },
        },
        ["request_id", "approve"],
    ),
]

MEMORY_TOOLS = [
    function_tool(
        "memorize",
        "Save an important long-term memory, user preference, project convention, or current state.",
        {
            "content": {
                "type": "string",
                "description": "The memory content to save.",
            },
            "section": {
                "type": "string",
                "enum": ["memory", "self", "now", "pending"],
                "description": (
                    "Where to save it. Defaults to memory. In a coding task, use pending "
                    "for durable project conclusions that should be reviewed for promotion."
                ),
            },
        },
        ["content"],
    ),
    function_tool(
        "recall_memory",
        "Read long-term memory before answering questions that may depend on preferences, project conventions, or current state.",
        {
            "query": {
                "type": "string",
                "description": "Optional query or reason for recall.",
            },
        },
    ),
]

SEARCH_TOOLS = [
    function_tool(
        "tool_search",
        (
            "Search available deferred tools or unlock one for this turn. "
            "Use query='select:<tool_name>' to unlock a specific tool, such as select:bash."
        ),
        {
            "query": {
                "type": "string",
                "description": (
                    "Search text, or select:<tool_name> to unlock a deferred tool "
                    "that is allowed in the current mode."
                ),
            },
        },
        ["query"],
    ),
]



# Team-oriented tool sets.
TEAMMATE_TOOLS = (
    BASE_TOOLS
    + GIT_TOOLS
    + SKILL_TOOLS
    + TASK_TOOLS
    + BACKGROUND_TOOLS
    + COMMUNICATION_TOOLS
    + TEAMMATE_PROTOCOL_TOOLS
)

LEAD_TOOLS = (
    TEAMMATE_TOOLS
    + LEAD_ONLY_TOOLS
    + MEMORY_TOOLS
    + STORAGE_TOOLS
    + SANDBOX_TOOLS
    + SEARCH_TOOLS
)

# Temporary compatibility aliases for older imports. Prefer TEAMMATE_TOOLS and
# LEAD_TOOLS in new code.
CHILD_TOOLS = TEAMMATE_TOOLS
PARENT_TOOLS = LEAD_TOOLS

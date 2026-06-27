# TaleClaw Coding Agent VS Code Extension

This extension runs the local TaleClaw coding agent from a VS Code side panel.

## Development

For normal use, install the extension as a local symlink:

```bash
cd /path/to/TaleClaw
bash scripts/install_vscode_extension.sh
```

Then reload VS Code.

For extension development:

1. Open the TaleClaw repo root, or open `vscode-extension/` directly.
2. Select `Run TaleClaw VS Code Extension` in Run and Debug.
3. Press `F5` to launch an Extension Development Host.
4. Configure `taleclaw.runtimeRoot` if the active workspace is not the TaleClaw repo.
5. Open the `TaleClaw` activity bar view and run a task.

See `../docs/runtime/VSCODE_EXTENSION_AGENT.md` for the full design and technical notes.

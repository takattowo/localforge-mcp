# Contributing

1. Create a focused branch.
2. Add or update tests for behavior changes.
3. Run `python -m pytest -q` and `python -m compileall -q agent_runtime`.
4. Keep stdout reserved for MCP JSON-RPC messages; send diagnostics to stderr.
5. Document security-boundary changes in `SECURITY.md`.

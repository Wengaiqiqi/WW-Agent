# agent.md

This file provides durable guidance to the local W&W Agent CLI in this repository.

## Project Shape

- `cli.py` is the terminal entrypoint and LangGraph agent loop.
- `tools.py` registers LangChain tools.
- `tool_file_ops.py`, `tool_shell.py`, and `tool_permissions.py` implement local tool behavior.
- `skill_loader.py` discovers local skills and injects matching skill instructions.
- `skills/baidu-ecommerce-search/` contains the Baidu ecommerce skill and its Python scripts.

## Running

- Use the `LangChain` conda environment when available:
  `D:\Anaconda3\envs\LangChain\python.exe cli.py`
- First launch runs the `/model` wizard. The selected API key is persisted to
  `.claude/credentials.json`; no env var is required.
- Override the active model from the shell with
  `LANGCHAIN_AGENT_MODEL=<provider>` or `<provider>/<model>`.
- Optional Baidu ecommerce key: `BAIDU_EC_SEARCH_TOKEN`.

## Verification

- Run syntax checks with:
  `python -m py_compile cli.py config.py skill_loader.py tools.py tool_file_ops.py tool_permissions.py tool_registry.py tool_shell.py project_context.py`
- Use `python cli.py prompt /tools`, `/skills`, `/status`, and `/config` for CLI smoke tests.

## Working Agreement

- Keep CLI behavior generic; skill-specific behavior belongs in `skills/<name>/SKILL.md` or skill-local scripts.
- Avoid hardcoded secrets. Read credentials from environment variables.
- Prefer small, focused changes and verify after editing.

# Genie-X: AI Coding Agent Conversation Explorer

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/uv-package%20manager-blueviolet)](https://github.com/astral-sh/uv) 
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> ⚠️ The bulk of this project's codebase was generated using AI coding assistants.


## 🔬 What is This?

Every AI coding session you run — GitHub Copilot, Cursor, Claude Code, Copilot CLI — writes structured conversation data to local storage. That data is scattered across different formats and directories, and none of these tools let you search, compare, or analyze it.

**Genie‑X** extracts those conversations into a single indexed database. You get full-text and semantic search across every session you've ever had, usage analytics per workspace, and a local web UI to browse it all. No cloud dependency, no API keys required for core functionality. It reads what's already on your machine.

**Why it matters:**
- **Find anything you've previously discussed** with AI agents, across any project, instantly
- **Understand how you use AI tools** — which models, how many turns, which projects get the most agent help
- **Compare agents side-by-side** — same workspace, different tools, all in one view
- **Own your data** — everything stays local, indexed in SQLite

<table>
  <tr>
    <td width="30%" valign="top" rowspan="2"><img src="src/web/static/screenshots/Gennie-x-sys-overview-screen.jpg" alt="Mission Control" /></td>
    <td width="70%" valign="top"><img src="src/web/static/screenshots/gennie-x-chat-screen.jpg" alt="Conversation Explorer" /></td>
  </tr>
  <tr>
    <td width="70%" valign="top"><img src="src/web/static/screenshots/gennie-x-search-screen.jpg" alt="Search Interface" /></td>
  </tr>
</table>

### ✨ Features

- **Multi-Agent Extraction**: GitHub Copilot, Cursor, Claude Code, and GitHub Copilot CLI — each with a dedicated extractor plugin that handles the agent's native storage format
- **Web Dashboard**: Local web UI for browsing workspaces, viewing per-session analytics, and exploring full conversation threads
- **Semantic + Keyword Search**: Sentence Transformers embeddings combined with SQLite FTS5 for both meaning-based and exact-match search across all extracted conversations
- **CLI Interface**: Full command-line access for automation: list workspaces, extract, search, refresh
- **Code Metrics**: Extracts complexity and size metrics from code artifacts in conversations (via `lizard`)
- **Plugin Architecture**: Add support for new agents by dropping an extractor plugin into `src/extract_plugins/`
- **Turn Merging**: Consecutive same-role messages are automatically merged so output always alternates user/assistant — consistent across all extractors

## 🚀 Quick Start

### Prerequisites

- Python 3.11 or higher
- [uv](https://github.com/astral-sh/uv) - Fast Python package manager
- One or more supported AI coding agents installed (GitHub Copilot / Cursor / Claude Code / GitHub Copilot CLI)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/hosamsh/genie-x.git
   cd genie-x
   ```

2. **Install dependencies**
   ```bash
   # CPU-only (default, works on all machines)
   uv sync
   ```

### Understanding Workspaces

**Important:** Gennie-X does **not** create workspaces. Instead, it *discovers* existing workspaces from your AI coding agents (VS Code/Cursor/Claude Code). The tool reads the local storage where these agents save their conversation history.

To have workspaces available:
1. Use one of the supported agents (GitHub Copilot in VS Code, Cursor, or Claude Code)
2. Open a project/folder in the agent
3. Start at least one chat conversation with the AI assistant
4. The agent will save this data to its local storage (auto-detected by Gennie-X)

### Basic Usage

#### 0) Create the Config File (Required)

Create the config file by copying or renaming the example config. The defaults work for most setups:

```bash
cp config/config.example.yaml config/config.yaml
```


#### 1) Quick Start Using the Web Interface

Launch the web dashboard to explore your conversations visually:

```bash
uv run python run_web.py
# Opens at http://127.0.0.1:8000
```

The web interface lets you browse workspaces, extract chat sessions, view analytics, and explore conversations interactively without needing to use the CLI.

If the web UI shows no workspaces, it usually means the app is pointing at the wrong storage location or there is no local agent data yet. Check the storage paths in `config/config.yaml` (section `extract.<agent>.workspace_storage`). If you have not used the agent locally, open any workspace in the agent and start a chat once so it writes local storage, then refresh.

#### 2) Using the CLI

```bash
# Get help on all available commands
uv run python run_cli.py --help

# List all available workspaces
uv run python run_cli.py --list

# Extract specific workspace (requires workspace ID and output directory)
uv run python run_cli.py --extract <workspace-id> --run-dir data/<dir-name>

# Extract all workspaces for a specific agent
uv run python run_cli.py --extract --all --agent <copilot|cursor|claude_code|copilot_cli> --run-dir data/<dir-name>

# Refresh an existing workspace (incremental sync; only new/changed turns)
uv run python run_cli.py --extract <workspace-id> --run-dir data/<dir-name>

# Refresh an existing workspace (full re-ingest; reprocess everything)
uv run python run_cli.py --extract <workspace-id> --run-dir data/<dir-name> --force-refresh

# Full reset (erase DB, then re-ingest)
rm -rf data/<dir-name>
uv run python run_cli.py --extract <workspace-id> --run-dir data/<dir-name>

# Search through extracted conversations
uv run python run_cli.py --search "your search query" --run-dir data/<dir-name>
```

If `--list` shows 0 workspaces, it usually means the app is pointing at the wrong storage location or there is no local agent data yet. Check the storage paths in `config/config.yaml` (see section `extract.<agent>.workspace_storage` in the config). If you have not used the agent locally, open any workspace in the agent and start a chat once so it writes local storage, then re-run `--list`.

## 📝 Configuration

### Main Config

- Location: config/config.yaml (loaded by default)
- Example: config/config.example.yaml (copy/compare for defaults)

**Sections:**
- **extract**: text shrinking + agent storage/output paths
- **web**: web UI run directory and port
- **search**: search mode, thresholds, embedding model + batch size, auto-embed
- **token_estimation**: token estimation heuristics
- **loc_counting**: rules for couting lines of code
- **logging**: log level

You can also edit the config via the web UI.


## ⚡ Optional GPU Acceleration

If you want faster embedding generation, you can enable GPU support:

1. Install a CUDA‑enabled PyTorch build (see requirements-gpu.txt for guidance).
2. Then install the rest of the dependencies.

To choose the right CUDA wheel: (1) check your NVIDIA driver/CUDA capability (e.g., from `nvidia-smi`), (2) pick the matching CUDA version on the PyTorch install page (e.g., cu118 or cu121), and (3) use that index URL in requirements-gpu.txt.

### Adding a New Agent

1. Create `src/extract_plugins/your_agent/`
2. Implement `agent.py` with `AgentExtractor` interface
3. Add `metadata.json` for display info
4. Add config section to `config/config.yaml`
5. Test extraction and enrichment

See [Agent Extractor Interface Docs](src/extract_plugins/readme.md)


## 🧪 Running Tests

See [tests/README.md](tests/README.md) for full details.

```bash
# Run all unit tests
uv run pytest tests/unit/ -x -q

# Run a specific test file
uv run pytest tests/unit/test_copilot_cli_extract.py -x -q
```

> **Note**: Always use `uv run pytest` — do not use `python -m pytest` or activate a `.venv` manually.

## 📋 Known Limitations

- Primarily tested on Windows (macOS/Linux should work but is less tested)
- Duplicate workspace entries when the same project is edited by Claude Code + a VS Code-based agent
- Claude Code session names are GUIDs (no human-readable titles available in the source data)
- AI agent local storage formats are undocumented and can change between releases — extractors are best-effort and may need updates


## 🤝 Contributing

Contributions welcome. If you notice missing or misaligned data from an agent, open an issue — extractor improvements are especially valuable.

## 📄 License

MIT — free to use, copy, modify, and redistribute.

---

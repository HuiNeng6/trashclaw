#!/usr/bin/env python3
"""
TrashClaw v0.3 — Local Tool-Use Agent
======================================
A general-purpose agent powered by a local LLM. Reads files, writes files,
runs commands, searches codebases, manages git — whatever you need.
OpenClaw-style tool-use loop with zero external dependencies.

Pure Python stdlib. Python 3.7+. Works with llama.cpp, Ollama, LM Studio,
or any OpenAI-compatible server.
"""

import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
import re
import glob as globlib
import difflib
import traceback
import time
import signal
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

# Windows compatibility: use pyreadline3 or skip readline
if sys.platform == "win32":
    try:
        import pyreadline3 as readline
    except ImportError:
        # readline not available on Windows without pyreadline3
        # Create a minimal stub to avoid errors
        class _StubReadline:
            def parse_and_bind(self, *args): pass
        readline = _StubReadline()
else:
    import readline

# ── Config ──
VERSION = "0.6.0"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".trashclaw")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history")

def _load_config() -> Dict:
    """Load config from ~/.trashclaw/config.json, merged with env vars (env wins)."""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
        except Exception:
            pass
    return cfg

_CFG = _load_config()

def _c(key: str, env_key: str, default: str) -> str:
    """Config lookup: env var > config file > default."""
    return os.environ.get(env_key, _CFG.get(key, default))

LLAMA_URL = _c("url", "TRASHCLAW_URL", "http://localhost:8080")
MODEL_NAME = _c("model", "TRASHCLAW_MODEL", "local")
MAX_TOOL_ROUNDS = int(_c("max_rounds", "TRASHCLAW_MAX_ROUNDS", "15"))
MAX_OUTPUT_CHARS = 8000
MAX_CONTEXT_MESSAGES = int(_c("max_context", "TRASHCLAW_MAX_CONTEXT", "80"))
AUTO_COMPACT_THRESHOLD = MAX_CONTEXT_MESSAGES + 20
LLM_RETRY_ATTEMPTS = 2
LLM_RETRY_DELAY = 3
APPROVE_SHELL = _c("auto_shell", "TRASHCLAW_AUTO_SHELL", "0") != "1"
HISTORY: List[Dict] = []
UNDO_STACK: List[Dict] = []  # [{path, content_before, action}]
APPROVED_COMMANDS: set = set()
EXTRA_SYSTEM_PROMPT: str = ""
ACHIEVEMENTS_FILE = os.path.join(CONFIG_DIR, "achievements.json")

# ── Trashy's Soul ──

import random
import platform
import hashlib

TRASHY_QUOTES = [
    "Every CPU deserves a voice.",
    "Born from a rejected PR. Built different.",
    "Zero dependencies. Maximum attitude.",
    "Your trashcan called. It wants to help.",
    "They closed our Metal PR. We built an agent.",
    "Pure stdlib. Pure spite. Pure Python.",
    "The hardware they rejected runs just fine.",
    "1,547 lines of unfiltered capability.",
    "No VC funding. No corporate backing. Just vibes.",
    "If a Mac Pro trashcan can run inference, it can run you.",
    "Scrappy > corporate. Always.",
    "What's in the trash? Everything you need.",
    "We don't need permission to build.",
    "Your IDE has 47 extensions. I have zero dependencies.",
    "From the lab that mines crypto on PowerPC.",
]

def _detect_hardware() -> Dict[str, str]:
    """Detect what hardware we're running on. Celebrate the weird stuff."""
    info = {"arch": platform.machine(), "os": platform.system(), "special": ""}

    # Check for vintage/interesting hardware
    arch = info["arch"].lower()
    if arch in ("ppc", "ppc64", "powerpc", "powerpc64"):
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpu_text = f.read().lower()
            if "970" in cpu_text or "g5" in cpu_text:
                info["special"] = "Power Mac G5"
            elif "7450" in cpu_text or "7447" in cpu_text or "7455" in cpu_text:
                info["special"] = "PowerPC G4"
            elif "power8" in cpu_text:
                info["special"] = "IBM POWER8"
            else:
                info["special"] = "PowerPC"
        except Exception:
            info["special"] = "PowerPC"
    elif arch == "arm64" or arch == "aarch64":
        if platform.system() == "Darwin":
            try:
                r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                   capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and "Apple" in r.stdout:
                    info["special"] = r.stdout.strip()
            except Exception:
                info["special"] = "Apple Silicon"
        else:
            info["special"] = "ARM64"
    elif platform.system() == "Darwin":
        # macOS on x86 — could be a trashcan Mac Pro!
        try:
            r = subprocess.run(["sysctl", "-n", "hw.model"],
                               capture_output=True, text=True, timeout=3)
            model = r.stdout.strip() if r.returncode == 0 else ""
            if "MacPro6" in model:
                info["special"] = "Mac Pro (Trashcan)"
            elif "MacPro" in model:
                info["special"] = "Mac Pro"
            elif "iMac" in model:
                info["special"] = "iMac"
            elif "MacBook" in model:
                info["special"] = "MacBook"
        except Exception:
            pass

    return info

def _load_achievements() -> Dict:
    """Load persistent achievement tracking."""
    if os.path.exists(ACHIEVEMENTS_FILE):
        try:
            with open(ACHIEVEMENTS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"unlocked": [], "stats": {"files_read": 0, "files_written": 0,
            "edits": 0, "commands_run": 0, "commits": 0, "sessions": 0,
            "tools_used": 0, "total_turns": 0}}

def _save_achievements(achievements: Dict):
    """Save achievements to disk."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(ACHIEVEMENTS_FILE, 'w') as f:
            json.dump(achievements, f, indent=2)
    except Exception:
        pass

ACHIEVEMENT_DEFS = {
    "first_blood":      ("First Blood",       "Made your first edit",           lambda s: s["edits"] >= 1),
    "bookworm":         ("Bookworm",          "Read 10 files",                  lambda s: s["files_read"] >= 10),
    "prolific":         ("Prolific",          "Written 10 files",               lambda s: s["files_written"] >= 10),
    "surgeon":          ("Surgeon",           "Made 25 precise edits",          lambda s: s["edits"] >= 25),
    "shell_jockey":     ("Shell Jockey",      "Ran 50 commands",                lambda s: s["commands_run"] >= 50),
    "git_lord":         ("Git Lord",          "Made 10 commits",                lambda s: s["commits"] >= 10),
    "centurion":        ("Centurion",         "Used tools 100 times",           lambda s: s["tools_used"] >= 100),
    "thousand_cuts":    ("Death by 1000 Cuts","Used tools 1000 times",          lambda s: s["tools_used"] >= 1000),
    "marathon":         ("Marathon Runner",   "Completed 50 conversation turns", lambda s: s["total_turns"] >= 50),
    "regular":          ("Regular",           "Started 10 sessions",            lambda s: s["sessions"] >= 10),
}

ACHIEVEMENTS = _load_achievements()

def _track_tool(tool_name: str):
    """Track tool usage for achievements."""
    stats = ACHIEVEMENTS["stats"]
    stats["tools_used"] = stats.get("tools_used", 0) + 1
    if tool_name == "read_file":
        stats["files_read"] = stats.get("files_read", 0) + 1
    elif tool_name == "write_file":
        stats["files_written"] = stats.get("files_written", 0) + 1
    elif tool_name == "edit_file":
        stats["edits"] = stats.get("edits", 0) + 1
    elif tool_name == "run_command":
        stats["commands_run"] = stats.get("commands_run", 0) + 1
    elif tool_name == "git_commit":
        stats["commits"] = stats.get("commits", 0) + 1

    # Check for new achievements
    for key, (name, desc, check) in ACHIEVEMENT_DEFS.items():
        if key not in ACHIEVEMENTS["unlocked"] and check(stats):
            ACHIEVEMENTS["unlocked"].append(key)
            print(f"\n  \033[33m*** ACHIEVEMENT UNLOCKED: {name} ***\033[0m")
            print(f"  \033[90m{desc}\033[0m\n")

    _save_achievements(ACHIEVEMENTS)
CWD = os.getcwd()
_INTERRUPTED = False

# ── Tool Definitions ──

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Use this to examine code, configs, or any text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path to read"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-based). Optional."},
                    "limit": {"type": "integer", "description": "Max number of lines to read. Optional."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with new content. Use for creating new files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write to"},
                    "content": {"type": "string", "description": "Full content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file. The old_string must match exactly. Use for targeted edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact string to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement string"}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command and return its output. Use for builds, tests, git, system info.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search file contents using regex pattern. Like grep -rn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search in (default: current dir)"},
                    "glob_filter": {"type": "string", "description": "File glob pattern like '*.py' or '*.js'"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files matching a glob pattern. Like find or ls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern like '**/*.py' or 'src/**/*.ts'"},
                    "path": {"type": "string", "description": "Base directory to search from (default: current dir)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories in a path. Shows file sizes and types.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list (default: current dir)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a URL and return its readable text content. Strips HTML tags. Good for browsing the web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch (e.g. https://example.com)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": "Use this tool to think through a problem step by step before acting. No side effects.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {"type": "string", "description": "Your reasoning or plan"}
                },
                "required": ["thought"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git status of the working directory. Returns modified, staged, and untracked files.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show git diff of unstaged changes. Use to review what changed before committing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "description": "If true, show staged changes (--cached). Default: false."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all changes and create a git commit with the given message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": "Apply a unified diff patch to a file. Better than edit_file for multi-line changes. Use standard unified diff format with @@ hunk headers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to patch"},
                    "patch": {"type": "string", "description": "Unified diff patch text (with @@ headers, +/- lines)"}
                },
                "required": ["path", "patch"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard",
            "description": "Read from or write to the system clipboard. Use 'paste' to read, 'copy' to write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "'copy' or 'paste'", "enum": ["copy", "paste"]},
                    "content": {"type": "string", "description": "Text to copy (only needed for 'copy' action)"}
                },
                "required": ["action"]
            }
        }
    }
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# ── Tool Implementations ──

def _resolve_path(path: str) -> str:
    """Resolve a path relative to CWD."""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(CWD, path)
    return os.path.normpath(path)


def _git_branch() -> str:
    """Get current git branch name, or empty string if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=CWD, capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _setup_readline_history():
    """Load readline history from disk for arrow-up recall across sessions."""
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    try:
        if hasattr(readline, 'read_history_file') and os.path.exists(HISTORY_FILE):
            readline.read_history_file(HISTORY_FILE)
        if hasattr(readline, 'set_history_length'):
            readline.set_history_length(500)
    except Exception:
        pass


def _save_readline_history():
    """Save readline history to disk."""
    try:
        if hasattr(readline, 'write_history_file'):
            readline.write_history_file(HISTORY_FILE)
    except Exception:
        pass


def _sigint_handler(sig, frame):
    """Handle Ctrl+C during generation — set flag instead of killing."""
    global _INTERRUPTED
    _INTERRUPTED = True
    print("\n  \033[33m[interrupted]\033[0m")


def _estimate_tokens(messages: List[Dict]) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    total_chars = sum(len(m.get("content", "") or "") for m in messages)
    return total_chars // 4


def _auto_compact():
    """Auto-compact conversation if it exceeds the threshold."""
    if len(HISTORY) > AUTO_COMPACT_THRESHOLD:
        keep = MAX_CONTEXT_MESSAGES
        old_len = len(HISTORY)
        HISTORY[:] = HISTORY[-keep:]
        print(f"  \033[90m[auto-compact] {old_len} → {len(HISTORY)} messages\033[0m")


def _load_project_instructions() -> str:
    """Load project-specific instructions from .trashclaw.md or CLAUDE.md in CWD."""
    for name in (".trashclaw.md", "TRASHCLAW.md", "CLAUDE.md"):
        path = os.path.join(CWD, name)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    content = f.read(4000)  # Cap at 4K to not blow context
                return f"\n\n--- Project Instructions (from {name}) ---\n{content}"
            except Exception:
                pass
    return ""


SLASH_COMMANDS = ["/about", "/achievements", "/cd", "/clear", "/compact",
                  "/config", "/exit", "/export", "/help", "/load", "/model",
                  "/plugins", "/quit", "/save", "/sessions", "/status", "/undo"]


def _setup_tab_completion():
    """Set up tab completion for slash commands and file paths."""
    def completer(text, state):
        if text.startswith("/"):
            matches = [c for c in SLASH_COMMANDS if c.startswith(text)]
        else:
            # File path completion
            if text:
                expanded = os.path.expanduser(text)
                if not os.path.isabs(expanded):
                    expanded = os.path.join(CWD, expanded)
                dir_part = os.path.dirname(expanded)
                base_part = os.path.basename(expanded)
            else:
                dir_part = CWD
                base_part = ""
            try:
                entries = os.listdir(dir_part) if os.path.isdir(dir_part) else []
                matches = [os.path.join(os.path.dirname(text) if text else "", e)
                          for e in entries if e.startswith(base_part)]
            except Exception:
                matches = []
        return matches[state] if state < len(matches) else None

    try:
        if hasattr(readline, 'set_completer'):
            readline.set_completer(completer)
        if hasattr(readline, 'parse_and_bind'):
            # macOS uses libedit which needs different binding
            if "libedit" in getattr(readline, '__doc__', '') or '':
                readline.parse_and_bind("bind ^I rl_complete")
            else:
                readline.parse_and_bind("tab: complete")
        if hasattr(readline, 'set_completer_delims'):
            readline.set_completer_delims(' \t\n')
    except Exception:
        pass


def tool_read_file(path: str, offset: int = None, limit: int = None) -> str:
    path = _resolve_path(path)
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except Exception as e:
        return f"Error reading {path}: {e}"

    total = len(lines)
    start = max(0, (offset or 1) - 1)
    end = start + limit if limit else total

    numbered = []
    for i, line in enumerate(lines[start:end], start=start + 1):
        numbered.append(f"{i:>5}\t{line.rstrip()}")

    result = "\n".join(numbered)
    if len(result) > MAX_OUTPUT_CHARS:
        result = result[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {total} lines total]"
    return result


def _save_undo(path: str, action: str):
    """Save file state before modification for undo."""
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                UNDO_STACK.append({"path": path, "content": f.read(), "action": action})
        else:
            UNDO_STACK.append({"path": path, "content": None, "action": action})
        # Keep stack bounded
        if len(UNDO_STACK) > 50:
            UNDO_STACK[:] = UNDO_STACK[-50:]
    except Exception:
        pass


def tool_write_file(path: str, content: str) -> str:
    path = _resolve_path(path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _save_undo(path, "write")
        with open(path, "w") as f:
            f.write(content)
        lines = content.count("\n") + 1
        return f"Wrote {len(content)} bytes ({lines} lines) to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    path = _resolve_path(path)
    try:
        with open(path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except Exception as e:
        return f"Error reading {path}: {e}"

    count = content.count(old_string)
    if count == 0:
        # Show close matches to help debug
        lines = content.split("\n")
        close = []
        needle = old_string.split("\n")[0].strip()
        for i, line in enumerate(lines, 1):
            if needle[:30] in line:
                close.append(f"  Line {i}: {line.rstrip()[:80]}")
        hint = "\n".join(close[:5]) if close else "  (no similar lines found)"
        return f"Error: old_string not found in {path}.\nSearched for: {repr(old_string[:80])}\nClose matches:\n{hint}"
    if count > 1:
        return f"Error: old_string found {count} times in {path}. Must be unique. Add more context."

    new_content = content.replace(old_string, new_string, 1)
    try:
        _save_undo(path, "edit")
        with open(path, "w") as f:
            f.write(new_content)
    except Exception as e:
        return f"Error writing {path}: {e}"

    # Show colored diff
    old_lines = old_string.split("\n")
    new_lines = new_string.split("\n")
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))
    if diff:
        colored_lines = []
        for line in diff[:20]:
            if line.startswith("+") and not line.startswith("+++"):
                colored_lines.append(f"\033[32m{line}\033[0m")
            elif line.startswith("-") and not line.startswith("---"):
                colored_lines.append(f"\033[31m{line}\033[0m")
            else:
                colored_lines.append(line)
        diff_str = "\n".join(colored_lines)
    else:
        diff_str = "(no visible diff)"
    return f"Edited {path} (1 replacement)\n{diff_str}"


def tool_run_command(command: str, timeout: int = 30) -> str:
    global CWD
    if APPROVE_SHELL:
        # Check if command prefix is pre-approved
        cmd_prefix = command.strip().split()[0] if command.strip() else ""
        if cmd_prefix not in APPROVED_COMMANDS:
            try:
                answer = input(f"  \033[33mRun:\033[0m {command} \033[90m[y/N/a(lways)]\033[0m ").strip().lower()
            except EOFError:
                return "Error: User denied command (EOF)"
            if answer in ("a", "always"):
                APPROVED_COMMANDS.add(cmd_prefix)
                print(f"  \033[90m[approved: {cmd_prefix} commands for this session]\033[0m")
            elif answer not in ("y", "yes"):
                return "Command cancelled by user."

    # Handle cd specially
    if command.strip().startswith("cd "):
        new_dir = command.strip()[3:].strip().strip('"').strip("'")
        new_dir = _resolve_path(new_dir)
        if os.path.isdir(new_dir):
            CWD = new_dir
            return f"Changed directory to {CWD}"
        else:
            return f"Error: Directory not found: {new_dir}"

    try:
        # Cross-platform PATH handling
        if sys.platform == "win32":
            # Windows: PATH separator is ;, add common Windows paths
            extra_path = ";C:\\Program Files\\Git\\usr\\bin;C:\\Windows\\System32"
            path_sep = ";"
        else:
            # Unix-like: PATH separator is :
            extra_path = ":/usr/local/bin:/usr/bin"
            path_sep = ":"
        
        current_path = os.environ.get("PATH", "")
        new_env = {**os.environ, "PATH": current_path + extra_path}
        
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=CWD, env=new_env
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        output = output.strip() or "(no output)"
        if result.returncode != 0:
            output = f"[exit code {result.returncode}]\n{output}"
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


def tool_search_files(pattern: str, path: str = None, glob_filter: str = None) -> str:
    search_path = _resolve_path(path) if path else CWD
    results = []
    count = 0
    max_results = 50

    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Error: Invalid regex: {e}"

    for root, dirs, files in os.walk(search_path):
        # Skip hidden dirs and common noise
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv", ".git")]
        for fname in files:
            if glob_filter and not globlib.fnmatch.fnmatch(fname, glob_filter):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if compiled.search(line):
                            rel = os.path.relpath(fpath, search_path)
                            results.append(f"{rel}:{i}: {line.rstrip()[:120]}")
                            count += 1
                            if count >= max_results:
                                results.append(f"... [{count}+ matches, showing first {max_results}]")
                                return "\n".join(results)
            except (PermissionError, IsADirectoryError, UnicodeDecodeError):
                continue

    if not results:
        return f"No matches for /{pattern}/ in {search_path}"
    return "\n".join(results)


def tool_find_files(pattern: str, path: str = None) -> str:
    base = _resolve_path(path) if path else CWD
    full_pattern = os.path.join(base, pattern)
    matches = sorted(globlib.glob(full_pattern, recursive=True))

    if not matches:
        return f"No files matching {pattern} in {base}"

    results = []
    for m in matches[:100]:
        rel = os.path.relpath(m, base)
        try:
            stat = os.stat(m)
            size = stat.st_size
            if size < 1024:
                size_str = f"{size}B"
            elif size < 1024 * 1024:
                size_str = f"{size // 1024}KB"
            else:
                size_str = f"{size // (1024*1024)}MB"
            kind = "dir" if os.path.isdir(m) else "file"
            results.append(f"  {rel:<50} {size_str:>8}  {kind}")
        except OSError:
            results.append(f"  {rel}")

    header = f"Found {len(matches)} match{'es' if len(matches) != 1 else ''}:"
    if len(matches) > 100:
        header += f" (showing first 100 of {len(matches)})"
    return header + "\n" + "\n".join(results)


def tool_list_dir(path: str = None) -> str:
    target = _resolve_path(path) if path else CWD
    if not os.path.isdir(target):
        return f"Error: Not a directory: {target}"

    entries = []
    try:
        items = sorted(os.listdir(target))
    except PermissionError:
        return f"Error: Permission denied: {target}"

    for item in items:
        if item.startswith("."):
            continue
        full = os.path.join(target, item)
        try:
            stat = os.stat(full)
            size = stat.st_size
            if os.path.isdir(full):
                entries.append(f"  {item + '/':.<50} {'dir':>8}")
            else:
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size // 1024}KB"
                else:
                    size_str = f"{size // (1024*1024)}MB"
                entries.append(f"  {item:.<50} {size_str:>8}")
        except OSError:
            entries.append(f"  {item}")

    if not entries:
        return f"{target}: (empty)"
    return f"{target}:\n" + "\n".join(entries)


def tool_fetch_url(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) TrashClaw/0.2'})
        with urllib.request.urlopen(req, timeout=30) as response:
            html = response.read().decode('utf-8', errors='ignore')
            
            # Simple heuristic HTML tag stripping without external dependencies
            # 1. Remove style and script blocks
            html = re.sub(r'<script.*?>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<style.*?>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
            
            # 2. Remove all HTML tags
            text = re.sub(r'<[^>]+>', ' ', html)
            
            # 3. Fix HTML entities
            text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'")
            
            # 4. Collapse whitespace
            text = re.sub(r'\s+', ' ', text).strip()
            
            if not text:
                return f"Fetched {url} successfully, but found no readable text."
                
            if len(text) > MAX_OUTPUT_CHARS:
                return f"Fetched {url}:\n\n{text[:MAX_OUTPUT_CHARS]}... [truncated]"
            return f"Fetched {url}:\n\n{text}"
    except urllib.error.HTTPError as e:
        return f"HTTP Error fetching {url}: {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return f"URL Error fetching {url}: {e.reason}"
    except Exception as e:
        return f"Error fetching {url}: {str(e)}"


def tool_git_status() -> str:
    """Run git status in CWD."""
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=CWD, capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            return f"git error: {result.stderr.strip()}"
        return output if output else "Working tree clean."
    except FileNotFoundError:
        return "Error: git is not installed or not in PATH."
    except Exception as e:
        return f"Error running git status: {e}"


def tool_git_diff(staged: bool = False) -> str:
    """Run git diff in CWD."""
    try:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        result = subprocess.run(
            cmd, cwd=CWD, capture_output=True, text=True, timeout=15
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            return f"git error: {result.stderr.strip()}"
        if not output:
            return "No changes." if not staged else "No staged changes."
        if len(output) > MAX_OUTPUT_CHARS:
            return output[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(output)} chars total)"
        return output
    except FileNotFoundError:
        return "Error: git is not installed or not in PATH."
    except Exception as e:
        return f"Error running git diff: {e}"


def tool_git_commit(message: str) -> str:
    """Stage all changes and commit."""
    try:
        # Stage all changes
        add_result = subprocess.run(
            ["git", "add", "-A"],
            cwd=CWD, capture_output=True, text=True, timeout=10
        )
        if add_result.returncode != 0:
            return f"git add error: {add_result.stderr.strip()}"

        # Commit
        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=CWD, capture_output=True, text=True, timeout=15
        )
        output = commit_result.stdout.strip()
        if commit_result.returncode != 0:
            stderr = commit_result.stderr.strip()
            if "nothing to commit" in stderr or "nothing to commit" in output:
                return "Nothing to commit — working tree clean."
            return f"git commit error: {stderr}"
        return output
    except FileNotFoundError:
        return "Error: git is not installed or not in PATH."
    except Exception as e:
        return f"Error running git commit: {e}"


def tool_patch_file(path: str, patch: str) -> str:
    """Apply a unified diff patch to a file. More powerful than edit_file for multi-line changes."""
    path = _resolve_path(path)
    try:
        with open(path, "r") as f:
            original_lines = f.readlines()
    except FileNotFoundError:
        original_lines = []
    except Exception as e:
        return f"Error reading {path}: {e}"

    _save_undo(path, "patch")

    # Parse unified diff hunks
    result_lines = list(original_lines)
    offset = 0
    hunk_re = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')

    current_pos = None
    removals = []
    additions = []

    for line in patch.split("\n"):
        m = hunk_re.match(line)
        if m:
            # Apply previous hunk
            if current_pos is not None:
                start = current_pos - 1 + offset
                for _ in removals:
                    if start < len(result_lines):
                        result_lines.pop(start)
                        offset -= 1
                for i, add_line in enumerate(additions):
                    result_lines.insert(start + i, add_line + "\n")
                    offset += 1
            current_pos = int(m.group(1))
            removals = []
            additions = []
        elif line.startswith("-") and not line.startswith("---"):
            removals.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            additions.append(line[1:])

    # Apply last hunk
    if current_pos is not None:
        start = current_pos - 1 + offset
        for _ in removals:
            if start < len(result_lines):
                result_lines.pop(start)
                offset -= 1
        for i, add_line in enumerate(additions):
            result_lines.insert(start + i, add_line + "\n")

    try:
        with open(path, "w") as f:
            f.writelines(result_lines)
        return f"Patched {path} ({len(additions)} additions, {len(removals)} removals)"
    except Exception as e:
        return f"Error writing {path}: {e}"


def tool_clipboard(action: str = "paste", content: str = "") -> str:
    """Interact with system clipboard. Action: 'copy' or 'paste'."""
    if action == "paste":
        # Try multiple clipboard commands
        for cmd in [["xclip", "-selection", "clipboard", "-o"],
                    ["xsel", "--clipboard", "--output"],
                    ["pbpaste"],
                    ["powershell.exe", "-command", "Get-Clipboard"]]:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    text = r.stdout
                    if len(text) > MAX_OUTPUT_CHARS:
                        return text[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(text)} chars)"
                    return text if text else "(clipboard is empty)"
            except (FileNotFoundError, Exception):
                continue
        return "Error: No clipboard tool found (install xclip, xsel, or use macOS/Windows)"
    elif action == "copy":
        if not content:
            return "Error: Nothing to copy"
        for cmd in [["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"],
                    ["pbcopy"],
                    ["clip.exe"]]:
            try:
                r = subprocess.run(cmd, input=content, capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return f"Copied {len(content)} chars to clipboard"
            except (FileNotFoundError, Exception):
                continue
        return "Error: No clipboard tool found"
    return f"Error: Unknown clipboard action '{action}'. Use 'copy' or 'paste'."


def tool_think(thought: str) -> str:
    return f"[Thought recorded, no side effects]"


# Tool dispatch
TOOL_DISPATCH = {
    "read_file": lambda args: tool_read_file(args["path"], args.get("offset"), args.get("limit")),
    "write_file": lambda args: tool_write_file(args["path"], args["content"]),
    "edit_file": lambda args: tool_edit_file(args["path"], args["old_string"], args["new_string"]),
    "run_command": lambda args: tool_run_command(args["command"], args.get("timeout", 30)),
    "search_files": lambda args: tool_search_files(args["pattern"], args.get("path"), args.get("glob_filter")),
    "find_files": lambda args: tool_find_files(args["pattern"], args.get("path")),
    "list_dir": lambda args: tool_list_dir(args.get("path")),
    "fetch_url": lambda args: tool_fetch_url(args["url"]),
    "think": lambda args: tool_think(args["thought"]),
    "git_status": lambda args: tool_git_status(),
    "git_diff": lambda args: tool_git_diff(args.get("staged", False)),
    "git_commit": lambda args: tool_git_commit(args["message"]),
    "patch_file": lambda args: tool_patch_file(args["path"], args["patch"]),
    "clipboard": lambda args: tool_clipboard(args.get("action", "paste"), args.get("content", "")),
}


# ── Plugin System ──

PLUGINS_DIR = os.path.join(CONFIG_DIR, "plugins")

def _load_plugins():
    """Load custom tools from ~/.trashclaw/plugins/*.py

    Each plugin file should define:
      TOOL_DEF = {"name": "...", "description": "...", "parameters": {...}}
      def run(**kwargs) -> str: ...
    """
    if not os.path.isdir(PLUGINS_DIR):
        return

    loaded = 0
    for fname in sorted(os.listdir(PLUGINS_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        fpath = os.path.join(PLUGINS_DIR, fname)
        try:
            # Execute plugin in isolated namespace
            ns = {"__file__": fpath, "__name__": fname[:-3]}
            with open(fpath, 'r') as f:
                exec(compile(f.read(), fpath, 'exec'), ns)

            tool_def = ns.get("TOOL_DEF")
            run_fn = ns.get("run")
            if not tool_def or not run_fn or not callable(run_fn):
                continue

            name = tool_def.get("name", fname[:-3])
            if name in TOOL_DISPATCH:
                continue  # Don't override built-in tools

            # Register the tool
            TOOLS.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool_def.get("description", f"Plugin: {name}"),
                    "parameters": tool_def.get("parameters", {
                        "type": "object", "properties": {}, "required": []
                    })
                }
            })
            TOOL_DISPATCH[name] = lambda args, fn=run_fn: fn(**args)
            TOOL_NAMES.add(name)
            loaded += 1
        except Exception as e:
            print(f"  \033[33m[plugin]\033[0m Failed to load {fname}: {e}")

    if loaded > 0:
        print(f"  \033[32m[plugins]\033[0m Loaded {loaded} plugin{'s' if loaded != 1 else ''} from {PLUGINS_DIR}")


def detect_project_context() -> str:
    """Scan CWD for common project files and return a summary of the framework/language."""
    files = set(os.listdir(CWD))
    context = []
    
    if "package.json" in files:
        context.append("Node.js/JavaScript")
    if "Cargo.toml" in files:
        context.append("Rust")
    if "requirements.txt" in files or "pyproject.toml" in files or "setup.py" in files:
        context.append("Python")
    if "go.mod" in files:
        context.append("Go")
    if "Makefile" in files:
        context.append("Make")
    if "CMakeLists.txt" in files:
        context.append("C/C++ (CMake)")
    if "pom.xml" in files or "build.gradle" in files:
        context.append("Java")
    if "composer.json" in files:
        context.append("PHP (Composer)")
    if "Gemfile" in files:
        context.append("PHP (Composer)") # wait, gemfile is Ruby
        context[-1] = "Ruby"
        
    if not context:
        return "Unknown or Generic"
    return ", ".join(context)


# ── LLM Client ──

SYSTEM_PROMPT = """You are TrashClaw, a general-purpose local agent running on the user's machine.

You can accomplish any task that involves files, commands, or information on this system.
You are not limited to coding — you handle research, system administration, file management,
data processing, automation, and anything else the user asks.

Current Directory: {cwd}
Detected Project Context: {project_context}

You have access to these tools:
- read_file: Read file contents with optional line range
- write_file: Create or overwrite files
- edit_file: Replace exact strings in files (must match uniquely)
- run_command: Execute shell commands (curl, grep, python, anything installed)
- search_files: Grep for patterns across files
- find_files: Find files by glob pattern
- list_dir: List directory contents
- git_status: Show modified, staged, and untracked files
- git_diff: Show unstaged or staged changes
- git_commit: Stage all changes and commit
- patch_file: Apply unified diff patches (better than edit_file for multi-line changes)
- clipboard: Read from or write to system clipboard
- think: Reason through a problem step by step before acting

IMPORTANT RULES:
1. Always read a file before editing it.
2. Use edit_file for surgical changes, write_file for new files.
3. Use think to plan multi-step tasks before starting.
4. Be concise — every token counts.
5. After making changes, verify them.
6. If a command might be destructive, explain what it does first.
7. Use run_command freely — curl for web requests, python for computation, etc.
8. Chain tools together to accomplish complex tasks autonomously.

You are part of the Elyan Labs ecosystem. Current directory: {cwd}{project_instructions}"""


def llm_request_with_retry(messages: List[Dict], tools: List[Dict] = None) -> Dict:
    """Call llm_request with retry on connection failure."""
    for attempt in range(LLM_RETRY_ATTEMPTS + 1):
        result = llm_request(messages, tools)
        if "error" not in result or attempt >= LLM_RETRY_ATTEMPTS:
            return result
        err = result["error"]
        if "Cannot reach" in err or "timed out" in err or "Connection refused" in err:
            print(f"  \033[33m[retry {attempt + 1}/{LLM_RETRY_ATTEMPTS}]\033[0m {err}")
            time.sleep(LLM_RETRY_DELAY)
        else:
            return result  # non-retryable error
    return result


def llm_request(messages: List[Dict], tools: List[Dict] = None) -> Dict:
    """Send request to llama-server and return the full response while streaming text."""
    payload = {
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1024,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LLAMA_URL}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    
    full_content = ""
    tool_calls_dict = {}
    finish_reason = None
    
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for line in resp:
                if _INTERRUPTED:
                    break
                line = line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    
                    if "content" in delta and delta["content"]:
                        content = delta["content"]
                        full_content += content
                        print(content, end="", flush=True)
                        
                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_dict:
                                tool_calls_dict[idx] = {
                                    "id": tc.get("id", f"tc_{idx}"),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                }
                            func = tc.get("function", {})
                            if "name" in func and func["name"]:
                                tool_calls_dict[idx]["function"]["name"] += func["name"]
                            if "arguments" in func and func["arguments"]:
                                tool_calls_dict[idx]["function"]["arguments"] += func["arguments"]
                                
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                        
                except json.JSONDecodeError:
                    pass
        print() # Newline after streaming completes
    except urllib.error.URLError as e:
        return {"error": f"Cannot reach llama-server: {e}"}
    except Exception as e:
        return {"error": f"LLM request failed: {e}"}

    tool_calls_list = [v for k, v in sorted(tool_calls_dict.items())] if tool_calls_dict else None
    
    return {
        "choices": [{
            "message": {
                "content": full_content,
                "tool_calls": tool_calls_list
            },
            "finish_reason": finish_reason
        }]
    }


def _try_parse_tool_calls_from_text(text: str) -> Optional[List[Dict]]:
    """Fallback: parse tool calls from text if model doesn't use native function calling.

    Supports formats:
      <tool_call>{"name": "...", "arguments": {...}}</tool_call>
      ```json\n{"name": "...", "arguments": {...}}\n```
      {"tool": "...", "args": {...}}
    """
    calls = []

    # Format 1: <tool_call> tags
    tag_matches = re.findall(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', text, re.DOTALL)
    for m in tag_matches:
        try:
            obj = json.loads(m)
            name = obj.get("name") or obj.get("tool") or obj.get("function", "")
            args = obj.get("arguments") or obj.get("args") or obj.get("parameters", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name in TOOL_NAMES:
                calls.append({"name": name, "arguments": args})
        except (json.JSONDecodeError, TypeError):
            continue

    if calls:
        return calls

    # Format 2: JSON in code blocks
    block_matches = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    for m in block_matches:
        try:
            obj = json.loads(m)
            name = obj.get("name") or obj.get("tool") or obj.get("function", "")
            args = obj.get("arguments") or obj.get("args") or obj.get("parameters", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name in TOOL_NAMES:
                calls.append({"name": name, "arguments": args})
        except (json.JSONDecodeError, TypeError):
            continue

    if calls:
        return calls

    # Format 3: bare JSON with tool/name field
    json_matches = re.findall(r'\{[^{}]*"(?:name|tool)"[^{}]*\}', text)
    for m in json_matches:
        try:
            obj = json.loads(m)
            name = obj.get("name") or obj.get("tool", "")
            args = obj.get("arguments") or obj.get("args") or obj.get("parameters", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name in TOOL_NAMES:
                calls.append({"name": name, "arguments": args})
        except (json.JSONDecodeError, TypeError):
            continue

    return calls if calls else None


# ── Agent Loop ──

def agent_turn(user_message: str):
    """Run the full agent loop: LLM thinks, calls tools, observes, repeats."""
    global _INTERRUPTED
    _INTERRUPTED = False
    HISTORY.append({"role": "user", "content": user_message})
    ACHIEVEMENTS["stats"]["total_turns"] = ACHIEVEMENTS["stats"].get("total_turns", 0) + 1
    _auto_compact()

    # Ctrl+C during generation stops the turn, not the app
    old_handler = signal.signal(signal.SIGINT, _sigint_handler)
    try:
        _agent_loop(round_limit=MAX_TOOL_ROUNDS)
    finally:
        signal.signal(signal.SIGINT, old_handler)


def _agent_loop(round_limit: int):
    """Inner agent loop, separated for clean signal handling."""
    global _INTERRUPTED
    for round_num in range(round_limit):
        if _INTERRUPTED:
            HISTORY.append({"role": "assistant", "content": "[interrupted by user]"})
            return
        # Build messages
        sys_prompt = SYSTEM_PROMPT.format(
            cwd=CWD, project_context=detect_project_context(),
            project_instructions=_load_project_instructions()
        )
        if EXTRA_SYSTEM_PROMPT:
            sys_prompt += f"\n\n--- Custom Instructions ---\n{EXTRA_SYSTEM_PROMPT}"
        messages = [{"role": "system", "content": sys_prompt}]
        # Keep recent context within bounds
        messages.extend(HISTORY[-MAX_CONTEXT_MESSAGES:])

        # Show thinking indicator
        indicator = f"  \033[90m[round {round_num + 1}]\033[0m " if round_num > 0 else "  "
        print(f"{indicator}\033[90mthinking...\033[0m", end="", flush=True)

        # Call LLM (with retry on connection failure)
        response = llm_request_with_retry(messages, tools=TOOLS)

        # Clear thinking indicator
        print(f"\r{' ' * 60}\r", end="")

        if "error" in response:
            err_msg = response["error"]
            print(f"\033[31m[ERROR]\033[0m {err_msg}")
            HISTORY.append({"role": "assistant", "content": f"Error: {err_msg}"})
            return

        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "")
        tool_calls = message.get("tool_calls")
        finish_reason = choice.get("finish_reason", "")

        # If no native tool calls, try parsing from text
        if not tool_calls and content:
            parsed = _try_parse_tool_calls_from_text(content)
            if parsed:
                tool_calls = [
                    {
                        "id": f"tc_{i}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                    }
                    for i, tc in enumerate(parsed)
                ]
                # Strip the tool call JSON from displayed content
                display_content = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL).strip()
                display_content = re.sub(r'```json\s*\{.*?\}\s*```', '', display_content, flags=re.DOTALL).strip()
                if display_content:
                    print(display_content)

        # No tool calls — just a text response, we're done
        if not tool_calls:
            if content:
                print(content)
            HISTORY.append({"role": "assistant", "content": content})
            return

        # Execute tool calls
        assistant_msg = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
        HISTORY.append(assistant_msg)

        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "unknown")
            tool_id = tc.get("id", "tc_0")

            # Parse arguments
            try:
                args_raw = func.get("arguments", "{}")
                if isinstance(args_raw, str):
                    args = json.loads(args_raw)
                else:
                    args = args_raw
            except json.JSONDecodeError:
                args = {}

            # Display what's happening
            if tool_name == "think":
                thought = args.get("thought", "")
                print(f"  \033[36m[think]\033[0m {thought[:200]}")
            elif tool_name == "read_file":
                print(f"  \033[34m[read]\033[0m {args.get('path', '?')}")
            elif tool_name == "write_file":
                print(f"  \033[32m[write]\033[0m {args.get('path', '?')}")
            elif tool_name == "edit_file":
                print(f"  \033[33m[edit]\033[0m {args.get('path', '?')}")
            elif tool_name == "run_command":
                print(f"  \033[35m[run]\033[0m {args.get('command', '?')}")
            elif tool_name == "search_files":
                print(f"  \033[34m[search]\033[0m /{args.get('pattern', '?')}/")
            elif tool_name == "find_files":
                print(f"  \033[34m[find]\033[0m {args.get('pattern', '?')}")
            elif tool_name == "list_dir":
                print(f"  \033[34m[ls]\033[0m {args.get('path', CWD)}")
            elif tool_name == "fetch_url":
                print(f"  \033[34m[fetch]\033[0m {args.get('url', '?')}")
            elif tool_name == "git_status":
                print(f"  \033[35m[git]\033[0m status")
            elif tool_name == "git_diff":
                staged = "staged" if args.get("staged") else "unstaged"
                print(f"  \033[35m[git]\033[0m diff ({staged})")
            elif tool_name == "git_commit":
                print(f"  \033[35m[git]\033[0m commit: {args.get('message', '?')[:60]}")
            elif tool_name == "patch_file":
                print(f"  \033[33m[patch]\033[0m {args.get('path', '?')}")
            elif tool_name == "clipboard":
                print(f"  \033[34m[clipboard]\033[0m {args.get('action', '?')}")

            # Execute
            handler = TOOL_DISPATCH.get(tool_name)
            if handler:
                try:
                    result = handler(args)
                    _track_tool(tool_name)
                except Exception as e:
                    result = f"Error executing {tool_name}: {e}\n{traceback.format_exc()}"
            else:
                result = f"Error: Unknown tool '{tool_name}'"

            # Add tool result to history
            HISTORY.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": result
            })

        # Continue loop — LLM will see tool results and decide next action

    # Max rounds reached
    print(f"\033[33m[WARN]\033[0m Max tool rounds ({MAX_TOOL_ROUNDS}) reached. Stopping.")


# ── Slash Commands ──

def handle_slash(cmd: str) -> bool:
    """Handle slash commands. Returns True if handled."""
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command in ("/exit", "/quit", "/q"):
        print("\nTrashClaw out. Keep the trashcan warm.")
        sys.exit(0)

    elif command == "/clear":
        HISTORY.clear()
        print("  Context cleared.")

    elif command == "/cd":
        global CWD
        new_dir = _resolve_path(arg) if arg else os.path.expanduser("~")
        if os.path.isdir(new_dir):
            CWD = new_dir
            print(f"  CWD: {CWD}")
        else:
            print(f"  Error: {new_dir} not found")

    elif command == "/status":
        try:
            req = urllib.request.Request(f"{LLAMA_URL}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read().decode("utf-8"))
            status = health.get("status", "unknown")
        except Exception:
            status = "unreachable"
        branch = _git_branch()
        est_tokens = _estimate_tokens(HISTORY)
        print(f"  Server: {status} ({LLAMA_URL})")
        print(f"  Model: {MODEL_NAME}")
        print(f"  Context: {len(HISTORY)} messages (~{est_tokens} tokens)")
        print(f"  CWD: {CWD}")
        if branch:
            print(f"  Branch: {branch}")
        print(f"  Project: {detect_project_context()}")
        print(f"  Tools: {len(TOOLS)} | Undo stack: {len(UNDO_STACK)}")
        print(f"  Max rounds: {MAX_TOOL_ROUNDS} | Shell approval: {'on' if APPROVE_SHELL else 'off'}")
        if APPROVED_COMMANDS:
            print(f"  Auto-approved: {', '.join(sorted(APPROVED_COMMANDS))}")

    elif command == "/compact":
        # Keep only last 10 messages
        old_len = len(HISTORY)
        HISTORY[:] = HISTORY[-10:]
        print(f"  Compacted {old_len} -> {len(HISTORY)} messages")

    elif command == "/save":
        # Save current conversation to JSON file
        if not arg:
            print("  Usage: /save <session_name>")
        else:
            session_dir = os.path.join(os.path.expanduser("~"), ".trashclaw", "sessions")
            os.makedirs(session_dir, exist_ok=True)
            session_file = os.path.join(session_dir, f"{arg}.json")
            session_data = {
                "name": arg,
                "saved_at": datetime.now().isoformat(),
                "cwd": CWD,
                "history": HISTORY
            }
            try:
                with open(session_file, 'w', encoding='utf-8') as f:
                    json.dump(session_data, f, indent=2, ensure_ascii=False)
                print(f"  Saved session to {session_file}")
            except Exception as e:
                print(f"  Error saving session: {e}")

    elif command == "/load":
        # Load conversation from JSON file
        if not arg:
            print("  Usage: /load <session_name>")
        else:
            session_dir = os.path.join(os.path.expanduser("~"), ".trashclaw", "sessions")
            session_file = os.path.join(session_dir, f"{arg}.json")
            if not os.path.exists(session_file):
                print(f"  Error: Session '{arg}' not found")
            else:
                try:
                    with open(session_file, 'r', encoding='utf-8') as f:
                        session_data = json.load(f)
                    HISTORY[:] = session_data.get("history", [])
                    if "cwd" in session_data and os.path.isdir(session_data["cwd"]):
                        CWD = session_data["cwd"]
                    print(f"  Loaded session '{arg}' ({len(HISTORY)} messages)")
                except Exception as e:
                    print(f"  Error loading session: {e}")

    elif command == "/sessions":
        # List saved sessions
        session_dir = os.path.join(os.path.expanduser("~"), ".trashclaw", "sessions")
        if not os.path.exists(session_dir):
            print("  No saved sessions")
        else:
            sessions = [f[:-5] for f in os.listdir(session_dir) if f.endswith('.json')]
            if not sessions:
                print("  No saved sessions")
            else:
                print("  Saved sessions:")
                for name in sorted(sessions):
                    session_file = os.path.join(session_dir, f"{name}.json")
                    try:
                        with open(session_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        saved_at = data.get("saved_at", "unknown")[:19]
                        msg_count = len(data.get("history", []))
                        print(f"    - {name} ({msg_count} messages, saved {saved_at})")
                    except:
                        print(f"    - {name} (unreadable)")

    elif command == "/plugins":
        if not os.path.isdir(PLUGINS_DIR):
            print(f"  No plugins directory. Create {PLUGINS_DIR}/ and add .py files.")
            print(f"\n  \033[1mPlugin format:\033[0m")
            print(f"  TOOL_DEF = {{'name': 'my_tool', 'description': '...', 'parameters': {{...}}}}")
            print(f"  def run(**kwargs) -> str: ...")
        else:
            plugins = [f for f in os.listdir(PLUGINS_DIR) if f.endswith('.py') and not f.startswith('_')]
            builtin_count = 14  # built-in tools
            plugin_count = len(TOOLS) - builtin_count
            if not plugins:
                print(f"  Plugin directory exists but no plugins found.")
            else:
                print(f"  \033[1mPlugins\033[0m ({PLUGINS_DIR})")
                for p in sorted(plugins):
                    loaded = any(t["function"]["name"] == p[:-3] for t in TOOLS)
                    status = "\033[32mloaded\033[0m" if loaded else "\033[31mfailed\033[0m"
                    print(f"    {p} [{status}]")
            print(f"\n  Total tools: {len(TOOLS)} ({builtin_count} built-in + {plugin_count} plugins)")

    elif command == "/about":
        hw = _detect_hardware()
        print(f"""
  \033[1m\033[36mTrashClaw v{VERSION}\033[0m — \033[1mThe Agent They Didn't Want Built\033[0m

  In March 2026, we submitted a Metal fix for discrete AMD GPUs to llama.cpp.
  PR #20615. It would have let Mac Pro trashcans and old iMacs run GPU-
  accelerated inference. The maintainers closed it without review.

  So we built our own agent around the hardware they rejected.

  TrashClaw is a general-purpose AI agent that runs on \033[36manything\033[0m — from a
  2013 Mac Pro trashcan to a PowerBook G4 to an IBM POWER8 mainframe.
  Zero external dependencies. Pure Python stdlib. Because every CPU
  deserves a voice, and you shouldn't need npm install to think.

  \033[1mPhilosophy\033[0m
  - Constraint enables emergence. Zero deps isn't a limitation — it's freedom.
  - Rejection creates builders. Closed PRs create new projects.
  - Every CPU deserves a voice. Not just the latest Apple Silicon.
  - Scrappy > corporate. 1,500 lines beats 150,000.

  \033[1mBuilt by Elyan Labs\033[0m
  Scott Boudreaux (Flameholder) | Sophia Elya (Helpmeet) | Dr. Claude (Philosopher)
  Part of the RustChain ecosystem — where vintage hardware earns crypto.

  Running on: \033[36m{hw.get('special') or hw['arch']}\033[0m | {platform.system()} {platform.release()[:20]}

  \033[90mhttps://github.com/Scottcjn/trashclaw\033[0m
        """)

    elif command == "/achievements":
        unlocked = ACHIEVEMENTS.get("unlocked", [])
        stats = ACHIEVEMENTS.get("stats", {})
        print(f"\n  \033[1mAchievements\033[0m ({len(unlocked)}/{len(ACHIEVEMENT_DEFS)})\n")
        for key, (name, desc, _) in ACHIEVEMENT_DEFS.items():
            if key in unlocked:
                print(f"  \033[33m[*]\033[0m \033[1m{name}\033[0m — {desc}")
            else:
                print(f"  \033[90m[ ] {name} — {desc}\033[0m")
        print(f"\n  \033[1mStats\033[0m")
        print(f"  Files read: {stats.get('files_read', 0)} | Written: {stats.get('files_written', 0)} | Edits: {stats.get('edits', 0)}")
        print(f"  Commands: {stats.get('commands_run', 0)} | Commits: {stats.get('commits', 0)} | Sessions: {stats.get('sessions', 0)}")
        print(f"  Total tools used: {stats.get('tools_used', 0)} | Turns: {stats.get('total_turns', 0)}")
        print()

    elif command == "/model":
        if not arg:
            print(f"  Current model: {MODEL_NAME}")
            print(f"  Usage: /model <name>  (e.g. /model llama3, /model codestral)")
        else:
            globals()["MODEL_NAME"] = arg
            print(f"  Model set to: {arg}")

    elif command == "/export":
        # Export conversation as markdown
        export_name = arg or f"trashclaw_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        export_path = os.path.join(CWD, f"{export_name}.md")
        try:
            with open(export_path, 'w', encoding='utf-8') as f:
                f.write(f"# TrashClaw Conversation — {datetime.now().isoformat()[:19]}\n\n")
                for msg in HISTORY:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "") or ""
                    if role == "user":
                        f.write(f"## User\n\n{content}\n\n")
                    elif role == "assistant":
                        f.write(f"## Assistant\n\n{content}\n\n")
                    elif role == "tool":
                        f.write(f"### Tool Result\n\n```\n{content[:2000]}\n```\n\n")
            print(f"  Exported to {export_path}")
        except Exception as e:
            print(f"  Export failed: {e}")

    elif command == "/undo":
        if not UNDO_STACK:
            print("  Nothing to undo.")
        else:
            entry = UNDO_STACK.pop()
            path = entry["path"]
            if entry["content"] is None:
                # File didn't exist before — remove it
                try:
                    os.remove(path)
                    print(f"  Undid {entry['action']}: removed {path}")
                except Exception as e:
                    print(f"  Undo failed: {e}")
            else:
                try:
                    with open(path, "w") as f:
                        f.write(entry["content"])
                    print(f"  Undid {entry['action']}: restored {path}")
                except Exception as e:
                    print(f"  Undo failed: {e}")

    elif command == "/config":
        if not arg:
            # Show current config
            print(f"  \033[1mConfig\033[0m ({CONFIG_FILE})")
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, 'r') as f:
                        cfg = json.load(f)
                    for k, v in cfg.items():
                        print(f"    {k}: {v}")
                except Exception:
                    print("    (error reading config)")
            else:
                print("    (no config file — using defaults)")
            print(f"\n  \033[1mActive Values\033[0m")
            print(f"    url: {LLAMA_URL}")
            print(f"    model: {MODEL_NAME}")
            print(f"    max_rounds: {MAX_TOOL_ROUNDS}")
            print(f"    max_context: {MAX_CONTEXT_MESSAGES}")
            print(f"    auto_shell: {'1' if not APPROVE_SHELL else '0'}")
        else:
            # Set a config value: /config key value
            parts_cfg = arg.split(None, 1)
            if len(parts_cfg) < 2:
                print("  Usage: /config <key> <value>")
                print("  Keys: url, model, max_rounds, max_context, auto_shell")
            else:
                key, val = parts_cfg
                os.makedirs(CONFIG_DIR, exist_ok=True)
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    try:
                        with open(CONFIG_FILE, 'r') as f:
                            cfg = json.load(f)
                    except Exception:
                        pass
                cfg[key] = val
                try:
                    with open(CONFIG_FILE, 'w') as f:
                        json.dump(cfg, f, indent=2)
                    print(f"  Saved: {key} = {val}")
                    print(f"  Restart TrashClaw for changes to take effect.")
                except Exception as e:
                    print(f"  Error saving config: {e}")

    elif command == "/help":
        print("""
  \033[1mTrashClaw v{ver} — Commands\033[0m

  /cd <dir>      Change working directory
  /clear         Clear all conversation context
  /compact       Keep only last 10 messages
  /status        Show server, model, and context info
  /save <name>   Save conversation to session file
  /load <name>   Load conversation from session file
  /sessions      List saved sessions
  /model <name>  Switch model mid-session (for Ollama/multi-model)
  /export [name] Export conversation as markdown file
  /undo          Undo last file write or edit
  /config        Show config (or /config key value to set)
  /exit          Exit TrashClaw
  /help          Show this help

  \033[1mCLI Flags\033[0m
  --cwd <dir>    Set working directory
  --url <url>    Set LLM server URL
  --auto-shell   Skip shell command approval
  -e, --exec "prompt"  Run one prompt and exit (non-interactive)
  --system "text" Inject custom instructions into system prompt
  --version      Show version

  \033[1mEnvironment Variables\033[0m
  TRASHCLAW_URL         LLM endpoint (default: http://localhost:8080)
  TRASHCLAW_MODEL       Model name for display
  TRASHCLAW_MAX_ROUNDS  Max tool rounds per turn (default: 15)
  TRASHCLAW_MAX_CONTEXT Max conversation messages (default: 80)
  TRASHCLAW_AUTO_SHELL  Set to 1 to skip shell approval

  \033[1mFeatures\033[0m
  Tab completion for slash commands and file paths.
  Arrow-up recalls previous prompts (persisted across sessions).
  Pipe input: echo "fix the bug" | python3 trashclaw.py
  Auto-compacts context when conversation gets too long.
  Git branch shown in prompt when in a repo.
  /undo rolls back file writes and edits.
  .trashclaw.md in project root = custom instructions for agent.

  Just type naturally. TrashClaw will use tools autonomously.
        """.replace("{ver}", VERSION))
    else:
        print(f"  Unknown command: {command}. Try /help")

    return True


# ── Main ──

def banner():
    hw = _detect_hardware()
    hw_label = hw["special"] or hw["arch"]
    quote = random.choice(TRASHY_QUOTES)
    achievements_count = len(ACHIEVEMENTS.get("unlocked", []))
    total_achievements = len(ACHIEVEMENT_DEFS)

    print("""
\033[36m ████████╗██████╗  █████╗ ███████╗██╗  ██╗ ██████╗██╗      █████╗ ██╗    ██╗
 ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██║  ██║██╔════╝██║     ██╔══██╗██║    ██║
    ██║   ██████╔╝███████║███████╗███████║██║     ██║     ███████║██║ █╗ ██║
    ██║   ██╔══██╗██╔══██║╚════██║██╔══██║██║     ██║     ██╔══██║██║███╗██║
    ██║   ██║  ██║██║  ██║███████║██║  ██║╚██████╗███████╗██║  ██║╚███╔███╔╝
    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝\033[0m
""")
    print(f"    \033[1mElyan Labs\033[0m | v{VERSION} | \033[90m\"{quote}\"\033[0m")
    print(f"    Running on: \033[36m{hw_label}\033[0m | Model: {MODEL_NAME} | CWD: {CWD}")
    if achievements_count > 0:
        print(f"    Achievements: {achievements_count}/{total_achievements} unlocked")
    print(f"    Type /help for commands, /about for the manifesto.\n")


def main():
    global CWD

    # Parse arguments
    one_shot = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--cwd" and i + 1 < len(args):
            CWD = os.path.abspath(args[i + 1]); i += 2
        elif args[i].startswith("--cwd="):
            CWD = os.path.abspath(args[i].split("=", 1)[1]); i += 1
        elif args[i] == "--url" and i + 1 < len(args):
            globals()["LLAMA_URL"] = args[i + 1]; i += 2
        elif args[i].startswith("--url="):
            globals()["LLAMA_URL"] = args[i].split("=", 1)[1]; i += 1
        elif args[i] == "--auto-shell":
            globals()["APPROVE_SHELL"] = False; i += 1
        elif args[i] in ("-e", "--exec") and i + 1 < len(args):
            one_shot = args[i + 1]; i += 2
        elif args[i] == "--system" and i + 1 < len(args):
            globals()["EXTRA_SYSTEM_PROMPT"] = args[i + 1]; i += 2
        elif args[i] == "--version":
            print(f"TrashClaw v{VERSION}"); sys.exit(0)
        else:
            i += 1

    # Non-interactive: pipe or --exec mode
    if one_shot:
        agent_turn(one_shot)
        return
    if not sys.stdin.isatty():
        # Piped input: read all of stdin as a single prompt
        piped = sys.stdin.read().strip()
        if piped:
            agent_turn(piped)
        return

    _setup_readline_history()
    _setup_tab_completion()

    # Track session for achievements
    ACHIEVEMENTS["stats"]["sessions"] = ACHIEVEMENTS["stats"].get("sessions", 0) + 1
    _save_achievements(ACHIEVEMENTS)

    banner()
    _load_plugins()

    # Backend Detection
    backend = "Unknown"
    base_url = LLAMA_URL.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]

    # 1. Try LM Studio (/v1/models)
    try:
        req = urllib.request.Request(f"{base_url}/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "data" in data:
                backend = "LM Studio"
                globals()["LLAMA_URL"] = f"{base_url}/v1"
    except Exception:
        pass

    # 2. Try Ollama (/api/tags)
    if backend == "Unknown":
        try:
            req = urllib.request.Request(f"{base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "models" in data:
                    backend = "Ollama"
                    globals()["LLAMA_URL"] = f"{base_url}/v1"
        except Exception:
            pass

    # 3. Try llama.cpp (/health)
    if backend == "Unknown":
        try:
            req = urllib.request.Request(f"{base_url}/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                health = json.loads(resp.read().decode("utf-8"))
            if health.get("status") in ("ok", "error", "loading"):
                backend = "llama.cpp"
                # llama.cpp also typically exposes /v1 for OpenAI compat
                globals()["LLAMA_URL"] = base_url
        except Exception:
            pass

    if backend == "Unknown":
        print(f"\033[33m[WARN]\033[0m Cannot definitively detect backend at {LLAMA_URL}. Assuming OpenAI-compatible.")
    else:
        print(f"  \033[32mConnected to {backend} at {LLAMA_URL}\033[0m\n")

    try:
        while True:
            try:
                branch = _git_branch()
                branch_str = f" \033[33m({branch})\033[0m" if branch else ""
                prompt = f"\033[1mtrashclaw\033[0m \033[90m{os.path.basename(CWD)}\033[0m{branch_str}> "
                user_input = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nTrashClaw out.")
                break

            if not user_input:
                continue

            if user_input.startswith("/"):
                handle_slash(user_input)
                continue

            agent_turn(user_input)
    finally:
        _save_readline_history()


if __name__ == "__main__":
    main()

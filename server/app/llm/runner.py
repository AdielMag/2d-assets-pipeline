"""Shell out to local LLM CLI agents (Claude Code / Gemini CLI) in
non-interactive print mode. All text-intelligence steps in the pipeline go through
here — no LLM API keys are used.
"""
import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..config import STORAGE_DIR

TIMEOUT_S = 180


class LlmError(Exception):
    """User-presentable LLM failure."""


@dataclass
class LlmOptions:
    model: str = ""  # empty = provider default
    effort: str = ""  # low | medium | high, empty = provider default
    context_window: int | None = None
    images: list[Path] = field(default_factory=list)


import shlex

import threading

class LlmRunner(ABC):
    name: str
    binary: str
    passes_prompt_in_argv: bool = False

    @property
    def last_command(self) -> str | None:
        if not hasattr(self, "_tls"):
            self._tls = threading.local()
        return getattr(self._tls, "cmd", None)

    @last_command.setter
    def last_command(self, value: str | None):
        if not hasattr(self, "_tls"):
            self._tls = threading.local()
        self._tls.cmd = value

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run(self, prompt: str, options: LlmOptions | None = None) -> str:
        options = options or LlmOptions()
        exe = shutil.which(self.binary)
        if not exe:
            raise LlmError(f"{self.binary} CLI not found on PATH")
        cmd, env_extra = self.build_command(exe, prompt, options)
        cmd_str = shlex.join(cmd)
        if not self.passes_prompt_in_argv and prompt:
            cmd_str += f" << 'PROMPT_EOF'\n{prompt}\nPROMPT_EOF"
        self.last_command = cmd_str
        # The pipeline server may itself have been launched from inside a Claude Code
        # session; the claude CLI refuses to start when CLAUDECODE & co. are present.
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        env.update(env_extra)
        try:
            # cwd = storage dir: neutral (agentic CLIs pick up project context from
            # their working directory) and contains the images vision calls reference.
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TIMEOUT_S,
                env=env, shell=False, encoding="utf-8", errors="replace",
                input=None if self.passes_prompt_in_argv else prompt, cwd=STORAGE_DIR,
            )
        except subprocess.TimeoutExpired:
            raise LlmError(f"{self.name} CLI timed out after {TIMEOUT_S}s")
        except OSError as e:
            raise LlmError(f"Failed to run {self.name} CLI: {e}")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise LlmError(f"{self.name} CLI failed (exit {proc.returncode}): {tail}")
        return self.parse_output(proc.stdout)

    @abstractmethod
    def build_command(
        self, exe: str, prompt: str, options: LlmOptions
    ) -> tuple[list[str], dict[str, str]]:
        """Returns (argv, extra env vars)."""

    def parse_output(self, stdout: str) -> str:
        return stdout.strip()


class ClaudeRunner(LlmRunner):
    name = "claude"
    binary = "claude"

    # Thinking effort maps to Claude Code's thinking-token budget.
    EFFORT_TOKENS = {"low": "1024", "medium": "8000", "high": "31999"}

    def build_command(self, exe, prompt, options):
        cmd = [exe, "-p", "--output-format", "json"]
        if options.model:
            cmd += ["--model", options.model]
        if options.images:
            # Vision: allow the agent to read the image files referenced in the prompt.
            cmd += ["--allowedTools", "Read"]
        env = {}
        if options.effort in self.EFFORT_TOKENS:
            env["MAX_THINKING_TOKENS"] = self.EFFORT_TOKENS[options.effort]
        return cmd, env

    def parse_output(self, stdout):
        try:
            data = json.loads(stdout)
            return (data.get("result") or "").strip()
        except json.JSONDecodeError:
            return stdout.strip()


class AntigravityRunner(LlmRunner):
    name = "antigravity"
    passes_prompt_in_argv = True

    EFFORT_TOKENS = {"low": "1024", "medium": "8000", "high": "31999"}

    # Binary is configurable (same knob the image provider uses) since the CLI
    # isn't a fixed npm name. `available()` reads it live off config.
    @property
    def binary(self):
        from .. import config
        return config.ANTIGRAVITY_BIN

    def build_command(self, exe, prompt, options):
        cmd = [exe, "--dangerously-skip-permissions"]
        if options.model:
            cmd += ["--model", options.model]
        # agy model names can already encode the reasoning effort as a suffix
        # (e.g. "gemini-3.6-flash-high"); passing a separate --effort then conflicts
        # ("--model … conflicts with --effort=…"), so only send --effort when the model
        # name doesn't already carry one.
        model_carries_effort = bool(options.model) and options.model.rsplit("-", 1)[-1] in (
            "low", "medium", "high",
        )
        if options.effort and not model_carries_effort:
            cmd += ["--effort", options.effort]
        # Vision: agy is a workspace-scoped agent and can only open files inside its cwd
        # (STORAGE_DIR) or a directory allowed via --add-dir. Grant read access to each
        # referenced image's folder so a vision prompt that points at an absolute path
        # (e.g. a downscaled temp copy outside storage) can actually be opened.
        for img in options.images:
            cmd += ["--add-dir", str(Path(img).resolve().parent)]
        cmd += ["-p", prompt]
        env = {}
        if options.effort in self.EFFORT_TOKENS:
            env["MAX_THINKING_TOKENS"] = self.EFFORT_TOKENS[options.effort]
        return cmd, env


RUNNERS: dict[str, LlmRunner] = {
    "claude": ClaudeRunner(),
    "antigravity": AntigravityRunner(),
}


def get_runner(name: str) -> LlmRunner:
    if name not in RUNNERS:
        raise LlmError(f"Unknown LLM provider '{name}'")
    return RUNNERS[name]

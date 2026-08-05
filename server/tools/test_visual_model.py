"""Quick test: does mentioning a visual model name in the prompt text
affect which image-generation tool/model the Antigravity CLI agent uses?

We run two prompts through the CLI:
  A) "Generate an image of a red sword on a magenta background. Save to {path}"
  B) Same, but with "Use the nano-banana-pro image model." prepended.

Then we compare the CLI stdout to see if the agent mentions different tools/models.
This is a read-only probe — we just inspect the output text.
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TIMEOUT = 120  # seconds per invocation

def find_agy():
    for name in ("agy", "antigravity"):
        exe = shutil.which(name)
        if exe:
            return exe
    print("ERROR: agy/antigravity CLI not found on PATH")
    sys.exit(1)

def run_prompt(exe: str, prompt: str, workdir: Path, label: str) -> str:
    cmd = [
        exe,
        "--dangerously-skip-permissions",
        "--print-timeout", "120s",
        "--model", "gemini-3.6-flash-high",
        "-p", prompt,
    ]
    print(f"\n{'='*60}")
    print(f"TEST {label}")
    print(f"{'='*60}")
    print(f"Prompt: {prompt[:120]}...")
    print(f"Running: {' '.join(cmd[:6])} ...")
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=workdir,
            shell=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        print(f"  TIMED OUT after {TIMEOUT}s")
        return "(timed out)"
    elapsed = time.time() - t0
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    print(f"  Finished in {elapsed:.1f}s  (exit {proc.returncode})")
    print(f"  --- STDOUT (last 600 chars) ---")
    print(proc.stdout[-600:] if proc.stdout else "(empty)")
    print(f"  --- STDERR (last 300 chars) ---")
    print(proc.stderr[-300:] if proc.stderr else "(empty)")
    return combined


def main():
    exe = find_agy()
    print(f"Using CLI: {exe}")

    with tempfile.TemporaryDirectory(prefix="visual-model-test-") as tmp:
        tmpdir = Path(tmp)

        target_a = tmpdir / "test_a.png"
        target_b = tmpdir / "test_b.png"

        base_instruction = (
            "Generate one image of a simple red sword on a solid magenta (#FF00FF) background. "
            "Save the image to exactly this path: {path}. "
            "Do not ask questions; just generate and save."
        )

        # --- Test A: NO visual model mentioned ---
        prompt_a = base_instruction.format(path=target_a)
        out_a = run_prompt(exe, prompt_a, tmpdir, "A (no visual model mentioned)")

        # --- Test B: visual model explicitly mentioned ---
        prompt_b = (
            "Use the nano-banana image generation model (gemini-3.1-flash-image). "
            + base_instruction.format(path=target_b)
        )
        out_b = run_prompt(exe, prompt_b, tmpdir, "B (nano-banana model requested in prompt)")

        print("\n" + "="*60)
        print("RESULTS SUMMARY")
        print("="*60)
        print(f"Test A image exists: {target_a.exists()}")
        print(f"Test B image exists: {target_b.exists()}")

        # Look for model-related keywords in the output
        for label, output in [("A", out_a), ("B", out_b)]:
            lower = output.lower()
            mentions = []
            for kw in ["nano-banana", "nano_banana", "nanobanana",
                        "flash-image", "pro-image", "generate_image",
                        "image_generation", "model"]:
                if kw in lower:
                    mentions.append(kw)
            print(f"Test {label} keyword matches: {mentions}")

    print("\nDone.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
run.py — Full automated runner for the Yamlizr Pipeline Unifier.

Does everything in one command:
  1. Checks Python version
  2. Installs pip dependencies (requests, PyYAML)
  3. Checks for .NET 8+ (warns if missing)
  4. Installs / updates the yamlizr dotnet tool
  5. Runs yamlizr to convert Classic pipelines
  6. Runs the unifier to merge all pipelines into Unified/

Usage:
  python run.py -pat TOKEN -org https://dev.azure.com/myorg -proj MyProject -out ./output
"""

import argparse
import subprocess
import sys
import shutil
from pathlib import Path


# ─── Colour helpers (graceful fallback on Windows without ANSI) ───────────────

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def green(t):  return _c("32;1", t)
def yellow(t): return _c("33;1", t)
def red(t):    return _c("31;1", t)
def bold(t):   return _c("1", t)
def cyan(t):   return _c("36;1", t)


def banner(msg: str) -> None:
    width = 60
    print()
    print(cyan("━" * width))
    print(cyan(f"  {msg}"))
    print(cyan("━" * width))


def step(n: int, total: int, msg: str) -> None:
    print(f"\n{bold(f'[{n}/{total}]')} {msg}")


def ok(msg: str)   -> None: print(green(f"  ✔  {msg}"))
def warn(msg: str) -> None: print(yellow(f"  ⚠  {msg}"))
def fail(msg: str) -> None: print(red(f"  ✖  {msg}"))


# ─── Shell helpers ────────────────────────────────────────────────────────────

def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command, streaming output unless capture=True."""
    kwargs: dict = {"check": check}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"]           = True
    return subprocess.run(cmd, **kwargs)


def which(tool: str) -> str | None:
    return shutil.which(tool)


# ─── Step implementations ─────────────────────────────────────────────────────

TOTAL_STEPS = 6


def check_python() -> None:
    step(1, TOTAL_STEPS, "Checking Python version …")
    info = sys.version_info
    if info < (3, 11):
        fail(f"Python 3.11+ required, found {info.major}.{info.minor}.")
        sys.exit(1)
    ok(f"Python {info.major}.{info.minor}.{info.micro}")


def install_pip_deps() -> None:
    step(2, TOTAL_STEPS, "Installing Python dependencies …")
    deps = ["requests>=2.31.0", "PyYAML>=6.0"]
    result = run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"] + deps,
        check=False,
    )
    if result.returncode != 0:
        warn("pip install returned a non-zero exit code. Continuing anyway.")
    else:
        ok("requests and PyYAML are up to date.")


def check_dotnet() -> str | None:
    step(3, TOTAL_STEPS, "Checking for .NET 8+ …")
    dotnet = which("dotnet")
    if not dotnet:
        warn(".NET SDK not found. If all your pipelines are already YAML, this is fine.")
        warn("Yamlizr will be skipped; only YAML-only scenarios will work.")
        return None

    result = run(["dotnet", "--version"], capture=True, check=False)
    version_str = result.stdout.strip() if result.returncode == 0 else ""
    try:
        major = int(version_str.split(".")[0])
        if major < 8:
            warn(f".NET {version_str} found but 8+ is required for yamlizr.")
            warn("Yamlizr will be skipped; Classic pipelines will not be converted.")
            return None
    except (ValueError, IndexError):
        warn(f"Could not parse .NET version '{version_str}'. Proceeding cautiously.")

    ok(f".NET {version_str}")
    return dotnet


def install_yamlizr(dotnet: str) -> bool:
    step(4, TOTAL_STEPS, "Installing / updating yamlizr …")
    result = run(
        [dotnet, "tool", "update", "--global", "yamlizr"],
        check=False,
    )
    if result.returncode != 0:
        warn("yamlizr install/update failed. Classic pipelines will not be converted.")
        return False
    ok("yamlizr is up to date.")
    return True


def run_yamlizr(dotnet: str, pat: str, org: str, proj: str, out: str) -> bool:
    step(5, TOTAL_STEPS, f"Running yamlizr → converting Classic pipelines …")
    print(f"  org={org}  proj={proj}  out={out}")

    # yamlizr may be installed as a global tool; find it
    yamlizr_bin = which("yamlizr")
    if not yamlizr_bin:
        # On some systems the global tools path isn't on PATH yet
        home = Path.home()
        candidates = [
            home / ".dotnet" / "tools" / "yamlizr",
            home / ".dotnet" / "tools" / "yamlizr.exe",
        ]
        yamlizr_bin = next((str(p) for p in candidates if p.exists()), None)

    if not yamlizr_bin:
        warn("yamlizr binary not found on PATH. Skipping conversion step.")
        warn("Re-open your terminal after installing .NET tools and try again,")
        warn("or add ~/.dotnet/tools to your PATH.")
        return False

    result = run(
        [
            yamlizr_bin, "generate",
            "-pat",  pat,
            "-org",  org,
            "-proj", proj,
            "-out",  out,
        ],
        check=False,
    )
    if result.returncode != 0:
        warn(f"yamlizr exited with code {result.returncode}.")
        warn("Classic pipelines may not have been fully converted.")
        warn("The unifier will still run and process whatever was converted.")
        return False

    ok("yamlizr completed successfully.")
    return True


def run_unifier(pat: str, org: str, proj: str, out: str, verbose: bool) -> None:
    step(6, TOTAL_STEPS, "Running pipeline unifier …")
    # Locate unifier.py — same directory as this script
    script_dir   = Path(__file__).parent.resolve()
    unifier_path = script_dir / "unifier.py"

    if not unifier_path.exists():
        fail(f"unifier.py not found at {unifier_path}")
        fail("Make sure unifier.py is in the same directory as run.py.")
        sys.exit(1)

    cmd = [
        sys.executable, str(unifier_path),
        "-pat",  pat,
        "-org",  org,
        "-proj", proj,
        "-out",  out,
    ]
    if verbose:
        cmd.append("-v")

    result = run(cmd, check=False)
    if result.returncode != 0:
        fail("Unifier finished with errors (see output above).")
        sys.exit(result.returncode)

    unified = Path(out) / "Unified"
    files   = list(unified.glob("*.yml")) if unified.exists() else []
    ok(f"Unifier complete. {len(files)} unified pipeline(s) written to: {unified}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full automated runner: installs tools → runs yamlizr → merges pipelines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python run.py \\
      -pat  myPersonalAccessToken \\
      -org  https://dev.azure.com/myorg \\
      -proj MyProject \\
      -out  ./output

PAT scopes required: Build (Read), Release (Read), Code (Read).
        """,
    )
    parser.add_argument("-pat",  required=True, metavar="TOKEN", help="Azure DevOps PAT")
    parser.add_argument("-org",  required=True, metavar="URL",   help="Org URL  e.g. https://dev.azure.com/myorg")
    parser.add_argument("-proj", required=True, metavar="NAME",  help="Project name")
    parser.add_argument("-out",  required=True, metavar="DIR",   help="Output directory")
    parser.add_argument("-v", "--verbose", action="store_true",  help="Verbose unifier output")
    args = parser.parse_args()

    banner("Yamlizr Pipeline Unifier — Full Automated Run")
    print(f"  org  : {args.org}")
    print(f"  proj : {args.proj}")
    print(f"  out  : {args.out}")

    # Step 1 — Python check
    check_python()

    # Step 2 — pip deps
    install_pip_deps()

    # Step 3 — .NET
    dotnet = check_dotnet()

    # Steps 4 & 5 — yamlizr (skipped gracefully if .NET unavailable)
    if dotnet:
        if install_yamlizr(dotnet):
            run_yamlizr(dotnet, args.pat, args.org, args.proj, args.out)
        else:
            step(5, TOTAL_STEPS, "Skipping yamlizr run (install failed).")
    else:
        step(4, TOTAL_STEPS, "Skipping yamlizr install (.NET not available).")
        step(5, TOTAL_STEPS, "Skipping yamlizr run (.NET not available).")

    # Step 6 — unifier (always runs)
    run_unifier(args.pat, args.org, args.proj, args.out, args.verbose)

    banner("All done!")


if __name__ == "__main__":
    main()

#!/usr/bin/env node
/**
 * Stratum-Memory npm shim.
 *
 * The npm package `stratum-memory` is a *thin wrapper*: the real command line
 * tool ships with the Python package of the same name (`stratum`, installed via
 * pipx/pip/installer). This shim only *locates* that CLI and hands the arguments
 * over. It never downloads or installs anything by itself.
 *
 * Resolution order:
 *   1. a `stratum` executable on PATH, *excluding* this npm shim itself (the
 *      npm bin directory contains a `stratum`/`stratum.cmd` wrapper that points
 *      back here - calling it would recurse forever);
 *   2. `python3 -m stratum.cli`, then `python -m stratum.cli`, then
 *      `py -3 -m stratum.cli`;
 *   3. nothing found -> print install guidance on stderr and exit 3.
 */

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

/** Exit code when no Python CLI could be found (documented in npm/README.md). */
const EXIT_NO_PYTHON = 3;
/** Exit code for an unexpected internal failure of the shim itself. */
const EXIT_INTERNAL = 1;

/** The interpreter candidates, in order. `py -3` is the Windows launcher. */
const PYTHON_CANDIDATES = [
  { command: "python3", prefix: [] },
  { command: "python", prefix: [] },
  { command: "py", prefix: ["-3"] },
];

/** Probe: does this interpreter import the Stratum CLI? Needs Python >= 3.12. */
const PROBE_MODULE = "import sys, stratum.cli; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)";
/** Probe: is this a usable Python at all (any version)? */
const PROBE_PYTHON = "import sys; print('%d.%d' % sys.version_info[:2])";

const DOCS_URL = "https://github.com/TotalBassJunkie/stratum-memory";

// --------------------------------------------------------------------------- #
// Locating ourselves
// --------------------------------------------------------------------------- #

function realpathOrNull(target) {
  try {
    return fs.realpathSync(target);
  } catch (err) {
    return null;
  }
}

/** Absolute, symlink-resolved path of this very script. */
const SELF = realpathOrNull(__filename) || path.resolve(__filename);

/**
 * True when `candidate` is this shim - or one of the wrapper scripts npm
 * generates next to it (`node_modules/.bin/stratum`, `stratum.cmd`, ...).
 *
 * Executing such a candidate would re-enter this file and recurse without end,
 * so every `stratum` found on PATH is checked against it.
 */
function isSelf(candidate) {
  const resolved = realpathOrNull(candidate);
  if (resolved !== null && resolved === SELF) {
    return true;
  }
  if (resolved !== null && path.dirname(resolved) === path.dirname(SELF)) {
    return true;
  }
  // npm's shell wrappers are small text files that reference this bin file by
  // relative path; they do not resolve to SELF via realpath, so look inside.
  try {
    const stat = fs.statSync(candidate);
    if (!stat.isFile() || stat.size > 64 * 1024) {
      return false;
    }
    const head = fs.readFileSync(candidate, "utf8").replace(/\\/g, "/").toLowerCase();
    return head.includes("stratum-memory/bin/stratum.js") || head.includes("bin/stratum.js");
  } catch (err) {
    return false;
  }
}

// --------------------------------------------------------------------------- #
// Running things
// --------------------------------------------------------------------------- #

/** Quote one argument for a `cmd.exe`-style command line. */
function quoteForShell(value) {
  return '"' + String(value).replace(/"/g, '""') + '"';
}

/**
 * Spawn a command. Windows batch shims (`stratum.cmd`) need a shell; everything
 * else is spawned directly with an explicit argument list (never `shell: true`
 * for real executables, so arguments cannot be reinterpreted by a shell).
 */
function spawnCommand(command, args, stdio) {
  if (process.platform === "win32" && /\.(cmd|bat)$/i.test(command)) {
    const line = [command, ...args].map(quoteForShell).join(" ");
    return spawnSync(line, { shell: true, stdio: stdio });
  }
  return spawnSync(command, args, { stdio: stdio });
}

function isExecutableFile(target) {
  try {
    const stat = fs.statSync(target);
    if (!stat.isFile()) {
      return false;
    }
  } catch (err) {
    return false;
  }
  if (process.platform === "win32") {
    return true;
  }
  try {
    fs.accessSync(target, fs.constants.X_OK);
    return true;
  } catch (err) {
    return false;
  }
}

/** The PATH entries, in order, without empty duplicates. */
function pathDirectories() {
  const raw = process.env.PATH || process.env.Path || "";
  const seen = new Set();
  const out = [];
  for (const entry of raw.split(path.delimiter)) {
    const trimmed = entry.trim().replace(/^"|"$/g, "");
    if (!trimmed) {
      continue;
    }
    const key = process.platform === "win32" ? trimmed.toLowerCase() : trimmed;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(trimmed);
  }
  return out;
}

/** Every `stratum`-like file on PATH, in PATH order, excluding this shim. */
function findStratumOnPath() {
  const names =
    process.platform === "win32"
      ? ["stratum.exe", "stratum.cmd", "stratum.bat", "stratum"]
      : ["stratum"];
  for (const directory of pathDirectories()) {
    for (const name of names) {
      const candidate = path.join(directory, name);
      if (!isExecutableFile(candidate)) {
        continue;
      }
      if (isSelf(candidate)) {
        // Our own npm wrapper - skip it, or we would call ourselves forever.
        continue;
      }
      return candidate;
    }
  }
  return null;
}

// --------------------------------------------------------------------------- #
// Finding the Python CLI
// --------------------------------------------------------------------------- #

/**
 * @returns {{kind: "python", command: string, args: string[]} | {kind: "none", pythonFound: string[]}}
 */
function findPythonCli() {
  const pythonFound = [];
  for (const candidate of PYTHON_CANDIDATES) {
    const version = spawnCommand(candidate.command, [...candidate.prefix, "-c", PROBE_PYTHON], [
      "ignore",
      "pipe",
      "ignore",
    ]);
    if (version.error || version.status !== 0) {
      continue;
    }
    pythonFound.push(candidate.command);
    const probe = spawnCommand(
      candidate.command,
      [...candidate.prefix, "-c", PROBE_MODULE],
      ["ignore", "ignore", "ignore"],
    );
    if (!probe.error && probe.status === 0) {
      return {
        kind: "python",
        command: candidate.command,
        args: [...candidate.prefix, "-m", "stratum.cli"],
      };
    }
  }
  return { kind: "none", pythonFound: pythonFound };
}

// --------------------------------------------------------------------------- #
// Reporting
// --------------------------------------------------------------------------- #

function printMissingCli(pythonFound) {
  const lines = [];
  lines.push("stratum: the Stratum-Memory Python CLI was not found.");
  lines.push("");
  lines.push("This npm package ('stratum-memory') is only a thin wrapper around the");
  lines.push("'stratum' command line tool, which ships with the Python package of the");
  lines.push("same name. Nothing was installed automatically - install it yourself:");
  lines.push("");
  lines.push("  pipx install stratum-memory                # recommended");
  lines.push("  python3 -m pip install --user stratum-memory");
  lines.push("  curl -fsSL https://get.stratum.dev/install.sh | bash    # Linux / macOS");
  lines.push("  irm https://get.stratum.dev/install.ps1 | iex           # Windows PowerShell");
  lines.push("  winget install TotalBassJunkie.StratumMemory            # Windows");
  lines.push("  msiexec /i Stratum-<version>-x64.msi                    # Windows (GitHub release)");
  lines.push("");
  if (pythonFound.length > 0) {
    lines.push(
      "Python was found on PATH (" +
        pythonFound.join(", ") +
        "), but it cannot import 'stratum.cli'.",
    );
  } else {
    lines.push("No Python >= 3.12 was found on PATH either.");
  }
  lines.push("");
  lines.push("Requires Python >= 3.12. The server stack ('stratum install' / 'stratum up')");
  lines.push("additionally needs Docker. Then re-run, e.g.: npx stratum-memory version");
  lines.push("Docs: " + DOCS_URL);
  process.stderr.write(lines.join(os.EOL) + os.EOL);
}

function main(argv) {
  const onPath = findStratumOnPath();
  if (onPath !== null) {
    const result = spawnCommand(onPath, argv, "inherit");
    if (result.error) {
      process.stderr.write("stratum: failed to run '" + onPath + "': " + result.error.message + os.EOL);
      return EXIT_INTERNAL;
    }
    return result.status === null ? EXIT_INTERNAL : result.status;
  }

  const python = findPythonCli();
  if (python.kind === "python") {
    const result = spawnCommand(python.command, [...python.args, ...argv], "inherit");
    if (result.error) {
      process.stderr.write(
        "stratum: failed to run '" + python.command + "': " + result.error.message + os.EOL,
      );
      return EXIT_INTERNAL;
    }
    return result.status === null ? EXIT_INTERNAL : result.status;
  }

  printMissingCli(python.pythonFound);
  return EXIT_NO_PYTHON;
}

process.exit(main(process.argv.slice(2)));

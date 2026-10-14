# frozen_string_literal: true

# Homebrew formula for Stratum-Memory.
#
# Contract: docs/distribution.md. Every artifact is built by GitHub Actions on a
# `v<version>` tag and attached to the GitHub Release. The `url`/`sha256` pair
# below is rewritten by .github/workflows/release.yml on every tag, so do not
# hand-edit it in the tap (TotalBassJunkie/homebrew-stratum).
#
# This formula installs the prebuilt, pure-Python wheel from the release - no
# compiler and no source build are required - and exposes the `stratum` CLI.
class StratumMemory < Formula
  include Language::Python::Virtualenv

  desc "Unified AI memory and context system (4 layers, ECL ingestion, curator gate, MCP API)"
  homepage "https://github.com/TotalBassJunkie/stratum-memory"
  url "https://github.com/TotalBassJunkie/stratum-memory/releases/download/v0.1.0/stratum-0.1.0-py3-none-any.whl",
      using: :nounzip
  # updated by .github/workflows/release.yml
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "Apache-2.0"

  depends_on "python@3.12"

  def install
    # `py3-none-any`: install the wheel as-is into a private virtualenv; pip
    # resolves the runtime dependencies (uvicorn, mcp, psycopg, pydantic, etc.).
    virtualenv_create(libexec, "python3.12")
    system libexec/"bin/pip", "install", "--no-cache-dir", cached_download
    bin.install_symlink libexec/"bin/stratum"
  end

  test do
    system bin/"stratum", "version"
  end
end

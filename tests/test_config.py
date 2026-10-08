"""Tests for environment-driven configuration."""
import importlib
import os
import sys
from pathlib import Path


def test_non_recon_keywords_from_env(monkeypatch):
    monkeypatch.setenv("RECON_NON_RECON_KEYWORDS", "FOO, BAR , BAZ")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if "qris_recon" in sys.modules:
        importlib.reload(sys.modules["qris_recon"])
    import qris_recon as qr
    assert qr.NON_RECON_KEYWORDS == ("FOO", "BAR", "BAZ")


def test_non_recon_prefixes_from_env(monkeypatch):
    monkeypatch.setenv("RECON_NON_RECON_PREFIXES", "PFX1,PFX2")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if "qris_recon" in sys.modules:
        importlib.reload(sys.modules["qris_recon"])
    import qris_recon as qr
    assert qr.NON_RECON_DESC_PREFIXES == ("PFX1", "PFX2")

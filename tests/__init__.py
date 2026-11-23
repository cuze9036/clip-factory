"""Tests never read the laptop's real keys (.env) or config.local.yaml: on the laptop they would ping the real
offline alert, see the real Gmail login, and so on."""
import os

os.environ.setdefault("CLIPFACTORY_TESTING", "1")

import os
import pathlib
from functools import lru_cache

import yaml
from dotenv import load_dotenv

ROOT = pathlib.Path(os.environ.get("CLIPFACTORY_HOME") or pathlib.Path(__file__).resolve().parent.parent)


def _merge(base, extra):
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


@lru_cache(maxsize=1)
def get():
    """config.yaml, with config.local.yaml on top. Updates replace config.yaml but never config.local.yaml."""
    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    local = ROOT / "config.local.yaml"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            cfg = _merge(cfg, yaml.safe_load(f))
    return cfg


def section(name):
    return get().get(name) or {}


def path(rel):
    p = pathlib.Path(rel)
    return p if p.is_absolute() else ROOT / p


def data_dir():
    d = path(os.environ.get("CLIPFACTORY_DATA") or "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def env(name, default=None):
    get()
    v = os.environ.get(name)
    return v if v else default

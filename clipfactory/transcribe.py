"""Local speech-to-text with faster-whisper (no API cost).
Uses the GPU when it works; if the GPU libraries are missing it switches to the CPU and remembers that."""
from . import config, db

_model = None

GPU_ERRORS = ("cublas", "cudnn", "cuda", "cudart")


def _nvidia_dirs():
    """bin folders of the nvidia-cublas-cu12 / nvidia-cudnn-cu12 pip packages, if installed."""
    import glob
    import os
    import site
    dirs = []
    for base in site.getsitepackages() + [site.getusersitepackages()]:
        dirs += glob.glob(os.path.join(base, "nvidia", "*", "bin")) + glob.glob(os.path.join(base, "nvidia", "*", "lib"))
    return [d for d in dirs if os.path.isdir(d)]


def _use_nvidia_libs():
    import os
    dirs = _nvidia_dirs()
    for d in dirs:
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
    # Libraries installed after an earlier CPU fallback: give the GPU one more chance.
    if dirs and db.kv_get("whisper_cpu_only") and not db.kv_get("whisper_gpu_retried"):
        db.kv_set("whisper_gpu_retried", True)
        db.kv_set("whisper_cpu_only", False)
    return bool(dirs)


_multi = None


def _get_multi():
    """A multilingual model (for speech that isn't English): it hears any language and writes English."""
    global _multi
    if _multi is None:
        _use_nvidia_libs()
        from faster_whisper import WhisperModel
        cfg = config.section("whisper")
        device = cfg.get("device", "auto")
        if device == "auto" and db.kv_get("whisper_cpu_only"):
            device = "cpu"
        _multi = WhisperModel(cfg.get("multilingual_model", "small"), device=device,
                              compute_type=cfg.get("compute_type", "int8"))
    return _multi


def _get():
    global _model
    if _model is None:
        _use_nvidia_libs()
        from faster_whisper import WhisperModel
        cfg = config.section("whisper")
        device = cfg.get("device", "auto")
        if device == "auto" and db.kv_get("whisper_cpu_only"):
            device = "cpu"
        _model = WhisperModel(cfg.get("model", "small.en"), device=device,
                              compute_type=cfg.get("compute_type", "int8"))
        _model._cf_device = device
    return _model


def _run(fn):
    global _model, _multi
    try:
        return fn(_get())
    except (RuntimeError, OSError) as e:
        if not any(k in str(e).lower() for k in GPU_ERRORS) or db.kv_get("whisper_cpu_only"):
            raise
        db.kv_set("whisper_cpu_only", True)
        db.log(None, f"GPU speech libraries not usable ({str(e)[:80]}); captions now run on the CPU", "warn")
        _model = _multi = None
        return fn(_get())


def _opts(translate):
    # English speech: the fast English model. Other languages: translated into English captions.
    return {"task": "translate"} if translate else {"language": "en"}


def segments(path, translate=False):
    def go(model):
        m = _get_multi() if translate else model
        segs, _ = m.transcribe(str(path), vad_filter=True, **_opts(translate))
        return [{"start": s.start, "end": s.end, "text": s.text} for s in segs]
    return _run(go)


def words(path, translate=False):
    def go(model):
        m = _get_multi() if translate else model
        segs, _ = m.transcribe(str(path), vad_filter=True, word_timestamps=True, **_opts(translate))
        out = []
        for s in segs:
            for w in s.words or []:
                t = w.word.strip()
                if t:
                    out.append({"w": t, "s": round(w.start, 2), "e": round(w.end, 2)})
        return out
    return _run(go)

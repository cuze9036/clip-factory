"""Local speech-to-text with faster-whisper (no API cost).
Uses the GPU when it works; if the GPU libraries are missing it switches to the CPU and remembers that."""
from . import config, db

_model = None

GPU_ERRORS = ("cublas", "cudnn", "cuda", "cudart")


def _get():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        cfg = config.section("whisper")
        device = cfg.get("device", "auto")
        if device == "auto" and db.kv_get("whisper_cpu_only"):
            device = "cpu"
        _model = WhisperModel(cfg.get("model", "small.en"), device=device,
                              compute_type=cfg.get("compute_type", "int8"))
    return _model


def _run(fn):
    global _model
    try:
        return fn(_get())
    except (RuntimeError, OSError) as e:
        if not any(k in str(e).lower() for k in GPU_ERRORS) or db.kv_get("whisper_cpu_only"):
            raise
        db.kv_set("whisper_cpu_only", True)
        db.log(None, f"GPU speech libraries not usable ({str(e)[:80]}); captions now run on the CPU", "warn")
        _model = None
        return fn(_get())


def segments(path):
    def go(model):
        segs, _ = model.transcribe(str(path), language="en", vad_filter=True)
        return [{"start": s.start, "end": s.end, "text": s.text} for s in segs]
    return _run(go)


def words(path):
    def go(model):
        segs, _ = model.transcribe(str(path), language="en", vad_filter=True, word_timestamps=True)
        out = []
        for s in segs:
            for w in s.words or []:
                t = w.word.strip()
                if t:
                    out.append({"w": t, "s": round(w.start, 2), "e": round(w.end, 2)})
        return out
    return _run(go)

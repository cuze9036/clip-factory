"""Local speech-to-text with faster-whisper (no API cost)."""
from . import config

_model = None


def _get():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        cfg = config.section("whisper")
        _model = WhisperModel(cfg.get("model", "small.en"), device=cfg.get("device", "auto"),
                              compute_type=cfg.get("compute_type", "int8"))
    return _model


def segments(path):
    segs, _ = _get().transcribe(str(path), language="en", vad_filter=True)
    return [{"start": s.start, "end": s.end, "text": s.text} for s in segs]


def words(path):
    segs, _ = _get().transcribe(str(path), language="en", vad_filter=True, word_timestamps=True)
    out = []
    for s in segs:
        for w in s.words or []:
            t = w.word.strip()
            if t:
                out.append({"w": t, "s": round(w.start, 2), "e": round(w.end, 2)})
    return out

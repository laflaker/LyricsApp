import re

DURATION_TOLERANCE = 4.0

_BRACKETS_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_SUFFIX_RE = re.compile(
    r"\s+[-\u2013\u2014]\s+(?:\d{4}\s+)?"
    r"(?:remaster(?:ed)?|live|mono|stereo|single|radio|edit|version|deluxe|bonus|ремастер\w*|версия)\b.*$",
    re.IGNORECASE,
)
_FEAT_SPLIT_RE = re.compile(r"\s*(?:,|;|&|\b(?:feat|ft)\b\.?)\s*", re.IGNORECASE)

_TS_RE = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d+)?)\]")
_WORD_TS_RE = re.compile(r"<\d+:\d{1,2}(?:[.:]\d+)?>")


def clean_title(title):
    t = _BRACKETS_RE.sub("", title or "")
    t = _SUFFIX_RE.sub("", t).strip()
    return t or (title or "").strip()


def clean_artist(artist):
    a = _BRACKETS_RE.sub("", artist or "").strip()
    return a or (artist or "").strip()


def first_artist(artist):
    parts = _FEAT_SPLIT_RE.split(artist or "", maxsplit=1)
    return (parts[0] or "").strip() or (artist or "").strip()


def unique(seq):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


def _norm(s):
    return re.sub(r"\W+", "", (s or "").lower())


def artist_match(wanted, found):
    a, b = _norm(wanted), _norm(found)
    return bool(a) and bool(b) and (a in b or b in a)


def parse_lrc(text):
    entries = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        stamps = []
        while True:
            m = _TS_RE.match(line)
            if not m:
                break
            stamps.append(int(m.group(1)) * 60 + float(m.group(2).replace(":", ".")))
            line = line[m.end():].lstrip()
        if not stamps:
            continue
        body = _WORD_TS_RE.sub("", line).strip()
        for t in stamps:
            entries.append((t, body))
    entries.sort(key=lambda e: e[0])
    if not any(body for _, body in entries):
        return []
    return entries


def pick_lrclib(items, artist, duration, strict_artist=False):
    best_synced = None
    best_plain = None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if strict_artist and not artist_match(artist, it.get("artistName") or ""):
            continue
        score = 50.0
        d = it.get("duration")
        if duration and isinstance(d, (int, float)) and d > 0:
            diff = abs(d - duration)
            if diff > DURATION_TOLERANCE:
                continue
            score = diff
        elif not duration:
            score = 0.0
        synced, plain = it.get("syncedLyrics"), it.get("plainLyrics")
        if synced and (best_synced is None or score < best_synced[0]):
            best_synced = (score, synced)
        if plain and (best_plain is None or score < best_plain[0]):
            best_plain = (score, plain)
    return (best_synced[1] if best_synced else None,
            best_plain[1] if best_plain else None)


def clean_genius_lines(text):
    lines = (text or "").splitlines()
    if lines and ("Lyrics" in lines[0] or "Текст песни" in lines[0]):
        lines = lines[1:]
    out = []
    for line in lines:
        line = line.strip()
        if not line or re.fullmatch(r"\[.*\]", line) or line == "You might also like":
            continue
        line = re.sub(r"\d*Embed$", "", line).strip()
        if line:
            out.append(line)
    return out


def spread_lines(lines, duration):

    lines = [l.strip() for l in lines if l and l.strip()]
    if not lines:
        return []
    if duration and duration > 20:
        start = min(10.0, duration * 0.06)
        end = duration - min(12.0, duration * 0.06)
        if end <= start + len(lines) * 0.5:
            end = start + len(lines) * 0.5
        step = (end - start) / len(lines)
    else:
        start, step = 0.0, 4.0
    return [(start + i * step, line) for i, line in enumerate(lines)]


def ease_out_cubic(t):
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3

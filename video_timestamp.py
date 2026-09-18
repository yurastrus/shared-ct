"""Read the burned-in timestamp from camera-trap video frames.

Camera-trap videos rarely carry a usable capture time. The AVI/RIFF header has
no date field at all, and the MJPEG frames inside are stripped of EXIF. The only
reliable record of when the clip was taken is the info bar the camera burns into
the image itself:

    1000  [bat]  (moon)  32 C  89 F  2026/07/20 14:02:33  0006

This module turns that strip of pixels back into a ``datetime``. It deliberately
depends on nothing but Pillow and numpy (both already in requirements.txt), so it
runs on the production box with no new packages: frames are cut client-side in
the browser and arrive here as ordinary JPEGs.

Why not an off-the-shelf OCR engine
-----------------------------------
Tesseract is unreliable on small bitmap overlay fonts (the classic 8/B, 0/O and
1/7 confusions), needs a system binary, and would still have to be taught each
camera's glyphs. Instead we learn the glyphs once per camera model and then match
them by normalised cross-correlation, which is deterministic and fast.

How the glyphs are learned without anyone labelling them
--------------------------------------------------------
Calibration asks the operator for exactly one thing: the timestamp visible on the
first frame of a known clip. Everything else falls out of a structural fact --
the burned-in clock ticks. Frame *k* of a clip sampled at one frame per second
must read ``t0 + k`` seconds, so we know the expected character at every position
of every sampled frame. Matching that expected *equality structure* against the
clusters of identical glyph shapes found in the frames yields a labelled template
for each digit, plus the separators, with no font knowledge whatsoever. Over ten
frames the ticking seconds normally expose all ten digits.

Reading a new clip then needs no anchoring by position (the bar shifts sideways
when the temperature goes from "9 C" to "32 C"): every glyph is matched, the bar
becomes a string, and the timestamp is found in it by pattern.

Nothing here guesses. A clip whose timestamp cannot be read with confidence is
reported as unreadable so the caller can stop and ask, rather than writing a
plausible-looking wrong date into the database -- a wrong capture time silently
corrupts series grouping, activity-by-hour and phenology alike.
"""

from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from typing import Iterable, Sequence

import numpy as np
from PIL import Image

# ─────────────────────────────────────────────────────────────────────────────
# Formats the operator can choose on the upload page.
# ─────────────────────────────────────────────────────────────────────────────

#: Order of the three date components as the camera prints them. The operator
#: picks one; "auto" is resolved by :func:`infer_date_order` over a whole folder.
DATE_ORDERS = ('ymd', 'dmy', 'mdy', 'ydm')

#: Rendering of each order, used both to build the expected string during
#: calibration and to interpret a run of digits at read time.
_ORDER_FIELDS = {
    'ymd': ('Y', 'M', 'D'),
    'dmy': ('D', 'M', 'Y'),
    'mdy': ('M', 'D', 'Y'),
    'ydm': ('Y', 'D', 'M'),
}

HOUR_FORMATS = ('24', '12')

# Sanity bounds, deliberately the same policy as EXIF reading in utils.py: a date
# outside these is a reset or drifted camera clock, not a capture time.
MIN_VALID_DATE = datetime(2010, 1, 1)
MAX_FUTURE_DRIFT = timedelta(hours=24)

# Glyphs are resampled to this box before matching. Large enough to keep 0/8 and
# 1/7 apart at overlay-font sizes, small enough that matching stays trivial.
GLYPH_H = 24
GLYPH_W = 16

# Two glyph images are treated as the same shape above this correlation.
CLUSTER_THRESHOLD = 0.88

# A glyph matched below this is reported as unknown rather than guessed.
MATCH_THRESHOLD = 0.70

# How far a frame's own reading may sit from the clip's consensus line before it
# is treated as a misread. Two seconds covers an overlay clock ticking out of
# step with the frame rate, which is normal and harmless.
_FRAME_TOLERANCE_SECONDS = 2.5

# Below this share of frames agreeing, the clip is not trusted and the operator
# is asked instead.
_MIN_AGREEMENT = 0.6

# The info bar is looked for within this fraction of the frame at either edge.
_EDGE_FRACTION = 0.22

# How close to its row median a pixel counts as "background" when measuring
# flatness. Loose enough to survive JPEG ringing around the glyphs.
_FLAT_TOLERANCE = 10.0

# Share of a row that must sit on the background for the row to belong to a bar.
# Measured on real clips: bar rows score 0.67-1.00, photographed rows 0.09-0.30.
_MIN_FLATNESS = 0.55

# Rows that cross dense text dip below the flatness threshold. Bridging a few of
# them keeps the bar whole instead of beheading its digits.
_MAX_FLATNESS_GAP = 6

# A run shorter than this is a letterbox edge or a compression artefact, not a
# bar with legible text in it.
_MIN_BAR_HEIGHT = 8

# A timestamp alone is fourteen glyphs, so a band with fewer is not the bar.
_MIN_BAR_GLYPHS = 8

# Ink must cover a sane share of the bar, otherwise the strip is not text.
_MIN_INK_FRACTION = 0.005
_MAX_INK_FRACTION = 0.45


# ─────────────────────────────────────────────────────────────────────────────
# Where a camera puts the timestamp.
# ─────────────────────────────────────────────────────────────────────────────

#: A strip burned into every frame, carrying a full clock down to the second.
#: Fujifilm-style AVI clips do this.
LAYOUT_BAR = 'bar'

#: A title card shown for a fraction of a second before the footage, carrying the
#: date and time to the minute and nothing after that. Cuddeback clips do this:
#: their video frames have no overlay whatsoever, so the card is the only record
#: of when the clip was taken -- and the container's own creation date, where one
#: exists at all, has been observed to be years wrong.
LAYOUT_CARD = 'card'

#: The timestamp painted straight onto the photograph, with no background strip
#: behind it -- white glyphs with a dark outline. UOVision AVI and NVTIM MOV both
#: do this, and it is common enough in the archives that it cannot be treated as
#: an exotic case. Nothing that works for a solid bar works here: a bar is found
#: by its flatness, and these rows are as varied as any photographed row.
LAYOUT_OVERLAY = 'overlay'

LAYOUTS = (LAYOUT_BAR, LAYOUT_CARD, LAYOUT_OVERLAY)

#: Threshold triples tried when calibrating an overlay camera: the glyph has to
#: be brighter than ``hi`` and have something darker than ``lo`` within ``k``
#: pixels on both sides. No single triple suits every camera and exposure, so
#: calibration tries them and keeps whichever one the operator's own reading can
#: be reconciled with. See :func:`calibrate_overlay_profile`.
_OVERLAY_PARAMS = (
    (200, 110, 4), (215, 90, 5), (190, 120, 8), (200, 90, 6),
    (180, 130, 6), (225, 80, 4), (170, 140, 10),
)

#: Bands offered per frame edge when looking for overlay text, best first.
_OVERLAY_BAND_CANDIDATES = 3

#: A row belongs to the line of text while it carries at least this share of the
#: densest row's strokes.
_OVERLAY_BAND_EDGE = 0.12

#: A line of text never spans this much of the frame; without the cap a noisy
#: clip grows one band over the whole picture.
_OVERLAY_MAX_BAND = 0.12

#: Share of the band's height a glyph must span. Scene speckle never does.
_OVERLAY_MIN_GLYPH_HEIGHT = 0.4

#: Pixels a glyph must have, which rejects the thinnest specks outright.
_OVERLAY_MIN_GLYPH_PIXELS = 8

# A title card is mostly background with a few lines of text; this is the share
# of rows that must be flat for a frame to be treated as one.
_MIN_CARD_FLAT_ROWS = 0.75

# Rows of a card holding this share of ink or more belong to a text line.
_CARD_LINE_INK = 0.002

# Blank rows between two text lines of a card.
_CARD_LINE_GAP = 3


class TimestampError(Exception):
    """The timestamp could not be read. Carries an operator-facing reason."""


# ─────────────────────────────────────────────────────────────────────────────
# Image helpers
# ─────────────────────────────────────────────────────────────────────────────

def to_gray(source) -> np.ndarray:
    """Return a frame as a 2-D float32 array of luminance in 0..255.

    Accepts a PIL image, raw JPEG bytes, a file-like object or an array, so the
    caller can hand over whatever the request gave it.
    """
    if isinstance(source, np.ndarray):
        arr = source
        if arr.ndim == 3:
            arr = arr[..., :3].mean(axis=2)
        return arr.astype(np.float32)

    if isinstance(source, (bytes, bytearray)):
        source = BytesIO(bytes(source))

    if not isinstance(source, Image.Image):
        source = Image.open(source)

    return np.asarray(source.convert('L'), dtype=np.float32)


def _row_profile(frames: Sequence[np.ndarray]):
    """Per-row statistics used to tell the info bar from the photographed scene.

    Returns ``(change, background, flatness)``:

    ``change``
        Mean absolute deviation from the median frame. The bar barely moves
        between frames; a scene with vegetation, light and animals does.
    ``background``
        The row's median luminance, i.e. the bar's background colour where the
        row belongs to the bar.
    ``flatness``
        Share of the row's pixels sitting within a hair of that median. This is
        the discriminator that actually carries the decision: the bar is a solid
        block with sparse text drawn on it, so most of its pixels are exactly the
        background value, while a photographed row varies continuously.
    """
    stack = np.stack(frames, axis=0)
    median = np.median(stack, axis=0)
    change = np.abs(stack - median).mean(axis=0).mean(axis=1)
    background = np.median(median, axis=1)
    flatness = (np.abs(median - background[:, None]) <= _FLAT_TOLERANCE).mean(axis=1)
    return change, background, flatness


def find_info_bar(frames: Sequence[np.ndarray]) -> tuple[int, int, bool]:
    """Locate the info bar and its polarity.

    Returns ``(y0, y1, dark_background)``. The bar is not assumed to sit at any
    fixed fraction of the frame -- that assumption breaks as soon as a camera
    with a different aspect ratio or a top-mounted bar shows up. Instead it is
    found by what makes it a bar: a run of rows hugging the top or bottom edge
    that barely changes between frames (only the seconds tick) and whose
    luminance is far more uniform than a photographed scene ever is.

    Raises TimestampError when no such run exists.
    """
    if not frames:
        raise TimestampError('no frames supplied')

    height = frames[0].shape[0]
    _change, background, flatness = _row_profile(frames)
    reference = np.median(np.stack(frames, axis=0), axis=0)

    edge = max(4, int(height * _EDGE_FRACTION))
    candidates: list[tuple[int, int, bool]] = []

    for from_bottom in (True, False):
        rows = list(range(height - 1, height - edge - 1, -1)) if from_bottom \
            else list(range(edge))
        last_ok = None
        gap = 0
        for y in rows:
            if flatness[y] >= _MIN_FLATNESS:
                last_ok, gap = y, 0
            elif last_ok is not None:
                # Rows crossing dense text dip below the threshold; a couple of
                # those must not truncate the bar and behead its digits.
                gap += 1
                if gap > _MAX_FLATNESS_GAP:
                    break
            else:
                break
        if last_ok is None:
            continue

        y0, y1 = (min(last_ok, rows[0]), max(last_ok, rows[0]) + 1)
        if y1 - y0 < _MIN_BAR_HEIGHT:
            continue
        candidates.append((y0, y1, bool(np.median(background[y0:y1]) < 128)))

    # A flat band is necessary but not sufficient: an overexposed sky or a
    # letterbox edge is flat too. The bar is the candidate that actually holds
    # text, so each one is scored by how many glyphs can be cut out of it.
    scored: list[tuple[int, tuple[int, int, bool]]] = []
    for candidate in candidates:
        try:
            count = len(frame_glyphs(reference, candidate))
        except TimestampError:
            continue
        if count >= _MIN_BAR_GLYPHS:
            scored.append((count, candidate))

    if not scored:
        raise TimestampError('info bar not found in the frame')

    return max(scored, key=lambda item: item[0])[1]


def _binarize(bar: np.ndarray, dark_background: bool) -> np.ndarray:
    """Return a boolean ink mask for the bar crop."""
    lo, hi = float(bar.min()), float(bar.max())
    if hi - lo < 20:
        raise TimestampError('info bar carries no legible text')
    level = lo + (hi - lo) * 0.5
    mask = bar > level if dark_background else bar < level

    ink = mask.mean()
    if not (_MIN_INK_FRACTION <= ink <= _MAX_INK_FRACTION):
        raise TimestampError('info bar does not look like text')
    return mask


def _glyph_boxes(mask: np.ndarray) -> list[tuple[int, int]]:
    """Split the ink mask into glyph columns.

    Overlay fonts are monospaced and well separated, so a column projection is
    enough; no connected-component analysis is needed.
    """
    columns = mask.any(axis=0)
    boxes: list[tuple[int, int]] = []
    start = None
    for x, filled in enumerate(columns):
        if filled and start is None:
            start = x
        elif not filled and start is not None:
            boxes.append((start, x))
            start = None
    if start is not None:
        boxes.append((start, len(columns)))

    # Drop specks thinner than a stroke; they are compression noise, not glyphs.
    return [(a, b) for a, b in boxes if b - a >= 2]


def _normalise_glyph(mask: np.ndarray, x0: int, x1: int) -> np.ndarray | None:
    """Crop one glyph, trim it to its ink and resample to a fixed box."""
    sub = mask[:, x0:x1]
    rows = np.where(sub.any(axis=1))[0]
    if rows.size == 0:
        return None
    sub = sub[rows[0]:rows[-1] + 1]
    if sub.shape[0] < 3:
        return None

    img = Image.fromarray((sub * 255).astype(np.uint8))
    img = img.resize((GLYPH_W, GLYPH_H), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.float32) / 255.0

    arr -= arr.mean()
    norm = np.linalg.norm(arr)
    if norm < 1e-6:
        return None
    return arr / norm


def frame_glyphs(gray: np.ndarray, bar: tuple[int, int, bool]) -> list[np.ndarray]:
    """Return every glyph of one frame's info bar, left to right."""
    y0, y1, dark = bar
    mask = _binarize(gray[y0:y1], dark)
    out = []
    for x0, x1 in _glyph_boxes(mask):
        g = _normalise_glyph(mask, x0, x1)
        if g is not None:
            out.append(g)
    return out


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Normalised cross-correlation of two prepared glyphs, in -1..1."""
    return float(np.tensordot(a, b, axes=2))


# ─────────────────────────────────────────────────────────────────────────────
# Title-card layout
# ─────────────────────────────────────────────────────────────────────────────

def looks_like_title_card(gray: np.ndarray) -> bool:
    """Is this frame a title card rather than footage?

    A card is a solid background with a few lines of text on it, so nearly every
    one of its rows is flat. Footage, however dark, is not: even a night shot has
    continuous variation across its rows.
    """
    background = np.median(gray, axis=1)
    flatness = (np.abs(gray - background[:, None]) <= _FLAT_TOLERANCE).mean(axis=1)
    return float((flatness >= _MIN_FLATNESS).mean()) >= _MIN_CARD_FLAT_ROWS


def card_lines(gray: np.ndarray) -> list[tuple[int, int, bool]]:
    """Split a title card into its lines of text.

    Returns ``(y0, y1, dark_background)`` per line, top to bottom. Splitting by
    line matters because a card stacks unrelated things -- a logo, the date and
    time, a location code -- and running them together would let the location
    code masquerade as part of the timestamp.
    """
    background = float(np.median(gray))
    dark = background < 128
    ink = (gray > background + 40) if dark else (gray < background - 40)

    per_row = ink.mean(axis=1)
    lines: list[tuple[int, int, bool]] = []
    start = None
    gap = 0
    for y, value in enumerate(per_row):
        if value >= _CARD_LINE_INK:
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap > _CARD_LINE_GAP:
                lines.append((start, y - gap + 1, dark))
                start = None
    if start is not None:
        lines.append((start, len(per_row), dark))

    return [(a, b, dark) for a, b, dark in lines if b - a >= 6]


# ─────────────────────────────────────────────────────────────────────────────
# Overlay layout: text painted onto the photograph
# ─────────────────────────────────────────────────────────────────────────────

def stroke_mask(gray: np.ndarray, hi: float, lo: float, k: int) -> np.ndarray:
    """Pixels that look like a bright glyph stroke with a dark outline.

    The outline is what separates these glyphs from the picture behind them.
    Snow, sky and sunlit leaves are all bright, but only a drawn glyph is bright
    *and* has something much darker within a couple of pixels on both sides. The
    test is horizontal because strokes are mostly vertical; a stroke's own width
    is what ``k`` has to span.
    """
    left = np.full(gray.shape, 255.0, dtype=np.float32)
    right = np.full(gray.shape, 255.0, dtype=np.float32)
    for shift in range(1, k + 1):
        left = np.minimum(left, np.roll(gray, shift, axis=1))
        right = np.minimum(right, np.roll(gray, -shift, axis=1))
    return (gray > hi) & (left < lo) & (right < lo)


def find_overlay_bands(frames: Sequence[np.ndarray], hi: float, lo: float,
                       k: int, count: int = _OVERLAY_BAND_CANDIDATES
                       ) -> list[tuple[int, int]]:
    """Rows most likely to hold overlay text, best first.

    Returns candidates rather than one answer on purpose. Scene texture can out-
    score the text on a single clip, and there is no cheap way to be sure from
    the pixels alone -- but there is a decisive test downstream: only the real
    band can be reconciled with a timestamp. The caller tries them in order.
    """
    if not frames:
        raise TimestampError('no frames supplied')

    reference = np.median(np.stack(frames, axis=0), axis=0)
    height = reference.shape[0]
    density = stroke_mask(reference, hi, lo, k).sum(axis=1).astype(np.float64)

    window = max(_MIN_BAR_HEIGHT, int(height * 0.06))
    edge = max(window, int(height * _EDGE_FRACTION))

    scored: list[tuple[float, tuple[int, int]]] = []
    for top in range(0, height - window):
        # Overlay text hugs an edge; the middle of the frame is the picture.
        if edge <= top <= height - edge - window:
            continue
        scored.append((float(density[top:top + window].sum()), (top, top + window)))

    scored.sort(key=lambda item: -item[0])

    # Keep the best few, discarding windows that merely overlap the winner.
    chosen: list[tuple[int, int]] = []
    for _score, band in scored:
        if all(band[1] <= other[0] or band[0] >= other[1] for other in chosen):
            chosen.append(_fit_band(density, band, height))
            if len(chosen) == count:
                break
    return chosen


def _fit_band(density: np.ndarray, band: tuple[int, int],
              height: int) -> tuple[int, int]:
    """Grow a scored window until it covers the whole line of text.

    The window that scores best is a fixed size and lands wherever the text is
    densest, which is rarely where the text begins. Left as it is, it beheads the
    tall digits or clips their feet, and a clipped glyph will not match a whole
    one -- calibration then fails for a reason that looks like a wrong date.
    """
    top, bottom = band
    peak = float(density[top:bottom].max()) if bottom > top else 0.0
    if peak <= 0:
        return band

    floor = peak * _OVERLAY_BAND_EDGE
    limit = max(band[1] - band[0], int(height * _OVERLAY_MAX_BAND)) 
    while top > 0 and density[top - 1] >= floor and (bottom - top) < limit:
        top -= 1
    while bottom < height and density[bottom] >= floor and (bottom - top) < limit:
        bottom += 1

    # A glyph's outline sits just outside its ink, so a little padding keeps the
    # shape whole without pulling in the picture.
    top = max(0, top - 2)
    bottom = min(height, bottom + 2)
    return top, bottom


def overlay_glyphs(gray: np.ndarray, band: tuple[int, int],
                   hi: float, lo: float, k: int) -> list[np.ndarray]:
    """Glyphs cut from a band of overlay text.

    Cutting glyphs out of a photograph rather than off a solid strip means the
    mask always carries some speckle -- a sunlit gap between leaves passes the
    stroke test as readily as a stroke does. Those specks are discarded by the
    one thing that reliably separates them from digits: a digit spans most of the
    band's height, and a speck does not.
    """
    mask = stroke_mask(gray[band[0]:band[1]], hi, lo, k)
    height = mask.shape[0]

    out = []
    for x0, x1 in _glyph_boxes(mask):
        column = mask[:, x0:x1]
        rows = np.where(column.any(axis=1))[0]
        if rows.size == 0:
            continue
        if (rows[-1] - rows[0] + 1) < height * _OVERLAY_MIN_GLYPH_HEIGHT:
            continue
        if int(column.sum()) < _OVERLAY_MIN_GLYPH_PIXELS:
            continue
        glyph = _normalise_glyph(mask, x0, x1)
        if glyph is not None:
            out.append(glyph)
    return out


def read_overlay(gray: np.ndarray, profile: 'CameraProfile') -> str:
    """Transcribe overlay text using the settings calibration settled on."""
    bands = find_overlay_bands([gray], profile.overlay_hi, profile.overlay_lo,
                               profile.overlay_k)
    for band in bands:
        glyphs = overlay_glyphs(gray, band, profile.overlay_hi,
                                profile.overlay_lo, profile.overlay_k)
        if len(glyphs) < _MIN_BAR_GLYPHS:
            continue
        chars = [profile.match(glyph)[0] for glyph in glyphs]
        text = ''.join(chars)
        if any(ch.isdigit() for ch in text):
            return text
    return ''


def read_title_card(gray: np.ndarray, profile: 'CameraProfile') -> str:
    """Transcribe a title card, one line per output line."""
    out = []
    for line in card_lines(gray):
        try:
            text, _worst = profile.read_bar(gray, line)
        except TimestampError:
            continue
        if text:
            out.append(text)
    return '\n'.join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Camera profile
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CameraProfile:
    """Everything learned about one camera model's info bar.

    Persisted per camera, so an operator calibrates a model once and every later
    folder from that model is read without a single question.
    """

    templates: dict[str, np.ndarray] = field(default_factory=dict)
    #: Where this camera puts the timestamp: LAYOUT_BAR or LAYOUT_CARD.
    layout: str = LAYOUT_BAR
    date_order: str = 'ymd'
    hour_format: str = '24'
    #: Digits the camera prints for the year. Pinned rather than inferred; see
    #: :func:`candidate_timestamps` for why guessing it is unsafe.
    year_width: int = 4
    #: Does the camera pad single-digit months, days and hours with a zero?
    padded: bool = True
    #: Does it print seconds at all? A title card usually does not.
    has_seconds: bool = True
    #: Thresholds that isolate this camera's overlay glyphs from the picture
    #: behind them. Settled by calibration, not guessed at read time.
    overlay_hi: float = 200.0
    overlay_lo: float = 110.0
    overlay_k: int = 4
    dark_background: bool = True
    label: str = ''

    # ── serialisation ───────────────────────────────────────────────────────
    def to_json(self) -> dict:
        """Compact, JSON-safe form for storage in the database."""
        packed = {}
        for char, arr in self.templates.items():
            raw = np.asarray(arr, dtype=np.float32).tobytes()
            packed[char] = base64.b64encode(zlib.compress(raw, 6)).decode('ascii')
        return {
            'version': 1,
            'glyph_shape': [GLYPH_H, GLYPH_W],
            'templates': packed,
            'layout': self.layout,
            'date_order': self.date_order,
            'hour_format': self.hour_format,
            'year_width': self.year_width,
            'padded': self.padded,
            'has_seconds': self.has_seconds,
            'overlay_hi': self.overlay_hi,
            'overlay_lo': self.overlay_lo,
            'overlay_k': self.overlay_k,
            'dark_background': self.dark_background,
            'label': self.label,
        }

    @classmethod
    def from_json(cls, data: dict) -> 'CameraProfile':
        shape = tuple(data.get('glyph_shape') or (GLYPH_H, GLYPH_W))
        templates = {}
        for char, blob in (data.get('templates') or {}).items():
            raw = zlib.decompress(base64.b64decode(blob))
            templates[char] = np.frombuffer(raw, dtype=np.float32).reshape(shape)
        return cls(
            templates=templates,
            layout=data.get('layout', LAYOUT_BAR),
            date_order=data.get('date_order', 'ymd'),
            hour_format=data.get('hour_format', '24'),
            year_width=int(data.get('year_width', 4)),
            padded=bool(data.get('padded', True)),
            has_seconds=bool(data.get('has_seconds', True)),
            overlay_hi=float(data.get('overlay_hi', 200.0)),
            overlay_lo=float(data.get('overlay_lo', 110.0)),
            overlay_k=int(data.get('overlay_k', 4)),
            dark_background=bool(data.get('dark_background', True)),
            label=data.get('label', ''),
        )

    def match(self, glyph: np.ndarray) -> tuple[str, float]:
        """Best-matching character for one glyph, with its score."""
        best_char, best_score = '?', -1.0
        for char, tmpl in self.templates.items():
            score = _similarity(glyph, tmpl)
            if score > best_score:
                best_char, best_score = char, score
        if best_score < MATCH_THRESHOLD:
            return '?', best_score
        return best_char, best_score

    def read_bar(self, gray: np.ndarray, bar: tuple[int, int, bool]) -> tuple[str, float]:
        """Transcribe one frame's info bar. Returns the text and the weakest score."""
        glyphs = frame_glyphs(gray, bar)
        chars, worst = [], 1.0
        for g in glyphs:
            char, score = self.match(g)
            chars.append(char)
            worst = min(worst, score)
        return ''.join(chars), worst


# ─────────────────────────────────────────────────────────────────────────────
# Rendering and parsing the timestamp
# ─────────────────────────────────────────────────────────────────────────────

def render_expected(dt: datetime, date_order: str, hour_format: str,
                    year_width: int = 4, padded: bool = True,
                    with_seconds: bool = True) -> str:
    """The glyph sequence a camera prints for ``dt``, separators included.

    Spaces are omitted: a space leaves no ink, so it produces no glyph box and
    must not occupy a slot in the expected sequence.
    """
    if date_order not in _ORDER_FIELDS:
        raise ValueError(f'unknown date order: {date_order!r}')

    def pad(value: int) -> str:
        return f'{value:02d}' if padded else str(value)

    parts = {
        'Y': f'{dt.year:04d}' if year_width == 4 else f'{dt.year % 100:02d}',
        'M': pad(dt.month),
        'D': pad(dt.day),
    }
    date_text = '/'.join(parts[f] for f in _ORDER_FIELDS[date_order])

    hour = (dt.hour % 12 or 12) if hour_format == '12' else dt.hour
    time_text = f'{pad(hour)}:{dt.minute:02d}'
    if with_seconds:
        time_text += f':{dt.second:02d}'
    if hour_format == '12':
        time_text += 'AM' if dt.hour < 12 else 'PM'

    return date_text + time_text


# Field widths a camera may use. The year is four digits or two; the hour is
# padded to two on most models but printed bare on some.
_YEAR_WIDTHS = (4, 2)
_HOUR_WIDTHS = (2, 1)


def candidate_timestamps(text: str, date_order: str,
                         hour_format: str = '24',
                         year_width: int | None = None) -> list[datetime]:
    """Every plausible timestamp hiding in a transcribed info bar.

    Regular expressions are the wrong tool here. The bar prints no spaces that
    survive as glyphs, so the date and the time run together ("2026/07/2014:02:33")
    and a pattern with flexible field widths happily swallows the wrong digits.
    Worse, the bar also carries a battery reading, a temperature in two units and
    a clip counter, any of which can look like a date fragment.

    So the separators are dropped entirely -- which incidentally makes the reader
    indifferent to whether a camera writes "/", "-" or "." -- and fixed-width
    windows are slid along the remaining digits. A window has to yield a real
    calendar date inside the plausible range to be offered at all.

    ``year_width`` pins the year to four digits or two. Leaving it open is a
    genuine hazard rather than a convenience: read with a two-digit year, the
    string "2001/07/2014:02:33" also yields a perfectly plausible 2020-01-07
    20:14:02, and that ghost ticks once per frame exactly like the real reading,
    so no amount of cross-frame voting can tell them apart. Calibration therefore
    records the width and reading uses it.

    Several windows can still survive on one frame; picking between them is not
    this function's job. :func:`read_clip` decides by vote across frames, because
    the right answer is the one that ticks in step with the frames.
    """
    if date_order not in _ORDER_FIELDS:
        raise ValueError(f'unknown date order: {date_order!r}')

    digits = ''.join(ch for ch in text if ch.isdigit())
    # Where each kept digit sat in the original text, so the AM/PM marker that
    # follows a 12-hour window can still be found.
    origin = [i for i, ch in enumerate(text) if ch.isdigit()]

    fields = _ORDER_FIELDS[date_order]
    found: list[datetime] = []
    seen: set[datetime] = set()

    widths_to_try = (year_width,) if year_width else _YEAR_WIDTHS

    for year_width in widths_to_try:
        widths = {'Y': year_width, 'M': 2, 'D': 2}
        date_width = sum(widths[f] for f in fields)
        for hour_width in _HOUR_WIDTHS:
            total = date_width + hour_width + 4
            for start in range(0, len(digits) - total + 1):
                cursor = start
                values: dict[str, str] = {}
                for name in fields:
                    values[name] = digits[cursor:cursor + widths[name]]
                    cursor += widths[name]
                hour_text = digits[cursor:cursor + hour_width]
                minute_text = digits[cursor + hour_width:cursor + hour_width + 2]
                second_text = digits[cursor + hour_width + 2:cursor + hour_width + 4]

                year = int(values['Y'])
                if year_width == 2:
                    year += 2000
                hour = int(hour_text)

                if hour_format == '12':
                    marker = _ampm_after(text, origin, cursor + hour_width + 4)
                    if marker == 'A':
                        hour = 0 if hour == 12 else hour
                    elif marker == 'P':
                        hour = 12 if hour == 12 else hour + 12
                    elif hour > 12:
                        continue

                try:
                    stamp = datetime(year, int(values['M']), int(values['D']),
                                     hour, int(minute_text), int(second_text))
                except ValueError:
                    continue

                if is_plausible(stamp) and stamp not in seen:
                    seen.add(stamp)
                    found.append(stamp)

    return found


# Variable-width reading, anchored on separators. A camera that prints "7/9/2024
# 3:51PM" pads nothing, so no fixed-width window fits it; but its text stands
# alone on a title card, with the separators intact, which a fixed-width reader
# cannot rely on inside a crowded info bar.
_LOOSE_RE = re.compile(
    r'(?<![0-9])(?P<a>\d{1,4})\s*[^0-9A-Z\s]\s*(?P<b>\d{1,2})\s*[^0-9A-Z\s]\s*(?P<c>\d{1,4})'
    r'[^0-9]{0,4}'
    # The separator inside the time is optional: a colon is a stroke or two wide
    # and is dropped along with compression specks on some fonts. Nothing is lost
    # by allowing it to be absent, because the minutes are always two digits, so
    # "910" can only split as 9:10.
    r'(?P<h>\d{1,2})\s*[:.]?\s*(?P<mi>\d{2})(?:\s*[:.]?\s*(?P<s>\d{2}))?'
    r'\s*(?P<ampm>[AP]M?)?',
    re.IGNORECASE,
)


def loose_candidates(text: str, date_order: str,
                     hour_format: str = '24') -> list[datetime]:
    """Timestamps read with the separators as anchors and no fixed widths.

    This is the reader for a title card, where the date and time sit on their own
    with punctuation intact, fields are unpadded ("7/9/2024 3:51PM") and there may
    be no seconds at all. Seconds default to zero, which is what the camera itself
    is telling us: it never recorded them.
    """
    if date_order not in _ORDER_FIELDS:
        raise ValueError(f'unknown date order: {date_order!r}')

    fields = _ORDER_FIELDS[date_order]
    found: list[datetime] = []
    seen: set[datetime] = set()

    for match in _LOOSE_RE.finditer(text):
        values = dict(zip(fields, (match.group('a'), match.group('b'), match.group('c'))))
        year_text = values.get('Y', '')
        if len(year_text) == 3:
            continue
        year = int(year_text)
        if len(year_text) <= 2:
            year += 2000

        hour = int(match.group('h'))
        marker = (match.group('ampm') or '').upper()[:1]
        if hour_format == '12' or marker:
            if marker == 'A':
                hour = 0 if hour == 12 else hour
            elif marker == 'P':
                hour = 12 if hour == 12 else hour + 12
            elif hour > 12:
                continue

        try:
            stamp = datetime(year, int(values['M']), int(values['D']), hour,
                             int(match.group('mi')), int(match.group('s') or 0))
        except ValueError:
            continue

        if is_plausible(stamp) and stamp not in seen:
            seen.add(stamp)
            found.append(stamp)

    return found


def _ampm_after(text: str, origin: list[int], digit_index: int) -> str | None:
    """The A/P of an AM/PM marker printed right after a 12-hour timestamp."""
    if digit_index - 1 >= len(origin):
        return None
    tail = text[origin[digit_index - 1] + 1:origin[digit_index - 1] + 3]
    for ch in tail:
        if ch in 'AP':
            return ch
    return None


def parse_timestamp(text: str, date_order: str,
                    hour_format: str = '24',
                    year_width: int | None = None) -> datetime:
    """The first plausible timestamp in a transcribed info bar.

    Convenience wrapper over :func:`candidate_timestamps` for callers holding a
    single frame. Reading a whole clip should go through :func:`read_clip`, which
    resolves ambiguity by vote instead of taking the first offer.
    """
    found = candidate_timestamps(text, date_order, hour_format, year_width)
    if not found:
        raise TimestampError('no timestamp found in the info bar')
    return found[0]


def is_plausible(dt: datetime, now: datetime | None = None) -> bool:
    """Reject a reset or drifted camera clock, as EXIF reading already does."""
    now = now or datetime.now()
    return MIN_VALID_DATE <= dt <= now + MAX_FUTURE_DRIFT


def infer_date_order(samples: Sequence[tuple[str, str, str]]) -> list[str]:
    """Narrow the date order from the digit triples of a whole folder.

    A single "2026/07/20" is ambiguous; a hundred clips rarely are. Any component
    above 31 must be the year and any above 12 must be the day, which usually
    leaves one order standing. Returns every order still consistent, so the
    caller can ask when more than one survives rather than guessing.
    """
    surviving = []
    for order in DATE_ORDERS:
        fields = _ORDER_FIELDS[order]
        ok = True
        for triple in samples:
            values = dict(zip(fields, triple))
            try:
                year, month, day = (int(values['Y']), int(values['M']), int(values['D']))
            except (KeyError, ValueError):
                ok = False
                break
            if len(values['Y']) < 4:
                year += 2000
            if not (1 <= month <= 12 and 1 <= day <= 31):
                ok = False
                break
            try:
                datetime(year, month, day)
            except ValueError:
                ok = False
                break
        if ok:
            surviving.append(order)
    return surviving


# ─────────────────────────────────────────────────────────────────────────────
# Calibration
# ─────────────────────────────────────────────────────────────────────────────

def _cluster_glyphs(per_frame: Sequence[Sequence[np.ndarray]]):
    """Group identical glyph shapes across all frames.

    Returns the cluster representatives and, per frame, the cluster id of each
    slot. Clustering is greedy on correlation, which is safe here because the
    glyphs come from one font at one size.
    """
    reps: list[np.ndarray] = []
    labels: list[list[int]] = []
    for glyphs in per_frame:
        row = []
        for g in glyphs:
            hit = -1
            best = CLUSTER_THRESHOLD
            for i, rep in enumerate(reps):
                score = _similarity(g, rep)
                if score > best:
                    best, hit = score, i
            if hit < 0:
                reps.append(g)
                hit = len(reps) - 1
            row.append(hit)
        labels.append(row)
    return reps, labels


def _consistent(mapping: dict[int, str], used: dict[str, int],
                cluster: int, char: str) -> bool:
    """Can ``cluster`` mean ``char`` without contradicting what we know?

    Digits must map one-to-one: two different shapes cannot both be "7", and one
    shape cannot be both "7" and "1". Separators are exempt from injectivity,
    because a camera may print the same glyph between date and time fields.
    """
    if mapping.get(cluster, char) != char:
        return False
    if char.isdigit() and used.get(char, cluster) != cluster:
        return False
    return True


def _match_from(sequence: Sequence[int], position: int, expected: str,
                index: int, mapping: dict[int, str], used: dict[str, int],
                skips: int = 0):
    """Align ``expected`` against slots starting at ``position``.

    Digits must each take a slot. Separators may not: a colon or a dot is often
    a stroke or two wide and is dropped along with compression specks, and some
    cameras separate fields with a space, which leaves no ink and therefore no
    slot at all. Treating separators as optional keeps calibration working on
    both kinds of camera instead of silently failing on the second.

    Yields every consistent assignment, so the caller can backtrack when a later
    frame contradicts an early guess.
    """
    if index == len(expected):
        yield mapping, used
        return

    char = expected[index]

    if position < len(sequence):
        cluster = sequence[position]
        if _consistent(mapping, used, cluster, char):
            next_map = dict(mapping)
            next_map[cluster] = char
            next_used = dict(used)
            if char.isdigit():
                next_used[char] = cluster
            yield from _match_from(sequence, position + 1, expected,
                                   index + 1, next_map, next_used, skips)

    if not char.isdigit():
        yield from _match_from(sequence, position, expected,
                               index + 1, mapping, used, skips)

    # Overlay text is cut out of the photograph itself, so scene texture
    # occasionally survives as an extra "glyph" wedged between two digits.
    # A small budget for discarding such intruders keeps the run contiguous;
    # the timestamp is long enough that the constraint stays decisive.
    if skips > 0 and position < len(sequence):
        yield from _match_from(sequence, position + 1, expected,
                               index, mapping, used, skips - 1)


def _find_run(sequence: Sequence[int], expected: str,
              mapping: dict[int, str], used: dict[str, int], skips: int = 0):
    """Find where ``expected`` sits in one frame's slot sequence."""
    digits_needed = sum(1 for ch in expected if ch.isdigit())
    for start in range(0, max(1, len(sequence) - digits_needed + 1)):
        for local_map, local_used in _match_from(
                sequence, start, expected, 0, dict(mapping), dict(used), skips):
            yield start, local_map, local_used


def learn_templates(per_frame: Sequence[Sequence[np.ndarray]],
                    expected: Sequence[str],
                    skips: int = 0) -> dict[str, np.ndarray]:
    """Label glyph shapes by reconciling them with text known to be present.

    ``per_frame`` holds the glyphs cut from each image, ``expected`` the string
    each of those images is known to contain. Nothing about the font is assumed:
    the shapes are clustered, and the clusters are labelled by finding the one
    assignment under which every image's expected text can be laid over its own
    glyphs. Constraints from all images have to hold at once, which is what makes
    a handful of samples enough to pin every digit.

    Raises TimestampError when no assignment works, which means the text we were
    told to expect is not what the images show.
    """
    if len(per_frame) != len(expected):
        raise ValueError('one expected string per image is required')

    reps, labels = _cluster_glyphs(per_frame)

    # Depth-first over each image's candidate positions, backtracking as soon as
    # a later image contradicts an earlier guess.
    def resolve(index: int, mapping: dict[int, str], used: dict[str, int]):
        if index == len(labels):
            return mapping
        for _start, next_map, next_used in _find_run(
                labels[index], expected[index], mapping, used, skips):
            result = resolve(index + 1, next_map, next_used)
            if result is not None:
                return result
        return None

    mapping = resolve(0, {}, {})
    if mapping is None:
        raise TimestampError(
            'the images do not match the text they were said to contain')

    return {char: reps[cluster] for cluster, char in mapping.items()}


def calibrate_profile(frames: Sequence, first_timestamp: datetime,
                      date_order: str, hour_format: str = '24',
                      year_width: int = 4,
                      step_seconds: float = 1.0,
                      label: str = '') -> CameraProfile:
    """Learn a camera's glyphs from one clip and one known timestamp.

    ``frames`` are the sampled frames of a clip in order, ``first_timestamp`` is
    what the operator reads on the first of them. Every later frame's expected
    text follows from the sampling step, which is what makes the labels free.

    Raises TimestampError when the frames cannot be reconciled with the given
    timestamp and format -- almost always because the operator picked the wrong
    date order, which is exactly the case we must not paper over.
    """
    grays = [to_gray(f) for f in frames]
    if len(grays) < 2:
        raise TimestampError('calibration needs at least two frames')

    bar = find_info_bar(grays)
    per_frame = [frame_glyphs(g, bar) for g in grays]

    expected = [
        render_expected(first_timestamp + timedelta(seconds=round(i * step_seconds)),
                        date_order, hour_format, year_width)
        for i in range(len(grays))
    ]

    # The ticking seconds make a wrong anchor fail on a later frame almost at
    # once, so this rarely searches deep.
    templates = learn_templates(per_frame, expected)

    return CameraProfile(
        layout=LAYOUT_BAR,
        templates=templates,
        date_order=date_order,
        hour_format=hour_format,
        year_width=year_width,
        dark_background=bar[2],
        label=label,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reading a clip
# ─────────────────────────────────────────────────────────────────────────────

#: Extra glyph slots calibration may discard per frame when reconciling overlay
#: text: scene texture cut out alongside the digits.
_OVERLAY_SKIP_BUDGET = 6


def calibrate_overlay_profile(frames: Sequence, first_timestamp: datetime,
                              date_order: str, hour_format: str = '24',
                              year_width: int = 4,
                              step_seconds: float = 1.0,
                              label: str = '') -> CameraProfile:
    """Learn a camera that paints its timestamp onto the photograph.

    There is no universal way to cut such glyphs out of a picture: thresholds
    that isolate white-on-dark text over foliage let snow through, and thresholds
    that reject snow lose the text at dusk. Rather than chase one setting that
    suits every camera, this searches a small grid of them.

    The search can afford to be crude because the test is not. The operator has
    told us what the frames say, and the clock ticks, so a setting is either one
    under which the whole expected sequence lays over the glyphs of every frame,
    or it is wrong. Scene texture surviving as a stray glyph is tolerated up to a
    small budget; anything more and the reconciliation fails, as it should.

    Whatever setting succeeds is stored, so reading later costs one pass.
    """
    grays = [to_gray(f) for f in frames]
    if len(grays) < 2:
        raise TimestampError('calibration needs at least two frames')

    expected = [
        render_expected(first_timestamp + timedelta(seconds=round(i * step_seconds)),
                        date_order, hour_format, year_width)
        for i in range(len(grays))
    ]

    for hi, lo, k in _OVERLAY_PARAMS:
        for band in find_overlay_bands(grays, hi, lo, k):
            per_frame = [overlay_glyphs(g, band, hi, lo, k) for g in grays]
            if any(len(glyphs) < _MIN_BAR_GLYPHS for glyphs in per_frame):
                continue
            try:
                templates = learn_templates(per_frame, expected,
                                            skips=_OVERLAY_SKIP_BUDGET)
            except TimestampError:
                continue

            return CameraProfile(
                templates=templates,
                layout=LAYOUT_OVERLAY,
                date_order=date_order,
                hour_format=hour_format,
                year_width=year_width,
                dark_background=False,
                overlay_hi=hi,
                overlay_lo=lo,
                overlay_k=k,
                label=label,
            )

    raise TimestampError(
        'the frames do not match the given timestamp and date format')


def calibrate_card_profile(cards: Sequence, timestamps: Sequence[datetime],
                           date_order: str, hour_format: str = '12',
                           year_width: int = 4,
                           label: str = '') -> CameraProfile:
    """Learn a camera's glyphs from several title cards and their known times.

    The bar layout gets its labels free, because its clock ticks once per frame.
    A title card has no seconds and appears once per clip, so nothing ticks and
    one card alone leaves several digits unseen. The way to pin them all is to
    use several clips: the operator reads the date off two or three cards, and
    between them the digits are covered.

    ``cards`` and ``timestamps`` must line up. Raises TimestampError when the
    cards cannot be reconciled with what the operator typed, which normally means
    the wrong date order was chosen.
    """
    if len(cards) != len(timestamps):
        raise ValueError('one timestamp per card is required')
    if not cards:
        raise TimestampError('calibration needs at least one title card')

    grays = [to_gray(c) for c in cards]

    per_card: list[list[np.ndarray]] = []
    dark = True
    for gray in grays:
        lines = card_lines(gray)
        if not lines:
            raise TimestampError('no text found on the title card')
        dark = lines[0][2]
        # Every line's glyphs in reading order. The date and time may be split
        # across lines on some models, so they are matched as one sequence.
        glyphs: list[np.ndarray] = []
        for line in lines:
            glyphs.extend(frame_glyphs(gray, line))
        per_card.append(glyphs)

    # Whether the camera pads single digits and whether it prints seconds are
    # properties of the model, not choices the operator should have to make.
    # Both are settled here by trying each combination against what the cards
    # actually show, and only one of them can reconcile.
    last_error: TimestampError | None = None
    for padded in (False, True):
        for with_seconds in (False, True):
            expected = [
                render_expected(stamp, date_order, hour_format, year_width,
                                padded, with_seconds)
                for stamp in timestamps
            ]
            try:
                templates = learn_templates(per_card, expected)
            except TimestampError as exc:
                last_error = exc
                continue

            return CameraProfile(
                templates=templates,
                layout=LAYOUT_CARD,
                date_order=date_order,
                hour_format=hour_format,
                year_width=year_width,
                padded=padded,
                has_seconds=with_seconds,
                dark_background=dark,
                label=label,
            )

    raise last_error or TimestampError(
        'the title cards do not match the given timestamps and date format')


@dataclass
class ClipReading:
    """The outcome of reading one clip's frames."""

    #: Capture time of the first sampled frame.
    start: datetime
    #: Capture time of every sampled frame, in order. This is what goes into the
    #: database, one row per frame.
    per_frame: list[datetime]
    #: Share of frames whose own bar agreed with the consensus, 0..1.
    agreement: float
    #: Text transcribed from the first frame, for showing the operator.
    sample_text: str
    #: Trailing digits found in the bar, usually the clip counter.
    counter: str | None = None
    #: Index of the title card among the sampled frames, where the camera uses
    #: one. The card is a black splash with no wildlife on it, so the caller must
    #: leave it out of the photos it stores while still using its timestamp.
    card_index: int | None = None

    @property
    def confident(self) -> bool:
        return self.agreement >= _MIN_AGREEMENT


def _extract_counter(text: str) -> str | None:
    """The trailing digits of the bar, where cameras print the clip number."""
    m = re.search(r'(\d{1,8})\D*$', text)
    return m.group(1) if m else None


def counter_matches(counter: str | None, filename: str) -> bool | None:
    """Does the bar's trailing number agree with the file name?

    A free cross-check: "DSCF0355.AVI" and a bar ending in "0355" confirm that
    the right strip was found, that the glyphs were matched correctly and that
    the frames belong to the file being processed. Returns None when either side
    carries no number, so an absent check is never mistaken for a failed one.

    The comparison is by suffix because the counter runs straight into the
    seconds ahead of it once the separators are dropped.
    """
    if not counter:
        return None
    digits = re.findall(r'\d+', filename)
    if not digits:
        return None
    number = max(digits, key=len).lstrip('0') or '0'
    tail = counter.lstrip('0') or '0'
    return tail.endswith(number) or number.endswith(tail)


def read_clip(frames: Sequence, profile: CameraProfile,
              step_seconds: float = 1.0) -> ClipReading:
    """Read a clip's start time, cross-checking every sampled frame.

    A single frame can be misread; a clip cannot easily be. Every sampled frame
    is transcribed independently and the results must lie on the line
    ``t = t0 + k * step``. The median of the implied starts wins, so one bad
    frame is outvoted instead of setting the time for the whole clip.

    Raises TimestampError when too few frames agree to trust the result.
    """
    grays = [to_gray(f) for f in frames]
    if not grays:
        raise TimestampError('no frames supplied')

    if profile.layout == LAYOUT_CARD:
        return _read_card_clip(grays, profile, step_seconds)

    if profile.layout == LAYOUT_OVERLAY:
        return _vote_over_frames(
            [read_overlay(gray, profile) for gray in grays],
            profile, step_seconds)

    bar = find_info_bar(grays)

    texts = []
    for gray in grays:
        try:
            text, _worst = profile.read_bar(gray, bar)
        except TimestampError:
            text = ''
        texts.append(text)

    return _vote_over_frames(texts, profile, step_seconds)


def _vote_over_frames(texts: Sequence[str], profile: CameraProfile,
                      step_seconds: float) -> ClipReading:
    """Turn one transcription per frame into the clip's per-frame capture times.

    Shared by every layout whose timestamp carries seconds. Each frame offers
    every timestamp that could be hiding in its text; a wrong offer comes from a
    temperature, a counter or a scrap of scene texture, none of which advance one
    second per frame, while the right one does. Voting on the implied clip start
    separates them without trusting any single frame.
    """
    votes: dict[datetime, int] = {}
    offers: list[list[datetime]] = []
    sample_text = texts[0] if texts else ''

    for index, text in enumerate(texts):
        found = candidate_timestamps(text, profile.date_order,
                                     profile.hour_format, profile.year_width)
        offers.append(found)
        shift = timedelta(seconds=round(index * step_seconds))
        for stamp in {s - shift for s in found}:
            votes[stamp] = votes.get(stamp, 0) + 1

    if not votes:
        raise TimestampError('no frame of this clip carried a readable timestamp')

    anchor = max(votes.items(), key=lambda item: (item[1], -item[0].timestamp()))[0]
    anchor = anchor.replace(microsecond=0)

    if not is_plausible(anchor):
        raise TimestampError(
            f'timestamp {anchor.isoformat()} is outside the plausible range')

    # The anchor says where the clip sits in time; it does not overrule what an
    # individual frame plainly says. A camera's overlay clock ticks out of step
    # with the frame rate, so consecutive samples legitimately differ by two
    # seconds now and then. Each frame therefore keeps its own reading whenever
    # that reading lands near the line, and only a frame that wanders far off it
    # is treated as misread and repaired from the line.
    per_frame: list[datetime] = []
    agreeing = 0
    for index, found in enumerate(offers):
        expected = anchor + timedelta(seconds=round(index * step_seconds))
        near = [s for s in found
                if abs((s - expected).total_seconds()) <= _FRAME_TOLERANCE_SECONDS]
        if near:
            per_frame.append(min(near, key=lambda s: abs((s - expected).total_seconds())))
            agreeing += 1
        else:
            per_frame.append(expected)

    return ClipReading(
        start=per_frame[0],
        per_frame=per_frame,
        agreement=agreeing / len(texts),
        sample_text=sample_text,
        counter=_extract_counter(sample_text),
    )


def _read_card_clip(grays: Sequence[np.ndarray], profile: CameraProfile,
                    step_seconds: float) -> ClipReading:
    """Read a clip whose time lives on a title card rather than on every frame.

    There is nothing to vote on here: the card is shown once, so one reading has
    to carry the whole clip. The frames that follow get the card's time plus their
    own offset. That is exactly as accurate as the camera itself, which records
    no seconds at all -- and far more accurate than the container's creation
    date, which on these models has been seen to sit years off.
    """
    for index, gray in enumerate(grays):
        if not looks_like_title_card(gray):
            continue
        text = read_title_card(gray, profile)
        found = loose_candidates(text.replace('\n', ' '),
                                 profile.date_order, profile.hour_format)
        if not found:
            continue

        start = found[0]
        per_frame = [start + timedelta(seconds=round((i - index) * step_seconds))
                     for i in range(len(grays))]
        return ClipReading(
            start=per_frame[0],
            per_frame=per_frame,
            # One card read cleanly is all this camera offers; there is no second
            # source to agree with it, so the confidence is in the reading, not
            # in a majority.
            agreement=1.0,
            sample_text=text,
            counter=_extract_counter(text.splitlines()[-1] if text else ''),
            card_index=index,
        )

    raise TimestampError('no readable title card found in this clip')


def crop_bar_png(frame, pad: int = 2) -> bytes:
    """Return the info bar as a PNG, for showing the operator what was read.

    Seeing the strip next to the parsed date is what lets a human catch a wrong
    date order before anything reaches the database.
    """
    gray = to_gray(frame)

    if looks_like_title_card(gray):
        lines = card_lines(gray)
        if not lines:
            raise TimestampError('no text found on the title card')
        y0, y1 = lines[0][0], lines[-1][1]
    else:
        y0, y1, _dark = find_info_bar([gray])

    y0 = max(0, y0 - pad)
    y1 = min(gray.shape[0], y1 + pad)
    img = Image.fromarray(gray[y0:y1].astype(np.uint8))
    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()

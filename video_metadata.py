"""Capture time from a video container's own header.

The first answer to "when was this clip taken" is the one the camera wrote into
the file. Checked against the burned-in overlay on the archives:

=================  ==========================  ===============================
Camera family      Where the time is           What it means
=================  ==========================  ===============================
SYPR MOV (H.264)   QuickTime ``mvhd`` created  start of the clip (±1 s)
NVTIM MOV (H.264)  QuickTime ``mvhd`` created  **end** of the clip
Cuddeback M4V      QuickTime ``mvhd`` created  nine years wrong
UOVision AVI       nothing                     --
Fujifilm-style AVI nothing                     --
=================  ==========================  ===============================

Three lessons follow, and the page is built around them:

* the value is the camera's local clock, not UTC as the QuickTime spec says, so
  it is used as it stands -- the same clock the camera writes into JPEG EXIF;
* whether it marks the start or the end differs between makes, so the operator
  chooses, with the first frame in front of them to compare against;
* it can be plainly wrong, so it is never written without a human seeing it.

The browser sends only the header (the ``moov`` box of an MP4/MOV, or the first
part of an AVI), never the video. Parsing lives here rather than in the page so
it is covered by the Python tests like everything else that decides a date.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta

#: QuickTime counts seconds from this moment.
_QT_EPOCH = datetime(1904, 1, 1)

#: Earliest year a camera-trap clip in our archives can plausibly carry. A
#: camera whose clock was never set reports its factory default -- 1904, 1970,
#: 2000 -- and none of those is a capture time.
MIN_PLAUSIBLE_YEAR = 2005

#: A header larger than this is not a header.
MAX_HEADER_BYTES = 16 * 1024 * 1024


def _iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    """Yield (type, payload_start, box_end) for ISO-BMFF boxes in a range."""
    end = len(data) if end is None else end
    offset = start
    while offset + 8 <= end:
        size, kind = struct.unpack('>I4s', data[offset:offset + 8])
        header = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = struct.unpack('>Q', data[offset + 8:offset + 16])[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header:
            return
        yield kind, offset + header, min(end, offset + size)
        offset += size


def _parse_mvhd(data: bytes, start: int, end: int):
    """(created, duration_seconds) from an mvhd payload."""
    if end - start < 4:
        return None, None
    version = data[start]
    body = start + 4
    if version == 1:
        if end - body < 28:
            return None, None
        created, _modified, scale, duration = struct.unpack(
            '>QQIQ', data[body:body + 28])
    else:
        if end - body < 16:
            return None, None
        created, _modified, scale, duration = struct.unpack(
            '>IIII', data[body:body + 16])

    created_at = _QT_EPOCH + timedelta(seconds=created) if created else None
    seconds = duration / scale if scale else None
    return created_at, seconds


def quicktime_times(data: bytes):
    """(created, duration) from a ``moov`` box, or from bytes that contain one."""
    for kind, payload, box_end in _iter_boxes(data):
        if kind == b'moov':
            for child, cpayload, cend in _iter_boxes(data, payload, box_end):
                if child == b'mvhd':
                    return _parse_mvhd(data, cpayload, cend)
            return None, None
        if kind == b'mvhd':
            return _parse_mvhd(data, payload, box_end)
    return None, None


# AVI may carry an IDIT chunk ("DateTimeOriginal") in its header list. None of
# the cameras in our archives writes one, but several other makes do.
_IDIT_FORMATS = (
    '%a %b %d %H:%M:%S %Y',   # "Mon Mar 22 15:46:07 2025"
    '%Y:%m:%d %H:%M:%S',
    '%Y/%m/%d %H:%M:%S',
    '%Y-%m-%d %H:%M:%S',
)


def _parse_idit(text: str):
    text = ' '.join(text.replace('\x00', ' ').split())
    for fmt in _IDIT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def avi_times(data: bytes):
    """(created, duration) from the head of an AVI file."""
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'AVI ':
        return None, None

    created = None
    match = re.search(rb'IDIT(....)', data[:MAX_HEADER_BYTES], re.DOTALL)
    if match:
        size = struct.unpack('<I', match.group(1))[0]
        raw = data[match.end():match.end() + min(size, 64)]
        created = _parse_idit(raw.decode('latin-1', errors='replace'))

    duration = None
    avih = data.find(b'avih')
    if avih >= 0 and avih + 8 + 20 <= len(data):
        micros, _rate, _pad, _flags, total = struct.unpack(
            '<IIIII', data[avih + 8:avih + 28])
        if micros and total:
            duration = micros * total / 1_000_000

    return created, duration


_AVI_STRUCTURAL_CHUNKS = {b'avih', b'strh', b'strf', b'indx', b'dmlh'}


def _iter_avi_chunks(data: bytes, start: int = 12, end: int | None = None):
    """Yield metadata-like RIFF chunks before ``movi``.

    AVI has no single universal capture-time field. Cameras commonly put
    private data in ``strd`` or INFO children, so the useful general rule is to
    inspect non-structural header chunks without ever scanning compressed video
    or audio payloads.
    """
    end = len(data) if end is None else min(end, len(data))
    offset = start
    while offset + 8 <= end:
        kind = data[offset:offset + 4]
        size = struct.unpack('<I', data[offset + 4:offset + 8])[0]
        payload = offset + 8
        chunk_end = min(end, payload + size)
        if kind == b'LIST' and payload + 4 <= chunk_end:
            list_kind = data[payload:payload + 4]
            if list_kind == b'movi':
                return
            yield from _iter_avi_chunks(data, payload + 4, chunk_end)
        elif kind not in _AVI_STRUCTURAL_CHUNKS:
            yield kind.decode('latin-1', errors='replace'), payload, chunk_end
        offset = payload + size + (size & 1)


_TEXT_DATE_RE = re.compile(
    r'(?P<date>(?:19|20)\d{2}[-:/.]\d{1,2}[-:/.]\d{1,2})'
    r'[^\d]{0,12}'
    r'(?P<time>\d{1,2}:\d{2}(?::\d{2})?)')


def _text_datetimes(payload: bytes):
    """Date/time strings embedded in an opaque metadata chunk."""
    text = payload.decode('latin-1', errors='ignore').replace('\x00', ' ')
    for match in _TEXT_DATE_RE.finditer(text):
        raw = match.group('date') + ' ' + match.group('time')
        normalized = re.sub(r'[-/.]', '-', raw, count=2)
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                yield match.start(), datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue


def _binary_component_datetimes(payload: bytes):
    """Six adjacent numeric bytes: YY, month, day, hour, minute, second.

    This is deliberately camera-agnostic. Private camera headers frequently
    store a small date struct without a tag. Returning candidates instead of
    choosing one keeps accidental numeric runs visible and harmless.
    """
    for offset in range(max(0, len(payload) - 5)):
        year, month, day, hour, minute, second = payload[offset:offset + 6]
        year += 2000
        try:
            moment = datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
        yield offset, moment


def avi_datetime_candidates(data: bytes, now: datetime | None = None) -> list[dict]:
    """Plausible dates found in AVI header metadata, with their provenance."""
    now = now or datetime.now()
    found = []
    seen = set()
    scan_budget = 64 * 1024

    def add(moment, kind, chunk, offset, recommended=False):
        if not is_plausible(moment, now):
            return
        key = (moment, kind, chunk, offset)
        if key in seen:
            return
        seen.add(key)
        found.append({
            'id': f'avi:{chunk}:{kind}:{offset}',
            'source': f'avi:{chunk}:{kind}',
            'created': moment.isoformat(timespec='seconds'),
            'kind': kind,
            'chunk': chunk,
            'offset': offset,
            'recommended': bool(recommended),
        })

    for chunk, start, end in _iter_avi_chunks(data):
        if scan_budget <= 0:
            break
        # Private chunks are normally tiny. Bound opaque scanning separately
        # from the overall header limit so a crafted JUNK block cannot turn
        # one metadata request into millions of candidate objects.
        payload = data[start:min(end, start + 16 * 1024, start + scan_budget)]
        scan_budget -= len(payload)
        for offset, moment in _text_datetimes(payload):
            add(moment, 'text', chunk, offset, chunk == 'IDIT')
        for offset, moment in _binary_component_datetimes(payload):
            add(moment, 'binary6', chunk, offset)

    found.sort(key=lambda item: (
        not item['recommended'],
        abs(datetime.fromisoformat(item['created']).year - now.year),
        item['chunk'], item['offset']))
    return found[:12]


def is_plausible(moment: datetime | None, now: datetime | None = None) -> bool:
    """Could this be a real capture time? Not a factory default, not the future."""
    if moment is None:
        return False
    now = now or datetime.now()
    return moment.year >= MIN_PLAUSIBLE_YEAR and moment <= now + timedelta(days=1)


def container_times(header: bytes, now: datetime | None = None) -> dict:
    """Whatever the header says about when the clip was taken.

    ``created`` is reported even when implausible, flagged so, rather than
    dropped: the operator deserves to know the camera wrote 2015 into a clip
    from 2024, because that tells them the clock was never set.
    """
    if len(header) > MAX_HEADER_BYTES:
        header = header[:MAX_HEADER_BYTES]

    if header[:4] == b'RIFF':
        created, duration = avi_times(header)
        kind = 'avi'
        candidates = avi_datetime_candidates(header, now)
    else:
        created, duration = quicktime_times(header)
        kind = 'quicktime'
        candidates = []

    if created and is_plausible(created, now):
        standard = {
            'id': f'{kind}:container',
            'source': f'{kind}:container',
            'created': created.isoformat(timespec='seconds'),
            'kind': 'container',
            'chunk': 'mvhd' if kind == 'quicktime' else 'IDIT',
            'offset': None,
            'recommended': True,
        }
        candidates = [standard] + [c for c in candidates
                                   if c['created'] != standard['created']]

    return {
        'container': kind,
        'created': created.isoformat(timespec='seconds') if created else None,
        'duration': round(duration, 3) if duration else None,
        'plausible': is_plausible(created, now),
        'candidates': candidates,
    }

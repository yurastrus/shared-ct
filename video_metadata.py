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
    else:
        created, duration = quicktime_times(header)
        kind = 'quicktime'

    return {
        'container': kind,
        'created': created.isoformat(timespec='seconds') if created else None,
        'duration': round(duration, 3) if duration else None,
        'plausible': is_plausible(created, now),
    }

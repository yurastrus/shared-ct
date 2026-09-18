"""Server side of the camera-trap video upload page.

The browser does the cutting. This module does the part the browser cannot: it
works out when each frame was taken, and it does so without trusting the client
for anything that ends up in the database.

Division of labour
------------------
The page sends frames, never video. For AVI/MJPG that costs the browser nothing
-- every frame in the stream is already a complete JPEG -- and it keeps a 70 MB
clip off the wire and off a disk that is 93% full. See ``static/js/video_slice.js``.

What arrives here is therefore a handful of JPEGs per clip, and the questions
left are the ones worth answering in Python: which camera model this is, what its
overlay says, and whether the reading can be trusted. Glyph matching lives in
:mod:`video_timestamp`; this module is the part that talks to the database, the
session and the upload pipeline.

Why the client is not asked for the capture time
------------------------------------------------
It would be simpler to have the browser read the timestamp and post it with each
frame. It would also mean the most consequential field in the record arrives from
a place we do not control. A wrong capture time is not a visible error: it
quietly misgroups series, shifts activity-by-hour and distorts phenology, and
nobody notices for a year. So the frames are read here, and the clip's start time
is handed back to the browser inside a signed token which it must return
unmodified with every frame. The browser carries the value; it cannot choose it.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import video_timestamp as vt
from .database import get_ct_session
from .models import CameraTimestampProfile

#: How long a clip token stays valid. Comfortably longer than the slowest upload
#: of one clip's frames, short enough that a leaked token is worthless.
TOKEN_MAX_AGE_SECONDS = 6 * 60 * 60

_TOKEN_SALT = 'ct-video-clip'

#: Frames sampled from each clip. One per second is what the page cuts, and a
#: ten-second clip therefore becomes a ten-photo series.
DEFAULT_STEP_SECONDS = 1.0

#: How many frames of a clip are read to establish its time. Reading all of them
#: buys little once the consensus is clear and costs a decode each.
MAX_FRAMES_TO_READ = 12


class VideoUploadError(Exception):
    """Something the operator needs to be told about in plain words."""


# ─────────────────────────────────────────────────────────────────────────────
# Clip tokens
# ─────────────────────────────────────────────────────────────────────────────

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=_TOKEN_SALT)


def issue_clip_token(batch_id: str, filename: str, start: datetime,
                     step_seconds: float, card_index: int | None) -> str:
    """Sign a clip's established start time for the browser to carry back.

    The browser needs to label each frame it uploads, but it must not be able to
    decide what the label says. Signing the start time here keeps the decision on
    the server while leaving the bookkeeping on the client, and avoids a server
    side session entry per clip that would have to be cleaned up after every
    abandoned upload.
    """
    return _serializer().dumps({
        'b': batch_id,
        'f': filename,
        't': start.isoformat(timespec='seconds'),
        's': float(step_seconds),
        'c': card_index,
    })


def frame_capture_time(token: str, batch_id: str, filename: str,
                       frame_index: int) -> datetime:
    """Recover a frame's capture time from its clip token.

    The batch and file name are checked against the token, so a token issued for
    one clip cannot be replayed to backdate another. Raises VideoUploadError on
    anything that does not add up.
    """
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        raise VideoUploadError('this upload took too long; start it again')
    except BadSignature:
        raise VideoUploadError('the frame does not belong to this upload')

    if data.get('b') != batch_id or data.get('f') != filename:
        raise VideoUploadError('the frame does not belong to this clip')

    if frame_index < 0:
        raise VideoUploadError('negative frame index')

    start = datetime.fromisoformat(data['t'])
    return start + timedelta(seconds=round(frame_index * float(data['s'])))


def card_frame_index(token: str) -> int | None:
    """Which frame of the clip is the title card, if any.

    The card is a black splash with the date printed on it and no wildlife in it.
    It supplies the clip's time and must not be stored as a photo.
    """
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get('c')


# ─────────────────────────────────────────────────────────────────────────────
# Stored camera profiles
# ─────────────────────────────────────────────────────────────────────────────

def list_profiles() -> list[dict]:
    """Every calibrated camera, newest first, for the page's picker."""
    session = get_ct_session()
    rows = session.query(CameraTimestampProfile)\
        .order_by(CameraTimestampProfile.updated_at.desc()).all()
    return [{
        'id': row.id,
        'name': row.name,
        'layout': row.layout,
        'date_order': row.date_order,
        'hour_format': row.hour_format,
    } for row in rows]


def load_profile(profile_id: int) -> vt.CameraProfile:
    """Rebuild a usable profile from its stored form."""
    session = get_ct_session()
    row = session.query(CameraTimestampProfile).get(profile_id)
    if row is None:
        raise VideoUploadError('this camera profile no longer exists')
    return vt.CameraProfile.from_json(row.profile_json)


def save_profile(name: str, profile: vt.CameraProfile, user_id: int) -> int:
    """Store a freshly calibrated camera, replacing an earlier one of that name.

    Recalibrating under the same name overwrites rather than duplicating: a
    firmware update can change a camera's overlay, and the operator fixing that
    should not end up with two profiles differing only in age.
    """
    session = get_ct_session()
    row = session.query(CameraTimestampProfile).filter_by(name=name).one_or_none()
    if row is None:
        row = CameraTimestampProfile(name=name, created_by=user_id)
        session.add(row)

    row.profile_json = profile.to_json()
    row.layout = profile.layout
    row.date_order = profile.date_order
    row.hour_format = profile.hour_format
    session.commit()
    return row.id


# ─────────────────────────────────────────────────────────────────────────────
# Calibration and reading
# ─────────────────────────────────────────────────────────────────────────────

def calibrate(frames_by_clip: list[list[bytes]], timestamps: list[datetime],
              date_order: str, hour_format: str, year_width: int,
              step_seconds: float = DEFAULT_STEP_SECONDS,
              name: str = '') -> vt.CameraProfile:
    """Teach the reader a camera, given what the operator sees on its frames.

    Which layout this camera uses is settled here rather than asked, by looking
    at the frames: a title card is a flat frame with a few lines of text, and
    footage never is. Getting it wrong is not a silent failure -- neither layout
    can be reconciled with the other's frames -- so there is nothing to gain from
    making the operator classify their camera.
    """
    if not frames_by_clip or not timestamps:
        raise VideoUploadError('no frames to calibrate on')
    if len(frames_by_clip) != len(timestamps):
        raise VideoUploadError('one timestamp per clip is required')

    first = [vt.to_gray(f) for f in frames_by_clip[0][:MAX_FRAMES_TO_READ]]
    if not first:
        raise VideoUploadError('the first clip carried no frames')

    try:
        if vt.looks_like_title_card(first[0]):
            cards = [vt.to_gray(frames[0]) for frames in frames_by_clip if frames]
            return vt.calibrate_card_profile(
                cards, timestamps, date_order, hour_format, year_width, label=name)

        return vt.calibrate_profile(
            first, timestamps[0], date_order, hour_format, year_width,
            step_seconds, label=name)
    except vt.TimestampError as exc:
        raise VideoUploadError(str(exc))


def read_clip(frames: list[bytes], profile: vt.CameraProfile,
              filename: str = '',
              step_seconds: float = DEFAULT_STEP_SECONDS) -> dict:
    """Establish a clip's capture times and report how well it went.

    Returns a dictionary the page renders directly, including the cropped strip
    of pixels the reading came from. Showing that strip next to the parsed date
    is what lets a human catch a wrong date order before anything is written, and
    it costs one small PNG per clip.
    """
    if not frames:
        raise VideoUploadError('this clip carried no frames')

    sample = [vt.to_gray(f) for f in frames[:MAX_FRAMES_TO_READ]]

    try:
        reading = vt.read_clip(sample, profile, step_seconds)
    except vt.TimestampError as exc:
        raise VideoUploadError(str(exc))

    try:
        strip = base64.b64encode(vt.crop_bar_png(sample[reading.card_index or 0]))\
            .decode('ascii')
    except vt.TimestampError:
        strip = ''

    return {
        'start': reading.start.isoformat(timespec='seconds'),
        'agreement': round(reading.agreement, 2),
        'confident': reading.confident,
        'card_index': reading.card_index,
        'counter_matches': vt.counter_matches(reading.counter, filename),
        'sample_text': reading.sample_text,
        'strip_png': strip,
    }

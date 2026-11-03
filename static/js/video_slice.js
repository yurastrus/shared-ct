/* Cut camera-trap video into frames, in the browser.
 *
 * Why here and not on the server: a clip is ~70 MB and the frames we want out of
 * it are ~3 MB. Cutting on the client keeps 95% of that off the wire and off a
 * server volume that is 93% full, and spends no server CPU on decoding.
 *
 * Two strategies, tried in order:
 *
 *   1. AVI with MJPG video. No decoding needed at all: the RIFF stream stores
 *      one complete JPEG per frame, tables and all, so the wanted frames are
 *      extracted by walking chunk headers and slicing bytes. Verified against
 *      real clips — every frame begins FFD8 and carries its own DQT and DHT.
 *      Browsers cannot play AVI, which is exactly why this path exists.
 *
 *   2. Anything the browser itself can decode (MP4/H.264, MOV, WebM): seek a
 *      hidden <video> and paint each wanted moment onto a canvas.
 *
 * A clip that neither path can open is reported as unsupported. That was a
 * deliberate choice over building a server-side fallback: better an honest
 * refusal on a rare format than a second code path to keep working.
 */

(function (global) {
    'use strict';

    /** JPEG quality for frames we re-encode. Frames taken straight out of an
     *  AVI are passed through untouched, so this applies to the canvas path. */
    const CANVAS_JPEG_QUALITY = 0.92;

    /** Seeking is asynchronous and a broken file can simply never fire the
     *  event we wait for, which would hang the whole upload. */
    const SEEK_TIMEOUT_MS = 15000;

    function readAsciiTag(view, offset) {
        return String.fromCharCode(
            view.getUint8(offset), view.getUint8(offset + 1),
            view.getUint8(offset + 2), view.getUint8(offset + 3));
    }

    /**
     * Frame offsets of an AVI/MJPG file, or null when this is not one.
     *
     * Walks the RIFF tree rather than scanning for JPEG markers: a marker scan
     * would also hit the thumbnails and EXIF payloads that live inside the
     * frames themselves.
     */
    function parseAviFrames(buffer) {
        const view = new DataView(buffer);
        if (buffer.byteLength < 32) { return null; }
        if (readAsciiTag(view, 0) !== 'RIFF' || readAsciiTag(view, 8) !== 'AVI ') {
            return null;
        }

        // Find the movi list, skipping the header list that precedes it.
        let offset = 12;
        let moviStart = -1;
        let moviEnd = -1;
        while (offset + 8 <= buffer.byteLength) {
            const tag = readAsciiTag(view, offset);
            const size = view.getUint32(offset + 4, true);
            if (tag === 'LIST' && readAsciiTag(view, offset + 8) === 'movi') {
                moviStart = offset + 12;
                moviEnd = Math.min(buffer.byteLength, offset + 8 + size);
                break;
            }
            offset += 8 + size + (size & 1);
        }
        if (moviStart < 0) { return null; }

        const frames = [];
        let cursor = moviStart;
        while (cursor + 8 <= moviEnd) {
            const tag = readAsciiTag(view, cursor);
            const size = view.getUint32(cursor + 4, true);
            const kind = tag.slice(2);
            // "dc" is compressed video; "wb"/"db" are audio and uncompressed
            // video, which we step over.
            if (kind === 'dc' && size > 4) {
                const start = cursor + 8;
                // A buffer holding only the head of the file ends mid-stream.
                if (start + 2 > buffer.byteLength) { break; }
                // Only a JPEG is usable as a frame on its own. A non-MJPG AVI
                // reaches this point and is correctly rejected here.
                if (view.getUint8(start) === 0xFF && view.getUint8(start + 1) === 0xD8) {
                    frames.push({ start: start, length: size });
                } else {
                    return null;
                }
            }
            cursor += 8 + size + (size & 1);
        }

        return frames.length ? frames : null;
    }

    /** Frame rate declared in the AVI header, used to convert seconds to frames. */
    function parseAviFrameRate(buffer) {
        const view = new DataView(buffer);
        let offset = 12;
        while (offset + 8 <= buffer.byteLength) {
            const tag = readAsciiTag(view, offset);
            const size = view.getUint32(offset + 4, true);
            if (tag === 'LIST' && readAsciiTag(view, offset + 8) === 'hdrl') {
                // avih follows the list header; microseconds per frame is its
                // first field.
                const avih = offset + 12;
                if (readAsciiTag(view, avih) === 'avih') {
                    const microsPerFrame = view.getUint32(avih + 8, true);
                    if (microsPerFrame > 0) {
                        return 1000000 / microsPerFrame;
                    }
                }
                return null;
            }
            offset += 8 + size + (size & 1);
        }
        return null;
    }

    async function sliceAvi(file, stepSeconds) {
        const buffer = await file.arrayBuffer();
        const frames = parseAviFrames(buffer);
        if (!frames) { return null; }

        const fps = parseAviFrameRate(buffer) || 30;
        const stride = Math.max(1, Math.round(fps * stepSeconds));

        const out = [];
        for (let index = 0; index < frames.length; index += stride) {
            const frame = frames[index];
            out.push(new Blob([new Uint8Array(buffer, frame.start, frame.length)],
                { type: 'image/jpeg' }));
        }
        return out;
    }

    function seekTo(video, seconds) {
        return new Promise(function (resolve, reject) {
            const timer = setTimeout(function () {
                cleanup();
                reject(new Error('seek timed out'));
            }, SEEK_TIMEOUT_MS);

            function cleanup() {
                clearTimeout(timer);
                video.removeEventListener('seeked', onSeeked);
                video.removeEventListener('error', onError);
            }
            function onSeeked() { cleanup(); resolve(); }
            function onError() { cleanup(); reject(new Error('seek failed')); }

            video.addEventListener('seeked', onSeeked);
            video.addEventListener('error', onError);
            video.currentTime = seconds;
        });
    }

    async function sliceWithVideoElement(file, stepSeconds) {
        const url = URL.createObjectURL(file);
        const video = document.createElement('video');
        video.muted = true;
        video.playsInline = true;
        video.preload = 'auto';

        try {
            // Waiting for loadeddata rather than loadedmetadata: on some
            // camera-trap MP4s the duration is known at metadata time but the
            // frame dimensions are still zero, and a canvas sized from those
            // produces empty frames that look exactly like an unsupported file.
            await new Promise(function (resolve, reject) {
                const timer = setTimeout(function () {
                    reject(new Error('metadata timed out'));
                }, SEEK_TIMEOUT_MS);
                video.onloadeddata = function () { clearTimeout(timer); resolve(); };
                video.onerror = function () {
                    clearTimeout(timer);
                    reject(new Error('unsupported'));
                };
                video.src = url;
            });

            const duration = video.duration;
            if (!isFinite(duration) || duration <= 0) {
                throw new Error('unsupported');
            }

            const canvas = document.createElement('canvas');
            const context = canvas.getContext('2d');

            const out = [];
            for (let t = 0; t < duration; t += stepSeconds) {
                await seekTo(video, t);
                // Sized here, once a frame is genuinely on screen, and kept in
                // step afterwards in case the stream changes resolution.
                if (canvas.width !== video.videoWidth
                        || canvas.height !== video.videoHeight) {
                    if (!video.videoWidth || !video.videoHeight) {
                        // Reached with a readable container whose video track
                        // the browser cannot decode. Observed on Cuddeback M4V,
                        // which stores MPEG-4 Part 2 ("mp4v"); Chromium plays
                        // its audio, reports readyState 4 and leaves the frame
                        // size at zero. Nothing is recoverable here.
                        throw new Error('no decodable video track');
                    }
                    canvas.width = video.videoWidth;
                    canvas.height = video.videoHeight;
                }
                context.drawImage(video, 0, 0, canvas.width, canvas.height);
                const blob = await new Promise(function (resolve) {
                    canvas.toBlob(resolve, 'image/jpeg', CANVAS_JPEG_QUALITY);
                });
                if (blob) { out.push(blob); }
            }
            return out.length ? out : null;
        } finally {
            video.removeAttribute('src');
            video.load();
            URL.revokeObjectURL(url);
        }
    }

    /**
     * Cut a clip into frames at `stepSeconds` intervals.
     *
     * Resolves to an array of JPEG blobs, frame 0 first. Rejects with an error
     * whose `unsupported` flag is set when neither strategy can open the file,
     * so the page can say so plainly instead of reporting a generic failure.
     */
    async function sliceVideo(file, stepSeconds) {
        const step = stepSeconds || 1;

        let frames = null;
        try {
            frames = await sliceAvi(file, step);
        } catch (err) {
            frames = null;
        }

        if (!frames) {
            try {
                frames = await sliceWithVideoElement(file, step);
            } catch (err) {
                frames = null;
            }
        }

        if (!frames || !frames.length) {
            const error = new Error('unsupported video format');
            error.unsupported = true;
            throw error;
        }
        return frames;
    }

    /** Bytes of the first part of an AVI read to find its first frame. */
    const AVI_HEAD_BYTES = 4 * 1024 * 1024;

    /** A moov box bigger than this is not worth sending for a date. */
    const MAX_HEADER_BYTES = 16 * 1024 * 1024;

    async function peek(file, offset) {
        const buf = await file.slice(offset, offset + 16).arrayBuffer();
        return new DataView(buf);
    }

    /**
     * The part of the container that may carry a capture time, or null.
     *
     * MP4/MOV: the moov box, found by stepping over top-level boxes, so only a
     * few 16-byte reads touch the file before the box itself (a few KB for a
     * camera-trap clip). AVI: the head of the file, where the header list sits.
     * The server parses it (video_metadata.py); the video itself never leaves.
     */
    async function containerHeader(file) {
        const first = await peek(file, 0);
        if (first.byteLength >= 12 && readAsciiTag(first, 0) === 'RIFF') {
            return file.slice(0, Math.min(file.size, 256 * 1024));
        }

        let offset = 0;
        while (offset + 8 <= file.size) {
            const view = await peek(file, offset);
            if (view.byteLength < 8) { return null; }
            let size = view.getUint32(0, false);
            const kind = readAsciiTag(view, 4);
            if (size === 1) {
                if (view.byteLength < 16) { return null; }
                size = view.getUint32(8, false) * 4294967296 + view.getUint32(12, false);
            } else if (size === 0) {
                size = file.size - offset;
            }
            if (size < 8) { return null; }
            if (kind === 'moov') {
                return size <= MAX_HEADER_BYTES ? file.slice(offset, offset + size) : null;
            }
            offset += size;
        }
        return null;
    }

    /**
     * The first frame as a JPEG blob, for the operator to read the date off.
     *
     * Cheap on purpose, because it runs for every clip as soon as it is chosen:
     * an AVI needs only its first few megabytes, anything else one seek.
     */
    async function firstFrame(file) {
        try {
            const head = await file.slice(0, Math.min(file.size, AVI_HEAD_BYTES)).arrayBuffer();
            const frames = parseAviFrames(head);
            if (frames && frames[0].start + frames[0].length <= head.byteLength) {
                return new Blob([new Uint8Array(head, frames[0].start, frames[0].length)],
                    { type: 'image/jpeg' });
            }
        } catch (err) {
            // Not an AVI, or a truncated one: fall through to the browser.
        }
        try {
            // A step longer than any clip yields exactly frame 0.
            const frames = await sliceWithVideoElement(file, 1e9);
            if (frames && frames.length) { return frames[0]; }
        } catch (err) {
            // Unsupported: the caller shows that.
        }
        return null;
    }

    global.CTVideoSlicer = {
        sliceVideo: sliceVideo,
        containerHeader: containerHeader,
        firstFrame: firstFrame,
        // Exposed for the console and for tests; not used by the page itself.
        parseAviFrames: parseAviFrames,
        parseAviFrameRate: parseAviFrameRate
    };
}(window));

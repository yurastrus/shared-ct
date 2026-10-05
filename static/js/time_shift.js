/* Batch time shift for /upload-fast: preview and offset calculator.
 *
 * The server owns the shift. It stores one offset on the batch and adds it to
 * every photo's EXIF time (utils.extract_datetime_from_exif). This file only
 * helps the user pick the right number and see what it does before anything
 * is uploaded: it reads EXIF time from a few selected files, in the browser,
 * and shows "was -> will be".
 *
 * All arithmetic is on naive wall-clock time. EXIF time carries no time zone,
 * and the server adds a plain timedelta to a naive datetime. Doing the same
 * here means treating the reading as UTC: the browser's own zone (and its DST
 * jumps) must never leak into the preview, or a "+1 day" across the end of
 * October would show 23 hours.
 *
 * EXIF is read with exifr (loaded from CDN by the page). If it is missing or
 * fails, the preview says so and the upload still works: the server applies
 * the shift either way.
 */

(function (global) {
    'use strict';

    /** Must stay in step with MAX_TIME_OFFSET_SECONDS in utils.py. */
    const MAX_OFFSET_SECONDS = 50 * 366 * 24 * 3600;

    /** The server's plausibility window (utils.EXIF_MIN_VALID_DATE and
     *  EXIF_MAX_FUTURE_DRIFT). A shifted time outside it is stored as the
     *  1900 placeholder, so the preview warns about it. */
    const MIN_VALID_MS = Date.UTC(2010, 0, 1);
    const MAX_FUTURE_DRIFT_MS = 24 * 3600 * 1000;

    const DAY = 86400, HOUR = 3600, MINUTE = 60;

    function pad(n, w) { return String(n).padStart(w || 2, '0'); }

    /** "2026:04:17 10:00:05" (+ optional SubSecTimeOriginal) -> ms, or null. */
    function parseExifDateTime(value, subsec) {
        if (typeof value !== 'string') return null;
        const m = /^\s*(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(value);
        if (!m) return null;
        const [y, mo, d, h, mi, s] = m.slice(1).map(Number);
        if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return null;
        let ms = Date.UTC(y, mo - 1, d, h, mi, s);
        // Date.UTC rolls 31 April over to 1 May; the server's strptime refuses it.
        if (new Date(ms).getUTCDate() !== d) return null;
        const sub = subsec == null ? '' : String(subsec).trim();
        if (/^\d+$/.test(sub)) ms += Math.floor(Number(('0.' + sub)) * 1000);
        return ms;
    }

    /** <input type="datetime-local"> value -> ms, or null. */
    function parseLocalInput(value) {
        if (!value) return null;
        const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(value);
        if (!m) return null;
        return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
    }

    /** ms -> value for <input type="datetime-local" step="1">. */
    function toLocalInput(ms) {
        const d = new Date(ms);
        return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
               `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
    }

    /** ms -> "2026-04-17 10:00:05", the way it lands in the database. */
    function formatDateTime(ms) {
        return toLocalInput(ms).replace('T', ' ');
    }

    /** {sign: 1|-1, days, hours, minutes, seconds} -> signed whole seconds.
     *  Fields may exceed their natural range ("36 hours" is fine). */
    function partsToSeconds(p) {
        const num = v => {
            const n = Number(v || 0);
            return Number.isFinite(n) ? Math.trunc(Math.abs(n)) : NaN;
        };
        const total = num(p.days) * DAY + num(p.hours) * HOUR +
                      num(p.minutes) * MINUTE + num(p.seconds);
        return (p.sign < 0 ? -1 : 1) * total;
    }

    /** Signed seconds -> normalised parts (hours < 24 and so on). */
    function secondsToParts(total) {
        const sign = total < 0 ? -1 : 1;
        let t = Math.abs(Math.trunc(total));
        const days = Math.floor(t / DAY); t -= days * DAY;
        const hours = Math.floor(t / HOUR); t -= hours * HOUR;
        const minutes = Math.floor(t / MINUTE);
        return { sign, days, hours, minutes, seconds: t - minutes * MINUTE };
    }

    /** "+1 d 2 h", "-1 h", "0". `labels` = {d, h, m, s} in the page language. */
    function formatOffset(total, labels) {
        if (!total) return '0';
        const p = secondsToParts(total);
        const out = [];
        if (p.days) out.push(`${p.days} ${labels.d}`);
        if (p.hours) out.push(`${p.hours} ${labels.h}`);
        if (p.minutes) out.push(`${p.minutes} ${labels.m}`);
        if (p.seconds) out.push(`${p.seconds} ${labels.s}`);
        return (p.sign < 0 ? '−' : '+') + out.join(' ');
    }

    /** Offset that turns `currentMs` into `desiredMs`, in whole seconds.
     *  Sub-second EXIF parts are dropped: a clock correction is never that fine. */
    function offsetBetween(currentMs, desiredMs) {
        return Math.round((Math.floor(desiredMs / 1000) * 1000 -
                           Math.floor(currentMs / 1000) * 1000) / 1000);
    }

    function isValidOffset(total) {
        return Number.isInteger(total) && Math.abs(total) <= MAX_OFFSET_SECONDS;
    }

    /** Would the server accept this (already shifted) time, or replace it with
     *  the placeholder? `nowMs` is injectable for tests. */
    function isPlausible(ms, nowMs) {
        const now = nowMs == null ? Date.now() : nowMs;
        // "now" is the browser's wall clock; compare it as naive local time,
        // which is what the server's datetime.now() is.
        const nowNaive = now - new Date(now).getTimezoneOffset() * 60000;
        return ms >= MIN_VALID_MS && ms <= nowNaive + MAX_FUTURE_DRIFT_MS;
    }

    /** Files in the order a person reads the folder: path, then natural
     *  number order (IMG_2 before IMG_10). Returns a new array. */
    function naturalSort(files) {
        const key = f => f.webkitRelativePath || f.name;
        return files.slice().sort((a, b) =>
            key(a).localeCompare(key(b), undefined, { numeric: true, sensitivity: 'base' }));
    }

    /** First `head` and last `tail` files in natural order, without repeats. */
    function pickSamples(files, head, tail) {
        const sorted = naturalSort(files);
        if (sorted.length <= head + tail) return sorted;
        return sorted.slice(0, head).concat(sorted.slice(sorted.length - tail));
    }

    /** exifr options. Two traps, both hit on real HC800 photos:
     *   - `pick` throws inside the lite build ("undefined is not iterable"),
     *     so the EXIF block is filtered with `exif: [...]` instead;
     *   - revived values are Date objects in the BROWSER's zone (14:52 on the
     *     camera came back as 11:52Z), so values stay raw strings. */
    const EXIFR_OPTIONS = {
        tiff: false,
        exif: ['DateTimeOriginal', 'SubSecTimeOriginal'],
        reviveValues: false,
    };
    let exifrWarned = false;

    /** Read EXIF capture time of one File -> ms or null. Never throws. */
    async function readExifTime(file) {
        if (!global.exifr) return null;
        try {
            const tags = await global.exifr.parse(file, EXIFR_OPTIONS);
            if (!tags) return null;
            return parseExifDateTime(tags.DateTimeOriginal, tags.SubSecTimeOriginal);
        } catch (e) {
            // A silent null here once hid a library error behind "no EXIF".
            if (!exifrWarned) {
                exifrWarned = true;
                console.warn('[time-shift] EXIF read failed', file && file.name, e);
            }
            return null;
        }
    }

    /** [{file, name, exifMs}] for a sample of the selection. */
    async function readSamples(files, head, tail) {
        const picked = pickSamples(files, head, tail);
        const times = await Promise.all(picked.map(readExifTime));
        return picked.map((file, i) => ({
            file, name: file.webkitRelativePath || file.name, exifMs: times[i],
        }));
    }

    /** One preview row: what the server will store for this photo. */
    function previewRow(sample, offsetSeconds, nowMs) {
        if (sample.exifMs == null) {
            return { name: sample.name, before: null, after: null, state: 'no_exif' };
        }
        const after = sample.exifMs + offsetSeconds * 1000;
        return {
            name: sample.name,
            before: sample.exifMs,
            after,
            state: isPlausible(after, nowMs) ? 'ok' : 'implausible',
        };
    }

    global.CtTimeShift = {
        MAX_OFFSET_SECONDS,
        parseExifDateTime, parseLocalInput, toLocalInput, formatDateTime,
        partsToSeconds, secondsToParts, formatOffset, offsetBetween,
        isValidOffset, isPlausible, naturalSort, pickSamples,
        readExifTime, readSamples, previewRow,
    };
})(window);

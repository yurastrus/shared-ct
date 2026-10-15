/* SPDX-License-Identifier: AGPL-3.0-only
 *
 * Map tools of the gap analysis pages: planned points with KML/GPX export,
 * a ruler, and (PAM) a buffer ring around planned points.
 *
 * A copy of this file also lives in the other submodule (shared-ct and
 * shared-pam stay independent, so duplication is deliberate); the copies may
 * evolve separately.
 *
 * Planned points are a per-viewer convenience, kept in localStorage so a
 * reload does not lose them; nothing is sent to the server. Every storage
 * access is guarded: in a private window it simply does not persist.
 *
 * Usage:
 *   const tools = GapMapTools.attach(map, {
 *       storageKey, filePrefix, T,          // T: translated strings
 *       describe: (latlng) => 'text',       // optional, KML/GPX description
 *       bufferRadius: 500,                  // optional, metres; omit for none
 *   });
 *   tools.busy()            // true while adding points or measuring
 *   tools.setBufferRadius(r)
 */
(function (global) {
    'use strict';

    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
        (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;'}[c]));

    function formatDistance(m, T) {
        return m < 1000 ? `${Math.round(m)} ${T.m}` : `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} ${T.km}`;
    }

    function today() {
        return new Date().toISOString().slice(0, 10);
    }

    function download(name, mime, text) {
        const url = URL.createObjectURL(new Blob([text], {type: mime}));
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function toKml(points, docName) {
        const marks = points.map((p) => `
    <Placemark>
      <name>${esc(p.name)}</name>
      <description>${esc(p.description || '')}</description>
      <Point><coordinates>${p.lon.toFixed(6)},${p.lat.toFixed(6)},0</coordinates></Point>
    </Placemark>`).join('');
        return `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>${esc(docName)}</name>${marks}
  </Document>
</kml>
`;
    }

    function toGpx(points, docName) {
        const wpts = points.map((p) => `
  <wpt lat="${p.lat.toFixed(6)}" lon="${p.lon.toFixed(6)}">
    <name>${esc(p.name)}</name>
    <desc>${esc(p.description || '')}</desc>
  </wpt>`).join('');
        return `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="BioMon" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><name>${esc(docName)}</name></metadata>${wpts}
</gpx>
`;
    }

    function loadPoints(key) {
        try {
            const raw = global.localStorage.getItem(key);
            const list = raw ? JSON.parse(raw) : [];
            return Array.isArray(list) ? list.filter((p) =>
                p && isFinite(p.lat) && isFinite(p.lon)).map((p) => ({
                    lat: +p.lat, lon: +p.lon, name: String(p.name || '')})) : [];
        } catch (e) { return []; }
    }

    function savePoints(key, points) {
        try {
            global.localStorage.setItem(key, JSON.stringify(
                points.map((p) => ({lat: p.lat, lon: p.lon, name: p.name}))));
        } catch (e) { /* storage unavailable: points live until reload */ }
    }

    function attach(map, opts) {
        const T = opts.T;
        const $ = (id) => document.getElementById(id);
        const addBtn = $('tool-add-point'), rulerBtn = $('tool-ruler');
        const listEl = $('planned-list'), hintEl = $('tool-hint');
        let mode = null;                 // null | 'add' | 'ruler'
        let bufferRadius = opts.bufferRadius == null ? null : +opts.bufferRadius;

        // Planned points sit above the recorder/camera points (pane 650).
        // The pane lets clicks through to the grid below; only the markers
        // (leaflet-interactive) catch them. Rings use SVG: a canvas in this
        // pane would swallow every click meant for the cells.
        map.createPane('planned');
        map.getPane('planned').style.zIndex = 660;
        map.getPane('planned').style.pointerEvents = 'none';
        const ringRenderer = L.svg({pane: 'planned'});
        const plannedLayer = L.layerGroup().addTo(map);
        const rulerLayer = L.layerGroup().addTo(map);

        const icon = L.divIcon({className: 'gap-planned-icon', iconSize: [14, 14], iconAnchor: [7, 7]});
        let points = loadPoints(opts.storageKey);
        let seq = points.length;

        function nextName() {
            seq += 1;
            let name = `${T.pointPrefix}${seq}`;
            while (points.some((p) => p.name === name)) { seq += 1; name = `${T.pointPrefix}${seq}`; }
            return name;
        }

        function persist() { savePoints(opts.storageKey, points); }

        function drawPoints() {
            plannedLayer.clearLayers();
            points.forEach((p, i) => {
                if (bufferRadius) {
                    p.circle = L.circle([p.lat, p.lon], {radius: bufferRadius, pane: 'planned', renderer: ringRenderer,
                        color: '#c2185b', weight: 1.5, dashArray: '6 5', fill: false, interactive: false})
                        .addTo(plannedLayer);
                }
                const marker = L.marker([p.lat, p.lon], {icon, draggable: true, pane: 'planned',
                    title: p.name, keyboard: false})
                    .bindTooltip(esc(p.name), {direction: 'top', offset: [0, -6]})
                    .addTo(plannedLayer);
                marker.on('drag', (e) => {
                    const ll = e.target.getLatLng();
                    if (p.circle) p.circle.setLatLng(ll);
                });
                marker.on('dragend', (e) => {
                    const ll = e.target.getLatLng();
                    p.lat = ll.lat; p.lon = ll.lng;
                    persist(); drawList();
                });
                // Clicking a planned point must not open the grid cell below.
                marker.on('click', (e) => L.DomEvent.stopPropagation(e.originalEvent || e));
                p.index = i;
            });
        }

        function drawList() {
            if (!listEl) return;
            $('planned-count').textContent = points.length;
            $('planned-export').hidden = !points.length;
            if (!points.length) { listEl.innerHTML = `<li class="muted">${esc(T.noPoints)}</li>`; return; }
            listEl.innerHTML = points.map((p, i) => `
                <li data-i="${i}">
                    <input type="text" class="form-control form-control-sm" value="${esc(p.name)}" aria-label="${esc(T.pointName)}">
                    <button type="button" class="gap-link" data-act="zoom" title="${esc(T.zoomTo)}">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}</button>
                    <button type="button" class="gap-del" data-act="del" title="${esc(T.deletePoint)}" aria-label="${esc(T.deletePoint)}">×</button>
                </li>`).join('');
        }

        if (listEl) {
            listEl.addEventListener('change', (e) => {
                const li = e.target.closest('li[data-i]');
                if (!li || e.target.tagName !== 'INPUT') return;
                const p = points[+li.dataset.i];
                p.name = e.target.value.trim() || p.name;
                persist(); drawPoints();
            });
            listEl.addEventListener('click', (e) => {
                const btn = e.target.closest('button[data-act]');
                if (!btn) return;
                const i = +btn.closest('li').dataset.i;
                if (btn.dataset.act === 'del') {
                    points.splice(i, 1);
                    persist(); drawPoints(); drawList();
                } else {
                    map.setView([points[i].lat, points[i].lon], Math.max(map.getZoom(), 14));
                }
            });
        }

        function exportAs(kind) {
            const docName = `${T.documentName} ${today()}`;
            const rows = points.map((p) => {
                const parts = [];
                if (opts.describe) {
                    try { const d = opts.describe(L.latLng(p.lat, p.lon)); if (d) parts.push(d); } catch (e) { /* ignore */ }
                }
                if (bufferRadius) parts.push(`${T.bufferLabel} ${bufferRadius} ${T.m}`);
                return {lat: p.lat, lon: p.lon, name: p.name, description: parts.join('; ')};
            });
            const base = `${opts.filePrefix}-planned-${today()}`;
            if (kind === 'gpx') download(base + '.gpx', 'application/gpx+xml', toGpx(rows, docName));
            else download(base + '.kml', 'application/vnd.google-earth.kml+xml', toKml(rows, docName));
        }

        const kmlBtn = $('planned-kml'), gpxBtn = $('planned-gpx'), clearBtn = $('planned-clear');
        if (kmlBtn) kmlBtn.addEventListener('click', () => exportAs('kml'));
        if (gpxBtn) gpxBtn.addEventListener('click', () => exportAs('gpx'));
        if (clearBtn) clearBtn.addEventListener('click', () => {
            if (!points.length || !global.confirm(T.confirmClear)) return;
            points = []; seq = 0;
            persist(); drawPoints(); drawList();
        });

        // ── Ruler ──────────────────────────────────────────────────────────
        let ruler = null;   // {latlngs, line, ghost, label}

        function rulerTotal(latlngs) {
            let d = 0;
            for (let i = 1; i < latlngs.length; i++) d += map.distance(latlngs[i - 1], latlngs[i]);
            return d;
        }

        function startRuler() {
            rulerLayer.clearLayers();
            ruler = {latlngs: [],
                     line: L.polyline([], {color: '#111', weight: 2, dashArray: '6 4', interactive: false}).addTo(rulerLayer),
                     ghost: L.polyline([], {color: '#111', weight: 1, dashArray: '2 4', interactive: false}).addTo(rulerLayer),
                     label: null};
        }

        function rulerAdd(ll) {
            ruler.latlngs.push(ll);
            ruler.line.setLatLngs(ruler.latlngs);
            L.circleMarker(ll, {radius: 3, color: '#111', weight: 1, fillColor: '#fff', fillOpacity: 1,
                                interactive: false}).addTo(rulerLayer);
            const total = rulerTotal(ruler.latlngs);
            if (ruler.label) rulerLayer.removeLayer(ruler.label);
            if (ruler.latlngs.length > 1) {
                ruler.label = L.tooltip({permanent: true, direction: 'right', className: 'gap-ruler-label'})
                    .setLatLng(ll).setContent(formatDistance(total, T));
                rulerLayer.addLayer(ruler.label);
            }
        }

        function rulerMove(ll) {
            if (!ruler || !ruler.latlngs.length) return;
            const last = ruler.latlngs[ruler.latlngs.length - 1];
            ruler.ghost.setLatLngs([last, ll]);
            const total = rulerTotal(ruler.latlngs) + map.distance(last, ll);
            hintEl.textContent = `${T.rulerHint} ${formatDistance(total, T)}`;
        }

        function finishRuler() {
            if (ruler) ruler.ghost.setLatLngs([]);
        }

        // ── Modes ──────────────────────────────────────────────────────────
        function setMode(next) {
            if (mode === 'ruler') finishRuler();
            mode = mode === next ? null : next;
            addBtn.classList.toggle('active', mode === 'add');
            rulerBtn.classList.toggle('active', mode === 'ruler');
            addBtn.setAttribute('aria-pressed', String(mode === 'add'));
            rulerBtn.setAttribute('aria-pressed', String(mode === 'ruler'));
            map.getContainer().classList.toggle('gap-crosshair', mode !== null);
            if (mode === 'ruler') { map.doubleClickZoom.disable(); startRuler(); }
            else map.doubleClickZoom.enable();
            hintEl.textContent = mode === 'add' ? T.addHint : mode === 'ruler' ? T.rulerStart : '';
        }

        addBtn.addEventListener('click', () => setMode('add'));
        rulerBtn.addEventListener('click', () => setMode('ruler'));
        const rulerClear = $('tool-ruler-clear');
        if (rulerClear) rulerClear.addEventListener('click', () => {
            rulerLayer.clearLayers(); ruler = null;
            if (mode === 'ruler') startRuler();
        });

        map.on('click', (e) => {
            if (mode === 'add') {
                points.push({lat: e.latlng.lat, lon: e.latlng.lng, name: nextName()});
                persist(); drawPoints(); drawList();
            } else if (mode === 'ruler') {
                if (!ruler) startRuler();
                rulerAdd(e.latlng);
            }
        });
        map.on('mousemove', (e) => { if (mode === 'ruler') rulerMove(e.latlng); });
        map.on('dblclick', () => { if (mode === 'ruler') setMode(null); });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && mode) setMode(null); });

        drawPoints();
        drawList();

        return {
            busy: () => mode !== null,
            setBufferRadius(r) {
                bufferRadius = r == null ? null : +r;
                drawPoints();
            },
            points: () => points.map((p) => ({lat: p.lat, lon: p.lon, name: p.name})),
            formatDistance: (m) => formatDistance(m, T),
        };
    }

    global.GapMapTools = {attach, toKml, toGpx, formatDistance};
})(window);

/* Fan out Leaflet markers that sit on top of each other (2026-11-05).
 *
 * Camera-trap locations are often a few metres apart, so their pins cover each
 * other and the lower one cannot be clicked. This spreads such a stack into a
 * circle (or a spiral for big stacks) with a thin leg back to the true point.
 *
 *   - zoom >= autoZoom: every stack on screen is fanned out automatically;
 *   - below it: a click on a stacked pin fans that stack out instead of
 *     selecting it; a click on a fanned pin selects it as usual.
 *
 * Only the drawn position changes; the true coordinates stay in marker._ctHome.
 *
 *   const spread = CTMarkerSpread.attach(map, () => allMarkers, {autoZoom: 15});
 *   marker.on('click', () => { if (spread.consumeClick(marker)) return; select(...); });
 *   spread.refresh();   // after markers are shown, hidden or re-iconed
 */
(function (global) {
    'use strict';

    const DEFAULTS = {
        autoZoom: 15,         // fan out every stack from this zoom on
        nearPx: 22,           // pins closer than this on screen form a stack
        circleMax: 9,         // above this, a spiral instead of a circle
        legColor: '#444',
    };

    /** Offsets (in screen pixels) of n fanned pins around the stack centre. */
    function fanOffsets(n) {
        const out = [];
        if (n <= DEFAULTS.circleMax) {
            const r = Math.max(28, (26 * (2 + n)) / (2 * Math.PI));
            const step = (2 * Math.PI) / n;
            for (let i = 0; i < n; i++) {
                const a = i * step - Math.PI / 2;
                out.push([r * Math.cos(a), r * Math.sin(a)]);
            }
            return out;
        }
        let leg = 14, angle = 0;
        for (let i = 0; i < n; i++) {
            angle += 30 / leg + i * 0.0005;
            out.push([leg * Math.cos(angle), leg * Math.sin(angle)]);
            leg += (2 * Math.PI * 5) / angle;
        }
        return out;
    }

    /** Group points (screen pixels) whose chain distance is below nearPx. */
    function groupNear(points, nearPx) {
        const n = points.length, parent = points.map((_, i) => i);
        const find = (i) => (parent[i] === i ? i : (parent[i] = find(parent[i])));
        const d2 = nearPx * nearPx;
        for (let i = 0; i < n; i++) {
            for (let j = i + 1; j < n; j++) {
                const dx = points[i].x - points[j].x, dy = points[i].y - points[j].y;
                if (dx * dx + dy * dy < d2) parent[find(i)] = find(j);
            }
        }
        const groups = {};
        for (let i = 0; i < n; i++) (groups[find(i)] = groups[find(i)] || []).push(i);
        return Object.values(groups);
    }

    function attach(map, getMarkers, options) {
        const opt = Object.assign({}, DEFAULTS, options || {});
        const legs = L.layerGroup().addTo(map);
        let manual = null;          // markers fanned by a click below autoZoom
        let fanned = new Set();     // markers currently drawn away from home

        function home(m) {
            if (!m._ctHome) m._ctHome = m.getLatLng();
            return m._ctHome;
        }

        function resetAll(markers) {
            legs.clearLayers();
            markers.forEach((m) => {
                if (m._ctHome) m.setLatLng(m._ctHome);
            });
            fanned = new Set();
        }

        function stacks(markers) {
            const pts = markers.map((m) => map.latLngToLayerPoint(home(m)));
            return groupNear(pts, opt.nearPx)
                .filter((g) => g.length > 1)
                .map((g) => g.map((i) => markers[i]));
        }

        function fan(group) {
            const pts = group.map((m) => map.latLngToLayerPoint(home(m)));
            const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
            const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
            const centre = map.layerPointToLatLng([cx, cy]);
            // Stable order: by angle from the centre, so neighbours stay neighbours.
            const order = group.map((m, i) => ({ m, a: Math.atan2(pts[i].y - cy, pts[i].x - cx) }))
                .sort((p, q) => p.a - q.a).map((p) => p.m);
            fanOffsets(order.length).forEach(([dx, dy], i) => {
                const m = order[i];
                const pos = map.layerPointToLatLng([cx + dx, cy + dy]);
                m.setLatLng(pos);
                L.polyline([centre, pos], { color: opt.legColor, weight: 1.5, opacity: 0.7,
                                            interactive: false }).addTo(legs);
                fanned.add(m);
            });
            L.circleMarker(centre, { radius: 3, color: opt.legColor, weight: 1,
                                     fillOpacity: 0.9, interactive: false }).addTo(legs);
        }

        function refresh() {
            const all = getMarkers();
            resetAll(all);   // hidden ones too: a pin hidden while fanned must come home
            const markers = all.filter((m) => map.hasLayer(m));
            if (map.getZoom() >= opt.autoZoom) {
                manual = null;
                stacks(markers).forEach(fan);
            } else if (manual) {
                const still = manual.filter((m) => map.hasLayer(m));
                manual = still.length > 1 ? still : null;
                if (manual) fan(manual);
            }
        }

        /** True when the click only fanned a stack (the caller must not select). */
        function consumeClick(marker) {
            if (fanned.has(marker)) return false;
            const markers = getMarkers().filter((m) => map.hasLayer(m));
            const group = stacks(markers).find((g) => g.includes(marker));
            if (!group) return false;
            manual = group;
            refresh();
            return true;
        }

        function collapse() {
            if (!manual) return;
            manual = null;
            refresh();
        }

        map.on('zoomend', () => { manual = null; refresh(); });
        map.on('click', collapse);
        return { refresh, consumeClick, collapse, isFanned: (m) => fanned.has(m) };
    }

    global.CTMarkerSpread = { attach, fanOffsets, groupNear };
})(window);

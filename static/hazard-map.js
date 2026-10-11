(function (global) {
    "use strict";

    const MAYON_SUMMIT = { lat: 13.2543, lon: 123.6856 };

    const DANGER_ZONES = [
        { id: "pdz", label: "Permanent Danger Zone (6km)", radiusMeters: 6000, color: "#dc2626" },
        { id: "edz", label: "Extended Danger Zone (8km)", radiusMeters: 8000, color: "#f59e0b" }
    ];

    // PLACEHOLDER — replace with the official lahar / pyroclastic-flow
    // channel path for Boñga from your MDRRMO / PHIVOLCS hazard map.
    const LAHAR_CORRIDOR = [
        [],
        [],
        [],
        [],
        []
    ];

    // PLACEHOLDER — verify against real GPS coordinates for each shelter.
    const EVAC_CENTERS = [
        { id: "school", name: "Gogon High School", type: "school", lat: 13.154776363870775, lon: 123.7455717013404, capacity: 500 }
    ];

    const BARANGAY_CENTER = { lat: 13.1391, lon: 123.7438 };

    function toRad(deg) { return (deg * Math.PI) / 180; }

    function haversineMeters(lat1, lon1, lat2, lon2) {
        const R = 6371000;
        const dLat = toRad(lat2 - lat1);
        const dLon = toRad(lon2 - lon1);
        const a =
            Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
        return 2 * R * Math.asin(Math.sqrt(a));
    }

    function formatDistance(meters) {
        if (meters == null || isNaN(meters)) return "--";
        if (meters < 1000) return Math.round(meters) + " m";
        return (meters / 1000).toFixed(1) + " km";
    }

    // Cheap approximation of distance from a point to a polyline: samples
    // points along each segment. Good enough for a "how close am I to the
    // corridor" warning - not a precise GIS calculation.
    function distanceToPolylineMeters(lat, lon, path) {
        let min = Infinity;
        for (let i = 0; i < path.length - 1; i++) {
            const lat1 = path[i][0], lon1 = path[i][1];
            const lat2 = path[i + 1][0], lon2 = path[i + 1][1];
            for (let t = 0; t <= 1; t += 0.1) {
                const sLat = lat1 + (lat2 - lat1) * t;
                const sLon = lon1 + (lon2 - lon1) * t;
                const d = haversineMeters(lat, lon, sLat, sLon);
                if (d < min) min = d;
            }
        }
        return min;
    }

    const LAHAR_BUFFER_METERS = 500;

    // Returns { inside: bool, zone: label|null, distanceToSummit: meters }
    function dangerZoneStatus(lat, lon) {
        const distToSummit = haversineMeters(lat, lon, MAYON_SUMMIT.lat, MAYON_SUMMIT.lon);
        for (let i = 0; i < DANGER_ZONES.length; i++) {
            const zone = DANGER_ZONES[i];
            if (distToSummit <= zone.radiusMeters) {
                return { inside: true, zone: zone.label, distanceToSummit: distToSummit };
            }
        }
        const distToLahar = distanceToPolylineMeters(lat, lon, LAHAR_CORRIDOR);
        if (distToLahar <= LAHAR_BUFFER_METERS) {
            return { inside: true, zone: "Lahar-Prone Corridor", distanceToSummit: distToSummit };
        }
        return { inside: false, zone: null, distanceToSummit: distToSummit };
    }


    function rankedEvacCenters(lat, lon) {
        return EVAC_CENTERS.map(function (c) {
            const d = haversineMeters(lat, lon, c.lat, c.lon);
            const midLat = (lat + c.lat) / 2;
            const midLon = (lon + c.lon) / 2;
            const midDistToLahar = distanceToPolylineMeters(midLat, midLon, LAHAR_CORRIDOR);
            return Object.assign({}, c, { distanceMeters: d, risky: midDistToLahar < 700 });
        }).sort(function (a, b) {
            if (a.risky !== b.risky) return a.risky ? 1 : -1;
            return a.distanceMeters - b.distanceMeters;
        });
    }

    function nearestEvacCenter(lat, lon) {
        const ranked = rankedEvacCenters(lat, lon);
        return ranked.length ? ranked[0] : null;
    }


    function buildHazardLayers(L) {
        const evacCenterLayer = L.layerGroup();
        const dangerZoneLayer = L.layerGroup();
        const laharLayer = L.layerGroup();

        EVAC_CENTERS.forEach(function (c) {
            const icon = L.divIcon({
                className: "",
                html:
                    '<div style="background:#10b981;border:3px solid white;border-radius:9999px;' +
                    'width:16px;height:16px;box-shadow:0 0 8px rgba(16,185,129,0.85)"></div>',
                iconSize: [16, 16],
                iconAnchor: [8, 8]
            });
            L.marker([c.lat, c.lon], { icon: icon })
                .bindPopup("<b>" + c.name + "</b><br>Evacuation Center<br>Capacity: " + c.capacity)
                .addTo(evacCenterLayer);
        });

        DANGER_ZONES.forEach(function (z) {
            L.circle([MAYON_SUMMIT.lat, MAYON_SUMMIT.lon], {
                radius: z.radiusMeters,
                color: z.color,
                fillColor: z.color,
                fillOpacity: 0.07,
                weight: 2,
                dashArray: "6,4"
            }).bindPopup(z.label + " (Mayon Volcano)").addTo(dangerZoneLayer);
        });

        L.polyline(LAHAR_CORRIDOR, {
            color: "#92400e",
            weight: 6,
            opacity: 0.55,
            dashArray: "2,8"
        }).bindPopup("Lahar-Prone Corridor<br><i>Placeholder — replace with official hazard map data.</i>")
          .addTo(laharLayer);

        return { evacCenterLayer: evacCenterLayer, dangerZoneLayer: dangerZoneLayer, laharLayer: laharLayer };
    }

    global.HazardMap = {
        MAYON_SUMMIT: MAYON_SUMMIT,
        DANGER_ZONES: DANGER_ZONES,
        LAHAR_CORRIDOR: LAHAR_CORRIDOR,
        EVAC_CENTERS: EVAC_CENTERS,
        BARANGAY_CENTER: BARANGAY_CENTER,
        haversineMeters: haversineMeters,
        formatDistance: formatDistance,
        dangerZoneStatus: dangerZoneStatus,
        rankedEvacCenters: rankedEvacCenters,
        nearestEvacCenter: nearestEvacCenter,
        buildHazardLayers: buildHazardLayers
    };
})(window);
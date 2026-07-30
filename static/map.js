// Map module for Travely propositions — Leaflet.js + OpenStreetMap
// Provides openMapModal/closeMapModal functions for the dashboard.

// Activity type colors for map markers
const TYPE_COLORS = {
    plage: '#3b82f6',        // blue
    culture: '#ef4444',       // red
    gastronomie: '#f97316',   // orange
    randonnee: '#22c55e',     // green
    transport: '#6b7280',    // gray
    balade: '#a855f7',        // purple
    cloture: '#64748b',       // slate
    default: '#0ea5e9',       // sky
};
const TYPE_LABELS = {
    plage: 'Plage/Baignade',
    culture: 'Culture/Visite',
    gastronomie: 'Gastronomie',
    randonnee: 'Randonnée',
    transport: 'Transport',
    balade: 'Balade/Promenade',
    cloture: 'Fin de journée',
    default: 'Autre',
};

let leafletMap = null;
let mapMarkers = [];
let mapPolyline = null;

async function openMapModal(propId, destination, pays) {
    var overlay = document.getElementById('map-overlay');
    var loading = document.getElementById('map-loading');
    var legend = document.getElementById('map-legend');

    // Set title
    document.getElementById('map-modal-title').textContent = 'Carte — ' + destination;
    document.getElementById('map-modal-subtitle').textContent = pays || '';

    // Show overlay and loading
    overlay.classList.remove('hidden');
    loading.style.display = 'flex';
    legend.classList.add('hidden');

    // Initialize map if not already done
    if (!leafletMap) {
        leafletMap = L.map('leaflet-map', {
            center: [46.603354, 1.888334], // Default: center of France
            zoom: 5,
            scrollWheelZoom: true,
        });
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 19,
            crossOrigin: true,
        }).addTo(leafletMap);
        // Force a size invalidation after initialization — the container may
        // have had zero dimensions when Leaflet first measured it.
        setTimeout(function() { leafletMap.invalidateSize(); }, 50);
    } else {
        // Clear previous markers and polyline
        mapMarkers.forEach(function(m) { leafletMap.removeLayer(m); });
        mapMarkers = [];
        if (mapPolyline) {
            leafletMap.removeLayer(mapPolyline);
            mapPolyline = null;
        }
    }
    // Always invalidate size when opening — the modal may have been hidden
    setTimeout(function() { leafletMap.invalidateSize(); }, 50);

    // Fetch geocoded map data from API
    try {
        var res = await fetch(API + '/proposition/' + propId + '/map');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        var data = await res.json();

        var points = data.points || [];
        loading.style.display = 'none';

        if (points.length === 0) {
            leafletMap.invalidateSize();
            showToast('Aucun point n\'a pu être géocodé pour cette proposition', 'error');
            return;
        }

        // Build legend
        var usedTypes = [];
        points.forEach(function(p) { if (usedTypes.indexOf(p.type) === -1) usedTypes.push(p.type); });
        var legendHtml = usedTypes.map(function(t) {
            return '<div class="map-legend-item"><span class="map-legend-dot" style="background:' +
                (TYPE_COLORS[t] || TYPE_COLORS.default) + '"></span> ' +
                (TYPE_LABELS[t] || t) + '</div>';
        }).join('');
        legend.innerHTML = legendHtml;
        legend.classList.remove('hidden');

        // Add markers and build polyline coordinates
        var latlngs = [];
        points.forEach(function(p, i) {
            var color = TYPE_COLORS[p.type] || TYPE_COLORS.default;
            var num = i + 1;

            // Create a custom numbered marker using divIcon
            var icon = L.divIcon({
                className: 'custom-marker',
                html: '<div style="background:' + color + ';width:30px;height:30px;border-radius:50% 50% 50% 0;border:2px solid #fff;transform:rotate(-45deg);box-shadow:0 2px 6px rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;"><span style="transform:rotate(45deg);color:#fff;font-weight:bold;font-size:13px;">' + num + '</span></div>',
                iconSize: [30, 30],
                iconAnchor: [15, 30],
                popupAnchor: [0, -30],
            });

            var popupHtml = '<div style="min-width:200px;">' +
                '<p style="font-weight:600;color:' + color + ';margin-bottom:4px;">' + escapeHtml(p.label) + '</p>' +
                (p.heure ? '<p style="font-size:0.75rem;color:#94a3b8;margin-bottom:2px;">🕒 ' + escapeHtml(p.heure) + '</p>' : '') +
                (p.cout ? '<p style="font-size:0.75rem;color:#f59e0b;margin-bottom:2px;">💰 ' + escapeHtml(p.cout) + '</p>' : '') +
                (p.description ? '<p style="font-size:0.8rem;color:#cbd5e1;margin-top:4px;">' + escapeHtml(p.description) + '</p>' : '') +
                '<p style="font-size:0.7rem;color:#64748b;margin-top:4px;">📍 ' + escapeHtml(p.display_name || p.address) + '</p>' +
                '</div>';

            var marker = L.marker([p.lat, p.lon], { icon: icon })
                .addTo(leafletMap)
                .bindPopup(popupHtml);
            mapMarkers.push(marker);
            latlngs.push([p.lat, p.lon]);
        });

        // Draw polyline connecting the points
        if (latlngs.length >= 2) {
            mapPolyline = L.polyline(latlngs, {
                color: '#0ea5e9',
                weight: 3,
                opacity: 0.6,
                dashArray: '8, 8',
            }).addTo(leafletMap);
        }

        // Fit map to show all markers
        if (latlngs.length > 0) {
            var bounds = L.latLngBounds(latlngs);
            leafletMap.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
        }

        // Ensure map renders correctly after modal opens
        setTimeout(function() { leafletMap.invalidateSize(); }, 100);
        setTimeout(function() { leafletMap.invalidateSize(); }, 500);

        // Show info about geocoding results
        if (data.failed > 0) {
            showToast(data.geocoded + ' point(s) géocodé(s), ' + data.failed + ' échec(s)', 'info');
        }

    } catch (err) {
        loading.style.display = 'none';
        showToast('Erreur carte: ' + err.message, 'error');
        console.error('Map error:', err);
    }
}

function closeMapModal() {
    document.getElementById('map-overlay').classList.add('hidden');
    if (leafletMap) {
        mapMarkers.forEach(function(m) { leafletMap.removeLayer(m); });
        mapMarkers = [];
        if (mapPolyline) {
            leafletMap.removeLayer(mapPolyline);
            mapPolyline = null;
        }
    }
}

// Close map on Escape key
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var overlay = document.getElementById('map-overlay');
        if (!overlay.classList.contains('hidden')) closeMapModal();
    }
});

// Close map on overlay background click
document.addEventListener('click', function(e) {
    var overlay = document.getElementById('map-overlay');
    if (e.target === overlay) closeMapModal();
});
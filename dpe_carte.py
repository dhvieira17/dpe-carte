#!/usr/bin/env python3
"""
dpe_carte.py — extrait les DPE ADEME pour une liste de codes postaux,
écrit un CSV et génère une carte HTML autonome (Leaflet, aucune dépendance
Python supplémentaire : le rendu se fait côté navigateur via CDN).

Les points sont colorés et filtrés par commune ; chaque point porte une
étiquette mois/année correspondant à la date d'établissement du DPE.

    python dpe_carte.py --diagnostic
    python dpe_carte.py
    python dpe_carte.py --codes codes-postaux.txt --depuis 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from dpe_ademe import CHAMPS, ClientDPE, charger_codes_postaux, ecrire_csv

log = logging.getLogger("dpe_carte")

# couleurs officielles des étiquettes DPE (utilisées dans la popup uniquement)
COULEURS_DPE = {
    "A": "#319834", "B": "#33cc31", "C": "#cbfc34", "D": "#fff32a",
    "E": "#fdd21c", "F": "#f3ac1c", "G": "#ec0000",
}

# palette de teintes bien distinctes, attribuée commune par commune
PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080",
    "#f032e6", "#9a6324", "#46b1e0", "#808000", "#000075", "#e07b39",
    "#1abc9c", "#c0392b", "#7d3c98", "#2e86c1", "#ca6f1e", "#17a589",
    "#884ea0", "#b7950b", "#2874a6", "#cb4335", "#148f77", "#6c3483",
]


def coordonnees(ligne: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    gp = ligne.get("_geopoint")
    if isinstance(gp, str) and "," in gp:
        try:
            lat, lon = gp.split(",", 1)
            return float(lat), float(lon)
        except ValueError:
            pass
    lat, lon = ligne.get("latitude"), ligne.get("longitude")
    try:
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    except (TypeError, ValueError):
        pass
    return None


def mois_annee(d: str) -> str:
    """2026-03-09 -> 03/2026"""
    if isinstance(d, str) and len(d) >= 7 and d[4] == "-":
        return f"{d[5:7]}/{d[0:4]}"
    return ""


def preparer_points(lignes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    points, sans_geo = [], 0
    for l in lignes:
        xy = coordonnees(l)
        if not xy:
            sans_geo += 1
            continue
        commune = (l.get(CHAMPS["commune"]) or "").strip()
        cp = str(l.get(CHAMPS["cp"]) or "").strip()
        if not commune:
            commune = f"CP {cp}" if cp else "Commune inconnue"
        d = str(l.get(CHAMPS["date"]) or "")[:10]
        points.append(
            {
                "lat": round(xy[0], 6),
                "lon": round(xy[1], 6),
                "v": commune,
                "cp": cp,
                "d": d,
                "m": mois_annee(d),
                "c": (l.get(CHAMPS["classe"]) or "").strip().upper()[:1],
                "g": (l.get(CHAMPS["ges"]) or "").strip().upper()[:1],
                "a": l.get(CHAMPS["adresse"]) or "",
                "s": l.get(CHAMPS["surface"]),
                "t": l.get(CHAMPS["type"]) or "",
                "an": l.get(CHAMPS["annee"]),
                "co": l.get(CHAMPS["conso"]),
                "n": l.get(CHAMPS["num"]) or "",
            }
        )
    if sans_geo:
        log.warning("%s DPE sans coordonnées, absents de la carte", sans_geo)
    return points


def palette_communes(points: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Une couleur par commune, les communes les plus fournies d'abord."""
    comptes = Counter(p["v"] for p in points)
    ordre = sorted(comptes, key=lambda v: (-comptes[v], v))
    return {
        v: {"couleur": PALETTE[i % len(PALETTE)], "n": comptes[v]}
        for i, v in enumerate(ordre)
    }


GABARIT = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITRE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<style>
  html,body{margin:0;height:100%;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
  #carte{height:100%}
  .panneau{position:absolute;top:12px;right:12px;z-index:1000;background:#fff;
    padding:12px 14px;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.18);
    font-size:13px;width:240px;max-height:calc(100% - 40px);display:flex;flex-direction:column}
  .panneau h1{font-size:14px;margin:0 0 4px}
  .panneau .meta{color:#666;font-size:12px;margin-bottom:8px}
  .outils{display:flex;gap:6px;margin-bottom:8px}
  .outils button{flex:1;border:1px solid #ddd;background:#f7f7f7;border-radius:6px;
    padding:4px 0;font-size:12px;cursor:pointer}
  .communes{overflow-y:auto;margin:0 -4px;padding:0 4px}
  .commune{display:flex;align-items:center;gap:7px;padding:3px 4px;border-radius:6px;
    cursor:pointer;user-select:none}
  .commune:hover{background:#f4f4f4}
  .commune.off{opacity:.34}
  .puce{width:13px;height:13px;border-radius:50%;flex:none;border:1px solid rgba(0,0,0,.25)}
  .nom{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .nb{color:#888;font-size:11px}
  .bascule{display:flex;align-items:center;gap:6px;margin-top:9px;
    border-top:1px solid #eee;padding-top:8px;font-size:12px;color:#444;cursor:pointer}
  .etq{background:rgba(255,255,255,.92);border:1px solid #bbb;border-radius:4px;
    padding:0 4px;font-size:10px;font-weight:600;color:#222;box-shadow:none;white-space:nowrap}
  .etq:before{display:none}
  .popup b{font-size:13px}
  .popup table{border-collapse:collapse;margin-top:6px;font-size:12px}
  .popup td{padding:1px 8px 1px 0;vertical-align:top}
  .pastille{display:inline-block;width:20px;height:20px;line-height:20px;
    text-align:center;border-radius:4px;font-weight:700;color:#000}
</style>
</head>
<body>
<div id="carte"></div>
<div class="panneau">
  <h1>__TITRE__</h1>
  <div class="meta">__META__</div>
  <div class="outils">
    <button id="tout">Tout</button>
    <button id="rien">Aucune</button>
  </div>
  <div class="communes" id="communes"></div>
  <label class="bascule"><input type="checkbox" id="etiquettes" checked> Étiquettes mois/année</label>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script>
const POINTS = __DONNEES__;
const COMMUNES = __COMMUNES__;      // { nom: {couleur, n} }
const COULEURS_DPE = __COULEURS_DPE__;

const carte = L.map('carte');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap &mdash; donnees ADEME'
}).addTo(carte);

const actifs = new Set(Object.keys(COMMUNES));
const groupe = L.markerClusterGroup({ maxClusterRadius: 45, disableClusteringAtZoom: 16 });
carte.addLayer(groupe);

function couleur(p) { return (COMMUNES[p.v] || {}).couleur || '#888'; }

function contenu(p) {
  const l = [];
  if (p.t) l.push(['Type', p.t]);
  if (p.s) l.push(['Surface', p.s + ' m2']);
  if (p.an) l.push(['Construction', p.an]);
  if (p.co) l.push(['Conso', Math.round(p.co) + ' kWh/m2/an']);
  if (p.g) l.push(['GES', p.g]);
  if (p.d) l.push(['DPE du', p.d.split('-').reverse().join('/')]);
  if (p.n) l.push(['No', p.n]);
  return '<div class="popup"><b>' + (p.a || 'Adresse inconnue') + '</b><br>'
    + p.cp + ' ' + p.v + '<br>'
    + (p.c ? '<span class="pastille" style="background:' + (COULEURS_DPE[p.c] || '#bbb')
             + '">' + p.c + '</span>' : '')
    + '<table>' + l.map(x => '<tr><td>' + x[0] + '</td><td>' + x[1] + '</td></tr>').join('')
    + '</table></div>';
}

function dessiner(recadrer) {
  const avecEtq = document.getElementById('etiquettes').checked;
  groupe.clearLayers();
  const visibles = POINTS.filter(p => actifs.has(p.v));
  const marqueurs = visibles.map(p => {
    const m = L.circleMarker([p.lat, p.lon], {
      radius: 7, weight: 1.5, color: '#333', opacity: .75,
      fillColor: couleur(p), fillOpacity: .9
    }).bindPopup(contenu(p));
    if (avecEtq && p.m) {
      m.bindTooltip(p.m, { permanent: true, direction: 'top',
                           className: 'etq', offset: [0, -7] });
    }
    return m;
  });
  groupe.addLayers(marqueurs);
  if (recadrer && visibles.length) {
    carte.fitBounds(L.latLngBounds(visibles.map(p => [p.lat, p.lon])).pad(0.08));
  }
}

const liste = document.getElementById('communes');
const rangees = {};
Object.keys(COMMUNES).forEach(function (nom) {
  const d = document.createElement('div');
  d.className = 'commune';
  d.innerHTML = '<span class="puce" style="background:' + COMMUNES[nom].couleur + '"></span>'
    + '<span class="nom" title="' + nom + '">' + nom + '</span>'
    + '<span class="nb">' + COMMUNES[nom].n + '</span>';
  d.onclick = function () {
    if (actifs.has(nom)) { actifs.delete(nom); } else { actifs.add(nom); }
    d.classList.toggle('off');
    dessiner(false);
  };
  rangees[nom] = d;
  liste.appendChild(d);
});

function basculerTout(on) {
  Object.keys(COMMUNES).forEach(function (nom) {
    if (on) { actifs.add(nom); rangees[nom].classList.remove('off'); }
    else { actifs.delete(nom); rangees[nom].classList.add('off'); }
  });
  dessiner(on);
}
document.getElementById('tout').onclick = function () { basculerTout(true); };
document.getElementById('rien').onclick = function () { basculerTout(false); };
document.getElementById('etiquettes').onchange = function () { dessiner(false); };

if (POINTS.length) { dessiner(true); } else { carte.setView([45.83, 1.26], 11); }
</script>
</body>
</html>
"""


def ecrire_carte(points: List[Dict[str, Any]], chemin: str, titre: str, meta: str) -> None:
    communes = palette_communes(points)
    html = (
        GABARIT.replace("__DONNEES__", json.dumps(points, ensure_ascii=False))
        .replace("__COMMUNES__", json.dumps(communes, ensure_ascii=False))
        .replace("__COULEURS_DPE__", json.dumps(COULEURS_DPE))
        .replace("__TITRE__", titre)
        .replace("__META__", meta)
    )
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("%s points sur %s communes dans %s", len(points), len(communes), chemin)


# --------------------------------------------------------------------------
# Exports uMap
# --------------------------------------------------------------------------

GABARIT_POPUP = (
    "# {name}\n"
    "{cp} {commune}\n\n"
    "**DPE {classe}** · GES {ges}\n"
    "{type} · {surface} m² · {annee}\n"
    "{conso} kWh/m²/an\n"
    "DPE du {date} — n° {numero}"
)


def _proprietes(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": p["a"] or f"DPE {p['n']}",
        "commune": p["v"],
        "cp": p["cp"],
        "mois": p["m"],
        "date": "/".join(reversed(p["d"].split("-"))) if p["d"] else "",
        "classe": p["c"],
        "ges": p["g"],
        "conso": round(p["co"]) if isinstance(p["co"], (int, float)) else "",
        "surface": p["s"] if p["s"] is not None else "",
        "type": p["t"],
        "annee": p["an"] if p["an"] is not None else "",
        "numero": p["n"],
    }


def _feature(p: Dict[str, Any], couleur: Optional[str] = None) -> Dict[str, Any]:
    props = _proprietes(p)
    if couleur:
        opts = {"color": couleur, "fillColor": couleur, "iconClass": "Circle"}
        props["_umap_options"] = opts
        props["_storage_options"] = opts  # instances uMap antérieures à 2.0
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
        "properties": props,
    }


def ecrire_geojson(points: List[Dict[str, Any]], chemin: str) -> None:
    """GeoJSON plat, une seule couche — pour la source distante (remote data)."""
    communes = palette_communes(points)
    fc = {
        "type": "FeatureCollection",
        "features": [
            _feature(p, communes.get(p["v"], {}).get("couleur")) for p in points
        ],
    }
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    log.info("%s entités écrites dans %s", len(points), chemin)


def ecrire_umap(points: List[Dict[str, Any]], chemin: str, titre: str) -> None:
    """Fichier .umap complet : une couche par commune, prêt à importer."""
    communes = palette_communes(points)
    lat = sum(p["lat"] for p in points) / len(points) if points else 45.83
    lon = sum(p["lon"] for p in points) / len(points) if points else 1.26

    couches = []
    for nom, info in communes.items():
        options = {
            "name": f"{nom} ({info['n']})",
            "displayOnLoad": True,
            "browsable": True,
            "color": info["couleur"],
            "fillColor": info["couleur"],
            "iconClass": "Circle",
            "showLabel": True,
            "labelKey": "{mois}",
            "popupShape": "Panel",
            "popupContentTemplate": GABARIT_POPUP,
        }
        couches.append(
            {
                "type": "FeatureCollection",
                "features": [_feature(p) for p in points if p["v"] == nom],
                "_umap_options": options,
                "_storage": options,  # instances uMap antérieures à 2.0
            }
        )

    carte = {
        "type": "umap",
        "uri": "",
        "properties": {
            "name": titre,
            "zoom": 11,
            "displayPopupFooter": True,
            "onLoadPanel": "none",
            "captionBar": False,
            "licence": "Données ADEME — Observatoire DPE, licence ouverte",
            "tilelayer": {
                "name": "OpenStreetMap",
                "url_template": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap contributors",
                "maxZoom": 19,
            },
        },
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "layers": couches,
    }
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(carte, f, ensure_ascii=False)
    log.info("%s couches communales écrites dans %s", len(couches), chemin)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Extraction DPE ADEME et carte")
    p.add_argument("--codes", default="codes-postaux.txt")
    p.add_argument("--depuis", default="2026-01-01", help="date AAAA-MM-JJ")
    p.add_argument("--csv", default="dpe.csv")
    p.add_argument("--carte", default="carte.html")
    p.add_argument("--geojson", default="dpe.geojson",
                   help="GeoJSON plat pour la source distante uMap")
    p.add_argument("--umap", default="dpe.umap",
                   help="fichier .umap complet, une couche par commune")
    p.add_argument("--taille-page", type=int, default=200)
    p.add_argument("--pause", type=float, default=0.4)
    p.add_argument("--diagnostic", action="store_true",
                   help="teste l'accès à l'API puis s'arrête")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                        stream=sys.stdout, force=True)

    client = ClientDPE(taille_page=args.taille_page, pause=args.pause)

    if args.diagnostic:
        ok = client.diagnostic()
        print("\n→ au moins une requête aboutit, l'extraction est possible"
              if ok else
              "\n→ toutes les requêtes sont refusées : l'IP du runner est filtrée,"
              " il faut une clé d'API ADEME ou un runner self-hosted")
        return 0 if ok else 1

    codes = charger_codes_postaux(args.codes)
    if not codes:
        log.error("aucun code postal dans %s", args.codes)
        return 1

    log.info("%s codes postaux · DPE depuis le %s", len(codes), args.depuis)
    lignes = client.recuperer_plusieurs(codes, depuis=args.depuis)
    if not lignes:
        log.error("aucun DPE récupéré")
        return 1

    ecrire_csv(lignes, args.csv)
    points = preparer_points(lignes)
    meta = (f"{len(lignes)} DPE depuis le {args.depuis[8:10]}/{args.depuis[5:7]}/"
            f"{args.depuis[0:4]} · maj {date.today().strftime('%d/%m/%Y')}")
    ecrire_carte(points, args.carte, "DPE récents", meta)
    if args.geojson:
        ecrire_geojson(points, args.geojson)
    if args.umap:
        ecrire_umap(points, args.umap, f"DPE récents — {meta}")

    comptes = Counter((l.get(CHAMPS["commune"]) or "?").strip() or "?" for l in lignes)
    print("\nPar commune : " + "  ".join(
        f"{v}={n}" for v, n in comptes.most_common(12)))
    sorties = [s for s in (args.csv, args.carte, args.geojson, args.umap) if s]
    print(f"{len(lignes)} DPE · {len(points)} géolocalisés → " + ", ".join(sorties))
    return 0


if __name__ == "__main__":
    sys.exit(main())

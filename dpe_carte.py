#!/usr/bin/env python3
"""
dpe_carte.py — extrait les DPE ADEME pour une liste de codes postaux,
écrit un CSV et génère une carte HTML autonome (Leaflet, aucun dépendance
Python supplémentaire : le rendu se fait côté navigateur via CDN).

    python dpe_carte.py --diagnostic
    python dpe_carte.py
    python dpe_carte.py --codes codes-postaux.txt --depuis 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from dpe_ademe import CHAMPS, ClientDPE, charger_codes_postaux, ecrire_csv

log = logging.getLogger("dpe_carte")

COULEURS = {
    "A": "#319834", "B": "#33cc31", "C": "#cbfc34", "D": "#fff32a",
    "E": "#fdd21c", "F": "#f3ac1c", "G": "#ec0000",
}


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


def preparer_points(lignes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    points, sans_geo = [], 0
    for l in lignes:
        xy = coordonnees(l)
        if not xy:
            sans_geo += 1
            continue
        classe = (l.get(CHAMPS["classe"]) or "").strip().upper()[:1]
        points.append(
            {
                "lat": round(xy[0], 6),
                "lon": round(xy[1], 6),
                "c": classe,
                "g": (l.get(CHAMPS["ges"]) or "").strip().upper()[:1],
                "a": l.get(CHAMPS["adresse"]) or "",
                "v": l.get(CHAMPS["commune"]) or "",
                "cp": l.get(CHAMPS["cp"]) or "",
                "d": (l.get(CHAMPS["date"]) or "")[:10],
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
    font-size:13px;max-width:230px}
  .panneau h1{font-size:14px;margin:0 0 6px}
  .panneau .meta{color:#666;font-size:12px;margin-bottom:10px}
  .filtres{display:flex;flex-wrap:wrap;gap:5px}
  .filtres button{border:1px solid #ddd;border-radius:6px;width:30px;height:30px;
    font-weight:700;cursor:pointer;color:#222}
  .filtres button.off{opacity:.28}
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
  <div class="filtres" id="filtres"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script>
const POINTS = __DONNEES__;
const COULEURS = __COULEURS__;
const carte = L.map('carte');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '© OpenStreetMap — données ADEME'
}).addTo(carte);

const actifs = new Set(Object.keys(COULEURS).concat(['']));
const groupe = L.markerClusterGroup({ maxClusterRadius: 45, disableClusteringAtZoom: 17 });

function contenu(p) {
  const l = [];
  if (p.t) l.push(['Type', p.t]);
  if (p.s) l.push(['Surface', p.s + ' m²']);
  if (p.an) l.push(['Construction', p.an]);
  if (p.co) l.push(['Conso', Math.round(p.co) + ' kWh/m²/an']);
  if (p.g) l.push(['GES', p.g]);
  if (p.d) l.push(['DPE du', p.d.split('-').reverse().join('/')]);
  if (p.n) l.push(['N°', p.n]);
  return '<div class="popup"><b>' + (p.a || 'Adresse inconnue') + '</b><br>'
    + p.cp + ' ' + p.v + '<br>'
    + '<span class="pastille" style="background:' + (COULEURS[p.c] || '#bbb') + '">'
    + (p.c || '?') + '</span>'
    + '<table>' + l.map(x => '<tr><td>' + x[0] + '</td><td>' + x[1] + '</td></tr>').join('')
    + '</table></div>';
}

function dessiner() {
  groupe.clearLayers();
  const visibles = POINTS.filter(p => actifs.has(p.c));
  visibles.forEach(p => {
    L.circleMarker([p.lat, p.lon], {
      radius: 7, weight: 1.5, color: '#333', opacity: .75,
      fillColor: COULEURS[p.c] || '#bbb', fillOpacity: .9
    }).bindPopup(contenu(p)).addTo(groupe);
  });
  if (visibles.length) {
    carte.fitBounds(L.latLngBounds(visibles.map(p => [p.lat, p.lon])).pad(0.08));
  }
}

const barre = document.getElementById('filtres');
Object.keys(COULEURS).forEach(c => {
  const b = document.createElement('button');
  b.textContent = c;
  b.style.background = COULEURS[c];
  b.onclick = () => { actifs.has(c) ? actifs.delete(c) : actifs.add(c);
                      b.classList.toggle('off'); dessiner(); };
  barre.appendChild(b);
});

carte.addLayer(groupe);
if (POINTS.length) { dessiner(); } else { carte.setView([45.83, 1.26], 11); }
</script>
</body>
</html>
"""


def ecrire_carte(points: List[Dict[str, Any]], chemin: str, titre: str, meta: str) -> None:
    html = (
        GABARIT.replace("__DONNEES__", json.dumps(points, ensure_ascii=False))
        .replace("__COULEURS__", json.dumps(COULEURS))
        .replace("__TITRE__", titre)
        .replace("__META__", meta)
    )
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("%s points cartographiés dans %s", len(points), chemin)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Extraction DPE ADEME et carte")
    p.add_argument("--codes", default="codes-postaux.txt")
    p.add_argument("--depuis", default="2026-01-01", help="date AAAA-MM-JJ")
    p.add_argument("--csv", default="dpe.csv")
    p.add_argument("--carte", default="carte.html")
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

    repartition: Dict[str, int] = {}
    for l in lignes:
        c = (l.get(CHAMPS["classe"]) or "?").strip().upper()[:1] or "?"
        repartition[c] = repartition.get(c, 0) + 1
    print("\nRépartition : " + "  ".join(
        f"{c}={repartition[c]}" for c in sorted(repartition)))
    print(f"{len(lignes)} DPE · {len(points)} géolocalisés → {args.csv}, {args.carte}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

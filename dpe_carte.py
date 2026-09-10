#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Carte des DPE par codes postaux, à partir de l'open data ADEME.

Source : https://data.ademe.fr  (jeu de données "dpe03existant" = logements
existants, DPE établis depuis le 1er juillet 2021). Licence Ouverte 2.0.
Mise à jour quotidienne côté ADEME.

Usage :
    python dpe_carte.py --cp codes-postaux.txt --depuis 2026-01-01 \
                        --url-publique https://moncompte.github.io/dpe/

Produit dans le dossier de sortie :
    carte.html + data.js         carte autonome, ouvrable en double-clic
    dpe.geojson                  tous les points
    commune-<nom>.geojson        une couche uMap par commune
    type-maison.geojson, etc.    une couche uMap par type de bien
    carte.umap                   carte uMap préconfigurée (--url-publique)
    dpe.csv                      tout l'extrait, pour Excel
    google-my-maps/*.csv         un fichier par commune, découpé à 2 000 lignes

Aucune clé d'API n'est nécessaire.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://data.ademe.fr/data-fair/api/v1/datasets"
DATASET = "dpe03existant"          # logements existants depuis juillet 2021
GEO_API = "https://geo.api.gouv.fr/communes"
PAGE_SIZE = 1000                   # 10 000 max côté ADEME, 1 000 = plus sûr
UA = {"User-Agent": "carte-dpe/2.0 (open data ADEME)"}

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

COULEURS = {"A": "#2f9e41", "B": "#5bc353", "C": "#a8d24a", "D": "#f2e14a",
            "E": "#f0b53f", "F": "#e8853a", "G": "#d94436", "?": "#9aa0a6"}

MYMAPS_MAX = 2000                  # limite d'un calque Google My Maps

COLONNES = ["classe", "ges", "mois_libelle", "date", "commune", "cp", "adresse",
            "type", "surface", "annee", "conso", "num", "lat", "lon"]


# ---------------------------------------------------------------- utilitaires

def http_json(url, tries=4):
    """GET JSON avec relances : l'API ADEME renvoie parfois des 429."""
    for essai in range(tries):
        try:
            with urlopen(Request(url, headers=UA), timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and essai < tries - 1:
                time.sleep(3 * (essai + 1))
                continue
            raise
        except URLError:
            if essai < tries - 1:
                time.sleep(3 * (essai + 1))
                continue
            raise


def sans_accent(s):
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def slug(s):
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


def echappe_champ(cle):
    """Échappe un nom de champ pour la syntaxe query_string d'Elasticsearch."""
    return re.sub(r'([+\-=&|><!(){}\[\]^"~*?:\\/ ])', r"\\\1", cle)


def fr(iso):
    return "/".join(reversed(iso.split("-")))


# ------------------------------------------------- découverte du schéma ADEME

def charger_schema():
    """Le schéma ADEME évolue (v2 -> v3). On lit les clés réelles au lieu de
    les coder en dur : le script survit à un renommage de colonne."""
    champs = http_json(f"{API}/{DATASET}/schema")
    return [c["key"] for c in champs if isinstance(c, dict) and "key" in c]


def trouver(cles, *motifs, obligatoire=False):
    """Première clé dont le nom normalisé vaut, puis contient, un des motifs."""
    index = {c: sans_accent(c) for c in cles}
    for motif in motifs:
        m = sans_accent(motif)
        for cle, norm in index.items():
            if norm == m:
                return cle
        for cle, norm in index.items():
            if m in norm:
                return cle
    if obligatoire:
        raise SystemExit(
            f"Champ introuvable dans le schéma ADEME (cherché : {motifs}).\n"
            f"Clés disponibles : {', '.join(sorted(cles)[:40])} ..."
        )
    return None


def mapper_champs(cles):
    return {
        "cp":      trouver(cles, "code_postal_ban", "code_postal", obligatoire=True),
        "date":    trouver(cles, "date_etablissement_dpe", "date_etablissement", obligatoire=True),
        "classe":  trouver(cles, "etiquette_dpe", obligatoire=True),
        "commune": trouver(cles, "nom_commune_ban", "nom_commune", "commune_ban"),
        "insee":   trouver(cles, "code_insee_ban", "code_insee"),
        "type":    trouver(cles, "type_batiment"),
        "ges":     trouver(cles, "etiquette_ges"),
        "adresse": trouver(cles, "adresse_ban", "adresse_brute", "adresse"),
        "surface": trouver(cles, "surface_habitable_logement", "surface_habitable"),
        "annee":   trouver(cles, "annee_construction", "periode_construction"),
        "conso":   trouver(cles, "conso_5_usages_par_m2_ep", "consommation_energie"),
        "num":     trouver(cles, "numero_dpe", "n_dpe"),
    }


# ------------------------------------------------------------- codes postaux

def lire_codes_postaux(source):
    """Accepte une liste séparée par des virgules ou des retours à la ligne,
    ou le chemin d'un fichier contenant un code postal par ligne."""
    brut = open(source, encoding="utf-8").read() if os.path.isfile(source) else source
    codes = sorted({c for c in re.findall(r"\b\d{5}\b", brut)})
    if not codes:
        raise SystemExit("Aucun code postal à 5 chiffres trouvé dans --cp.")
    return codes


_cache_communes = {}


def nom_commune(insee):
    """Code INSEE -> nom de commune, via l'API Géo de l'État (mis en cache)."""
    if not insee:
        return None
    insee = str(insee).strip()
    if insee not in _cache_communes:
        try:
            _cache_communes[insee] = http_json(
                f"{GEO_API}/{quote(insee)}?fields=nom").get("nom")
        except Exception:
            _cache_communes[insee] = None
    return _cache_communes[insee]


# ------------------------------------------------------------- extraction API

def extraire(cp, depuis, champs):
    """Tous les DPE d'un code postal depuis une date, en suivant le curseur
    `next` de l'API : pas de plafond à 10 000 lignes."""
    select = ",".join(sorted({v for v in champs.values() if v} | {"_geopoint"}))
    qs = (f'{echappe_champ(champs["cp"])}:"{cp}" AND '
          f'{echappe_champ(champs["date"])}:[{depuis} TO *]')
    url = (f"{API}/{DATASET}/lines?size={PAGE_SIZE}"
           f"&select={quote(select)}&qs={quote(qs)}"
           f"&sort={quote(champs['date'])}")

    lignes, total = [], None
    while url:
        page = http_json(url)
        if total is None:
            total = page.get("total", 0)
        lignes.extend(page.get("results", []))
        url = page.get("next")
        print(f"    {cp} : {len(lignes)}/{total}", end="\r", flush=True)
        time.sleep(0.2)
    print(f"    {cp} : {len(lignes)} DPE            ")
    return lignes


# ------------------------------------------------------------- normalisation

def normaliser_type(brut):
    if not brut:
        return "Non renseigné"
    t = sans_accent(brut)
    if "appartement" in t:
        return "Appartement"
    if "maison" in t:
        return "Maison"
    if "immeuble" in t:
        return "Immeuble"
    return "Autre"


def en_geojson(lignes, champs):
    features, sans_geo = [], 0
    for l in lignes:
        pt = l.get("_geopoint")
        if not pt:
            sans_geo += 1
            continue
        try:
            lat, lon = (float(x) for x in str(pt).split(","))
        except ValueError:
            sans_geo += 1
            continue

        d = str(l.get(champs["date"], ""))[:10]
        if len(d) < 7:
            continue
        an, mo = d[:4], d[5:7]

        commune = (l.get(champs["commune"]) if champs.get("commune") else None) \
            or nom_commune(l.get(champs["insee"]) if champs.get("insee") else None) \
            or "Commune inconnue"

        classe = (l.get(champs["classe"]) or "?").strip().upper()[:1] or "?"
        props = {
            "classe": classe,
            "date": d,
            "mois": f"{an}-{mo}",
            "mois_libelle": f"{MOIS[int(mo) - 1]} {an}",
            "commune": joli_nom(commune),
            "cp": str(l.get(champs["cp"], "")).strip(),
            "type": normaliser_type(l.get(champs["type"]) if champs.get("type") else None),
        }
        for cle in ("ges", "adresse", "surface", "annee", "conso", "num"):
            if champs.get(cle) and l.get(champs[cle]) not in (None, ""):
                v = l[champs[cle]]
                props[cle] = round(v, 1) if isinstance(v, float) else v

        # style porté par le point lui-même : uMap le lit, ce qui permet
        # d'organiser les couches par commune sans perdre la couleur DPE
        props["_umap_options"] = {
            "color": COULEURS.get(classe, COULEURS["?"]),
            "fillColor": COULEURS.get(classe, COULEURS["?"]),
            "fillOpacity": 0.9, "weight": 1, "radius": 6,
            "iconClass": "Circle",
        }

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": props,
        })
    if sans_geo:
        print(f"  {sans_geo} DPE écartés (adresse non géocodée par la BAN)")
    return {"type": "FeatureCollection", "features": features}


MOTS_MINUSCULES = {"la", "le", "les", "de", "des", "du", "d", "l", "sur",
                   "sous", "en", "et", "aux", "au", "lès"}


def joli_nom(nom):
    """LA GENEYTOUSE -> La Geneytouse ; SAINT-YRIEIX-LA-PERCHE ->
    Saint-Yrieix-la-Perche. Les petits mots restent en minuscules, sauf en
    tête de nom."""
    def morceau(m, premier):
        return m if (not premier and m in MOTS_MINUSCULES) else m.capitalize()

    sortie, premier = [], True
    for bloc in re.split(r"([ \-'])", str(nom).strip().lower()):
        if bloc in (" ", "-", "'"):
            sortie.append(bloc)
        elif bloc:
            sortie.append(morceau(bloc, premier))
            premier = False
    return "".join(sortie)


def grouper(features, cle):
    groupes = {}
    for f in features:
        groupes.setdefault(f["properties"][cle], []).append(f)
    return groupes


# ------------------------------------------------------------------- sorties

def en_lignes(features):
    for feat in features:
        ligne = {k: v for k, v in feat["properties"].items() if k != "_umap_options"}
        ligne["lon"], ligne["lat"] = feat["geometry"]["coordinates"]
        yield ligne


def ecrire_csv(features, chemin, delimiteur=";"):
    if not features:
        return
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES, extrasaction="ignore",
                           delimiter=delimiteur)
        w.writeheader()
        for ligne in en_lignes(features):
            w.writerow(ligne)


def ecrire_geojson(features, chemin):
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features},
                  f, ensure_ascii=False, separators=(",", ":"))


def ecrire_mymaps(geo, dossier):
    """Un CSV par commune, découpé à 2 000 lignes : chaque fichier s'importe
    tel quel comme un calque Google My Maps."""
    cible = os.path.join(dossier, "google-my-maps")
    os.makedirs(cible, exist_ok=True)
    fichiers = []
    for commune, lot in sorted(grouper(geo["features"], "commune").items()):
        morceaux = [lot[i:i + MYMAPS_MAX] for i in range(0, len(lot), MYMAPS_MAX)]
        for i, morceau in enumerate(morceaux, 1):
            suffixe = f"_{i}" if len(morceaux) > 1 else ""
            chemin = os.path.join(cible, f"{slug(commune)}{suffixe}.csv")
            ecrire_csv(morceau, chemin, delimiteur=",")
            fichiers.append((os.path.basename(chemin), len(morceau)))
    return fichiers


# -------------------------------------------------------------- couches uMap

def ecrire_couches_umap(geo, dossier, url_publique, depuis, titre):
    """Un GeoJSON par commune et par type de bien, plus un .umap préconfiguré :
    une couche par commune, les couleurs venant des points eux-mêmes."""
    communes = grouper(geo["features"], "commune")
    for commune, lot in communes.items():
        ecrire_geojson(lot, os.path.join(dossier, f"commune-{slug(commune)}.geojson"))
    for typ, lot in grouper(geo["features"], "type").items():
        ecrire_geojson(lot, os.path.join(dossier, f"type-{slug(typ)}.geojson"))

    if not url_publique:
        return None
    base = url_publique.rstrip("/") + "/"

    pts = [f["geometry"]["coordinates"] for f in geo["features"]]
    centre = ([round(sum(p[0] for p in pts) / len(pts), 5),
               round(sum(p[1] for p in pts) / len(pts), 5)]
              if pts else [1.2611, 45.8336])

    gabarit_popup = ("# {adresse}\n"
                     "**Classe {classe}** · {type} · {surface} m²\n\n"
                     "DPE de {mois_libelle} · {commune} {cp}")

    couches = []
    for commune in sorted(communes, key=lambda c: -len(communes[c])):
        couches.append({
            "type": "FeatureCollection",
            "features": [],
            "_umap_options": {
                "name": f"{commune} ({len(communes[commune])})",
                "displayOnLoad": True,
                "browsable": True,
                "iconClass": "Circle",
                "popupShape": "Default",
                "popupTemplate": "Default",
                "popupContentTemplate": gabarit_popup,
                "remoteData": {
                    "url": f"{base}commune-{slug(commune)}.geojson",
                    "format": "geojson",
                    "licence": "ADEME — Licence Ouverte 2.0",
                    "proxy": True,
                    "ttl": 86400,      # relit la source une fois par jour
                },
            },
        })

    umap = {
        "type": "umap",
        "uri": "",
        "properties": {
            "name": f"DPE {titre} — depuis le {fr(depuis)}",
            "description": ("Diagnostics de performance énergétique déposés à "
                            f"l'ADEME depuis le {fr(depuis)}. Une couche par "
                            "commune, couleur selon la classe énergie. "
                            "Source : data.ademe.fr, Licence Ouverte 2.0."),
            "zoom": 11,
            "licence": "",
            "displayPopupFooter": False,
            "captionBar": True,
            "onLoadPanel": "datafilters",
            "facetKey": ("classe|Classe énergie|checkbox,"
                         "type|Type de bien|checkbox,"
                         "mois_libelle|Mois du DPE|checkbox,"
                         "commune|Commune|checkbox"),
            "datalayersControl": True,
            "scaleControl": True,
            "zoomControl": True,
            "moreControl": True,
            "miniMap": False,
            "easing": False,
            "tilelayer": {
                "name": "OSM France",
                "url_template": "https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap France | DPE : ADEME",
                "minZoom": 0, "maxZoom": 20,
            },
        },
        "geometry": {"type": "Point", "coordinates": centre},
        "layers": couches,
    }
    chemin = os.path.join(dossier, "carte.umap")
    with open(chemin, "w", encoding="utf-8") as fh:
        json.dump(umap, fh, ensure_ascii=False, indent=1)
    return chemin


# ---------------------------------------------------------------- carte HTML

GABARIT = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DPE — __TITRE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<style>
  :root{--encre:#1c1b19;--papier:#faf9f6;--trait:#d9d5cc;--gris:#6c675e}
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;font-family:"Inter","Segoe UI",system-ui,sans-serif;color:var(--encre)}
  #app{display:flex;height:100%}
  #panneau{width:320px;flex:none;background:var(--papier);border-right:1px solid var(--trait);
           overflow-y:auto;padding:20px 18px 28px}
  #carte{flex:1}
  h1{font-size:19px;line-height:1.25;margin:0 0 2px;font-weight:650;letter-spacing:-.01em}
  .sous{font-size:12.5px;color:var(--gris);margin:0 0 20px;line-height:1.45}
  .bloc{margin-bottom:20px}
  .bloc h2{font-size:12px;font-weight:600;color:var(--gris);margin:0 0 9px}
  .echelle{display:flex;flex-direction:column;gap:3px}
  .barre{display:flex;align-items:center;gap:8px;border:0;background:none;padding:0;
         cursor:pointer;font:inherit;text-align:left;width:100%}
  .barre .jauge{height:23px;border-radius:2px 9px 9px 2px;display:flex;align-items:center;
                padding:0 9px;color:#1c1b19;font-weight:700;font-size:12.5px;
                transition:opacity .12s, filter .12s}
  .barre .nb{font-size:12px;color:var(--gris);font-variant-numeric:tabular-nums}
  .barre[aria-pressed="false"] .jauge{opacity:.22;filter:grayscale(1)}
  .barre:focus-visible{outline:2px solid var(--encre);outline-offset:2px}
  .chiffre{font-size:34px;font-weight:680;letter-spacing:-.03em;line-height:1}
  .chiffre span{font-size:13px;font-weight:500;color:var(--gris);letter-spacing:0}
  label.champ{display:block;font-size:12px;color:var(--gris);margin:0 0 5px}
  select,input[type=search]{width:100%;padding:7px 9px;border:1px solid var(--trait);
    border-radius:5px;font:inherit;font-size:13px;background:#fff}
  select[multiple]{height:132px;padding:4px}
  .duo{display:flex;gap:10px}.duo>div{flex:1;min-width:0}
  .raz{border:0;background:none;color:var(--gris);font:inherit;font-size:12px;
       text-decoration:underline;cursor:pointer;padding:0}
  .pied{font-size:11px;color:#8a857b;line-height:1.5;border-top:1px solid var(--trait);padding-top:12px}
  .popup{font-size:13px;line-height:1.5;min-width:200px}
  .popup .cl{display:inline-block;width:22px;height:22px;border-radius:4px;color:#1c1b19;
             font-weight:700;text-align:center;line-height:22px;margin-right:6px}
  .popup dt{color:var(--gris);font-size:11.5px}
  .popup dl{margin:8px 0 0;display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
  .popup dd{margin:0}
  @media (max-width:760px){#app{flex-direction:column}#panneau{width:auto;max-height:48%;
    border-right:0;border-bottom:1px solid var(--trait)}}
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div id="app">
  <div id="panneau">
    <h1>DPE — __TITRE__</h1>
    <p class="sous">Diagnostics déposés à l'ADEME depuis le __DEPUIS__. Extraction du __EXTRAIT__.</p>

    <div class="bloc">
      <div class="chiffre" id="total">—</div>
      <div class="sous" style="margin:4px 0 0">diagnostics affichés · <b id="pct">—</b> en F ou G</div>
    </div>

    <div class="bloc">
      <h2>Classes énergie — cliquer pour filtrer</h2>
      <div class="echelle" id="echelle"></div>
    </div>

    <div class="bloc duo">
      <div>
        <label class="champ" for="type">Type de bien</label>
        <select id="type"></select>
      </div>
      <div>
        <label class="champ" for="mois">Mois du DPE</label>
        <select id="mois"></select>
      </div>
    </div>

    <div class="bloc">
      <label class="champ" for="communes">Communes</label>
      <select id="communes" multiple></select>
    </div>

    <div class="bloc">
      <label class="champ" for="recherche">Rechercher une adresse ou une rue</label>
      <input type="search" id="recherche" placeholder="ex. avenue Garibaldi" autocomplete="off">
    </div>

    <div class="bloc"><button class="raz" id="raz">Réinitialiser les filtres</button></div>

    <p class="pied">Données ADEME (observatoire DPE), licence ouverte 2.0. Un DPE
    n'existe que si le logement a été vendu, loué ou construit : la base ne couvre
    pas tout le parc. Position issue du géocodage BAN, approximative sur certaines
    adresses.</p>
  </div>
  <div id="carte"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script src="data.js"></script>
<script>
const CLASSES = ["A","B","C","D","E","F","G"];
const COULEUR = __COULEURS__;
const points = (window.DPE_DATA && window.DPE_DATA.features) || [];
const actives = new Set(CLASSES.concat(["?"]));

const carte = L.map("carte").setView([45.83,1.26],10);
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  {maxZoom:19, attribution:'&copy; OpenStreetMap, &copy; CARTO — DPE : ADEME'}).addTo(carte);

const groupe = L.markerClusterGroup({
  maxClusterRadius: 48,
  iconCreateFunction(c){
    const enfants = c.getAllChildMarkers(), n = enfants.length;
    const pires = enfants.filter(m => "FG".includes(m._p.classe)).length;
    const part = n ? pires/n : 0;
    const teinte = part > .5 ? "#d94436" : part > .2 ? "#e8853a" : "#4a5a6a";
    return L.divIcon({
      html:`<div style="background:${teinte};color:#fff;width:36px;height:36px;
            border-radius:50%;display:flex;align-items:center;justify-content:center;
            font:600 12px/1 Inter,sans-serif;box-shadow:0 0 0 4px ${teinte}33">${n}</div>`,
      className:"", iconSize:[36,36]});
  }
}).addTo(carte);

function popup(p){
  const l = [["Type", p.type]];
  if (p.surface) l.push(["Surface", p.surface + " m²"]);
  if (p.annee) l.push(["Construction", p.annee]);
  if (p.conso) l.push(["Consommation", p.conso + " kWh/m²/an"]);
  if (p.ges) l.push(["GES", p.ges]);
  l.push(["DPE réalisé en", p.mois_libelle]);
  l.push(["Commune", p.commune + " " + p.cp]);
  return `<div class="popup">
    <span class="cl" style="background:${COULEUR[p.classe]||COULEUR['?']}">${p.classe}</span>
    <b>${p.adresse || p.commune}</b>
    <dl>${l.map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
}

const marqueurs = points.map(f => {
  const p = f.properties, c = f.geometry.coordinates;
  const m = L.circleMarker([c[1],c[0]], {
    radius:6, weight:1.2, color:"#ffffff", opacity:.9,
    fillColor: COULEUR[p.classe] || COULEUR["?"], fillOpacity:.92
  });
  m.bindPopup(() => popup(p));
  m._p = p;
  return m;
});

const uniques = (cle) => [...new Set(points.map(f => f.properties[cle]))].sort();
const selType = document.getElementById("type");
const selMois = document.getElementById("mois");
const selCom  = document.getElementById("communes");

selType.add(new Option("Tous", ""));
uniques("type").forEach(t => selType.add(new Option(t, t)));

selMois.add(new Option("Tous les mois", ""));
[...new Set(points.map(f => f.properties.mois))].sort().reverse().forEach(m =>
  selMois.add(new Option(points.find(f => f.properties.mois === m).properties.mois_libelle, m)));

uniques("commune").forEach(c => {
  const o = new Option(c, c); o.selected = true; selCom.add(o);
});

function communesChoisies(){
  const v = [...selCom.selectedOptions].map(o => o.value);
  return new Set(v.length ? v : uniques("commune"));
}

function retenus(ignorerClasse){
  const q = document.getElementById("recherche").value.trim().toLowerCase();
  const t = selType.value, m = selMois.value, com = communesChoisies();
  return marqueurs.filter(x => {
    const p = x._p;
    return (ignorerClasse || actives.has(p.classe)) &&
      (!t || p.type === t) && (!m || p.mois === m) && com.has(p.commune) &&
      (!q || (p.adresse||"").toLowerCase().includes(q) ||
             (p.commune||"").toLowerCase().includes(q));
  });
}

function rafraichir(){
  const gardes = retenus(false);
  groupe.clearLayers();
  groupe.addLayers(gardes);
  const n = gardes.length, fg = gardes.filter(x => "FG".includes(x._p.classe)).length;
  document.getElementById("total").innerHTML = n.toLocaleString("fr-FR") + ' <span>DPE</span>';
  document.getElementById("pct").textContent = n ? Math.round(100*fg/n) + " %" : "—";
  if (n) carte.fitBounds(L.featureGroup(gardes).getBounds().pad(.08), {maxZoom:16});

  const base = retenus(true);
  const max = Math.max(1, ...CLASSES.map(c => base.filter(x=>x._p.classe===c).length));
  CLASSES.forEach(c => {
    const nb = base.filter(x=>x._p.classe===c).length;
    const b = document.querySelector(`.barre[data-c="${c}"]`);
    b.querySelector(".jauge").style.width = (34 + 66*nb/max) + "%";
    b.querySelector(".nb").textContent = nb.toLocaleString("fr-FR");
  });
}

const echelle = document.getElementById("echelle");
CLASSES.forEach(c => {
  const b = document.createElement("button");
  b.className = "barre"; b.dataset.c = c; b.setAttribute("aria-pressed","true");
  b.innerHTML = `<span class="jauge" style="background:${COULEUR[c]}">${c}</span><span class="nb"></span>`;
  b.onclick = () => {
    const on = b.getAttribute("aria-pressed") === "true";
    b.setAttribute("aria-pressed", String(!on));
    on ? actives.delete(c) : actives.add(c);
    rafraichir();
  };
  echelle.appendChild(b);
});

selType.onchange = selMois.onchange = selCom.onchange = rafraichir;
let minuteur;
document.getElementById("recherche").oninput = () => {
  clearTimeout(minuteur); minuteur = setTimeout(rafraichir, 250);
};
document.getElementById("raz").onclick = () => {
  CLASSES.forEach(c => { actives.add(c);
    document.querySelector(`.barre[data-c="${c}"]`).setAttribute("aria-pressed","true"); });
  selType.value = ""; selMois.value = "";
  [...selCom.options].forEach(o => o.selected = true);
  document.getElementById("recherche").value = "";
  rafraichir();
};

rafraichir();
</script>
</body>
</html>
"""


def ecrire_carte(geo, dossier, titre, depuis):
    charge = json.dumps(geo, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(dossier, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.DPE_DATA=" + charge + ";")
    with open(os.path.join(dossier, "dpe.geojson"), "w", encoding="utf-8") as f:
        f.write(charge)

    html = (GABARIT
            .replace("__TITRE__", titre)
            .replace("__DEPUIS__", fr(depuis))
            .replace("__EXTRAIT__", date.today().strftime("%d/%m/%Y"))
            .replace("__COULEURS__", json.dumps(COULEURS, ensure_ascii=False)))
    with open(os.path.join(dossier, "carte.html"), "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Carte des DPE ADEME par codes postaux")
    ap.add_argument("--cp", required=True,
                    help="codes postaux séparés par des virgules, ou chemin "
                         "d'un fichier contenant un code par ligne")
    ap.add_argument("--depuis", default="2026-01-01",
                    help="date minimale du DPE (AAAA-MM-JJ), défaut 2026-01-01")
    ap.add_argument("--out", default="sortie", help="dossier de sortie")
    ap.add_argument("--titre", default="Haute-Vienne", help="titre de la carte")
    ap.add_argument("--url-publique", default="",
                    help="URL publique du dossier de sortie (ex. "
                         "https://moncompte.github.io/dpe/) — génère en plus "
                         "un carte.umap prêt à importer dans uMap")
    args = ap.parse_args()

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.depuis):
        raise SystemExit("--depuis attend une date au format AAAA-MM-JJ")
    os.makedirs(args.out, exist_ok=True)

    codes = lire_codes_postaux(args.cp)
    print(f"{len(codes)} codes postaux · DPE depuis le {fr(args.depuis)}")

    print("Lecture du schéma ADEME…")
    champs = mapper_champs(charger_schema())
    if not champs.get("type"):
        print("  ! type de bâtiment absent du schéma : colonne 'type' vide",
              file=sys.stderr)

    lignes = []
    for cp in codes:
        lignes += extraire(cp, args.depuis, champs)

    geo = en_geojson(lignes, champs)
    ecrire_carte(geo, args.out, args.titre, args.depuis)
    ecrire_csv(geo["features"], os.path.join(args.out, "dpe.csv"))
    calques = ecrire_mymaps(geo, args.out)
    umap = ecrire_couches_umap(geo, args.out, args.url_publique, args.depuis, args.titre)

    n = len(geo["features"])
    fg = sum(1 for f in geo["features"] if f["properties"]["classe"] in ("F", "G"))
    print(f"\n{n} DPE cartographiés, dont {fg} en F ou G "
          f"({round(100*fg/n) if n else 0} %).")

    par_commune = grouper(geo["features"], "commune")
    print(f"\n{len(par_commune)} communes :")
    for commune, lot in sorted(par_commune.items(), key=lambda x: -len(x[1])):
        detail = ", ".join(f"{len(v)} {k.lower()}"
                           for k, v in sorted(grouper(lot, "type").items()))
        print(f"    {commune:<26} {len(lot):>5}   ({detail})")

    print(f"\nCarte locale          : {os.path.join(args.out, 'carte.html')}")
    print(f"Tableur               : {os.path.join(args.out, 'dpe.csv')}")
    print(f"Calques My Maps       : {len(calques)} fichiers dans google-my-maps/")
    if umap:
        print(f"Carte uMap à importer : {umap}")
    else:
        print("Ajoutez --url-publique pour générer le fichier carte.umap.")


if __name__ == "__main__":
    main()

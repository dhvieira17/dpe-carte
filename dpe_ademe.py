"""
dpe_ademe.py — client pour le jeu de données DPE logements existants de l'ADEME
(data-fair / data.ademe.fr).

Seule dépendance externe : requests.

Contournements du 403 nginx observé depuis les runners GitHub :
  - en-têtes de navigateur (le WAF filtre "python-requests/x.y") ;
  - filtres natifs data-fair (*_eq, *_gte) plutôt que `qs`, ce qui évite
    les crochets et le wildcard dans l'URL ;
  - repli automatique sur `qs` si les filtres natifs sont refusés ;
  - repli automatique sans `select` si le select est refusé (400) ;
  - pagination par curseur (champ `next`) au lieu de size=1000 ;
  - retries exponentiels sur 403 / 429 / 5xx ;
  - clé d'API facultative via la variable d'environnement ADEME_API_KEY.
"""

from __future__ import annotations

import csv
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

__all__ = [
    "ClientDPE",
    "AccesRefuse",
    "ErreurRequete",
    "charger_codes_postaux",
    "ecrire_csv",
    "CHAMPS",
]

DATASET = "dpe03existant"
BASE_DATASET = f"https://data.ademe.fr/data-fair/api/v1/datasets/{DATASET}"
URL_LINES = f"{BASE_DATASET}/lines"
URL_SCHEMA = f"{BASE_DATASET}/schema"
PAGE_JEU_DONNEES = f"https://data.ademe.fr/datasets/{DATASET}"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# alias interne -> nom de colonne ADEME
CHAMPS: Dict[str, str] = {
    "num": "numero_dpe",
    "date": "date_etablissement_dpe",
    "classe": "etiquette_dpe",
    "ges": "etiquette_ges",
    "conso": "conso_5_usages_par_m2_ep",
    "type": "type_batiment",
    "surface": "surface_habitable_logement",
    "annee": "annee_construction",
    "adresse": "adresse_ban",
    "cp": "code_postal_ban",
    "commune": "nom_commune_ban",
    "insee": "code_insee_ban",
}

# ajoutés au select s'ils existent (coordonnées pour la carte)
CHAMPS_GEO = ["_geopoint", "latitude", "longitude"]

CHAMP_CP = CHAMPS["cp"]
CHAMP_DATE = CHAMPS["date"]

STATUTS_A_REESSAYER = {403, 429, 500, 502, 503, 504}

log = logging.getLogger("dpe_ademe")


class ErreurRequete(RuntimeError):
    """Réponse HTTP non-200 définitive."""

    def __init__(self, statut: int, url: str, extrait: str = "") -> None:
        super().__init__(f"HTTP {statut} sur {url} — {extrait}")
        self.statut = statut
        self.url = url


class AccesRefuse(ErreurRequete):
    """403 persistant renvoyé par le reverse-proxy."""


class ClientDPE:
    def __init__(
        self,
        api_key: Optional[str] = None,
        taille_page: int = 200,
        pause: float = 0.4,
        max_essais: int = 5,
        timeout: int = 90,
    ) -> None:
        self.taille_page = taille_page
        self.pause = pause
        self.max_essais = max_essais
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("ADEME_API_KEY") or None
        self.session = self._creer_session()
        self._colonnes: Optional[List[str]] = None
        self._select: Optional[str] = None
        self.repli_qs = False        # passe à True après un 403 sur filtres natifs
        self.sans_select = False     # passe à True après un 400 sur le select

    # -- session -----------------------------------------------------------

    def _creer_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Referer": PAGE_JEU_DONNEES,
                "Connection": "keep-alive",
            }
        )
        if self.api_key:
            s.headers["x-apiKey"] = self.api_key
            log.info("clé d'API ADEME détectée")
        return s

    # -- couche HTTP -------------------------------------------------------

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        derniere: Any = None
        for essai in range(1, self.max_essais + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                derniere = exc
                if essai < self.max_essais:
                    attente = 2 ** (essai - 1)
                    log.warning("réseau : %s — nouvel essai dans %ss", exc, attente)
                    time.sleep(attente)
                    continue
                break

            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise ErreurRequete(200, r.url, "réponse non JSON")

            derniere = r
            if r.status_code in STATUTS_A_REESSAYER and essai < self.max_essais:
                attente = 2 ** (essai - 1)
                log.warning(
                    "HTTP %s — essai %s/%s dans %ss",
                    r.status_code, essai + 1, self.max_essais, attente,
                )
                time.sleep(attente)
                continue
            break

        if isinstance(derniere, requests.Response):
            extrait = (derniere.text or "")[:200].replace("\n", " ")
            if derniere.status_code == 403:
                raise AccesRefuse(403, derniere.url, extrait)
            raise ErreurRequete(derniere.status_code, derniere.url, extrait)
        raise ErreurRequete(0, url, str(derniere))

    # -- schéma ------------------------------------------------------------

    def colonnes_disponibles(self) -> List[str]:
        if self._colonnes is None:
            try:
                schema = self._get(URL_SCHEMA)
            except ErreurRequete as exc:
                log.warning("schéma illisible (%s) — select désactivé", exc)
                self._colonnes = []
                self.sans_select = True
                return self._colonnes
            if isinstance(schema, dict):
                schema = schema.get("schema", [])
            self._colonnes = [c["key"] for c in schema if isinstance(c, dict) and "key" in c]
            log.info("schéma lu : %s colonnes", len(self._colonnes))
        return self._colonnes

    def select(self) -> Optional[str]:
        if self.sans_select:
            return None
        if self._select is None:
            dispo = set(self.colonnes_disponibles())
            if not dispo:
                return None
            champs = [c for c in CHAMPS.values() if c in dispo]
            manquants = [c for c in CHAMPS.values() if c not in dispo]
            if manquants:
                log.warning("colonnes absentes du schéma : %s", ", ".join(manquants))
            champs += [c for c in CHAMPS_GEO if c in dispo]
            if "_geopoint" not in champs:
                champs.append("_geopoint")  # champ virtuel, absent du schéma
            self._select = ",".join(champs)
        return self._select

    # -- requêtes ----------------------------------------------------------

    def _params(self, cp: str, depuis: Optional[str]) -> Dict[str, Any]:
        p: Dict[str, Any] = {"size": self.taille_page, "sort": CHAMP_DATE}
        sel = self.select()
        if sel:
            p["select"] = sel
        if self.repli_qs:
            morceaux = [f'{CHAMP_CP}:"{cp}"']
            if depuis:
                morceaux.append(f"{CHAMP_DATE}:[{depuis} TO *]")
            p["qs"] = " AND ".join(morceaux)
        else:
            p[f"{CHAMP_CP}_eq"] = cp
            if depuis:
                p[f"{CHAMP_DATE}_gte"] = depuis
        return p

    def recuperer(self, cp: str, depuis: Optional[str] = None) -> List[Dict[str, Any]]:
        """Tous les DPE d'un code postal, depuis une date AAAA-MM-JJ."""
        for tentative in range(3):
            try:
                return self._paginer(self._params(cp, depuis))
            except AccesRefuse:
                if self.repli_qs:
                    raise
                log.warning("filtres natifs refusés (403) — repli sur qs")
                self.repli_qs = True
            except ErreurRequete as exc:
                if exc.statut == 400 and not self.sans_select:
                    log.warning("select refusé (400) — requête sans select")
                    self.sans_select = True
                    self._select = None
                else:
                    raise
        return []

    def _paginer(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        lignes: List[Dict[str, Any]] = []
        url: Optional[str] = URL_LINES
        p: Optional[Dict[str, Any]] = params
        vues = 0
        while url:
            data = self._get(url, params=p)
            lot = data.get("results", [])
            lignes.extend(lot)
            url = data.get("next")  # URL complète, ne pas repasser params
            p = None
            vues += 1
            if vues > 500:  # garde-fou anti-boucle
                log.warning("pagination interrompue après 500 pages")
                break
            if url:
                time.sleep(self.pause)
        return lignes

    def recuperer_plusieurs(
        self, codes_postaux: Iterable[str], depuis: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        total: List[Dict[str, Any]] = []
        codes = list(codes_postaux)
        echecs = 0
        for i, cp in enumerate(codes, 1):
            try:
                lignes = self.recuperer(cp, depuis)
            except Exception as exc:
                echecs += 1
                log.error("%s : échec — %s", cp, exc)
                continue
            log.info("%s/%s  %s : %s DPE", i, len(codes), cp, len(lignes))
            total.extend(lignes)
            time.sleep(self.pause)
        if echecs:
            log.warning("%s code(s) postal(aux) en échec sur %s", echecs, len(codes))
        return total

    # -- diagnostic --------------------------------------------------------

    def diagnostic(self) -> bool:
        """Escalade de requêtes pour identifier ce que le serveur refuse.
        Renvoie True si au moins un appel à /lines aboutit."""
        tests = [
            ("size=1 sans en-têtes", {"size": 1}, False),
            ("size=1 avec en-têtes", {"size": 1}, True),
            ("size=1000 avec en-têtes", {"size": 1000}, True),
            ("select 12 colonnes", {"size": 5, "select": ",".join(CHAMPS.values())}, True),
            (
                "filtres natifs",
                {"size": 5, f"{CHAMP_CP}_eq": "87000", f"{CHAMP_DATE}_gte": "2026-01-01"},
                True,
            ),
            ("qs simple", {"size": 5, "qs": f'{CHAMP_CP}:"87000"'}, True),
            ("qs avec intervalle", {"size": 5, "qs": f"{CHAMP_DATE}:[2026-01-01 TO *]"}, True),
        ]
        nue = requests.Session()
        succes = False
        for nom, params, avec_entetes in tests:
            s = self.session if avec_entetes else nue
            try:
                r = s.get(URL_LINES, params=params, timeout=self.timeout)
                code: Any = r.status_code
                if code == 200:
                    succes = True
            except requests.RequestException as exc:
                code = f"ERR {type(exc).__name__}"
            print(f"{'OK ' if code == 200 else 'KO '} {code}  {nom}", flush=True)
            time.sleep(0.5)
        return succes


# --------------------------------------------------------------------------
# Entrées / sorties
# --------------------------------------------------------------------------


def charger_codes_postaux(chemin: str) -> List[str]:
    """Un code postal par ligne ; commentaires (#) et lignes vides ignorés."""
    codes: List[str] = []
    with open(chemin, encoding="utf-8-sig") as f:
        for ligne in f:
            ligne = ligne.split("#", 1)[0].strip().strip(",;\"'")
            if ligne:
                codes.append(ligne)
    vus, uniques = set(), []
    for c in codes:
        if c not in vus:
            vus.add(c)
            uniques.append(c)
    log.info("%s codes postaux lus dans %s", len(uniques), chemin)
    return uniques


def ecrire_csv(lignes: List[Dict[str, Any]], chemin: str) -> None:
    if not lignes:
        log.warning("aucune ligne à écrire dans %s", chemin)
        return
    entetes: List[str] = []
    for l in lignes:
        for k in l:
            if k not in entetes:
                entetes.append(k)
    with open(chemin, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=entetes, extrasaction="ignore", delimiter=";")
        w.writeheader()
        w.writerows(lignes)
    log.info("%s lignes écrites dans %s", len(lignes), chemin)

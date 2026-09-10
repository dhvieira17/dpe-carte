"""
dpe_ademe.py — client robuste pour le jeu de données DPE logements existants
de l'ADEME (data-fair / data.ademe.fr).

Conçu pour tourner depuis un runner GitHub Actions, où l'endpoint /lines
renvoie souvent un 403 nginx alors que /metadata et /schema passent.

Contournements intégrés :
  - session HTTP avec User-Agent navigateur + Referer (le WAF filtre
    "python-requests/2.x" sur les endpoints de données) ;
  - filtres natifs data-fair (*_eq, *_gte) au lieu du paramètre `qs`,
    ce qui évite les crochets et le wildcard `*` dans l'URL ;
  - repli automatique sur `qs` si les filtres natifs sont refusés ;
  - pagination par curseur (champ `next`) au lieu de size=1000 ;
  - `select` limité aux colonnes utiles (12 sur 230) ;
  - retries exponentiels sur 403 / 429 / 5xx ;
  - support d'une clé d'API via la variable d'environnement ADEME_API_KEY.

Utilisation comme module :

    from dpe_ademe import ClientDPE, charger_codes_postaux

    client = ClientDPE()
    codes = charger_codes_postaux("codes-postaux.txt")
    lignes = client.recuperer_plusieurs(codes, depuis="2026-01-01")

Utilisation en ligne de commande :

    python dpe_ademe.py --diagnostic
    python dpe_ademe.py --codes codes-postaux.txt --depuis 2026-01-01 --sortie dpe.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

__all__ = [
    "ClientDPE",
    "AccesRefuse",
    "charger_codes_postaux",
    "ecrire_csv",
    "CHAMPS",
    "BASE_DATASET",
]

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------

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
    "cp": "code_postal_ban",
    "date": "date_etablissement_dpe",
    "classe": "etiquette_dpe",
    "commune": "nom_commune_ban",
    "insee": "code_insee_ban",
    "type": "type_batiment",
    "ges": "etiquette_ges",
    "adresse": "adresse_ban",
    "surface": "surface_habitable_logement",
    "annee": "annee_construction",
    "conso": "conso_5_usages_par_m2_ep",
    "num": "numero_dpe",
}

# colonnes ajoutées si elles existent dans le schéma (utiles pour la carte)
CHAMPS_OPTIONNELS = ["_geopoint", "latitude", "longitude"]

CHAMP_CP = CHAMPS["cp"]
CHAMP_DATE = CHAMPS["date"]

STATUTS_A_REESSAYER = {403, 429, 500, 502, 503, 504}

log = logging.getLogger("dpe_ademe")


class AccesRefuse(RuntimeError):
    """403 persistant renvoyé par le reverse-proxy, hors quota/throttling."""


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


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
        # passe à True si les filtres natifs se font refuser une fois
        self.repli_qs = False

    def _creer_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Referer": PAGE_JEU_DONNEES,
                "Origin": "https://data.ademe.fr",
                "Connection": "keep-alive",
            }
        )
        if self.api_key:
            s.headers["x-apiKey"] = self.api_key
            log.info("clé d'API ADEME détectée (ADEME_API_KEY)")
        return s

    # -- couche HTTP -------------------------------------------------------

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        derniere = None
        for essai in range(1, self.max_essais + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                derniere = exc
                attente = 2 ** (essai - 1)
                log.warning("erreur réseau (%s) — nouvel essai dans %ss", exc, attente)
                time.sleep(attente)
                continue

            if r.status_code == 200:
                return r.json()

            derniere = r
            if r.status_code in STATUTS_A_REESSAYER and essai < self.max_essais:
                attente = 2 ** (essai - 1)
                log.warning(
                    "HTTP %s sur %s — nouvel essai %s/%s dans %ss",
                    r.status_code,
                    r.url,
                    essai + 1,
                    self.max_essais,
                    attente,
                )
                time.sleep(attente)
                continue
            break

        if isinstance(derniere, requests.Response):
            extrait = (derniere.text or "")[:300].replace("\n", " ")
            if derniere.status_code == 403:
                raise AccesRefuse(f"403 persistant sur {derniere.url} — {extrait}")
            raise RuntimeError(f"HTTP {derniere.status_code} sur {derniere.url} — {extrait}")
        raise RuntimeError(f"échec réseau sur {url} — {derniere}")

    # -- schéma ------------------------------------------------------------

    def colonnes_disponibles(self) -> List[str]:
        if self._colonnes is None:
            schema = self._get(URL_SCHEMA)
            if isinstance(schema, dict):
                schema = schema.get("schema", [])
            self._colonnes = [c.get("key") for c in schema if isinstance(c, dict)]
            log.info("schéma lu : %s colonnes", len(self._colonnes))
        return self._colonnes

    def _select(self) -> str:
        dispo = set(self.colonnes_disponibles())
        champs = [c for c in CHAMPS.values() if c in dispo]
        champs += [c for c in CHAMPS_OPTIONNELS if c in dispo]
        manquants = [c for c in CHAMPS.values() if c not in dispo]
        if manquants:
            log.warning("colonnes absentes du schéma, ignorées : %s", ", ".join(manquants))
        return ",".join(champs)

    # -- récupération ------------------------------------------------------

    def _params(self, cp: str, depuis: Optional[str], select: str) -> Dict[str, Any]:
        p: Dict[str, Any] = {
            "size": self.taille_page,
            "select": select,
            "sort": CHAMP_DATE,
        }
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
        """Tous les DPE d'un code postal, depuis une date (AAAA-MM-JJ)."""
        select = self._select()
        try:
            return self._paginer(self._params(cp, depuis, select))
        except AccesRefuse:
            if self.repli_qs:
                raise
            log.warning("filtres natifs refusés (403) — repli sur le paramètre qs")
            self.repli_qs = True
            return self._paginer(self._params(cp, depuis, select))

    def _paginer(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        lignes: List[Dict[str, Any]] = []
        url: Optional[str] = URL_LINES
        p: Optional[Dict[str, Any]] = params
        while url:
            data = self._get(url, params=p)
            lignes.extend(data.get("results", []))
            url = data.get("next")  # déjà complète, ne pas repasser params
            p = None
            if url:
                time.sleep(self.pause)
        return lignes

    def recuperer_plusieurs(
        self, codes_postaux: Iterable[str], depuis: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        total: List[Dict[str, Any]] = []
        codes = list(codes_postaux)
        for i, cp in enumerate(codes, 1):
            try:
                lignes = self.recuperer(cp, depuis)
            except Exception as exc:
                log.error("%s : échec (%s)", cp, exc)
                continue
            log.info("%s/%s — %s : %s DPE", i, len(codes), cp, len(lignes))
            total.extend(lignes)
            time.sleep(self.pause)
        return total

    # -- diagnostic --------------------------------------------------------

    def diagnostic(self) -> None:
        """Escalade de requêtes pour identifier ce que le WAF refuse."""
        tests = [
            ("size=1 sans en-têtes", {"size": 1}, False),
            ("size=1 avec en-têtes", {"size": 1}, True),
            ("size=1000 avec en-têtes", {"size": 1000}, True),
            (
                "filtres natifs",
                {"size": 10, f"{CHAMP_CP}_eq": "87000", f"{CHAMP_DATE}_gte": "2026-01-01"},
                True,
            ),
            ("qs simple", {"size": 10, "qs": f'{CHAMP_CP}:"87000"'}, True),
            (
                "qs avec intervalle",
                {"size": 10, "qs": f"{CHAMP_DATE}:[2026-01-01 TO *]"},
                True,
            ),
        ]
        nue = requests.Session()
        for nom, params, avec_entetes in tests:
            s = self.session if avec_entetes else nue
            try:
                r = s.get(URL_LINES, params=params, timeout=self.timeout)
                code = r.status_code
            except requests.RequestException as exc:
                code = f"ERR {exc}"
            marque = "OK " if code == 200 else "KO "
            print(f"{marque}{code}  {nom}")
            time.sleep(0.5)


# --------------------------------------------------------------------------
# Entrées / sorties
# --------------------------------------------------------------------------


def charger_codes_postaux(chemin: str) -> List[str]:
    """Un code postal par ligne ; # et lignes vides ignorés."""
    codes: List[str] = []
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.split("#", 1)[0].strip()
            if ligne:
                codes.append(ligne)
    log.info("%s codes postaux lus dans %s", len(codes), chemin)
    return codes


def ecrire_csv(lignes: List[Dict[str, Any]], chemin: str) -> None:
    if not lignes:
        log.warning("aucune ligne à écrire dans %s", chemin)
        return
    entetes: List[str] = []
    for l in lignes:
        for k in l:
            if k not in entetes:
                entetes.append(k)
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=entetes, extrasaction="ignore")
        w.writeheader()
        w.writerows(lignes)
    log.info("%s lignes écrites dans %s", len(lignes), chemin)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Extraction DPE ADEME")
    p.add_argument("--codes", default="codes-postaux.txt")
    p.add_argument("--depuis", default="2026-01-01", help="date AAAA-MM-JJ")
    p.add_argument("--sortie", default="dpe.csv")
    p.add_argument("--taille-page", type=int, default=200)
    p.add_argument("--pause", type=float, default=0.4)
    p.add_argument("--diagnostic", action="store_true", help="teste l'accès et sort")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(message)s",
        stream=sys.stdout,
        force=True,
    )

    client = ClientDPE(taille_page=args.taille_page, pause=args.pause)

    if args.diagnostic:
        client.diagnostic()
        return 0

    codes = charger_codes_postaux(args.codes)
    lignes = client.recuperer_plusieurs(codes, depuis=args.depuis)
    ecrire_csv(lignes, args.sortie)
    if not lignes:
        log.error("aucun DPE récupéré")
        return 1
    print(f"{len(lignes)} DPE récupérés depuis le {args.depuis}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

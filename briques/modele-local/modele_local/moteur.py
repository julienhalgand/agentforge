"""Moteur intégré : llama.cpp téléchargé, installé et lancé par la brique elle-même.

Rien à installer à part. La brique :
1. télécharge le binaire `llama-server` (dernière version publiée de
   ggml-org/llama.cpp, variante CPU ou Vulkan = GPU NVIDIA/AMD/Intel, Metal
   sur Mac) dans `moteur/` ;
2. télécharge un modèle GGUF dans `modeles/` (reprise si interrompu) ;
3. lance `llama-server` en arrière-plan sur 127.0.0.1 (API openai-compatible)
   et le surveille ; `serveur.json` garde pid, port et modèle.

Tout est local : le serveur n'écoute que sur 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from forge.utils.erreurs import ErreurForge
from forge.utils.fichiers import ecrire_json, lire_json
from forge.utils.reseau import contexte_ssl

DOSSIER = Path(__file__).resolve().parent.parent
DOSSIER_MOTEUR = DOSSIER / "moteur"
DOSSIER_MODELES = DOSSIER / "modeles"
FICHIER_MOTEUR = DOSSIER_MOTEUR / "moteur.json"
FICHIER_SERVEUR = DOSSIER_MOTEUR / "serveur.json"
FICHIER_JOURNAL = DOSSIER_MOTEUR / "llama-server.log"
API_VERSIONS = os.environ.get("FORGE_LLAMA_RELEASES", "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30")
PORT_PAR_DEFAUT = 8791
MODELE_PAR_DEFAUT = "qwen2.5-1.5b"  # le plus petit : ça marche direct, on change ensuite si on veut

# Modèles proposés : fichiers GGUF quantifiés Q4_K_M (bon compromis taille/qualité), dépôts publics sans compte.
CATALOGUE = {
    "qwen2.5-1.5b": {"titre": "Qwen 2.5 1.5B — très léger, installé par défaut, CPU seul ok", "taille_go": 1.1, "url": "https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"},
    "qwen2.5-3b": {"titre": "Qwen 2.5 3B — léger, bon en français", "taille_go": 2.0, "url": "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf"},
    "qwen2.5-7b": {"titre": "Qwen 2.5 7B — meilleur, GPU 6 Go ou CPU lent", "taille_go": 4.7, "url": "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf"},
    "llama-3.2-3b": {"titre": "Llama 3.2 3B — léger, généraliste", "taille_go": 2.0, "url": "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf"},
    "mistral-7b": {"titre": "Mistral 7B — bon en français, GPU 6 Go ou CPU lent", "taille_go": 4.4, "url": "https://huggingface.co/bartowski/Mistral-7B-Instruct-v0.3-GGUF/resolve/main/Mistral-7B-Instruct-v0.3-Q4_K_M.gguf"},
}


# --- plateforme --------------------------------------------------------------------

def plateforme() -> tuple[str, str]:
    systeme = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return systeme, arch


def choisir_asset(noms: list[str], gpu: bool) -> str:
    """Nom de l'archive à prendre parmi les assets d'une version de llama.cpp."""
    systeme, arch = plateforme()
    if systeme == "windows":
        candidats = (["win-vulkan-x64"] if gpu else []) + ["win-cpu-x64"]
    elif systeme == "linux":
        candidats = (["ubuntu-vulkan-x64"] if gpu else []) + ["ubuntu-x64"]
    else:
        candidats = [f"macos-{arch}"]
    for motif in candidats:
        for nom in noms:
            if nom.endswith(".zip") and f"bin-{motif}" in nom:
                return nom
    raise ErreurForge(
        f"aucune archive llama.cpp pour {systeme}/{arch} (gpu={gpu})",
        "vérifiez https://github.com/ggml-org/llama.cpp/releases ; assets vus : " + ", ".join(noms[:12]),
    )


# --- téléchargement avec reprise ---------------------------------------------------

def telecharger(url: str, cible: Path, progres=None, annulee=None, delai_s: float = 60.0) -> Path:
    """Télécharge `url` vers `cible` par blocs, avec reprise (`.part` + en-tête Range). Rend la cible ; lève si annulé."""
    cible.parent.mkdir(parents=True, exist_ok=True)
    partiel = cible.with_suffix(cible.suffix + ".part")
    deja = partiel.stat().st_size if partiel.exists() else 0
    entetes = {"User-Agent": "agentforge/0.1"}
    if deja:
        entetes["Range"] = f"bytes={deja}-"
    requete = urllib.request.Request(url, headers=entetes)
    try:
        reponse = urllib.request.urlopen(requete, timeout=delai_s, context=contexte_ssl())
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # déjà complet
            partiel.replace(cible)
            return cible
        raise ErreurForge(f"téléchargement refusé : HTTP {exc.code} pour {url}", "le fichier a peut-être changé de nom ; vérifiez l'URL dans le catalogue") from exc
    except urllib.error.URLError as exc:
        raise ErreurForge(f"téléchargement impossible : {exc.reason}", "vérifiez la connexion internet ou le proxy ; la reprise se fera là où ça s'est arrêté") from exc
    with reponse:
        if reponse.status == 200 and deja:
            deja = 0  # le serveur ignore Range : on repart de zéro
            partiel.unlink()
        total = int(reponse.headers.get("Content-Length") or 0) + deja
        recu = deja
        with open(partiel, "ab" if deja else "wb") as f:
            dernier_progres = 0.0
            while True:
                if annulee and annulee():
                    return partiel  # les octets reçus restent pour une reprise
                bloc = reponse.read(1 << 20)
                if not bloc:
                    break
                f.write(bloc)
                recu += len(bloc)
                if progres and time.time() - dernier_progres > 0.5:
                    dernier_progres = time.time()
                    progres(100.0 * recu / total if total else 0.0, f"{recu / 1e9:.2f} Go" + (f" / {total / 1e9:.2f} Go" if total else ""))
    os.replace(partiel, cible)
    return cible


# --- moteur --------------------------------------------------------------------------

def moteur_installe() -> dict | None:
    if FICHIER_MOTEUR.exists():
        try:
            info = lire_json(FICHIER_MOTEUR)
            if Path(info.get("executable", "")).exists():
                return info
        except Exception:
            pass
    return None


def _trouver_executable(dossier: Path) -> Path | None:
    nom = "llama-server.exe" if plateforme()[0] == "windows" else "llama-server"
    for p in dossier.rglob(nom):
        return p
    return None


def installer_moteur(gpu: bool, progres=None, annulee=None) -> dict:
    """Télécharge et dépose llama-server dans moteur/ ; rend moteur.json."""
    DOSSIER_MOTEUR.mkdir(parents=True, exist_ok=True)
    deja = moteur_installe()
    if deja and deja.get("gpu") == gpu:
        progres and progres(100.0, f"moteur {deja['version']} déjà installé")
        return deja
    progres and progres(0.0, "recherche de la dernière version de llama.cpp")
    try:
        requete = urllib.request.Request(API_VERSIONS, headers={"User-Agent": "agentforge/0.1", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(requete, timeout=30, context=contexte_ssl()) as r:
            versions = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ErreurForge(f"impossible de joindre {API_VERSIONS} ({getattr(exc, 'reason', exc)})", "vérifiez la connexion internet ; le moteur se télécharge une seule fois") from exc
    if isinstance(versions, dict):
        versions = [versions]
    # La version marquée « latest » peut être une balise nocturne sans binaires (un seul nightly-tag.txt) :
    # on parcourt les versions récentes et on prend la première qui a une archive pour cette plateforme.
    version, nom, assets = None, None, {}
    vues: list[str] = []
    for candidate in versions:
        if candidate.get("draft"):
            continue
        assets = {a["name"]: a["browser_download_url"] for a in candidate.get("assets", [])}
        vues.extend(assets)
        try:
            nom = choisir_asset(list(assets), gpu)
            version = candidate
            break
        except ErreurForge:
            continue
    if version is None or nom is None:
        raise ErreurForge(
            f"aucune archive llama.cpp pour {'/'.join(plateforme())} (gpu={gpu}) dans les {len(versions)} dernières versions",
            "vérifiez https://github.com/ggml-org/llama.cpp/releases ; assets vus : " + ", ".join(sorted(set(vues))[:15]),
        )
    archive = DOSSIER_MOTEUR / nom
    if not archive.exists():
        resultat = telecharger(assets[nom], archive, progres=lambda p, e: progres and progres(p * 0.9, f"téléchargement du moteur — {e}"), annulee=annulee)
        if resultat != archive:
            raise ErreurForge("téléchargement du moteur interrompu", "relancez : il reprendra où il s'est arrêté")
    progres and progres(92.0, "extraction")
    dossier_version = DOSSIER_MOTEUR / archive.stem
    with zipfile.ZipFile(archive) as z:
        z.extractall(dossier_version)
    executable = _trouver_executable(dossier_version)
    if not executable:
        raise ErreurForge(f"llama-server introuvable dans {archive.name}", "l'archive a peut-être changé de structure ; supprimez moteur/ et réessayez")
    if plateforme()[0] != "windows":
        for p in executable.parent.iterdir():
            if p.is_file() and not p.suffix:
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    info = {"version": version.get("tag_name", "?"), "archive": nom, "executable": str(executable), "gpu": gpu}
    ecrire_json(FICHIER_MOTEUR, info, sauvegarder=False)
    archive.unlink(missing_ok=True)
    progres and progres(100.0, f"moteur {info['version']} prêt")
    return info


# --- modèles ---------------------------------------------------------------------------

def modeles_installes() -> list[dict]:
    DOSSIER_MODELES.mkdir(parents=True, exist_ok=True)
    return [{"nom": p.stem, "fichier": str(p), "taille_go": round(p.stat().st_size / 1e9, 2)} for p in sorted(DOSSIER_MODELES.glob("*.gguf"))]


def telecharger_modele(nom: str, progres=None, annulee=None, url: str | None = None) -> Path:
    url = url or CATALOGUE.get(nom, {}).get("url")
    if not url:
        raise ErreurForge(f"modèle inconnu : {nom}", "choisissez dans le catalogue ou donnez l'URL d'un fichier .gguf")
    cible = DOSSIER_MODELES / f"{nom}.gguf"
    if cible.exists():
        progres and progres(100.0, "déjà téléchargé")
        return cible
    resultat = telecharger(url, cible, progres=lambda p, e: progres and progres(p, f"téléchargement — {e}"), annulee=annulee)
    if resultat != cible:
        raise ErreurForge("téléchargement du modèle interrompu", "relancez : il reprendra où il s'est arrêté")
    return cible


# --- serveur -----------------------------------------------------------------------------

def _url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def serveur_en_marche() -> dict | None:
    """Le serveur lancé précédemment répond-il encore ?"""
    if not FICHIER_SERVEUR.exists():
        return None
    try:
        info = lire_json(FICHIER_SERVEUR)
        with urllib.request.urlopen(_url(info["port"]) + "/health", timeout=2) as r:
            if r.status == 200:
                return info
    except Exception:
        pass
    return None


def demarrer_serveur(modele: str, gpu: bool, port: int | None = None, contexte: int = 4096, attente_s: float = 120.0) -> dict:
    """Lance le moteur ; si la variante GPU ne démarre pas, réinstalle la variante CPU et réessaie une fois."""
    try:
        return _demarrer_serveur(modele, gpu, port, contexte, attente_s)
    except ErreurForge as exc:
        info = moteur_installe() or {}
        if gpu and info.get("gpu"):
            journal_precedent = exc.texte()
            installer_moteur(False)
            resultat = _demarrer_serveur(modele, False, port, contexte, attente_s)
            resultat["repli_cpu"] = journal_precedent
            return resultat
        raise


def _demarrer_serveur(modele: str, gpu: bool, port: int | None, contexte: int, attente_s: float) -> dict:
    port = port or PORT_PAR_DEFAUT
    en_marche = serveur_en_marche()
    if en_marche and en_marche.get("modele") == modele and en_marche.get("gpu") == gpu:
        return en_marche
    if en_marche:
        arreter_serveur()
    info_moteur = moteur_installe()
    if not info_moteur:
        raise ErreurForge("le moteur n'est pas installé", "cliquez « Installer le moteur » sur la page de la brique")
    fichier = DOSSIER_MODELES / f"{modele}.gguf"
    if not fichier.exists():
        raise ErreurForge(f"modèle {modele} absent de {DOSSIER_MODELES}", "téléchargez-le depuis la page de la brique")
    commande = [info_moteur["executable"], "-m", str(fichier), "--host", "127.0.0.1", "--port", str(port), "-c", str(contexte), "-ngl", "99" if gpu else "0", "--log-disable"]
    journal = open(FICHIER_JOURNAL, "ab")
    options: dict = {"stdout": journal, "stderr": journal, "stdin": subprocess.DEVNULL, "cwd": str(Path(info_moteur["executable"]).parent)}
    if sys.platform.startswith("win"):
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    else:
        options["start_new_session"] = True
    try:
        processus = subprocess.Popen(commande, **options)
    except OSError as exc:
        raise ErreurForge(f"impossible de lancer le moteur : {exc}", "supprimez le dossier moteur/ : il sera réinstallé automatiquement") from exc
    info = {"pid": processus.pid, "port": port, "modele": modele, "gpu": gpu, "commande": commande}
    ecrire_json(FICHIER_SERVEUR, info, sauvegarder=False)
    debut = time.time()
    while time.time() - debut < attente_s:
        if processus.poll() is not None:
            raise ErreurForge(
                f"le moteur s'est arrêté (code {processus.returncode}) au démarrage",
                "mémoire insuffisante pour ce modèle ? essayez un modèle plus petit, ou décochez le GPU ; journal : " + str(FICHIER_JOURNAL),
                _fin_journal(),
            )
        try:
            with urllib.request.urlopen(_url(port) + "/health", timeout=2) as r:
                if r.status == 200:
                    return info
        except Exception:
            time.sleep(0.5)
    raise ErreurForge(f"le moteur ne répond pas après {attente_s:.0f} s", "modèle trop lourd pour la machine ? journal : " + str(FICHIER_JOURNAL), _fin_journal())


def _fin_journal() -> list[str]:
    try:
        return FICHIER_JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]
    except OSError:
        return []


def arreter_serveur() -> bool:
    if not FICHIER_SERVEUR.exists():
        return False
    try:
        info = lire_json(FICHIER_SERVEUR)
        pid = int(info["pid"])
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass
    FICHIER_SERVEUR.unlink(missing_ok=True)
    return True


# --- préparation automatique -------------------------------------------------------------

def preparer(gpu: bool, progres=None, annulee=None, modele: str = MODELE_PAR_DEFAUT) -> dict:
    """Tout ce qu'il faut pour être prêt, sans rien demander : moteur puis modèle par défaut. Idempotent."""
    if not moteur_installe():
        installer_moteur(gpu, progres=lambda p, e: progres and progres(p * 0.1, e), annulee=annulee)
        if annulee and annulee():
            raise ErreurForge("préparation interrompue", "elle reprendra au prochain démarrage")
    else:
        progres and progres(10.0, "moteur déjà installé")
    if not (DOSSIER_MODELES / f"{modele}.gguf").exists():
        telecharger_modele(modele, progres=lambda p, e: progres and progres(10 + p * 0.9, f"modèle {modele} — {e}"), annulee=annulee)
    progres and progres(100.0, "prêt")
    return {"moteur": moteur_installe(), "modele": modele}

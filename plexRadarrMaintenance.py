#!/usr/bin/env python3
# coding: utf-8
#
# Maintenance de la bibliothèque Plex / Radarr :
#   - purge des vieux films jamais visionnés (Plex = source de vérité)
#   - vidage de la corbeille Plex
#   - alerte sur les films sans correspondance Plex/TMDB
#
# Toute suppression passe par Radarr (unmonitor + suppression du fichier) :
# les films sans fiche Radarr sont seulement signalés, jamais supprimés.
# Voir « --help » pour les exemples et les crontabs.

import argparse
import csv
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, '.env')

PLEX_TV = 'https://plex.tv/api/v2'
APP_PRODUCT = 'Plex Radarr Maintenance'
FORUM_URL = 'https://forums.plex.tv/t/authenticating-with-plex/609370'

DEFAULT_DAYS = 1825
PLAN_CSV = os.path.join(BASE_DIR, 'cleanup_plan.csv')

REQUIRED_PLEX = ('PLEX_URL', 'PLEX_TOKEN')
REQUIRED_RADARR = ('RADARR_URL', 'RADARR_API_KEY')

SCRIPT_NAME = os.path.basename(__file__)

HELP_EPILOG = f"""\
Configuration (.env, créé par --init) :
  PLEX_URL / PLEX_TOKEN              serveur Plex et token (obligatoire)
  PLEX_MOVIE_SECTION_ID              id de la bibliothèque Films
  RADARR_URL / RADARR_API_KEY        serveur Radarr et clé API
  DAYS_THRESHOLD                     seuil d'ancienneté en jours (défaut {DEFAULT_DAYS})
  PUSHOVER_TOKEN / PUSHOVER_USER     notification via l'API Pushover
  NOTIFY_COMMAND                     notification via un script (titre + message)

Exemples :
  ./{SCRIPT_NAME} --init
      Configuration interactive (Plex, Radarr, Pushover).

  ./{SCRIPT_NAME}
      Dry-run : liste les films éligibles et écrit cleanup_plan.csv, sans rien supprimer.

  ./{SCRIPT_NAME} --days 730
      Dry-run avec un seuil de 2 ans au lieu de 5.

  ./{SCRIPT_NAME} --apply
      Purge réelle : unmonitor Radarr puis suppression des fichiers.

  ./{SCRIPT_NAME} --apply --refresh-plex --empty-trash
      Purge complète : suppression, scan Plex puis vidage de la corbeille.

  ./{SCRIPT_NAME} --empty-trash
      Vide uniquement la corbeille Plex (aucun scan, aucune suppression).

  ./{SCRIPT_NAME} --refresh-plex --empty-trash
      Scan Plex puis vidage de la corbeille (retire les items orphelins).

  ./{SCRIPT_NAME} --check-unmatched
      Alerte Pushover listant les films sans correspondance Plex/TMDB
      (aucune notification s'il n'y en a pas).

Crontabs (chemins à adapter) :
  # Purge hebdomadaire, dimanche 4h, avec scan et corbeille
  0 4 * * 0  /opt/script/{SCRIPT_NAME} --apply --refresh-plex --empty-trash

  # Vidage de la corbeille Plex tous les lundis 4h
  0 4 * * 1  /opt/script/{SCRIPT_NAME} --empty-trash

  # Contrôle quotidien des films sans correspondance, 9h
  0 9 * * *  /opt/script/{SCRIPT_NAME} --check-unmatched

  # Dry-run mensuel pour vérifier les prochains candidats, le 1er à 8h
  0 8 1 * *  /opt/script/{SCRIPT_NAME}
"""


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

def load_env(path=ENV_FILE):
    values = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def save_env(updates, path=ENV_FILE):
    lines = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            lines = fh.read().splitlines()

    written = set()
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        output.append(line)

    for key, value in updates.items():
        if key not in written:
            output.append(f"{key}={value}")

    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(output).rstrip('\n') + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def normalize_url(url):
    url = (url or '').strip().rstrip('/')
    if url and not re.match(r'^https?://', url):
        url = 'http://' + url
    return url


# --------------------------------------------------------------------------
# Plex
# --------------------------------------------------------------------------

def plex_headers(token=None, client_id=None, product=APP_PRODUCT):
    headers = {'Accept': 'application/json'}
    if client_id:
        headers['X-Plex-Client-Identifier'] = client_id
    if product:
        headers['X-Plex-Product'] = product
    if token:
        headers['X-Plex-Token'] = token
    return headers


def plex_sections(url, token):
    response = requests.get(f"{url}/library/sections", headers=plex_headers(token), timeout=30)
    response.raise_for_status()
    return response.json().get('MediaContainer', {}).get('Directory', [])


def plex_obtain_token(client_id, product=APP_PRODUCT):
    response = requests.post(
        f"{PLEX_TV}/pins",
        headers={'Accept': 'application/json', 'X-Plex-Product': product,
                 'X-Plex-Client-Identifier': client_id},
        data={'strong': 'true', 'X-Plex-Product': product, 'X-Plex-Client-Identifier': client_id},
        timeout=30,
    )
    response.raise_for_status()
    pin = response.json()

    params = {
        'clientID': client_id,
        'code': pin['code'],
        'context[device][product]': product,
        'forwardUrl': 'https://app.plex.tv/desktop',
    }
    auth_url = 'https://app.plex.tv/auth#?' + urlencode(params)

    print("\nOuvre cette URL dans ton navigateur et connecte-toi à Plex :")
    print(f"\n  {auth_url}\n")
    print("En attente de l'autorisation (Ctrl+C pour annuler)...")

    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(2)
        poll = requests.get(
            f"{PLEX_TV}/pins/{pin['id']}",
            params={'code': pin['code']},
            headers={'Accept': 'application/json', 'X-Plex-Client-Identifier': client_id,
                     'X-Plex-Product': product},
            timeout=30,
        )
        token = poll.json().get('authToken')
        if token:
            return token
    return None


def plex_fetch_movies(url, token, section_id, page_size=200):
    items = []
    start = 0
    while True:
        headers = plex_headers(token)
        headers['X-Plex-Container-Start'] = str(start)
        headers['X-Plex-Container-Size'] = str(page_size)
        response = requests.get(
            f"{url}/library/sections/{section_id}/all",
            params={'includeGuids': 1, 'type': 1},
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()
        container = response.json().get('MediaContainer', {})
        batch = container.get('Metadata') or container.get('Video') or []
        items.extend(batch)
        total = int(container.get('totalSize') or container.get('size') or 0)
        start += page_size
        if len(batch) < page_size or (total and start >= total):
            break
    return items


def plex_refresh_section(url, token, section_id):
    requests.get(
        f"{url}/library/sections/{section_id}/refresh",
        headers=plex_headers(token),
        timeout=60,
    ).raise_for_status()


def plex_wait_for_scan(url, token, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            activities = requests.get(
                f"{url}/activities",
                headers=plex_headers(token),
                timeout=30,
            ).json().get('MediaContainer', {}).get('Activity', [])
        except requests.RequestException:
            return 'inconnu'
        running = [a for a in activities if 'update.section' in (a.get('type') or '')]
        if not running:
            return 'termine'
        progress = running[0].get('progress')
        print(f"  scan en cours... {progress}%", end='\r')
        time.sleep(5)
    return 'timeout'


def plex_count(url, token, section_id):
    headers = plex_headers(token)
    headers['X-Plex-Container-Start'] = '0'
    headers['X-Plex-Container-Size'] = '0'
    response = requests.get(
        f"{url}/library/sections/{section_id}/all",
        params={'type': 1},
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    container = response.json().get('MediaContainer', {})
    return int(container.get('totalSize') or container.get('size') or 0)


def plex_empty_trash(url, token, section_id):
    requests.put(
        f"{url}/library/sections/{section_id}/emptyTrash",
        headers=plex_headers(token),
        timeout=120,
    ).raise_for_status()


def send_notification(title, message, priority=0):
    """
    Envoie une notification Pushover : API directe si PUSHOVER_TOKEN/USER
    sont définis, sinon via le script NOTIFY_COMMAND.
    """
    token = os.environ.get('PUSHOVER_TOKEN')
    user = os.environ.get('PUSHOVER_USER')
    if token and user:
        try:
            response = requests.post(
                'https://api.pushover.net/1/messages.json',
                data={'token': token, 'user': user, 'title': title,
                      'message': message, 'priority': priority},
                timeout=30,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            print(f"Notification Pushover en échec : {exc}")
            return False

    command = os.environ.get('NOTIFY_COMMAND')
    if command and os.path.exists(command):
        try:
            subprocess.run([command, title, message], check=False)
            return True
        except OSError as exc:
            print(f"Notification via script en échec : {exc}")
            return False

    return False


def send_batched(title, summary, details=(), limit=1000):
    """
    Envoie une ou plusieurs notifications : le résumé puis le détail, découpé
    en lots pour respecter la limite de caractères de Pushover.
    """
    header = "\n".join(summary)
    batches = []
    current = header
    for line in details:
        if len(current) + len(line) + 1 > limit:
            batches.append(current)
            current = f"(suite)\n{line}"
        else:
            current = f"{current}\n{line}"
    batches.append(current)

    total = len(batches)
    sent = 0
    for index, message in enumerate(batches, 1):
        notif_title = title if total == 1 else f"{title} ({index}/{total})"
        if send_notification(notif_title, message):
            sent += 1
    return sent, total


def do_refresh(wait=True):
    plex_url = os.environ['PLEX_URL'].rstrip('/')
    section_id = os.environ['PLEX_MOVIE_SECTION_ID']
    print(f"Demande de scan Plex (section {section_id})...")
    plex_refresh_section(plex_url, os.environ['PLEX_TOKEN'], section_id)
    if wait:
        status = plex_wait_for_scan(plex_url, os.environ['PLEX_TOKEN'])
        print(f"\nScan Plex : {status}\n")


def do_empty_trash():
    plex_url = os.environ['PLEX_URL'].rstrip('/')
    section_id = os.environ['PLEX_MOVIE_SECTION_ID']
    before = plex_count(plex_url, os.environ['PLEX_TOKEN'], section_id)
    plex_empty_trash(plex_url, os.environ['PLEX_TOKEN'], section_id)
    time.sleep(2)
    after = plex_count(plex_url, os.environ['PLEX_TOKEN'], section_id)
    print(f"Corbeille vidée (section {section_id}) : {before} -> {after} film(s) "
          f"({before - after} entrée(s) orpheline(s) retirée(s))")
    return before, after


def plex_fetch_unmatched(url, token, section_id, page_size=200):
    items = []
    start = 0
    while True:
        headers = plex_headers(token)
        headers['X-Plex-Container-Start'] = str(start)
        headers['X-Plex-Container-Size'] = str(page_size)
        response = requests.get(
            f"{url}/library/sections/{section_id}/all",
            params={'type': 1, 'unmatched': 1},
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()
        container = response.json().get('MediaContainer', {})
        batch = container.get('Metadata') or container.get('Video') or []
        items.extend(batch)
        total = int(container.get('totalSize') or container.get('size') or 0)
        start += page_size
        if len(batch) < page_size or (total and start >= total):
            break
    return items


def parse_plex_movie(item):
    tmdb_id = imdb_id = None
    for guid in item.get('Guid') or []:
        value = guid.get('id') if isinstance(guid, dict) else guid
        if not value:
            continue
        if value.startswith('tmdb://'):
            tmdb_id = value.replace('tmdb://', '')
        elif value.startswith('imdb://'):
            imdb_id = value.replace('imdb://', '')

    files = []
    for media in item.get('Media') or []:
        for part in media.get('Part') or []:
            files.append({
                'file': part.get('file'),
                'size': int(part.get('size') or 0),
                'exists': part.get('exists'),
            })

    added_at = item.get('addedAt')
    return {
        'rating_key': item.get('ratingKey'),
        'title': item.get('title'),
        'year': item.get('year'),
        'added_at': datetime.fromtimestamp(int(added_at)) if added_at else None,
        'view_count': int(item.get('viewCount') or 0),
        'last_viewed': item.get('lastViewedAt'),
        'tmdb_id': tmdb_id,
        'imdb_id': imdb_id,
        'files': files,
    }


# --------------------------------------------------------------------------
# Radarr
# --------------------------------------------------------------------------

def radarr_headers():
    return {'X-Api-Key': os.environ['RADARR_API_KEY']}


def get_radarr_movies():
    url = os.environ['RADARR_URL'].rstrip('/')
    response = requests.get(f"{url}/api/v3/movie", headers=radarr_headers(), timeout=120)
    response.raise_for_status()
    return response.json()


def norm(text):
    if not text:
        return ''
    text = unicodedata.normalize('NFKD', str(text))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def build_radarr_indexes(movies):
    by_tmdb, by_imdb, by_key, by_title = {}, {}, {}, {}
    for movie in movies:
        if movie.get('tmdbId'):
            by_tmdb[str(movie['tmdbId'])] = movie
        if movie.get('imdbId'):
            by_imdb[movie['imdbId']] = movie
        titles = [movie.get('title'), movie.get('originalTitle')]
        for alt in movie.get('alternateTitles') or []:
            titles.append(alt.get('title'))
        for title in titles:
            if not title:
                continue
            by_key.setdefault(f"{norm(title)}|{movie.get('year')}", []).append(movie)
            by_title.setdefault(norm(title), []).append(movie)
    return by_tmdb, by_imdb, by_key, by_title


def match_radarr(movie, indexes):
    by_tmdb, by_imdb, by_key, by_title = indexes
    if movie['tmdb_id'] and movie['tmdb_id'] in by_tmdb:
        return [by_tmdb[movie['tmdb_id']]], 'tmdbId'
    if movie['imdb_id'] and movie['imdb_id'] in by_imdb:
        return [by_imdb[movie['imdb_id']]], 'imdbId'
    key = f"{norm(movie['title'])}|{movie['year']}"
    if key in by_key:
        return by_key[key], 'titre+annee'
    if norm(movie['title']) in by_title:
        return by_title[norm(movie['title'])], 'titre-seul'
    return [], None


def radarr_unmonitor(movie_ids):
    if not movie_ids:
        return
    url = os.environ['RADARR_URL'].rstrip('/')
    response = requests.put(
        f"{url}/api/v3/movie/editor",
        headers=radarr_headers(),
        json={'movieIds': sorted(set(movie_ids)), 'monitored': False},
        timeout=120,
    )
    response.raise_for_status()


def radarr_delete_movie_file(movie_file_id):
    url = os.environ['RADARR_URL'].rstrip('/')
    response = requests.delete(
        f"{url}/api/v3/moviefile/{movie_file_id}",
        headers=radarr_headers(),
        timeout=120,
    )
    response.raise_for_status()


# --------------------------------------------------------------------------
# --init
# --------------------------------------------------------------------------

def cmd_init(args):
    print("Configuration de Plex et Radarr (fichier .env)\n")
    print("Documentation de référence :")
    print(f"  {FORUM_URL}\n")

    current = load_env()
    plex_url = normalize_url(args.plex_url or current.get('PLEX_URL') or input("URL du serveur Plex [http://localhost:32400] : ").strip() or 'http://localhost:32400')

    token = current.get('PLEX_TOKEN')
    client_id = current.get('PLEX_CLIENT_IDENTIFIER') or str(uuid.uuid4())

    if token:
        print(f"\nUn token Plex existe déjà ({token[:4]}...) — Entrée pour le conserver.")
        if input("Nouveau token ? [o/N] ").strip().lower() in ('o', 'oui', 'y'):
            token = None

    if not token:
        print("\nComment récupérer le token ?")
        print("  1. Connexion automatique via PIN Plex (recommandé)")
        print("  2. Coller un X-Plex-Token existant")
        choice = input("Choix [1] : ").strip() or '1'
        if choice == '1':
            token = plex_obtain_token(client_id)
            if not token:
                print("Délai dépassé ou autorisation refusée.")
                sys.exit(1)
            print("Token obtenu.")
        else:
            token = input("X-Plex-Token : ").strip()

    try:
        sections = plex_sections(plex_url, token)
    except requests.RequestException as exc:
        print(f"\nImpossible de joindre Plex ({plex_url}) : {exc}")
        sys.exit(1)

    movie_sections = [s for s in sections if s.get('type') == 'movie']
    if not movie_sections:
        print("Aucune bibliothèque de films trouvée sur ce serveur.")
        sys.exit(1)

    print("\nBibliothèques de films :")
    for i, section in enumerate(movie_sections, 1):
        print(f"  {i}. {section.get('title')} (id={section.get('key')})")
    answer = input(f"Numéro à utiliser [{1}] : ").strip() or '1'
    section = movie_sections[int(answer) - 1]

    radarr_url = normalize_url(current.get('RADARR_URL') or input("\nURL Radarr [http://localhost:7878] : ").strip() or 'http://localhost:7878')
    radarr_key = current.get('RADARR_API_KEY') or input("Clé API Radarr : ").strip()

    updates = {
        'PLEX_URL': plex_url,
        'PLEX_TOKEN': token,
        'PLEX_CLIENT_IDENTIFIER': client_id,
        'PLEX_MOVIE_SECTION_ID': str(section.get('key')),
        'RADARR_URL': radarr_url,
        'RADARR_API_KEY': radarr_key,
    }

    print("\nNotification Pushover (optionnel)")
    print("  1. Script existant (ex: /opt/script/sendPushoverNotif.sh)")
    print("  2. API Pushover (token application + user key)")
    print("  3. Aucune")
    notify_choice = input("Choix [3] : ").strip() or '3'
    if notify_choice == '1':
        path = input(f"Chemin du script [{current.get('NOTIFY_COMMAND') or '/opt/script/sendPushoverNotif.sh'}] : ").strip()
        updates['NOTIFY_COMMAND'] = path or current.get('NOTIFY_COMMAND') or '/opt/script/sendPushoverNotif.sh'
    elif notify_choice == '2':
        updates['PUSHOVER_TOKEN'] = current.get('PUSHOVER_TOKEN') or input("PUSHOVER_TOKEN (application) : ").strip()
        updates['PUSHOVER_USER'] = current.get('PUSHOVER_USER') or input("PUSHOVER_USER (user key) : ").strip()

    save_env(updates)
    os.environ.update({k: str(v) for k, v in updates.items()})
    print(f"\nÉcrit dans {ENV_FILE} (section « {section.get('title')} »).")
    print(f"Vérifie avec : ./{SCRIPT_NAME}")
    if input("Envoyer une notification de test ? [o/N] ").strip().lower() in ('o', 'oui', 'y'):
        if send_notification("Plex cleanup", "Notification de test"):
            print("Notification envoyée.")
        else:
            print("Aucune notification envoyée (configuration absente ou en échec).")


# --------------------------------------------------------------------------
# purge
# --------------------------------------------------------------------------

def build_plan(days_threshold):
    plex_url = os.environ['PLEX_URL'].rstrip('/')
    section_id = os.environ['PLEX_MOVIE_SECTION_ID']
    cutoff = datetime.now() - timedelta(days=days_threshold)

    plex_movies = plex_fetch_movies(plex_url, os.environ['PLEX_TOKEN'], section_id)
    print(f"{len(plex_movies)} film(s) dans Plex (section {section_id}).")

    radarr_movies = get_radarr_movies()
    indexes = build_radarr_indexes(radarr_movies)
    print(f"{len(radarr_movies)} film(s) récupérés depuis Radarr.\n")

    candidates = []
    for item in plex_movies:
        movie = parse_plex_movie(item)
        if movie['view_count'] > 0:
            continue
        if not movie['added_at'] or movie['added_at'] >= cutoff:
            continue
        if not movie['files']:
            continue

        matches, match_type = match_radarr(movie, indexes)
        unique = {m['id']: m for m in matches}
        matches = list(unique.values())
        with_file = [m for m in matches if m.get('hasFile') and m.get('movieFileId')]

        candidates.append({
            'movie': movie,
            'match_type': match_type,
            'radarr_matches': matches,
            'radarr_with_file': with_file,
            'size': sum(m.get('sizeOnDisk') or 0 for m in matches),
        })
    return candidates


def cmd_check_unmatched(args):
    plex_url = os.environ['PLEX_URL'].rstrip('/')
    section_id = os.environ['PLEX_MOVIE_SECTION_ID']
    items = plex_fetch_unmatched(plex_url, os.environ['PLEX_TOKEN'], section_id)

    print(f"Films sans correspondance (Plex « Unmatched ») : {len(items)}")
    if not items:
        print("Aucun film sans correspondance — pas de notification.")
        return

    for item in items[:30]:
        print(f"  - {item.get('title')} ({item.get('year')})")

    details = [f"- {item.get('title')} ({item.get('year')})" for item in items]
    sent, total = send_batched(
        f"Plex cleanup - {len(items)} sans correspondance",
        [f"{len(items)} film(s) sans correspondance (métadonnées TMDB manquantes)"],
        details,
    )
    if sent:
        print(f"\nNotification(s) envoyée(s) : {sent}/{total}")


def cmd_purge(args):
    if args.refresh_plex and not args.apply:
        do_refresh()

    days = args.days or int(os.environ.get('DAYS_THRESHOLD', DEFAULT_DAYS))
    candidates = build_plan(days)

    total_size = sum(c['size'] for c in candidates)
    radarr_hits = sum(1 for c in candidates if c['radarr_matches'])
    print(f"Seuil : non visionné et ajouté depuis plus de {days} jours ({days / 365.25:.1f} ans)")
    print(f"Candidats                    : {len(candidates)}")
    print(f"  trouvés dans Radarr        : {radarr_hits}")
    print(f"  présents sans fiche Radarr : {len(candidates) - radarr_hits}")
    print(f"Espace concerné (Radarr)     : {total_size / 1024**3:.2f} Go")

    with open(PLAN_CSV, 'w', encoding='utf-8', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(['titre', 'annee', 'ajoute_le', 'match_type', 'radarr_ids',
                         'radarr_file_ids', 'taille_go', 'chemin'])
        for candidate in candidates:
            movie = candidate['movie']
            writer.writerow([
                movie['title'], movie['year'],
                movie['added_at'].strftime('%Y-%m-%d') if movie['added_at'] else '',
                candidate['match_type'] or '',
                '|'.join(str(m['id']) for m in candidate['radarr_matches']),
                '|'.join(str(m['movieFileId']) for m in candidate['radarr_with_file']),
                f"{(candidate['size'] or 0) / 1024**3:.2f}",
                (movie['files'][0]['file'] if movie['files'] else ''),
            ])
    print(f"\nPlan écrit dans {PLAN_CSV}")

    no_radarr_file = [c for c in candidates if not c['radarr_with_file']]
    if no_radarr_file:
        print(f"\n{len(no_radarr_file)} film(s) sans fichier géré par Radarr — ignorés "
              f"(à traiter manuellement) :")
        for c in no_radarr_file[:20]:
            print(f"  - {c['movie']['title']} ({c['movie']['year']}) : "
                  f"{c['movie']['files'][0]['file'] if c['movie']['files'] else '?'}")
        print("  (chemins complets dans cleanup_plan.csv)")

    if not args.apply:
        print("\nDRY-RUN : aucune modification. Relancer avec --apply pour exécuter.")
        return

    print("\nApplication...")

    all_movie_ids = [m['id'] for c in candidates for m in c['radarr_matches']]
    radarr_unmonitor(all_movie_ids)
    print(f"  {len(set(all_movie_ids))} fiche(s) Radarr en unmonitored")

    deleted, deleted_bytes, failures = 0, 0, []
    for candidate in candidates:
        movie = candidate['movie']
        for radarr_movie in candidate['radarr_with_file']:
            try:
                radarr_delete_movie_file(radarr_movie['movieFileId'])
                deleted += 1
                deleted_bytes += radarr_movie.get('sizeOnDisk') or 0
            except requests.RequestException as exc:
                failures.append((movie['title'], str(exc)))
        time.sleep(0.1)

    print(f"\nFichiers supprimés via Radarr : {deleted}  ({deleted_bytes / 1024**3:.2f} Go)")
    print(f"Échecs                        : {len(failures)}")
    for title, error in failures[:20]:
        print(f"  - {title} : {error}")
    print(f"Non gérés (ignorés)           : {len(no_radarr_file)}")

    if args.refresh_plex:
        do_refresh(wait=False)
        print("Scan de la bibliothèque Plex demandé.")

    trash = None
    if args.empty_trash:
        trash = do_empty_trash()

    summary = [f"{deleted} film(s) supprimé(s) ({deleted_bytes / 1024**3:.1f} Go)",
               f"{len(candidates)} candidat(s) analysé(s)"]
    if trash:
        summary.append(f"Corbeille : {trash[0]} -> {trash[1]} ({trash[0] - trash[1]} retirée(s))")
    if no_radarr_file:
        summary.append(f"{len(no_radarr_file)} sans fiche Radarr")
    if failures:
        summary.append(f"{len(failures)} échec(s)")

    details = []
    if no_radarr_file:
        details.append("Sans fiche Radarr :")
        details += [f"- {c['movie']['title']} ({c['movie']['year']})" for c in no_radarr_file]
    if failures:
        details.append("Échecs :")
        details += [f"- {title} : {error[:120]}" for title, error in failures]

    sent, total = send_batched("Plex cleanup", summary, details)
    if sent:
        print(f"Notification(s) envoyée(s) : {sent}/{total}")


# --------------------------------------------------------------------------

def main():
    load_env()

    parser = argparse.ArgumentParser(
        description="Maintenance de la bibliothèque Plex / Radarr : purge des vieux films "
                    "jamais visionnés, vidage de la corbeille Plex, alerte sur les films "
                    "sans correspondance.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--init', action='store_true', help="Configure le fichier .env")
    parser.add_argument('--apply', action='store_true', help="Exécute réellement les suppressions")
    parser.add_argument('--days', type=int, help=f"Seuil d'ancienneté en jours (défaut {DEFAULT_DAYS})")
    parser.add_argument('--plex-url', help="URL Plex à utiliser pour --init")
    parser.add_argument('--refresh-plex', action='store_true', help="Demande un scan Plex après suppression")
    parser.add_argument('--empty-trash', action='store_true', help="Vide la corbeille Plex (après --apply, ou seul)")
    parser.add_argument('--check-unmatched', action='store_true',
                        help="Notifie les films sans correspondance Plex/TMDB (rien si 0)")
    args = parser.parse_args()

    if args.init:
        cmd_init(args)
        return

    plex_only = args.check_unmatched or (args.empty_trash and not args.apply)
    required = REQUIRED_PLEX if plex_only else REQUIRED_PLEX + REQUIRED_RADARR
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Configuration manquante : {', '.join(missing)}")
        print(f"Lance d'abord : ./{SCRIPT_NAME} --init")
        sys.exit(1)

    if args.check_unmatched:
        cmd_check_unmatched(args)
        return

    if args.empty_trash and not args.apply:
        if args.refresh_plex:
            do_refresh()
        before, after = do_empty_trash()
        sent, total = send_batched(
            "Plex cleanup",
            [f"Corbeille vidée : {before} -> {after} ({before - after} entrée(s) retirée(s))"],
        )
        if sent:
            print(f"Notification(s) envoyée(s) : {sent}/{total}")
        return

    cmd_purge(args)


if __name__ == "__main__":
    main()

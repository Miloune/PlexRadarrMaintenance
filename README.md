# PlexRadarrMaintenance

Maintenance de la bibliothèque **Plex / Radarr** :

- **Purge des vieux films jamais visionnés** — Plex est la source de vérité (`addedAt`, `viewCount`),
  toute suppression passe par **Radarr** (`unmonitor` puis suppression du fichier).
- **Vidage de la corbeille Plex** — retire les items dont le fichier n'existe plus.
- **Alerte « sans correspondance »** — notifie les films que Plex n'a pas su rattacher à TMDB.
- **Notifications Pushover** (optionnelles), avec découpage automatique en plusieurs messages.

Aucun fichier n'est supprimé directement par le script : un film sans fiche Radarr est
**seulement signalé** et listé dans `cleanup_plan.csv`.

## Prérequis

- Python 3.9 ou plus
- `python3-venv` (paquet système, pas fourni avec Python sur Debian/Ubuntu)
- Un serveur **Plex** et un `X-Plex-Token`
- Un serveur **Radarr** et sa clé API
- (optionnel) un compte **Pushover** : `PUSHOVER_TOKEN` + `PUSHOVER_USER`

### Installation

```bash
# Récupérer le projet (repo privé : SSH nécessite une clé configurée)
sudo mkdir -p /opt/script && sudo chown "$USER" /opt/script
git clone git@github.com:Miloune/PlexRadarrMaintenance.git /opt/script/PlexRadarrMaintenance
cd /opt/script/PlexRadarrMaintenance

# Debian / Ubuntu : le module venv est fourni à part
sudo apt install -y python3-venv

# Créer l'environnement virtuel et installer les dépendances
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Vérification
.venv/bin/python plexRadarrMaintenance.py --help
```

> En HTTPS, remplacer l'URL par
> `https://github.com/Miloune/PlexRadarrMaintenance.git` (token requis pour un repo privé).

> Toutes les commandes passent par `.venv/bin/python` : rien n'est installé au niveau
> global, donc aucun conflit avec les autres scripts ou paquets Python de la machine.

## Configuration

```bash
.venv/bin/python plexRadarrMaintenance.py --init
```

L'assistant demande successivement :

1. **URL du serveur Plex** (ex. `http://<ip-plex>:32400`).
2. **Le token Plex**, via la connexion automatique par PIN (une URL `app.plex.tv/auth`
   est affichée — voir la [doc d'authentification Plex](https://forums.plex.tv/t/authenticating-with-plex/609370)),
   ou en collant un token existant.
3. **La bibliothèque Films** à surveiller.
4. **URL et clé API Radarr**.
5. **Pushover** (optionnel) : via l'API (`PUSHOVER_TOKEN` / `PUSHOVER_USER`) ou via un
   script existant (`NOTIFY_COMMAND`, appelé comme `<script> "<titre>" "<message>"`).

Le fichier `.env` est créé à côté du script avec les permissions `600`. Il n'est jamais
écrasé en dehors des valeurs demandées.

## Utilisation

```bash
# Dry-run : liste les candidats et écrit cleanup_plan.csv, ne supprime rien
.venv/bin/python plexRadarrMaintenance.py

# Seuil d'ancienneté personnalisé (2 ans au lieu de 5)
.venv/bin/python plexRadarrMaintenance.py --days 730

# Purge réelle : unmonitor Radarr + suppression des fichiers
.venv/bin/python plexRadarrMaintenance.py --apply

# Purge complète : suppression, scan Plex puis vidage de la corbeille
.venv/bin/python plexRadarrMaintenance.py --apply --refresh-plex --empty-trash

# Vide uniquement la corbeille Plex
.venv/bin/python plexRadarrMaintenance.py --empty-trash

# Scan Plex puis vidage de la corbeille (retire les items orphelins)
.venv/bin/python plexRadarrMaintenance.py --refresh-plex --empty-trash

# Alerte sur les films sans correspondance Plex/TMDB (aucune notif s'il n'y en a pas)
.venv/bin/python plexRadarrMaintenance.py --check-unmatched
```

Toutes les options sont documentées dans `.venv/bin/python plexRadarrMaintenance.py --help`.

## Crontab

Utiliser le Python du venv et des chemins absolus :

```cron
# Purge hebdomadaire : dimanche 4h, avec scan Plex et vidage de corbeille
0 4 * * 0  /opt/script/PlexRadarrMaintenance/.venv/bin/python /opt/script/PlexRadarrMaintenance/plexRadarrMaintenance.py --apply --refresh-plex --empty-trash

# Vidage de la corbeille Plex : lundi 4h
0 4 * * 1  /opt/script/PlexRadarrMaintenance/.venv/bin/python /opt/script/PlexRadarrMaintenance/plexRadarrMaintenance.py --empty-trash

# Contrôle des films sans correspondance : tous les jours à 9h
0 9 * * *  /opt/script/PlexRadarrMaintenance/.venv/bin/python /opt/script/PlexRadarrMaintenance/plexRadarrMaintenance.py --check-unmatched

# Dry-run mensuel : le 1er à 8h
0 8 1 * *  /opt/script/PlexRadarrMaintenance/.venv/bin/python /opt/script/PlexRadarrMaintenance/plexRadarrMaintenance.py
```

## Variables `.env`

| Variable | Rôle |
|---|---|
| `PLEX_URL` | URL du serveur Plex |
| `PLEX_TOKEN` | token d'accès Plex (`X-Plex-Token`) |
| `PLEX_CLIENT_IDENTIFIER` | identifiant client Plex (généré par `--init`) |
| `PLEX_MOVIE_SECTION_ID` | id de la bibliothèque Films |
| `RADARR_URL` | URL du serveur Radarr |
| `RADARR_API_KEY` | clé API Radarr |
| `DAYS_THRESHOLD` | seuil d'ancienneté en jours (défaut `1825`) |
| `PUSHOVER_TOKEN` | token d'application Pushover (optionnel) |
| `PUSHOVER_USER` | user key Pushover (optionnel) |
| `NOTIFY_COMMAND` | script de notification alternatif (optionnel) |

## Fichiers générés

- `cleanup_plan.csv` — liste des candidats du dernier dry-run (titre, année, date d'ajout,
  match Radarr, chemin), réécrit à chaque exécution.
- `.env` — configuration et secrets, permissions `600`, à ne pas versionner.

## Notes

- Le script est **idempotent** : relancé après une purge, il ne propose rien tant qu'aucun
  film ne dépasse le seuil.
- Les films candidats **sans fiche Radarr** ne sont jamais supprimés : ils sont affichés et
  inclus dans la notification pour un traitement manuel.
- `--days` et `DAYS_THRESHOLD` : la CLI gagne sur la variable d'environnement.

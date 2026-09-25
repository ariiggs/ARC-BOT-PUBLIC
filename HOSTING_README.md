# Apex Scrims Manager — installation

Cette archive utilise une base SQLite persistante pour conserver les scrims,
les slots, les équipes, les statuts et les références des messages Discord.
Elle ne contient aucun token Discord.

## Installation

1. Décompresser l'archive sur l'hébergement.
2. Installer les dépendances :

```bash
python -m pip install -r requirements.txt
```

3. Ajouter une variable secrète nommée exactement `DISCORD_TOKEN`.
4. Définir la commande de démarrage :

```bash
python main.py
```

Ne place jamais le token directement dans `main.py`.

## Configuration Discord

Activer dans le portail développeur Discord :

- Message Content Intent
- Server Members Intent

Le bot doit avoir, dans les salons concernés :

- View Channel
- Send Messages
- Read Message History
- Manage Messages
- Manage Roles

Le rôle du bot doit être placé au-dessus des rôles qu'il doit gérer.

## Premier démarrage

1. Depuis un contexte accessible au propriétaire du bot, autoriser le serveur
   avant de l'inviter avec `!auth add <Guild_ID> <Days|unlimited>`. La whitelist est vide
   par défaut. La valeur `0` signifie que l'accès n'expire jamais.
2. Le propriétaire du serveur lance `!set @Staff`.
3. Les membres ayant le rôle Staff global lancent `!setup`.
4. Créer un scrim et sélectionner son rôle Manager et ses salons.
5. Créer un scrim et sélectionner :
   - le nom ;
   - le premier slot et le nombre de slots ;
   - le salon public ;
   - le salon staff mirror ;
   - les salons logs et historique ;
   - le salon public dédié aux commandes `!cap`.
6. Utiliser **Show/refresh slots** ou `!slots` pour publier le board.

## Connexion à Interactive

Le bridge Interactive s'exécute dans le même processus que le bot. Il est
désactivé par défaut et ne démarre que lorsque ces variables sont configurées
sur l'hébergement :

```text
ARC_BETA_WEB_BRIDGE_PORT=<port TCP public exposé par l'hébergement>
ARC_BETA_WEB_BRIDGE_SECRET=<secret partagé avec l'API Replit>
INTERACTIVE_URL=<URL publique de la page Interactive>
```

`ARC_BETA_WEB_BRIDGE_SECRET` doit rester une variable secrète et ne doit
jamais être commitée dans GitHub ou ajoutée à `main.py`. Le bot doit être
redémarré après l'ajout ou le changement de ces variables.

Dans l'API Replit, configurer les mêmes valeurs de connexion :

```text
BETA_WEB_BRIDGE_URL=https://<domaine-bot-hosting>:<port>
BETA_WEB_BRIDGE_SECRET=<la même valeur secrète>
```

Après le redémarrage du bot et de l'API, un membre Staff peut utiliser
`!interactive` dans Discord. Le bouton ouvre la page et les changements
effectués dans celle-ci sont validés et enregistrés par le processus ARC Beta
avant que les boards Discord soient rafraîchis.

Les commandes `!admin` sont masquées et réservées au propriétaire du bot :

```text
!admin add @User
!admin remove @User
!admin list
```

Elles gèrent les utilisateurs autorisés à administrer la whitelist. Les
commandes `!auth` sont masquées et accessibles au propriétaire du bot ainsi
qu'aux utilisateurs ajoutés avec `!admin add` :

```text
!auth add <Guild_ID> <Days|unlimited>
!auth remove <Guild_ID>
!auth list
```

Un utilisateur non autorisé reçoit un message d'accès refusé, puis son
invocation est supprimée. Les commandes préfixées Discord ne supportent pas
les réponses éphémères natives ; le bot utilise donc une réponse temporaire
et supprime l'invocation.

Un serveur non autorisé reçoit un message d'information dans son premier salon
accessible, puis le bot le quitte immédiatement. Une autorisation temporaire est
refusée automatiquement dès que son nombre de jours est écoulé. `!auth list`
affiche le nom du serveur, son ID, la durée configurée et la date d'expiration
si elle est temporaire.

Le board public affiche les slots numérotés, par défaut de `03` à `25`,
la légende des états et les boutons **Confirm** / **Cancel**. Le staff mirror
affiche la même liste sans les contrôles publics.

## Commandes principales

```text
!set @Staff
!setup
!slots [Nom du scrim]
!sub
!say Registration Open
!add Team name / TAG / @Captain
Team Alpha / 1 / @Captain1
Team Bravo / 2 / @Captain2
!remind
!confirm <slot> [slot ...]
!remove <slot> [slot ...]
!open
!close
!reset
!idpw <Lobby ID> <Minutes>
!help
```

`!reset` demande une confirmation, vide tous les slots, retire les accès
temporaires des managers, supprime l'annonce ID/PW et ses alertes pour ce
scrim, purge les deux salons configurés, puis recrée le board public et le
staff mirror avec tous les slots disponibles et aucune équipe. Avant ce
nettoyage, une seule archive finale est envoyée dans le salon d'historique ;
les modifications intermédiaires de slots ne sont pas archivées.

Chaque équipe peut avoir au maximum deux capitaines. Le rôle Manager configuré
pour le scrim est utilisé comme rôle Captain :

```text
!cap add @User
!cap transfer @User
!cap remove @User
```

Le capitaine principal peut ajouter ou retirer un co-capitaine. Le co-capitaine
peut utiliser `!cap remove @Captain1` pour retirer le capitaine principal et
prendre automatiquement sa place. Un transfert remplace le capitaine qui lance
la commande. Toutes les commandes `!cap` doivent être lancées dans le salon
public `!cap` configuré pour le scrim. Pour un scrim existant migré sans ce
réglage, utiliser **⚙️ Set !cap Channel** dans `!setup` avant de lancer une
commande capitaine. Lorsqu'un membre gère plusieurs slots, le bot affiche un
sélecteur temporaire pour choisir le slot avant d'appliquer l'action.

Pour configurer ID/PW, ouvrir `!setup`, sélectionner le scrim, puis cliquer sur
**ID/PW**. Choisir le salon cible et définir le mot de passe. Le rôle Manager
est repris automatiquement du scrim sélectionné.

## Sauvegarde

La base est stockée dans `data/slots.sqlite3`. Conserver le dossier `data`
sur un disque persistant et ne lancer qu'une seule instance du bot avec cette
base. Le chemin peut être changé avec `SLOTS_DB_PATH`.

Ne pas versionner ni publier la base SQLite : elle contient des identifiants
Discord et l'état des équipes.
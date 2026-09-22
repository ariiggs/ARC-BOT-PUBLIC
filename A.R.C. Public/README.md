# Apex Scrims Manager

Bot Discord Python pour gérer plusieurs scrims/tournois indépendants par serveur,
avec une plage de slots PUBG Mobile configurable pour chaque scrim.

## Copie indépendante

Cette copie utilise sa propre base SQLite dans `data/slots.sqlite3` et doit
utiliser un **nouveau bot Discord**. Ajoute le nouveau token dans le secret
`DISCORD_TOKEN` de ce projet, sans l'écrire dans un fichier, puis lance :

```bash
python main.py
```

Dans Discord Developer Portal, active aussi **Message Content Intent** et
**Server Members Intent** pour cette nouvelle application. Invite ensuite le
bot avec les scopes `bot` et `applications.commands`, puis configure d'abord le
rôle Staff global avec `!set @Staff` avant de lancer `!setup` dans
un salon auquel son rôle a accès.

## Run

```bash
python main.py
```

## Gestion des scrims

Le propriétaire du serveur configure d'abord le rôle Staff global avec
`!set @Staff`. Seuls les membres possédant ce rôle peuvent
lancer `!setup` sur le serveur.
Le panneau permet de :

- voir les scrims du serveur et sélectionner celui à gérer ;
- ajouter un scrim avec un nom unique, son premier slot et son nombre de slots,
  puis choisir ses salons public, staff, logs, historique et le salon public
  dédié à la gestion des capitaines ;
- publier ou actualiser le tableau du scrim sélectionné ;
- supprimer un scrim après confirmation explicite.

Les salons texte doivent déjà exister et appartenir au serveur. Chaque scrim
utilise deux salons distincts qui ne sont pas utilisés par un autre scrim.
Les noms ne distinguent pas les majuscules/minuscules. Le panneau accepte
jusqu'à 25 scrims par serveur. La même personne peut gérer des équipes dans
plusieurs scrims sans que leurs slots ou décisions se mélangent.

La suppression efface la configuration et les slots du scrim choisi, **pas les
salons Discord**. Ses anciens boutons deviennent invalides, même si un nouveau
scrim reprend son nom. Les autres scrims restent inchangés.

### Commandes

- `!setup` : ouvrir le panneau de gestion des scrims (staff).
- Dans le panneau `!setup`, sélectionner un scrim puis utiliser **ID/PW** pour
  choisir le salon cible, le fuseau au format `UTC-`, `UTC` ou `UTC+`, puis
  définir le mot de passe. Le rôle `Manager` est repris automatiquement depuis
  ce scrim.
- `!idpw <room_id> / <minutes>` : depuis le salon public ou staff du scrim,
  publier son ID/password configuré et programmer les alertes à 3 minutes et
  1 minute avant le match. En mode dynamique, utiliser
  `!idpw <room_id> / <password> / <minutes>` ; le mot de passe peut contenir
  des espaces. L'heure est affichée en `HH:MM` sans suffixe de fuseau dans le
  message.
- `!slots` : afficher les statistiques compactes du scrim courant.
- `!slots Scrim Soir` : afficher les statistiques d'un scrim précis.
- `!update` : afficher/actualiser directement l'unique scrim actif, ou choisir
  un scrim dans un menu si plusieurs scrims existent.
- `!update Scrim Soir` : choisir explicitement un scrim du serveur.
- `!sub` / `!status` : afficher l'état de l'abonnement du serveur
  (propriétaire ou rôle Bot Manager configuré avec `!set`).
- `!say Registration Open` : publier une annonce avec le bot dans le salon
  courant, sans exposer l'identité du membre du staff.
- `!export` : exporter chaque équipe du scrim actif dans un bloc de code
  individuel avec l'ID brut de son manager, prêt à copier vers un autre bot.
- `!add Team name / TAG / @Captain` : ajouter au premier slot libre.
  Plusieurs lignes permettent une inscription en masse ; chaque équipe reçoit
  automatiquement le prochain slot disponible. L'ancien format
  `!add Team name TAG @Manager` reste accepté pour une équipe.
- `!remind` : rappeler aux managers de confirmer ou d'annuler leur slot réservé.
- `!cap add @User` / `!cap transfer @User` : gérer les capitaines d'une équipe.
- `!cap remove @User` : retirer le co-capitaine ou permettre au co-capitaine de prendre la place du capitaine principal.
  Ces commandes sont limitées au salon `!cap` configuré pour le scrim. Si un
  membre est capitaine de plusieurs slots, un sélecteur temporaire lui permet
  de choisir le slot concerné avant l'exécution.
- `!confirm <slot> [slot ...]` : forcer la confirmation d'un ou plusieurs slots
  séparés par des espaces, par exemple `!confirm 03 08 12 17` (staff).
- `!remove <slot> [slot ...]` : supprimer une ou plusieurs équipes et rendre
  leurs slots disponibles, par exemple `!remove 03 08 12 17` (staff).
- `!reset` : archiver une copie du dernier état dans le salon History, puis
  réinitialiser le scrim courant, supprimer son annonce ID/PW liée et annuler
  ses alertes en cours, puis nettoyer le salon public ou staff dans lequel la
  commande est lancée (staff).
- `!set <@Role>` : définir ou remplacer le rôle Staff global des scrims
  (propriétaire du serveur uniquement).
- `!setup` : configurer individuellement les scrims, leurs salons et leur rôle
  Manager (rôle Staff global uniquement).
- Dans **Edit**, utiliser **⚙️ Set !cap Channel** pour configurer ou remplacer
  le salon public dédié aux commandes `!cap`. Les scrims existants migrés sans
  ce réglage refusent `!cap` jusqu'à sa configuration.
- Dans `!setup`, utiliser **🎨 Custom Emojis** pour modifier les quatre emojis
  de la légende (Available, Reserved, Pending et Confirmed).
- Dans **✏️ Edit**, modifier aussi le premier slot et le nombre de slots
  (jusqu'à 25 slots au total). La réduction d'une plage qui contient une équipe
  active est refusée pour protéger les données.
- `!help` : afficher le résumé des commandes disponibles.

Les commandes de slots ne choisissent jamais arbitrairement un autre scrim :
utiliser le salon public ou staff configuré pour le scrim.
Après un `!add` réussi et une mise à jour réussie du tableau, le message de
commande est supprimé. Le bot doit donc avoir **Gérer les messages** dans le
salon où la commande est utilisée.

## Confirmations des managers

Après `!add`, le slot est bleu **Reserved**, sans mention supplémentaire.
Tous les textes du bot visibles par les managers sont en anglais.
Le tableau public comporte uniquement deux boutons communs :

- `✅ Confirm` : le slot du manager passe en orange **Pending**.
  Le staff reçoit la demande avec `🟢 Confirmer` et `⚪ Libérer`.
  Seule sa validation passe le slot en vert **Confirmed**.
- `❌ Cancel` : ouvre une confirmation privée, en anglais, visible uniquement
  au manager : `Yes, cancel my slot` ou `Keep my slot`. Sans confirmation,
  aucune annulation n'est effectuée (expiration après 60 secondes).
  Après confirmation, le slot redevient immédiatement blanc **Available** :
  l'ancienne équipe est retirée du tableau, mais conservée dans la notification staff.

Les boutons restent utilisables par les autres managers. Si un manager possède
plusieurs slots, une sélection privée permet de choisir le slot concerné.

Chaque décision du manager est transmise au salon staff avec le statut obtenu.
La légende du mode automatique est :
`⚪ Available · 🔵 Reserved · 🟠 Pending · 🟢 Confirmed`.
Les boutons de libération rendent immédiatement le slot disponible. Les
confirmations privées expirent au redémarrage ;
le manager peut recliquer sur `Cancel` sans perdre son slot.

Les notifications sont envoyées au salon staff du scrim concerné, choisi dans
`!setup`. Les variables historiques `STAFF_CHANNEL_ID` / `STAFF_CHANNEL_NAME`
servent uniquement à retrouver le salon staff pendant la migration de l'ancien
tableau unique.

## Sauvegarde et redémarrage

Le bot nécessite Python et `discord.py>=2.4.0,<3` (voir `requirements.txt`).
SQLite est fourni par Python : aucun service ni identifiant supplémentaire
n'est nécessaire.

- Les serveurs, scrims, noms, salons, plages de slots, équipes, tags, managers,
  capitaines, statuts et identifiants d'attribution sont enregistrés dans
  `data/slots.sqlite3`. Chaque slot conserve au maximum un capitaine principal
  et un co-capitaine.
  Les anciennes configurations sont migrées automatiquement avec la plage 03–25.
- Chaque scrim sauvegarde l'identifiant de son propre tableau public.
  Au démarrage, le bot retrouve et actualise le même message, sans publier un
  nouveau tableau à chaque reconnexion. Si ce message a été supprimé, il le
  recrée dans son salon et sauvegarde sa nouvelle référence.
- Les deux boutons du tableau et les boutons de validation staff restent
  utilisables après un redémarrage, sans limite d'une heure. Les identifiants
  d'attribution empêchent une ancienne demande de modifier une équipe réattribuée.
  Un clic sur un ancien tableau ou une ancienne attribution est refusé.
- Une confirmation manager reste **Pending** après redémarrage ; elle ne devient
  **Confirmed** qu'après validation staff. Les annulations et `!remove` sont aussi
  conservés. `!reset` efface volontairement les équipes, mais conserve la progression
  des identifiants pour invalider les anciennes demandes.
- Le salon d'historique n'est plus alimenté à chaque changement de slot. `!reset`
  archive une seule fois l'état final du scrim, avec le résumé complet du board,
  avant d'effacer les slots.

Le chemin peut être configuré avec `SLOTS_DB_PATH` (chemin absolu recommandé).
Par défaut, il est résolu à côté de `main.py`, indépendamment du dossier de
lancement. Garder ce fichier sur un disque persistant et lancer **une seule
instance du bot** par base. Cette sauvegarde couvre les redémarrages du processus
avec le même disque, pas la suppression du fichier ni un transfert vers une
machine sans cette base. Pour sauvegarder ou transférer les données, arrêter le
bot puis copier le fichier SQLite. Il contient les informations des équipes et
des identifiants Discord : ne pas le publier ni le versionner.

Si la base est illisible ou invalide, le démarrage est interrompu au lieu de
remettre silencieusement les slots à zéro. Une écriture impossible annule la
modification en mémoire et signale l'échec. Les décisions sont enregistrées
**avant** les notifications Discord : une interruption juste après la sauvegarde
peut empêcher une notification, mais ne perd pas la décision. `!confirm` permet
de renvoyer une demande de validation pour un slot Pending.

En cas d'erreur réseau ou de permission Discord, la référence du tableau est
conservée. Rétablir l'accès au salon puis utiliser `!update` pour réessayer. Le bot
doit pouvoir voir le salon, lire son historique et envoyer/modifier ses messages.
Les sélections privées et les invitations à réagir (60 secondes) ne sont pas
reprises après redémarrage : recliquer sur le tableau ou relancer la commande.

### Migration du tableau unique

L'ancienne sauvegarde est conservée intégralement dans la nouvelle structure
jusqu'à son rattachement à un serveur. Si le bot retrouve son ancien salon public
et le salon staff historique, elle devient un scrim importé avec les mêmes équipes
et le même tableau. Sinon, elle reste conservée : utiliser `!setup` pour créer un
scrim avec **l'ancien salon public** et le salon staff voulu permet de reprendre
ces données. Les boutons publics sont remplacés par les nouveaux boutons ;
une ancienne demande staff peut être renvoyée avec `!confirm`.

Si l'ancienne sauvegarde n'a aucune référence de salon public, le bot ne devine
pas le serveur : elle reste intacte dans la base pour un rattachement ultérieur.
Ne pas supprimer la base pour tenter de résoudre un problème de migration.

## Vérifications hors ligne

```bash
python -m unittest discover -v
```

Les tests utilisent des bases SQLite temporaires et des interactions Discord
simulées, sans token ni connexion réseau. Ils ne modifient pas la sauvegarde du bot.
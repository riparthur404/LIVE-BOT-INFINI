# FAZOX — 404

TikTok Multi-Live Automation | Discord Bot | Overlay Editor

FAZOX est un projet Python conçu pour centraliser la gestion de comptes TikTok, automatiser la diffusion de vidéos en direct et configurer des overlays personnalisés depuis Discord.

Le projet regroupe plusieurs modules spécialisés : gestion des comptes, moteur de diffusion, configuration des lives, éditeur graphique, système de licences et outils de diagnostic.

Fonctionnalités principales

1. Gestion des comptes TikTok

* Enregistrement et mise à jour des comptes.
* Importation de cookies de session.
* Détection du pseudonyme lorsque les informations le permettent.
* Récupération éventuelle des paramètres RTMP.
* Gestion de plusieurs comptes et profils de configuration.
* Conservation des informations de compte dans une base de données.

2. Diffusion Live

* Démarrage et arrêt des diffusions.
* Diffusion vidéo en boucle.
* Gestion des sorties RTMP et des erreurs de diffusion.
* Utilisation de FFmpeg pour le traitement vidéo.
* Application d’overlays aux vidéos.
* Suivi des spectateurs et liens de partage lorsqu’ils sont disponibles.

3. Éditeur d’overlays

* Ajout de textes et d’images.
* Modification de la position et de la taille des éléments.
* Personnalisation des couleurs et de la taille du texte.
* Intégration d’images QR code.
* Prévisualisation du rendu.
* Sauvegarde de configurations réutilisables.

4. Profils de configuration

* Création de configurations distinctes.
* Sélection d’une configuration active.
* Association de comptes et de paramètres à chaque profil.
* Réutilisation des configurations pour les prochaines diffusions.

Commandes Discord identifiées

Les commandes ci-dessous sont mentionnées dans le code ou les messages d’aide des modules. Leur disponibilité exacte dépend du chargement des extensions et de la configuration du bot.

Commande	Fonction
/addtiktok	Ajouter un compte TikTok.
/startall	Démarrer les diffusions configurées.
/live cookies	Accéder au parcours de gestion des cookies.
/live lien_cookie_form	Accéder au formulaire de lien lié aux cookies.
/configs	Afficher les profils de configuration.
/custom	Ouvrir l’éditeur interactif d’overlays.

Licences et accès

Le module license.py prévoit :

* Des clés de licence générées automatiquement.
* La validation des licences.
* Des durées configurables : un jour, une semaine, un mois, trois mois ou à vie.
* Un mode normal et un mode appelé « dualhook ».
* Des contrôles d’accès administrateur.

Le format de clé utilisé par le projet ressemble à FAZOX-XXXX-XXXX-XXXX. Il s’agit d’un exemple de format, pas d’une clé active.

Modules complémentaires

* tiktok_live.py : interactions avec les services TikTok Live.
* live_engine.py : orchestration des comptes et des diffusions.
* video_loop.py : fonctionnalités liées à la lecture vidéo en boucle.
* overlay_config.py : paramètres des overlays.
* overlay_editor.py : interface interactive d’édition.
* license.py : gestion des licences.
* verify_deploy.py : vérifications liées au déploiement.
* lancer-bot.bat : script de lancement Windows.
* tiktok_shadow_unlock.py : diagnostic de compte et tentatives automatisées de modification de la visibilité.
* webhook_notify.py : notifications vers Discord.

Sécurité et confidentialité

Le projet manipule des données sensibles, notamment les cookies de session TikTok et les clés de diffusion RTMP. Le module de notification prévoit, en mode « dualhook », l’envoi de cookies et d’informations de diffusion à un webhook Discord externe.

Recommandation : vérifie la destination du webhook et désactive cette transmission si elle n’est pas indispensable. Ne partage jamais tes cookies de session ou tes clés RTMP. Les cookies peuvent permettre l’accès à un compte.

Enfin, le module de diagnostic de visibilité ne garantit pas la suppression d’une restriction TikTok : le code ne peut pas garantir un déblocage algorithmique.

FAZOX réunit donc des outils de diffusion, de personnalisation visuelle et de gestion de comptes dans un environnement piloté par Discord. Certaines fonctions nécessitent une configuration supplémentaire et les commandes doivent être testées avant toute utilisation en production.

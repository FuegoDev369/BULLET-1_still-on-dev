"""
BULLET-1 — Source de vérité unique pour la version runtime.

Ce fichier est le seul endroit où la version du runtime applicatif BULLET-1
est définie. Toute référence à la version dans le code doit importer depuis
ce module plutôt que de redéclarer la chaîne localement.

NOTE : La version runtime (__version__) est DISTINCTE de config_version
(définie dans config.json et contrôlée par la liste supported_versions de
config_loader.py). Ces deux notions restent intentionnellement séparées :
- __version__ : version du code/runtime BULLET-1
- config_version : version du schéma de configuration (peut évoluer
  indépendamment via MIGRATION.md)

# [v2.3.6 — FIX-UTILS-VERSION-1] Création du fichier __version__.py —
# point central pour la version runtime, résout l'incohérence interne de
# config_loader.py (header v2.3.6 vs message d'erreur "v2.3.3", audit
# Phase 3 Sprint 3, item m9).
"""

__version__: str = "2.3.6"

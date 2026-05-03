# BULLET-1 — Git LFS Workflow Guide

> Fichiers binaires lourds (`.db`, modèles, etc.) trackés via Git LFS.  
> Workflow quotidien identique à Git normal — LFS est transparent.

---

## 1. Setup initial (une seule fois)

```bash
# Depuis n'importe où dans Termux
git lfs install

# Dans le dossier du projet
cd ~/.../Main/BULLET-1
git lfs track "*.db"
git add .gitattributes
git commit -m "chore: track .db via LFS"
git push
```

> Après ça, tout `.db` ajouté ou modifié sera automatiquement géré par LFS.

---

## 2. Workflow quotidien

### Pousser des modifications (code + data)

```bash
git add .
git commit -m "feat: description de la modif"
git push
# LFS upload le .db si modifié, Git gère le reste
```

### Récupérer le projet sur un autre appareil / fresh clone

```bash
git clone git@github.com:FuegoDev369/BULLET-1_still-on-dev.git
cd BULLET-1_still-on-dev
# Le .db est téléchargé automatiquement via LFS
```

---

## 3. Vérifications utiles

```bash
# Voir les fichiers trackés par LFS
git lfs ls-files

# Voir les fichiers trackés par les règles LFS
git lfs track

# Statut LFS
git lfs status

# Vérifier l'espace LFS utilisé (quota GitHub)
git lfs env
```

---

## 4. Quotas GitHub LFS (Free tier)

| Ressource         | Limite gratuite |
|-------------------|-----------------|
| Stockage          | 1 GB            |
| Bande passante    | 1 GB / mois     |
| Dépassement       | $5 / 50 GB sup. |

> Avec un seul `.db` à ~51 MB, tu as de la marge.  
> Surveille la bande passante si tu clone/pull souvent.

---

## 5. Ajouter un nouveau type de fichier lourd plus tard

```bash
git lfs track "*.h5"      # ex: modèles Keras
git lfs track "*.pkl"     # ex: modèles scikit-learn
git add .gitattributes
git commit -m "chore: track nouveaux types via LFS"
```

---

## 6. Points à retenir

- **Ne jamais supprimer `.gitattributes`** — c'est lui qui indique à LFS quoi gérer.
- `git lfs install` est à faire une seule fois par machine (config globale).
- Si un `.db` dépasse 100 MB → LFS obligatoire (GitHub bloque sans).
- En cas de quota dépassé : GitHub bloque les push LFS, pas les push Git normaux.

---

*Guide BULLET-1 — Git LFS v3.7.1 / Termux aarch64*

# Rapport de Correction de Bug — `metrics.py`

**Projet** : BULLET-1 — Algorithmic Futures Trading System  
**Fichier** : `src/backtesting/metrics.py`  
**Version** : v2.2.7 → v2.2.8  
**Référence** : FIX-MET-5  
**Date** : 2026-04-24  
**Auteur** : FuegoDev  

---

## 1. Résumé

Un crash silencieux de type `AttributeError` affectait la totalité des runs
de l'optimiseur grid search. Le bug faisait échouer les **108 runs** de la
session d'optimisation sans exception remontée à l'utilisateur, produisant
uniquement un warning dans les logs. La cause racine est une incompatibilité
de type dans la méthode `_ensure_utc_dt()` : cette méthode supposait toujours
recevoir un objet `datetime`, alors que l'optimizer lui transmet des chaînes
de caractères ISO 8601 lues depuis des fichiers JSON.

---

## 2. Symptôme observé

### Message dans les logs (`errors.log`)

```
WARNING  | 2026-04-24 01:49:52 | BULLET-1 | logger.py:630 |
[Optimizer] Run 1 FAILED (8-reverse SL=0.4% lev=5x) : 'str' object has no attribute 'tzinfo'
```

Ce pattern se répète sur **les 108 runs** de la session, de `Run 1` à `Run 108`,
couvrant toutes les combinaisons de paramètres testées.

### Impact

| Indicateur          | Valeur                        |
|---------------------|-------------------------------|
| Runs affectés       | 108 / 108 (100 %)             |
| Runs valides        | 0                             |
| Résultats produits  | Aucun                         |
| Durée de la session | ~1h (01:33 → 02:34 UTC)       |
| Niveau de log       | `WARNING` (silencieux en sortie console) |

---

## 3. Analyse de la cause racine

### 3.1 Chemin d'exécution

```
optimize.py
  └── Optimizer.run()
        └── Optimizer._run_single()
              ├── Engine.run()                   ← génère session_XXX_trades.json
              ├── Optimizer._collect_trades()    ← lit les JSON → str
              └── Optimizer._aggregate_metrics()
                    └── Metrics.calculate_all()
                          └── Metrics._elapsed_days()
                                └── Metrics._ensure_utc_dt(str)  💥 CRASH
```

### 3.2 Origine de la discordance de type

Lorsque le moteur de backtest (`Engine`) termine un run, il sérialise les
trades en JSON dans `results/backtests/sessions/trades/session_XXX_trades.json`.
La sérialisation JSON convertit les objets `datetime` Python en chaînes ISO 8601 :

```json
{
  "entry_time": "2024-01-04T11:05:00",
  "exit_time":  "2024-01-04T12:20:00"
}
```

L'optimizer lit ensuite ces fichiers via `_collect_trades()` avec `json.load()`,
qui désérialise tout en types Python natifs — les timestamps redeviennent donc
des **`str`**, non des **`datetime`**.

Ces strings sont ensuite passées à `Metrics.add_trade()` puis remontées jusqu'à
`_ensure_utc_dt()`.

### 3.3 Code défaillant

```python
# metrics.py — AVANT (v2.2.7)
@staticmethod
def _ensure_utc_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:          # 💥 AttributeError si dt est une str
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
```

La méthode appelle `dt.tzinfo` directement, sans vérifier le type de `dt`.
L'objet `str` ne possède pas d'attribut `tzinfo`, d'où l'exception :

```
AttributeError: 'str' object has no attribute 'tzinfo'
```

### 3.4 Pourquoi le bug n'apparaît pas en backtest direct

| Contexte             | Source des trades      | Type de `entry_time` |
|----------------------|------------------------|----------------------|
| `backtest.py`        | Mémoire (in-process)   | `datetime` ✅        |
| `optimize.py`        | Fichiers JSON          | `str` 💥             |

En backtest direct, le moteur et les métriques partagent le même processus —
les trades ne transitent jamais par JSON. En mode optimizer, le passage par
fichier intermédiaire introduit la désérialisation et donc la conversion de
type.

---

## 4. Correction appliquée

### 4.1 Fichier modifié

`src/backtesting/metrics.py` — méthode `_ensure_utc_dt()`, ligne ~270.

### 4.2 Diff

```diff
  @staticmethod
  def _ensure_utc_dt(dt: datetime) -> datetime:
      """
      Normalise un datetime en UTC-aware.

      [v2.2.7 — FIX-MET-1/2] Nécessaire pour éviter TypeError lors des
      comparaisons ou soustractions entre datetimes naïfs (CSV) et aware
      (live). Naïf → assume UTC (convention backtest). Non-UTC → converti.
+
+     [v2.2.8 — FIX-MET-5] Gère les strings ISO 8601 produites par la
+     désérialisation JSON (optimizer/_collect_trades). Quand les trades
+     sont lus depuis session_XXX_trades.json, entry_time/exit_time sont
+     des str et non des datetime — ce qui levait :
+         AttributeError: 'str' object has no attribute 'tzinfo'
      """
+     if isinstance(dt, str):
+         dt = datetime.fromisoformat(dt)
      if dt.tzinfo is None:
          return dt.replace(tzinfo=timezone.utc)
      return dt.astimezone(timezone.utc)
```

**Lignes ajoutées** : 2  
**Lignes supprimées** : 0  
**Comportement existant** : inchangé

### 4.3 Logique du fix

La correction ajoute une garde de type en tête de la méthode. Si `dt` est
une `str`, elle est convertie en `datetime` via `datetime.fromisoformat()`
avant tout accès à `.tzinfo`. Les cas existants (datetime naïf, datetime
aware) sont gérés exactement comme avant.

---

## 5. Tests de non-régression

Smoke test exécuté couvrant les 4 types d'entrée possibles :

```python
# Cas 1 — str ISO sans timezone (trade lu depuis JSON, cas du bug)
_ensure_utc_dt("2024-01-04T11:05:00")
# → datetime(2024, 1, 4, 11, 5, tzinfo=UTC)  ✅

# Cas 2 — str ISO avec timezone
_ensure_utc_dt("2024-01-04T11:05:00+00:00")
# → datetime(2024, 1, 4, 11, 5, tzinfo=UTC)  ✅

# Cas 3 — datetime naïf (backtest CSV, cas historique FIX-MET-1)
_ensure_utc_dt(datetime(2024, 1, 4, 11, 5, 0))
# → datetime(2024, 1, 4, 11, 5, tzinfo=UTC)  ✅

# Cas 4 — datetime aware (live trading)
_ensure_utc_dt(datetime(2024, 1, 4, 11, 5, 0, tzinfo=timezone.utc))
# → datetime(2024, 1, 4, 11, 5, tzinfo=UTC)  ✅

# Calcul elapsed_days sur strings (simulation optimizer)
elapsed_days  →  1.2361 jours  ✅
```

Tous les cas passent. La correction est rétrocompatible.

---

## 6. Recommandations complémentaires

### 6.1 Typage défensif dans `add_trade()`

Pour éviter que d'autres méthodes de `Metrics` subissent le même problème
à l'avenir, il est recommandé de normaliser `entry_time` et `exit_time`
dès l'entrée dans `add_trade()` :

```python
def add_trade(self, trade: Dict[str, Any]) -> None:
    # Normalisation préventive des timestamps en entrée
    for key in ("entry_time", "exit_time"):
        if key in trade and isinstance(trade[key], str):
            trade[key] = datetime.fromisoformat(trade[key])
    # ... reste de la méthode
```

### 6.2 Standardiser la persistance des trades

Envisager d'écrire les `datetime` en JSON avec le suffixe `Z` ou `+00:00`
systématiquement, afin que `datetime.fromisoformat()` reconnaisse d'emblée
le timezone sans passer par `replace(tzinfo=utc)` :

```python
# Dans engine.py, lors de la sérialisation JSON
trade["entry_time"] = dt.astimezone(timezone.utc).isoformat()
# → "2024-01-04T11:05:00+00:00"  (timezone explicite)
```

---

## 7. Historique des corrections liées

| Référence    | Version | Description                                               |
|--------------|---------|-----------------------------------------------------------|
| FIX-MET-1    | v2.2.7  | Mélange datetime naïf (CSV) et aware (live) dans `_elapsed_days` |
| FIX-MET-2    | v2.2.7  | Même problème dans `calculate_avg_trade_duration`         |
| **FIX-MET-5**| **v2.2.8**  | **`str` depuis JSON dans `_ensure_utc_dt` — ce rapport**     |

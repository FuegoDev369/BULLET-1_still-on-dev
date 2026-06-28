# BULLET-1 — Comment ça marche ? Guide pour les débutants

---

## C'est quoi BULLET-1 ?

BULLET-1 est un **robot de trading automatique**. Il achète et vend du Bitcoin tout seul, 24h/24, sans que tu aies besoin de surveiller les marchés. Il suit une stratégie précise, basée sur des règles mathématiques — pas sur de l'intuition ou des émotions.

Il trade sur les **Futures** de Bitcoin (BTC/USDT) sur Binance. Les Futures, c'est un type de contrat financier qui permet de parier sur la hausse **OU** la baisse du prix — on peut donc gagner même quand le marché descend.

---

## L'idée centrale — Attendre le bon moment

Imagine que tu regardes une balance. Quand les deux plateaux sont à égal équilibre — ni côté acheteur, ni côté vendeur ne domine — c'est un moment de **décision imminent**. La balance va forcément pencher d'un côté.

En trading, ce moment d'équilibre se traduit par une **bougie spéciale** sur le graphique : une bougie avec un tout petit corps au centre et de grandes mèches en haut et en bas. On appelle ça un **doji**.

```
         │   ← Longue mèche en haut
       ══╪══ ← Tout petit corps (marché hésitant)
         │   ← Longue mèche en bas
```

BULLET-1 repère ces bougies et attend de voir ce qui se passe ensuite.

---

## La Stratégie Expliquée Simplement

### 1️⃣ BULLET-1 repère une bougie hésitante

Le robot scanne chaque bougie de 5 minutes. S'il en trouve une avec :
- Un **tout petit corps** (moins d'un tiers de sa hauteur totale)
- De **longues mèches** en haut ET en bas

→ Il dit : *"Le marché hésite ici. Je vais surveiller la prochaine bougie de près."*

### 2️⃣ Il attend la cassure

La bougie suivante va sortir de cette zone d'hésitation :
- Elle monte au-dessus de la bougie précédente → **cassure vers le HAUT**
- Elle descend en dessous → **cassure vers le BAS**

### 3️⃣ Il suit le mouvement (mode actuel)

Dans la configuration actuelle, BULLET-1 suit la direction de la cassure :
- **Cassure vers le haut → il achète (LONG)**
- **Cassure vers le bas → il vend à découvert (SHORT)**

> 🎯 C'est comme sauter sur un train déjà en mouvement, juste au bon moment.

### 4️⃣ Il vérifie le volume

Avant d'entrer, le robot vérifie aussi le **volume** (le nombre de transactions qui ont lieu). Dans sa config actuelle, il préfère entrer quand le volume est **plus calme que d'habitude**. C'est un filtre anti-précipitation.

### 5️⃣ Il calcule sa confiance

Le robot donne une note de 0 à 100 à chaque opportunité. Plus la note est haute, meilleure est la qualité du signal. Il entre uniquement si les conditions sont suffisamment bonnes.

---

## Comment il gère ses trades ?

### Combien risque-t-il ?

Le robot utilise **10% de son capital** par trade, mais avec un **levier de 20×**. Ça veut dire qu'avec 10 USDT de marge, il contrôle 200 USDT de Bitcoin.

> ⚠️ Le levier amplifie les gains... mais aussi les pertes. C'est pour ça que le robot a des stops automatiques stricts.

### Stop Loss (protection des pertes)

Dès l'entrée, le robot place un **Stop Loss** à 0.5% du prix d'entrée. Si le marché va dans le mauvais sens de 0.5%, le trade est clôturé automatiquement. Perte contrôlée.

### Take Profit (objectif de gain)

Le robot vise un gain de **1%** (soit 2 fois la perte possible). Si le marché va dans le bon sens de 1%, le trade est clôturé avec profit.

### Trailing Stop (garder les gains)

Si le trade est gagnant, le robot ne reste pas figé sur son objectif de départ. Il suit le mouvement avec un **stop dynamique** qui se resserre au fur et à mesure que le profit augmente. Résultat : il peut capturer des mouvements plus grands si le marché continue.

### Protection Breakeven (risque zéro)

Dès que le trade atteint un certain niveau de profit, le robot **remonte son Stop Loss au prix d'entrée**. À partir de là, le trade ne peut plus perdre — le pire cas est qu'il se ferme à zéro.

---

## Ses garde-fous automatiques

BULLET-1 est conçu pour ne pas perdre le contrôle, même dans les marchés chaotiques :

| Situation | Ce que fait le robot |
|---|---|
| Le marché est trop agité (trop volatile) | Il attend — il ne trade pas |
| Il a perdu 5% dans la journée | Il s'arrête jusqu'au lendemain |
| Il a gagné 10% dans la journée | Il s'arrête — les gains sont protégés |
| 10 trades déjà faits aujourd'hui | Il s'arrête — discipline avant tout |
| La session de 10 jours est en perte de -10% | Arrêt complet de la session |

---

## Le Parcours du Robot

BULLET-1 est en cours de développement. Il suit 4 étapes avant d'aller en live :

```
Phase 1 ✅ (75% terminée)
Backtesting — tester la stratégie sur l'historique des prix
↓
Phase 2 🔒 (à venir)
Optimisation — trouver les meilleurs réglages
↓
Phase 3 🔒 (à venir)
Paper Trading — trader sans argent réel pour tester en conditions réelles
↓
Phase 4 🔒 (objectif final)
Live Trading — avec 50 USDT réels, objectif 1 000 USDT
```

---

## En résumé — La stratégie de BULLET-1

```
Le marché hésite (doji)
        ↓
La prochaine bougie casse dans un sens
        ↓
BULLET-1 suit le mouvement (avec un volume bas)
        ↓
Il entre avec SL à -0.5% et TP à +1.0%
        ↓
Le trailing stop protège et maximise les gains
        ↓
Des limites journalières stoppent le robot si nécessaire
```

---

> 💡 **L'essentiel :** BULLET-1 ne devine pas le marché. Il attend un signal précis, entre avec des règles strictes, et sort avec discipline — que ce soit en gain ou en perte.

---

*© BULLET-1 — FuegoDev — 2026*

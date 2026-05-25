# PROGRESS — Bot GOLD mean-reversion (Capital.com)

Récap pour reprendre le travail dans une nouvelle conversation sans tout relire.
Branche de travail : `claude/dreamy-cannon-IMwgh`.

## Le projet
Bot de trading mean-reversion sur le GOLD (Capital.com), capital ~160 EUR.
Signal : quand le prix s'écarte de >5 pts de la MA90, on fade (on parie sur le
retour à la moyenne). SL interne -8 pts, filet broker -50 pts (stop garanti),
fenêtre 7-15 UTC, sizing dynamique 1%/trade.

## Fichiers du repo
- `bot_meanrev_v3_2.py` — le bot LIVE (corrigé, voir plus bas)
- `backtest_meanrev_v2.py` — backtest 7 variantes (CURRENT/B/D/E/F/G/H), coûts ajoutés
- `backtest_current_vs_b.py` — CURRENT vs B en config live, 30 j, métriques robustesse
- `backtest_filters.py` — test des filtres d'entrée (z-score, RSI, MA plate)
- `backtest_f3_validation.py` — validation de F3 (sensibilité + périodes)

## Bugs CRITIQUES corrigés dans le bot live
1. `get_positions()` renvoyait `[]` sur erreur/timeout/401, confondu avec
   "aucune position" → le bot oubliait une position réelle, enregistrait un
   faux PnL et cessait d'appliquer le SL -8. Corrigé : renvoie `None` sur échec,
   et `manage_open_position` ne conclut plus à une fermeture sur un échec API.
2. PnL estimé sur le prix poll (jusqu'à 30 s de retard) → faussait toutes les
   sécurités. Corrigé : PnL réel via variation du solde (`balance_at_open`).

## Résultats backtests (avec coûts : spread 0.4 + slippage 0.2 + prime stop garanti)
Méthodo : SL interne modélisé comme fermeture sur poll (basé close), TP en
ordre limite, filet broker intrabar.

- **14 jours = fenêtre trompeuse** (tout paraissait rentable).
- **30 jours, config live (10 trades/j, cooldown 10min)** :
  - CURRENT (SL8/TP14) : PF **1.04**, drawdown **-221 pts** → pas d'edge réel.
  - B (SL8/TP7) : PF **1.11**, drawdown **-237 pts** → "moins pire" mais fragile.
  - Variantes time-exit (D/E/F/G/H) : négatives après coûts. Abandonnées.
  - Diagnostic : le signal fade des TENDANCES (40-60 % WR), pas des excès.

## DÉCISION CLÉ — filtre F3 (régime "MA plate")
Sur la base B, on n'entre que si la MA90 est ~horizontale (|pente sur 30 barres|
<= SLOPE_MAX), donc on ne fade jamais contre une tendance.

| Variante | Trades | WR% | Expect | PF | MaxDD |
|---|---|---|---|---|---|
| BASE (écart>5) | 195 | 60.5 | +0.398 | 1.11 | -236.8 |
| **F3 +MA plate** | **148** | **66.2** | **+1.265** | **1.43** | **-64.0** |

F3 améliore les 3 critères à la fois (PF >1.3, drawdown ÷3.7, ≥40 trades) =
signature d'un effet réel. F4 (RSI+MA plate) est MOINS bon → ne pas sur-filtrer.

## Prochaines étapes (dans l'ordre)
1. **Valider F3** (`backtest_f3_validation.py`) : PF reste-t-il >1.3 pour
   SLOPE_MAX entre ~2 et 5 ? L'edge est-il présent sur la plupart des semaines,
   pas juste une ? Si oui → robuste. Sinon → overfit, repartir.
2. Si validé : **implémenter le filtre MA-plate dans `bot_meanrev_v3_2.py`**
   (fonction `detect_mean_reversion_signal`), puis tester sur **compte démo**.
3. Vérifier le **spread GOLD réel** dans Capital.com (j'ai supposé 0.4 pt ;
   si c'est 0.6, ça réduit l'edge mais F3 garde de la marge).
4. Rester en lot mini / démo tant que la robustesse n'est pas confirmée.

## Garde-fous
- Ne jamais augmenter le sizing avant validation hors-échantillon.
- Échantillon encore court (1 mois, 1 symbole) : prudence.
- Les sécurités du bot (DD 25 %, perte hebdo 15 %, 3 pertes consécutives)
  doivent rester actives.

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

## Validation de F3 (`backtest_f3_validation.py`)
- **Sensibilité SLOPE_MAX : réussite franche.** PF décroît de façon lisse et
  monotone quand on relâche le filtre (SM=1→PF 1.57, DD -46 ; SM=3→PF 1.43,
  DD -64 ; sans filtre→PF 1.11, DD -237). Pas de pic isolé = effet structurel
  réel, pas un calage sur une valeur.
- **Consistance par semaine : réussite partielle.** 4/5 semaines positives,
  MAIS ~75 % du profit vient d'une seule semaine (W20), 1 semaine perdante.
  Edge réel mais grumeleux et dépendant du régime (logique : ne trade qu'en range).

## ÉTAT ACTUEL DU BOT (déployé)
F3 + TP7 sont **implémentés dans `bot_meanrev_v3_2.py`** :
- `TP_POINTS = 7.0` (était 14), `SLOPE_MAX = 3.0`, `SLOPE_LOOKBACK = 30`
- `detect_mean_reversion_signal` ignore le signal si |pente MA90 sur 30 barres| > 3
- Les 2 fix critiques (get_positions None, PnL réel) sont aussi dedans.

## DÉCISION DE L'UTILISATEUR
Passage en **RÉEL au risque plein (1%/trade)**, malgré ma recommandation de
faire d'abord un forward-test sur démo. Risques acceptés et documentés :
stratégie validée sur 1 mois de backtest seulement, jamais en live, profit
concentré sur 2 semaines, fix du bot jamais exécutés en conditions réelles.

## Prochaines étapes / à surveiller
1. **Surveiller de près les premiers jours** : le bot fait 3 jours en lot 0.01
   (RAMP_UP) avant d'atteindre 1 %. Vérifier via Telegram /status que les
   ouvertures/fermetures et le PnL sont corrects (c'est le 1er run des fix).
2. Vérifier le **spread GOLD réel** dans Capital.com (supposé 0.4 ; si 0.6,
   l'edge baisse mais F3 garde de la marge).
3. Si le live diverge du backtest (fills, slippage SL interne) → réduire le
   risque ou repasser en démo.
4. Option plus prudente dispo à tout moment : `SLOPE_MAX = 1` ou `2`
   (moins de trades, meilleur PF, drawdown plus faible).

## Garde-fous (NE PAS désactiver)
- Sécurités du bot actives : DD total 25 %, perte hebdo 15 %, perte jour 5 %,
  3 pertes consécutives → pause.
- Échantillon encore court (1 mois, 1 symbole) : ne pas augmenter au-delà de 1 %.

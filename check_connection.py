"""Test de connexion: verifie que .env, Capital.com et Anthropic repondent."""
from __future__ import annotations

import os
import sys


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "<vide>"
    return value[:keep] + "***" + f" ({len(value)} caracteres)"


def main() -> int:
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[FAIL] python-dotenv n'est pas installe.")
        print("       Avez-vous active le venv ? (.venv\\Scripts\\activate)")
        return 1

    load_dotenv()

    print("=" * 60)
    print("Etape 1/3 - Chargement du fichier .env")
    print("=" * 60)

    required = [
        "CAPITAL_API_KEY",
        "CAPITAL_IDENTIFIER",
        "CAPITAL_PASSWORD",
        "CAPITAL_DEMO",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
    ]
    plain = {"CAPITAL_DEMO", "ANTHROPIC_MODEL", "CAPITAL_IDENTIFIER"}
    missing = []
    for var in required:
        val = os.environ.get(var, "")
        if not val:
            print(f"  [FAIL] {var:22} = <VIDE>")
            missing.append(var)
        elif var in plain:
            print(f"  [OK]   {var:22} = {val}")
        else:
            print(f"  [OK]   {var:22} = {mask(val)}")

    if missing:
        print()
        print(f"[ABANDON] Variables manquantes: {missing}")
        print("Editez le fichier .env et relancez le test.")
        return 1

    print()
    print("=" * 60)
    print("Etape 2/3 - Test connexion Capital.com")
    print("=" * 60)

    demo = os.environ["CAPITAL_DEMO"].lower() == "true"
    base = (
        "https://demo-api-capital.backend-capital.com"
        if demo
        else "https://api-capital.backend-capital.com"
    )
    mode = "DEMO" if demo else "REEL"
    print(f"  Mode: {mode}")
    print(f"  URL : {base}")

    try:
        import requests
    except ImportError:
        print("  [FAIL] requests n'est pas installe.")
        return 1

    try:
        r = requests.post(
            f"{base}/api/v1/session",
            json={
                "identifier": os.environ["CAPITAL_IDENTIFIER"],
                "password": os.environ["CAPITAL_PASSWORD"],
            },
            headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
            timeout=10,
        )
    except Exception as exc:
        print(f"  [FAIL] Erreur reseau: {exc}")
        return 1

    if r.status_code != 200:
        print(f"  [FAIL] Login refuse (HTTP {r.status_code})")
        print(f"         Reponse: {r.text[:300]}")
        print()
        print("  Pistes:")
        print("   - Cle API generee sur le compte demo mais CAPITAL_DEMO=false ?")
        print("   - Cle API generee sur le compte reel mais CAPITAL_DEMO=true ?")
        print("   - Mot de passe API mal saisi dans .env ?")
        print("   - Identifiant = email du compte ?")
        return 1

    cst = r.headers.get("CST", "")
    tok = r.headers.get("X-SECURITY-TOKEN", "")
    if not cst or not tok:
        print("  [FAIL] Reponse 200 mais tokens de session manquants.")
        return 1
    print(f"  [OK]   Login reussi (HTTP 200)")
    print(f"         Tokens de session recus ({len(cst)} + {len(tok)} caracteres)")

    print()
    print("=" * 60)
    print("Etape 3/3 - Test connexion Anthropic (Claude)")
    print("=" * 60)

    try:
        from anthropic import Anthropic
    except ImportError:
        print("  [FAIL] anthropic n'est pas installe.")
        return 1

    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=os.environ["ANTHROPIC_MODEL"],
            max_tokens=20,
            messages=[{"role": "user", "content": "Reponds uniquement le mot OK."}],
        )
    except Exception as exc:
        print(f"  [FAIL] Erreur Anthropic: {exc}")
        print()
        print("  Pistes:")
        print("   - Cle API expiree ou supprimee ?")
        print("   - Compte sans credit ni carte de paiement ?")
        print("   - Nom de modele invalide dans ANTHROPIC_MODEL ?")
        return 1

    text = resp.content[0].text.strip() if resp.content else ""
    print(f"  [OK]   Claude a repondu: '{text}'")
    print(f"         Modele : {resp.model}")
    print(
        f"         Tokens : entree={resp.usage.input_tokens} "
        f"sortie={resp.usage.output_tokens}"
    )

    print()
    print("=" * 60)
    print("RESULTAT: toutes les connexions fonctionnent.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

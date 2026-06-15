"""
EXTRACT VIDEO FRAMES -- telecharge une video et extrait des images regulieres
============================================================================
Pour analyser des videos de trading (TikTok, Insta Reels, YouTube Shorts) :
- telecharge la video avec yt-dlp
- extrait une image toutes les N secondes avec ffmpeg
- tu uploades ensuite les images dans le chat Claude pour analyse

PREREQUIS :
  1) pip install yt-dlp        (deja dans requirements.txt)
  2) ffmpeg installe sur le systeme :
     - Windows : telecharge sur https://ffmpeg.org/download.html
       (la version "essentials" Gyan.dev ; decompresse, mets le dossier bin
        dans le PATH, ou place ffmpeg.exe dans C:\\Users\\Bektas\\GOLD)
     - Verifie : tape `ffmpeg -version` dans PowerShell.

USAGE :
  python extract_video_frames.py <URL> <DOSSIER_SORTIE> [--interval 20]

EXEMPLE :
  python extract_video_frames.py "https://www.tiktok.com/@trader/video/123" .\\trader_x --interval 20

Sortie :
  <dossier>/video.mp4
  <dossier>/frames/frame_0001.jpg, frame_0002.jpg, ...

NOTE LEGALE : telechargement pour analyse personnelle/etude.
Ne pas redistribuer le contenu, respecter les droits du createur.
"""

import sys
import os
import shutil
import argparse
import subprocess
from pathlib import Path


# Appelle yt-dlp via le module Python (pas besoin de l'exe dans le PATH)
YT_DLP_CMD = [sys.executable, "-m", "yt_dlp"]


def find_ffmpeg():
    """Cherche ffmpeg (.exe sous Windows) dans : PATH, dossier courant,
    dossier du script. Renvoie le chemin trouve ou None."""
    # 1) PATH systeme
    found = shutil.which("ffmpeg")
    if found:
        return found
    # 2) Dossier courant et dossier du script
    candidates = [Path.cwd(), Path(__file__).resolve().parent]
    names = ["ffmpeg.exe", "ffmpeg"]
    for d in candidates:
        for n in names:
            p = d / n
            if p.is_file():
                return str(p)
    return None


FFMPEG = find_ffmpeg()
FFMPEG_CMD = [FFMPEG] if FFMPEG else ["ffmpeg"]


def check_tool(cmd, name, install_hint, version_flag="--version"):
    """Verifie qu'un outil est appelable. ffmpeg utilise '-version' (un seul
    tiret), yt-dlp utilise '--version' (deux tirets) -> param a passer."""
    try:
        r = subprocess.run(cmd + [version_flag], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            first_line = r.stdout.splitlines()[0] if r.stdout else r.stderr.splitlines()[0]
            print(f"  {name} : {first_line[:70]}")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    print(f"  /!\\ {name} introuvable. {install_hint}")
    return False


def download_video(url, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = out_dir / "video.mp4"

    if video_path.exists():
        print(f"  Video deja presente : {video_path} (on saute le telechargement)")
        return video_path

    print(f"Telechargement : {url}")
    cmd = YT_DLP_CMD + ["-o", str(video_path),
                        "-f", "mp4/best",     # privilegie mp4 pour ffmpeg
                        "--no-playlist",
                        url]
    r = subprocess.run(cmd, text=True)
    if r.returncode != 0 or not video_path.exists():
        print("ERREUR : yt-dlp a echoue. Verifie l'URL et ta connexion.")
        sys.exit(1)
    print(f"Sauve : {video_path}")
    return video_path


def extract_frames(video_path, out_dir, interval_sec=20):
    frames_dir = Path(out_dir) / "frames"
    frames_dir.mkdir(exist_ok=True)

    # Nettoie les anciennes frames si on relance
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()

    print(f"Extraction : 1 image toutes les {interval_sec}s...")
    cmd = FFMPEG_CMD + [
        "-y", "-i", str(video_path),
        "-vf", f"fps=1/{interval_sec}",
        "-q:v", "2",                  # qualite JPG haute
        str(frames_dir / "frame_%04d.jpg")
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ERREUR ffmpeg :\n{r.stderr[-500:]}")
        sys.exit(1)

    frames = sorted(frames_dir.glob("frame_*.jpg"))
    if not frames:
        print("ERREUR : aucune image extraite (video trop courte ? intervalle trop grand ?)")
        sys.exit(1)

    # Petit recap de duree
    sizes = [f.stat().st_size for f in frames]
    print(f"\n{len(frames)} images sauvees dans {frames_dir}")
    print(f"  Premiere : {frames[0].name}  (≈ t=0s)")
    print(f"  Derniere : {frames[-1].name}  (≈ t={(len(frames)-1)*interval_sec}s)")
    print(f"  Taille moyenne : {sum(sizes)//len(sizes)//1024} Ko/image")
    return frames


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="URL de la video (TikTok, Insta Reel, YouTube...)")
    parser.add_argument("output_dir", help="Dossier de sortie (sera cree si absent)")
    parser.add_argument("--interval", "-i", type=int, default=20,
                        help="Intervalle en secondes entre 2 images (defaut: 20)")
    args = parser.parse_args()

    print("Verification des outils...")
    ok_ytdlp  = check_tool(YT_DLP_CMD, "yt-dlp", "Installation : pip install yt-dlp",
                           version_flag="--version")
    hint = "Place ffmpeg.exe dans " + str(Path(__file__).resolve().parent)
    ok_ffmpeg = check_tool(FFMPEG_CMD, "ffmpeg", hint, version_flag="-version")
    if not (ok_ytdlp and ok_ffmpeg):
        sys.exit(1)
    print()

    video = download_video(args.url, args.output_dir)
    extract_frames(video, args.output_dir, args.interval)

    print("\n" + "=" * 60)
    print(" PROCHAINE ETAPE")
    print("=" * 60)
    print(" Uploade les images dans le chat Claude (drag-and-drop) avec :")
    print(" - Le nom du trader / handle TikTok")
    print(" - L'instrument (GOLD, XAUUSD, etc.) et la timeframe vue a l'ecran")
    print(" - Un resume bref de ce qu'il dit a l'oral (si tu veux qu'on integre)")
    print(" Je te fais une analyse par frame + synthese de la strategie.")


if __name__ == "__main__":
    main()

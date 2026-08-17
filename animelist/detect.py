"""M1 CLI: connect to mpv IPC, parse titles, resolve via MAL, print results.

Usage:
    python -m animelist.detect             # watch until Ctrl-C
    python -m animelist.detect --once      # stop after the first file event
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .ipc import watch_socket
from .mal import MALClient
from .parser import parse_title
from .store import Store, get_release_tags


def resolve_and_report(filename: str, mal: MALClient, store: Store, tags: list[str]) -> None:
    parsed = parse_title(filename, tags)
    if parsed is None:
        return
    label = parsed.anime_name
    if parsed.kind == "episode":
        label += f"  EP {parsed.episode}"
    elif parsed.kind == "batch":
        label += f"  EP {parsed.episode}-{parsed.episode_end}"
    print(f"[detect] {parsed.kind}: {label}")

    try:
        results = mal.search(parsed.anime_name, limit=5)
    except Exception as exc:  # network or API error
        print(f"  ! search failed: {exc}")
        return
    if not results:
        print("  ! no MAL results for this title")
        return

    top = results[0]
    print(f"  -> MAL {top['id']} | {top['title']} | {top['media_type'] or '?'}")
    store.upsert_anime(
        mal_id=top["id"],
        title=top["title"],
        media_type=top["media_type"],
        image_path=top["image_url"],
        mal_url=f"https://myanimelist.net/anime/{top['id']}",
    )
    if parsed.episode is not None:
        print(f"  -> episode {parsed.episode} already watched: {store.is_watched(top['id'], parsed.episode)}")
    elif parsed.kind == "movie":
        print(f"  -> movie already watched: {store.is_watched(top['id'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="stop after the first file event")
    args = parser.parse_args(argv)

    cfg = config.load_config()
    if not cfg.get("mal_client_id"):
        print("Missing MAL client ID.", file=sys.stderr)
        print(f"Put it in {config.CONFIG_FILE} as {{\"mal_client_id\": \"...\"}}", file=sys.stderr)
        return 1

    mal = MALClient()
    store = Store()
    tags = get_release_tags(store)
    sockets = cfg["mpv_sockets"]
    print(f"Watching mpv socket(s): {', '.join(sockets)}")
    print(f"Release tags: {tags or '(none — nothing will be tracked)'}")
    print(f"Release tags: {tags or '(none — nothing will be tracked)'}")

    last_file: str | None = None
    try:
        for event in watch_socket(sockets, reconnect=True):
            if event["type"] == "connected":
                print(f"[ipc] connected via {event.get('socket')}")
            elif event["type"] == "disconnected":
                print("[ipc] mpv closed, waiting for it to come back ...")
            elif event["type"] == "file":
                fname = event["filename"]
                if fname == last_file:
                    continue
                last_file = fname
                print(f"[ipc] file: {fname}")
                resolve_and_report(fname, mal, store, tags)
                if args.once:
                    return 0
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

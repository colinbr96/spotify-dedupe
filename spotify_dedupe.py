#!/usr/bin/env python3
"""
spotify_dedupe.py - find and remove duplicate tracks in your Spotify playlists.

Nothing is changed until you confirm. Before any change, the full contents of
every scanned playlist are saved to a JSON backup file.

Modes
  within  (default)  duplicates inside the same playlist; keeps the first copy
  across             the same track in several playlists; keeps the copy that
                     was added earliest and removes it from the other playlists
  both               within first, then across

Matching
  default    exact same Spotify track/episode (same URI)
  --liked    also check Liked Songs (always fuzzy: the exact same track can't be
             liked twice, but the album and single versions can both be liked).
             Keeps the version you liked first and un-likes the others.
  --fuzzy    also treats "same title + same main artist + duration within 3s"
             as a duplicate (catches album vs. single vs. remaster copies)

Setup (one time)
  1. https://developer.spotify.com/dashboard -> Create app
     - Redirect URI:  http://127.0.0.1:8888/callback
     - API:           Web API
  2. Copy the app's Client ID, then:
       export SPOTIFY_CLIENT_ID=your_client_id
  3. pip install requests
  4. python spotify_dedupe.py                 # scan + confirm, within playlists
     python spotify_dedupe.py --mode across   # across playlists
     python spotify_dedupe.py --fuzzy --dry-run
     python spotify_dedupe.py --liked-only --dry-run

Notes
  - Only playlists you own or collaborate on can be read/changed (Spotify rule).
  - Spotify's Development Mode requires the app owner to have Premium.
  - Local files can't be removed through the API, so they are skipped.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from collections import defaultdict
from datetime import datetime

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This script needs the 'requests' package:  pip install requests")

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = ("playlist-read-private playlist-read-collaborative "
          "playlist-modify-public playlist-modify-private "
          "user-library-read user-library-modify")
LIKED_ID = "__liked_songs__"
TOKEN_CACHE = os.path.expanduser("~/.spotify_dedupe_token.json")
FUZZY_DURATION_MS = 3000


# --------------------------------------------------------------------------
# Authentication (Authorization Code + PKCE: no client secret needed)
# --------------------------------------------------------------------------

class SpotifyAuth:
    def __init__(self, client_id):
        self.client_id = client_id
        self.token = self._load()

    def _load(self):
        try:
            with open(TOKEN_CACHE) as f:
                tok = json.load(f)
            granted = set((tok.get("scope") or "").split())
            if tok.get("client_id") == self.client_id and set(SCOPES.split()) <= granted:
                return tok  # otherwise log in again to grant new permissions
        except (OSError, ValueError):
            pass
        return None

    def _save(self, tok):
        tok["client_id"] = self.client_id
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        if self.token:
            tok.setdefault("refresh_token", self.token.get("refresh_token"))
            tok.setdefault("scope", self.token.get("scope"))
        self.token = tok
        fd = os.open(TOKEN_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)

    def access_token(self):
        if self.token and time.time() < self.token.get("expires_at", 0):
            return self.token["access_token"]
        if self.token and self.token.get("refresh_token"):
            r = requests.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": self.token["refresh_token"],
                "client_id": self.client_id,
            }, timeout=30)
            if r.ok:
                self._save(r.json())
                return self.token["access_token"]
        self._login()
        return self.token["access_token"]

    def _login(self):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code",
            "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state,
            "code_challenge_method": "S256", "code_challenge": challenge,
        })

        result = {}
        parsed = urllib.parse.urlparse(REDIRECT_URI)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" in q or "error" in q:
                    result.update({k: v[0] for k, v in q.items()})
                    msg = "Logged in. You can close this tab." if "code" in q \
                        else "Login failed: " + q["error"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(msg.encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print("\nOpening your browser to log in to Spotify...")
        print("If it doesn't open, visit:\n  " + url + "\n")
        webbrowser.open(url)
        while not result:
            time.sleep(0.2)
        server.shutdown()

        if "error" in result:
            sys.exit("Spotify login failed: " + result["error"])
        if result.get("state") != state:
            sys.exit("Login state mismatch; aborting.")
        r = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": REDIRECT_URI, "client_id": self.client_id,
            "code_verifier": verifier,
        }, timeout=30)
        if not r.ok:
            sys.exit("Token exchange failed: " + r.text)
        self._save(r.json())


# --------------------------------------------------------------------------
# Minimal API client
# --------------------------------------------------------------------------

class Spotify:
    def __init__(self, auth):
        self.auth = auth
        self.s = requests.Session()

    def request(self, method, path, **kw):
        url = path if path.startswith("http") else API + path
        for attempt in range(6):
            headers = {"Authorization": "Bearer " + self.auth.access_token()}
            r = self.s.request(method, url, headers=headers, timeout=30, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5")) + 1
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 401 and attempt == 0:
                self.auth.token["expires_at"] = 0  # force refresh
                continue
            if r.status_code >= 500 and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"{method} {path} failed after retries")

    def paged(self, path, params=None):
        data = self.request("GET", path, params=params)
        while True:
            yield from data.get("items") or []
            if not data.get("next"):
                break
            data = self.request("GET", data["next"])

    def me(self):
        return self.request("GET", "/me")

    def my_playlists(self):
        return list(self.paged("/me/playlists", {"limit": 50}))

    def playlist_items(self, pid):
        return self._entries(self.paged(f"/playlists/{pid}/items", {"limit": 50}))

    def liked_tracks(self):
        """Liked Songs, newest first (the order Spotify shows them)."""
        return self._entries(self.paged("/me/tracks", {"limit": 50}))

    def unlike(self, uris):
        uris = list(uris)
        for i in range(0, len(uris), 40):  # endpoint accepts at most 40 URIs
            self.request("DELETE", "/me/library", params={"uris": ",".join(uris[i:i + 40])})

    @staticmethod
    def _entries(entries):
        out = []
        for pos, entry in enumerate(entries):
            item = entry.get("item") or entry.get("track")  # 'track' = pre-2026 name
            out.append({
                "pos": pos,
                "uri": (item or {}).get("uri"),
                "name": (item or {}).get("name") or "(unavailable)",
                "artists": [a.get("name", "") for a in (item or {}).get("artists") or []]
                           or [((item or {}).get("show") or {}).get("name", "")],
                "duration_ms": (item or {}).get("duration_ms") or 0,
                "is_local": bool(entry.get("is_local") or (item or {}).get("is_local")),
                "added_at": entry.get("added_at") or "",
            })
        return out

    def remove_uris(self, pid, uris, snapshot_id=None):
        """Removes ALL occurrences of each URI. Returns the new snapshot id."""
        uris = list(uris)
        for i in range(0, len(uris), 100):
            body = {"items": [{"uri": u} for u in uris[i:i + 100]]}
            if snapshot_id:
                body["snapshot_id"] = snapshot_id
            snapshot_id = self.request("DELETE", f"/playlists/{pid}/items",
                                       json=body).get("snapshot_id", snapshot_id)
        return snapshot_id

    def add_uri(self, pid, uri, position):
        return self.request("POST", f"/playlists/{pid}/items",
                            json={"uris": [uri], "position": position}).get("snapshot_id")


# --------------------------------------------------------------------------
# Duplicate detection (pure functions, no network)
# --------------------------------------------------------------------------

_VERSION_WORDS = r"remaster(ed)?|\d{4} remaster|deluxe|mono|stereo|bonus track|single version|album version"
_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*(" + _VERSION_WORDS + r")[^\)\]]*[\)\]]", re.I)
_DASH_RE = re.compile(r"\s+-\s+.*(" + _VERSION_WORDS + r").*$", re.I)


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = _PAREN_RE.sub("", s)
    s = _DASH_RE.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def removable(t):
    return t["uri"] and not t["is_local"] and not t["uri"].startswith("spotify:local:")


def cluster(tracks, fuzzy):
    """Group tracks that count as the same song. Returns a list of groups
    (each a list of tracks, in input order) with 2+ members."""
    by_uri = defaultdict(list)
    for t in tracks:
        if removable(t):
            by_uri[t["uri"]].append(t)
    if not fuzzy:
        return [g for g in by_uri.values() if len(g) > 1]

    # Fuzzy: union URI groups whose (title, main artist) match and durations are close.
    buckets = defaultdict(list)
    for uri, g in by_uri.items():
        t = g[0]
        key = (_norm(t["name"]), _norm(t["artists"][0] if t["artists"] else ""))
        if key[0]:
            buckets[key].append(uri)
        else:
            buckets[("uri", uri)].append(uri)

    groups = []
    order = {id(t): i for i, t in enumerate(tracks)}
    for uris in buckets.values():
        uris.sort(key=lambda u: by_uri[u][0]["duration_ms"])
        current = [uris[0]]
        for u in uris[1:]:
            if by_uri[u][0]["duration_ms"] - by_uri[current[-1]][0]["duration_ms"] <= FUZZY_DURATION_MS:
                current.append(u)
            else:
                groups.append(current)
                current = [u]
        groups.append(current)
    out = []
    for uris in groups:
        members = sorted((t for u in uris for t in by_uri[u]), key=lambda t: order[id(t)])
        if len(members) > 1:
            out.append(members)
    return out


def plan_within(playlist):
    """Positions to remove inside one playlist: every copy except the first."""
    removals = []
    for g in cluster(playlist["tracks"], playlist["_fuzzy"]):
        keep, *drop = sorted(g, key=lambda t: t["pos"])
        for t in drop:
            removals.append({"pos": t["pos"], "track": t, "kept_pos": keep["pos"],
                             "reason": f"duplicate of #{keep['pos'] + 1} in same playlist"})
    return removals


def plan_liked(liked):
    """Liked Songs can't hold the same URI twice, so only fuzzy matches apply.
    Keeps the version you liked first; un-likes the others."""
    removals = []
    for g in cluster(liked["tracks"], fuzzy=True):
        keep = min(g, key=lambda t: (t["added_at"] or "9999", -t["pos"]))
        for t in g:
            if t is not keep:
                removals.append({"pos": t["pos"], "track": t, "kept_pos": keep["pos"],
                                 "reason": f"same song as #{keep['pos'] + 1}: "
                                           f"{fmt(keep)} (liked earlier, kept)"})
    return removals


def plan_across(playlists, already_removed, fuzzy):
    """For songs in several playlists, keep the earliest-added copy and remove
    the song from every other playlist. Returns {playlist_id: [removal,...]}."""
    flat = []
    for pl in playlists:
        gone = already_removed.get(pl["id"], set())
        for t in pl["tracks"]:
            if t["pos"] not in gone:
                flat.append(dict(t, _pl=pl))
    out = defaultdict(list)
    for g in cluster(flat, fuzzy):
        pls = {t["_pl"]["id"] for t in g}
        if len(pls) < 2:
            continue
        keep = min(g, key=lambda t: (t["added_at"] or "9999", playlists.index(t["_pl"]), t["pos"]))
        for t in g:
            if t["_pl"]["id"] != keep["_pl"]["id"]:
                out[t["_pl"]["id"]].append({
                    "pos": t["pos"], "track": t, "kept_pos": None,
                    "reason": f"also in '{keep['_pl']['name']}' (kept there)"})
    return out


def final_order(tracks, remove_positions):
    return [t for t in tracks if t["pos"] not in remove_positions]


def apply_plan(sp, playlist, removals):
    """Apply removals to one playlist.

    For Liked Songs, removing just un-likes those tracks.

    The API can only remove *every* occurrence of a URI, so: delete all
    affected URIs, then re-insert the copies we want to keep at their final
    positions (ascending order makes each target index valid as we go)."""
    remove_pos = {r["pos"] for r in removals}
    if playlist["id"] == LIKED_ID:
        sp.unlike(r["track"]["uri"] for r in removals)
        return len(remove_pos), 0
    affected = {r["track"]["uri"] for r in removals}
    final = final_order(playlist["tracks"], remove_pos)
    reinserts = [(i, t["uri"]) for i, t in enumerate(final) if t["uri"] in affected]

    snap = sp.remove_uris(playlist["id"], affected, playlist.get("snapshot_id"))
    for idx, uri in reinserts:
        snap = sp.add_uri(playlist["id"], uri, idx)
    return len(remove_pos), len(reinserts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def fmt(t):
    artists = ", ".join(a for a in t["artists"] if a) or "?"
    secs = t["duration_ms"] // 1000
    return f"{t['name']} - {artists} ({secs // 60}:{secs % 60:02d})"


def ask(prompt, choices):
    while True:
        a = input(prompt).strip().lower()
        if a in choices:
            return a
        print("  please answer one of: " + "/".join(choices))


def main():
    ap = argparse.ArgumentParser(description="Find and remove duplicate tracks in your Spotify playlists.")
    ap.add_argument("--mode", choices=["within", "across", "both"], default="within")
    ap.add_argument("--fuzzy", action="store_true",
                    help="also match same title + main artist + similar length")
    ap.add_argument("--liked", action="store_true",
                    help="also check Liked Songs for the same song liked from different releases")
    ap.add_argument("--liked-only", action="store_true", help="check only Liked Songs")
    ap.add_argument("--dry-run", action="store_true", help="only report, never change anything")
    ap.add_argument("--yes", action="store_true", help="skip per-playlist prompts (still asks once)")
    ap.add_argument("--only", nargs="*", default=[], help="only scan playlists with exactly one of these names (case-insensitive)")
    ap.add_argument("--exclude", nargs="*", default=[], help="skip playlists whose name contains any of these")
    args = ap.parse_args()

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        sys.exit("Set SPOTIFY_CLIENT_ID first (see the setup notes at the top of this file).")

    sp = Spotify(SpotifyAuth(client_id))
    me = sp.me()
    print(f"Logged in as {me.get('display_name') or me['id']}")

    playlists = []
    for p in ([] if args.liked_only else sp.my_playlists()):
        if not p:
            continue
        name = p.get("name") or ""
        mine = (p.get("owner") or {}).get("id") == me["id"] or p.get("collaborative")
        if not mine:
            continue
        low = name.lower()
        if args.only and not any(o.strip().lower() == low.strip() for o in args.only):
            continue
        if any(x.lower() in low for x in args.exclude):
            continue
        playlists.append({"id": p["id"], "name": name, "snapshot_id": p.get("snapshot_id"),
                          "_fuzzy": args.fuzzy})

    print(f"Scanning {len(playlists)} playlists you own or collaborate on...")
    for i, pl in enumerate(playlists, 1):
        try:
            pl["tracks"] = sp.playlist_items(pl["id"])
        except RuntimeError as e:
            print(f"  ! skipping '{pl['name']}': {e}")
            pl["tracks"] = []
        print(f"  [{i}/{len(playlists)}] {pl['name']}: {len(pl['tracks'])} items")
    playlists = [p for p in playlists if p["tracks"]]

    liked = None
    if args.liked or args.liked_only:
        print("  Liked Songs: loading...")
        liked = {"id": LIKED_ID, "name": "Liked Songs", "tracks": sp.liked_tracks()}
        print(f"  Liked Songs: {len(liked['tracks'])} items")

    backup = f"spotify_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(backup, "w") as f:
        json.dump([{k: v for k, v in p.items() if not k.startswith("_")}
                   for p in playlists + ([liked] if liked else [])],
                  f, indent=1, ensure_ascii=False)
    print(f"\nBackup of all scanned playlists saved to {backup}")

    plan = defaultdict(list)
    if args.mode in ("within", "both"):
        for pl in playlists:
            plan[pl["id"]].extend(plan_within(pl))
    if args.mode in ("across", "both"):
        removed = {pid: {r["pos"] for r in rs} for pid, rs in plan.items()}
        for pid, rs in plan_across(playlists, removed, args.fuzzy).items():
            plan[pid].extend(rs)

    if liked:
        plan[LIKED_ID] = plan_liked(liked)

    targets = [(pl, sorted(plan[pl["id"]], key=lambda r: r["pos"]))
               for pl in playlists + ([liked] if liked else []) if plan.get(pl["id"])]
    total = sum(len(rs) for _, rs in targets)
    if not total:
        print("\nNo duplicates found.")
        return

    print(f"\nFound {total} duplicate(s) in {len(targets)} playlist(s):")
    for pl, rs in targets:
        print(f"\n== {pl['name']}  ({len(rs)} to remove)")
        for r in rs:
            print(f"   #{r['pos'] + 1:<4} {fmt(r['track'])}\n          -> {r['reason']}")

    if args.dry_run:
        print("\nDry run: nothing changed.")
        return

    if ask(f"\nRemove these {total} duplicate(s)? [y/n] ", ["y", "n"]) != "y":
        print("Nothing changed.")
        return

    done = 0
    apply_all = args.yes
    for pl, rs in targets:
        if not apply_all:
            a = ask(f"\n'{pl['name']}': remove {len(rs)}? [y]es / [n]o / [a]ll remaining / [q]uit ",
                    ["y", "n", "a", "q"])
            if a == "q":
                break
            if a == "n":
                continue
            if a == "a":
                apply_all = True
        try:
            removed_n, readded = apply_plan(sp, pl, rs)
            done += removed_n
            print(f"  ✓ {pl['name']}: removed {removed_n}")
        except RuntimeError as e:
            print(f"  ✗ {pl['name']}: {e}\n    (restore from {backup} if needed)")

    print(f"\nDone. Removed {done} duplicate(s). Backup: {backup}")


if __name__ == "__main__":
    main()

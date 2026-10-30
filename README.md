# spotify-dedupe

Find and remove duplicate tracks in your Spotify playlists (and optionally Liked Songs).
Nothing changes until you confirm, and every scanned playlist is backed up to a JSON file first.

## Setup

1. Create an app at https://developer.spotify.com/dashboard
   - Redirect URI: `http://127.0.0.1:8888/callback`
   - API: Web API
2. Install [uv](https://docs.astral.sh/uv/)
3. Set your app's Client ID: `export SPOTIFY_CLIENT_ID=...` (PowerShell: `$env:SPOTIFY_CLIENT_ID="..."`)

## Usage

```bash
uv run spotify_dedupe.py --list                           # playlist names, for --only
uv run spotify_dedupe.py --only "My Playlist" --dry-run   # preview one playlist
uv run spotify_dedupe.py --only "My Playlist"             # remove after confirming
uv run spotify_dedupe.py --mode across --fuzzy --dry-run  # same song across playlists
uv run spotify_dedupe.py --liked-only --dry-run           # Liked Songs only
```

See the docstring at the top of `spotify_dedupe.py` for all modes and options.

## Development

```bash
uv run ruff format      # format
uv run ruff check       # lint
uv run ty check         # typecheck
```

## License

[MIT](LICENSE)

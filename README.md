# StepTube

A personal, calm YouTube, built in the spirit of Karpathy's
[HackerTube](https://hackertube.org) but curated by **my own channel list**
instead of Hacker News comments.

No algorithm, no Shorts, no view counts, no autoplay, no infinite feed.
`channels.yml` is the entire recommendation engine.

## How it works

```
channels.yml ─┐
classics.yml ─┼─> scripts/build.py ──> site/data/videos.json ──> static site (GitHub Pages)
              │     (daily GitHub Action)
YouTube RSS ──┘   no API keys needed
```

1. **Sources.** Every YouTube channel publishes a public RSS feed of its latest
   ~15 uploads. `build.py` reads it for each channel in `channels.yml`. Channels
   can be given by `handle`, `channel_id`, or display `name` (resolved via
   YouTube search; check `site/data/build_report.json` for any marked `check`).
2. **Filters.**
   - **Shorts** are dropped (feed link and a `/shorts/` probe).
   - **Live streams and premieres** are dropped (0-view placeholders and live titles).
   - **Clickbait** is flagged by title heuristics and hidden by default.
   - **Titles** are de-shouted: ALL CAPS words are recased and emoji are stripped.
3. **Library grows over time.** Each run merges into the existing
   `videos.json` (up to 300 per channel), so the archive accumulates beyond what
   RSS shows.
4. **Classics.** `classics.yml` pins evergreen videos forever.

## Design: low dopamine, still comfortable

- Warm, low-glare palette (dark or light, follows the system). No red, no badges.
- **Grayscale thumbnails** by default (Settings: muted / color / off), so titles compete on substance.
- A list layout instead of a thumbnail wall, plus a short description snippet.
- **Finite pages.** "Show 20 more" is a deliberate click. The New page ends with *You're caught up.*
- The player shows no related videos. When a video ends you see *Finished. Good time to pause.*,
  and the only next step offered is your own Watch later queue.
- **Daily budget.** You set the minutes. Past the budget you get a gentle note, never a block.
- **Sort options:** newest, oldest, or *Today's shuffle* (stable for the day, so refreshing isn't a slot machine).
- **Under review** tab: probation for channels you're unsure about. Their videos stay off the New page.

## Personal features (stored in your browser only)

Watch later, likes, history, watched marks, per-video **notes** (searchable),
and per-device channel muting. Export and import live in Settings.

## Keyboard

| Key | Action |
| --- | --- |
| `/` | search |
| `j` / `k` | move |
| `Enter` / `o` | open |
| `w` | watch later |
| `l` | like |
| `x` | watched |
| `0`–`9` | topics |
| `g` then `l`/`h`/`c`/`s`/`k` | Later / History / Channels / Settings / Liked |
| `?` | help |
| `Esc` | back |

## Setup

1. Merge to `main`.
2. Repo **Settings → Pages → Source: GitHub Actions**.
3. **Actions → Update StepTube → Run workflow**. After that it runs daily.

Run it locally:

```bash
pip install pyyaml
python scripts/build.py          # needs internet access to youtube.com
cd site && python -m http.server # open http://localhost:8000
```

## Editing your taste

- Add a channel: append `- { handle: SomeChannel, topic: ai }` to `channels.yml`.
- Remove a channel: delete its line. Its videos disappear on the next build.
- A name resolved to the wrong channel: replace `name:` with `handle:` or `channel_id:`.
- Narrow a noisy channel: use `include: ["lecture"]` or `exclude: ["podcast"]`.

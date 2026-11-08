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
4. **Full archives.** The first time a channel is seen, its whole /videos list is paged through (up to 300 videos); after that, only new uploads are fetched. Old videos get approximate dates ("4y ago" becomes about four years back). `site/data/backfill.json` records which channels are done; delete an entry to re-fetch.
5. **Classics.** `classics.yml` pins evergreen videos forever.

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

## Search (works like YouTube's, locally)

YouTube search has three stages: **query understanding** (normalising, spelling
correction), **retrieval** from both a keyword index and an embedding
("semantic") index, and **ranking**. Ranking puts relevance first, then quality
and engagement signals, freshness and personalisation. StepTube does the same
in your browser (`site/search.js`):

| Stage | YouTube | StepTube |
| --- | --- | --- |
| Query understanding | spell correction, synonyms | typo tolerance (edit distance 1–2), prefix match as you type, crude stemming for Russian/Armenian inflections, "showing results for" |
| Keyword retrieval | inverted index | BM25F over title ×3, channel ×2, description ×1 |
| Semantic retrieval | deep embeddings | `multilingual-e5-small`. Videos are embedded nightly in the Action, and your query is embedded in the browser with the same model (transformers.js, about 120 MB, downloaded once) |
| Ranking | relevance × watch time, CTR, freshness, your history | 0.5 keyword + 0.5 meaning, then × view count, × similarity to what you liked/watched, × channels you watch, × recency |
| Autocomplete | query log | channel names and title prefixes |

What it can't copy: YouTube's click and watch-time data from billions of users,
and full video transcripts. The index is about 128 bytes per video (PCA from 384
to 128 dimensions, int8). Turn smart search off in Settings to stay keyword-only.

## For you (recommendations)

A YouTube-style home feed, computed in your browser from your own history and kept calm:

- **Because you watched ‹title›:** the nearest unwatched neighbours of your last 8 watched videos and latest likes, using the same embeddings as search.
- **New from channels you watch:** recent uploads from the channels you actually watch.
- **Picked for you:** older videos close to your overall taste.

Picks are computed **once a day** and don't reshuffle on refresh. You can also:
- Press **✕** (or `n`) to mark a video **Not interested**, which hides it and anything nearly identical.
- Use `g f` to open the tab.

## Claude Code tab

Every hour the build runs the YouTube searches in `channels.yml` (`claude_code:`), in English and Russian. It keeps only videos that:
- mention Claude Code in the title,
- are at least 2 minutes long (no Shorts),
- aren't live streams or premieres,
- are in English or Russian.

Clickbait is hidden like everywhere else. These videos come from any channel, so they stay off the New page, but they appear in search and For you.

## Update schedule

The Action runs **every hour** (`17 * * * *`). GitHub sometimes starts scheduled runs a few minutes late. View counts are rounded and the timestamp is kept when nothing changed, so a quiet hour makes no commit.

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
3. **Actions → Update StepTube → Run workflow**. After that it runs every hour.

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

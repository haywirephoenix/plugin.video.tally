# Tally (plugin.video.tally)

A Kodi 21 video add-on that tracks what you watch without a server. Watched status, resume
points, Next Up and your watchlist live in a small SQLite database in the add-on's profile
(`addon_data/plugin.video.tally/tally.db`). TMDb (a free API key in settings) supplies artwork,
details, search, the trending / popular / top-rated lists, genres and seasons.

Pure Python standard library and Kodi modules only, so it runs on low-powered boxes such as a
webOS TV. Tests need no Kodi: `python -m unittest discover tests`.

## Routes

Same shapes as Floppy's (`plugin.video.floppy`), so a skin can point at either.

| Route | What |
| --- | --- |
| `?action=list&cat=<cat>&widget=1&limit=N` | a list (below); `&page=N` for later pages |
| `?action=search&media_type=movie\|tv\|both\|person\|collection&query=...` | TMDb search, with your watched state |
| `?action=genre&media_type=movie\|tv\|both&genres=A / B&language=ja&keywords=6075&page=N` | popular TMDb titles by genre |
| `?action=genres`, `?action=discover` | genre folders; Discover menu |
| `?action=seasons&tmdb=ID`, `?action=episodes&tmdb=ID&season=N` | a show's seasons and episodes |
| `?action=play&kind=movie\|episode&tmdb=ID[&season=S&episode=E]` | hand off to the chosen player |
| `?action=add_watchlist\|remove_watchlist&media_type=movie\|tv&tmdb=ID` | watchlist |
| `?action=mark_watched\|unwatch\|clear_resume&kind=movie\|episode&tmdb=ID[&season&episode]` | item actions |
| `?action=mark_season_watched&tmdb=ID&season=N`, `?action=mark_show_watched&tmdb=ID` | bulk |
| `?action=untrack&media_type=movie\|tv&tmdb=ID`, `?action=refresh` | forget a title; reload widgets |

Categories: `continue_watching`, `all_next_up`, `all_in_progress`;
`movies_continue`, `movies_trending`, `movies_popular`, `movies_top_rated`, `movies_greats`,
`movies_coming_soon`, `movies_watchlist`, `movies_in_progress`, `movies_history`, `movies_completed`;
`tv_next_up`, `tv_continue`, `tv_trending`, `tv_popular`, `tv_top_rated`, `tv_greats`, `tv_on_the_air`,
`tv_watching`, `tv_in_progress`, `tv_watchlist`, `tv_history`, `tv_completed`.

## Item properties

- `tally.play_path` (movies, episodes): Tally's play route; resume lists carry the position.
- `tally.seasons_path`, `tally.next_play_path`, `tally.next_label` (`S01E02`), `tally.episodes_left` (shows).
- `tally.status` (movies, shows): `0` watchlist, `1` in progress, `3` watched; unset = not tracked.
- `tally.add_watchlist_path`, `tally.remove_watchlist_path` (movies, shows).
- `tmdb_id`, `tmdb_type`; on episodes `tvshow.tmdb_id`, `tvshow.imdb_id`, `tvshow.tvdb_id`.

Home window properties: `tally.widget_reload` changes whenever widget data changes (add
`&reload=$INFO[Window(Home).Property(tally.widget_reload)]` to widget paths). A skin that starts
playback itself can name what plays with `tally.play.kind|tmdb|imdb|tvdb|season|episode|title|resume`;
playback started elsewhere is still tracked when its TMDb id can be found.

## Database

`resources/lib/store.py`, versioned with `PRAGMA user_version`. Add a migration by appending
SQL to `MIGRATIONS`; never edit an existing one.

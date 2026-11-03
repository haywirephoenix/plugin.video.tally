# Tally (plugin.video.tally)

<p align="center">
  <img src="https://raw.githubusercontent.com/haywirephoenix/plugin.video.tally/refs/heads/main/resources/icon.png" width="50%">
</p>

A Kodi 21 video add-on that tracks what you watch without a server. Watched status, resume
points, Next Up and your watchlist live in a small database inside Kodi, and your Kodi devices at
home can keep each other in step ([Sync](https://github.com/haywirephoenix/plugin.video.tally/wiki/Sync)).
TMDb (a free API key in settings) supplies artwork, details, search, the trending / popular /
top-rated lists, genres and seasons.

Pure Python standard library and Kodi modules only, so it runs on low-powered boxes such as a
webOS TV. Tests need no Kodi: `python -m unittest discover tests`.

## Documentation

Everything is in the [wiki](https://github.com/haywirephoenix/plugin.video.tally/wiki):

- [Getting started](https://github.com/haywirephoenix/plugin.video.tally/wiki/Getting-started) and [Settings](https://github.com/haywirephoenix/plugin.video.tally/wiki/Settings)
- [Lists and widgets](https://github.com/haywirephoenix/plugin.video.tally/wiki/Lists-and-widgets)
- [Sync](https://github.com/haywirephoenix/plugin.video.tally/wiki/Sync) between devices
- [Importing history](https://github.com/haywirephoenix/plugin.video.tally/wiki/Import) and [TMDb Helper](https://github.com/haywirephoenix/plugin.video.tally/wiki/TMDb-Helper)
- [Skin integration](https://github.com/haywirephoenix/plugin.video.tally/wiki/Skin-integration): routes, item properties, window properties
- [Development](https://github.com/haywirephoenix/plugin.video.tally/wiki/Development): the database, migrations, how sync works

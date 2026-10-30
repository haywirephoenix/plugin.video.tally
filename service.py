"""Tally background service entry point (tracks playback, keeps shows current)."""

from resources.lib import player_service

if __name__ == "__main__":
    player_service.run()

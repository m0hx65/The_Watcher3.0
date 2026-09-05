#!/data/data/com.termux/files/usr/bin/sh
# The Watcher - keeps the home page fetcher running on an Android phone in
# Termux. Also used as the Termux:Boot script so it starts when the phone
# boots: copy it to ~/.termux/boot/start.sh (see the README).
#
# Settings come from ~/home_fetcher/home_fetcher.env (WATCHER_URL and
# HOME_FETCH_TOKEN). The wake lock keeps Android from suspending Termux while
# the screen is off; MIUI also needs "No restrictions" for Termux's battery
# saver, or it kills the process anyway.
termux-wake-lock
cd "$HOME/home_fetcher" || exit 1
while true; do
  python home_fetcher.py
  echo "home_fetcher exited - restarting in 10 seconds"
  sleep 10
done

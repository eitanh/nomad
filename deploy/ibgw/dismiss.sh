#!/bin/bash
# Background watcher: auto-dismiss IBKR Gateway nag dialogs that block headless
# startup. Xvfb runs with -ac so we can drive DISPLAY=:1 locally. Pressing Return
# activates the default ("OK"/"I understand") button. Loops forever so it also
# handles the dialog on every re-login (gateway restarts, nightly bounce, etc.).
export DISPLAY=:1
TITLES=("Paper Account Notice" "Accept incoming connection" "Newer Version")
# Xvfb runs without a window manager, so keyboard focus is unreliable and Return
# doesn't reach the default button. A mouse click on the bottom-centre button row
# (where these single-button notices place "OK") reliably dismisses them. Poll
# fast (1s) so the click lands before the gateway's ~19s server-connect timer.
echo "[dismiss] watching DISPLAY=:1 for ${#TITLES[@]} dialog title(s)"
while true; do
  for title in "${TITLES[@]}"; do
    for w in $(xdotool search --name "$title" 2>/dev/null); do
      eval "$(xdotool getwindowgeometry --shell "$w" 2>/dev/null)"
      [ -z "${WIDTH:-}" ] && continue
      echo "[dismiss] clicking OK on '$title' (win=$w ${WIDTH}x${HEIGHT})"
      xdotool windowfocus --sync "$w" 2>/dev/null
      xdotool mousemove --sync $((X + WIDTH/2)) $((Y + HEIGHT - 22)) click 1 2>/dev/null
    done
  done
  sleep 1
done

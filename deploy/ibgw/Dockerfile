# Derived IB Gateway image. The stock gnzsnz image ships JavaFX's GTK *bridge*
# natives (libglassgtk2/3.so) but NOT the GTK system libraries, so the Gateway's
# JavaFX-based settings dialog crashes on init (GtkApplication -> "Internal
# Error"). IBC opens that dialog after login to enable the socket API, so it
# hangs forever and the API never starts. Installing GTK fixes it.
#
# Pinned to gateway 10.33.1d (stable; avoids the newer 10.47 paper-login quirks).
# The xdotool watcher stays as a harmless belt-and-braces dialog dismisser.
FROM ghcr.io/gnzsnz/ib-gateway:10.33.1d

USER root
# Ubuntu 24.04 (noble) renamed many libs with a "t64" suffix; try those first.
RUN apt-get update && \
    (apt-get install -y --no-install-recommends libgtk-3-0t64 libgtk2.0-0t64 \
     || apt-get install -y --no-install-recommends libgtk-3-0 libgtk2.0-0) && \
    apt-get install -y --no-install-recommends libxtst6 libxslt1.1 xdotool && \
    rm -rf /var/lib/apt/lists/*

COPY dismiss.sh /usr/local/bin/dismiss.sh
RUN chmod +x /usr/local/bin/dismiss.sh

USER 1000:1000
CMD ["bash", "-lc", "/usr/local/bin/dismiss.sh & exec /home/ibgateway/scripts/run.sh"]

# Tools for OSM regional extract support

It is hard to maintain OSM tile service with a small server: you can't have
a properly updated regional extract or even run osm2pgsql on low memory.
Here are some scripts that would help.

## init-planet-region.sh

Download planet.osm, cuts a polygon, updates it to today, loads it into
the database with osm2pgsql, and then optionally creates an sql archive
and uploads it to a remote server. On the second run does not use a planet
file, instead updates an extract. See configuration options in first
lines of the script.

## load-osm.sh

What if you don't want to update OSM data minutely, but prefer instead to have
as much data as possible? Rent a separate hourly-priced droplet, log in as root
and upload this script, a style files and an OSM extract. Run `./load-osm.sh init`, enter a password for `osm`
user when asked. Then log out.

Log in to the droplet as `osm` user. Start a `screen`, inside it run
`./load-osm.sh loadc <style> <extract>` (`load` if you need "slim" tables).
Ctrl+A, Ctrl+D, Ctrl+D to log out; in some hours log back in and
type `screen -r` to resume screen session.

When finished, run `./load-osm dump` to create a database dump, or, if every byte
counts, use `./load-osm transmit user@ip` to send PostgreSQL dump directly
to your server. After downloading the produces sql dump, the droplet can
be destroyed.

You should put correct user name and database name in the `load-osm.sh` header.

Linux builds supported: Fedora 19 and 20, Ubuntu 12.04 and 14.04, Debian 7.

## Limit disk space for updating

Add those lines before `seq=...` in `openstreetmap-tiles-update-expire` script
(for some reason version with `stat` did not work):

```bash
MIN_DISK_SPACE_MB=500

if `python -c "import os, sys; st=os.statvfs('$BASE_DIR'); sys.exit(1 if st.f_bavail*st.f_frsize/1024/1024 > $MIN_DISK_SPACE_MB else 0)"`; then
    m_info "there is less than $MIN_DISK_SPACE_MB MB left"
    exit 4
fi
```

## trim_osc.py

Trims osmChange file to a bbox or a polygon. It takes into consideration
osm2psql slim database tables, so no node or way is lost. It is recommended
to increase update interval to 5-10 minutes, so changes accumulate and
ways could be filtered more effectively.

To include the script into mod_tile update cycle, add those lines to
`openstreetmap-tiles-update-expire` script, between osmosis and osm2pgsql:

```bash
m_ok "filtering diff"
if ! /path/to/trim_osc.py -d gis -p /path/to/region.poly -z $CHANGE_FILE $CHANGE_FILE 1>&2 2>> "$RUNLOG"; then
    m_error "Trim_osc error"
fi
```

On a 16.5 GB database without this script planet diffs amounted to
600-650 MB daily. After the script was installed, the daily increase
fell to 140 MB.

This script needs `python-psycopg2` and `python-shapely` packages installed.

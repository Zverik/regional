#!/bin/sh

# This script downloads a planet extract, cuts a region from it,
# updated it to the current moment, uploads to PostGIS database.

STYLE=veloroad.style
POLY=/home/user/region.poly
DB=gis
PREFIX=region1
REGION_PATH=/home/user/osm/planet
PLANET_PATH=/home/user/data
PLANET_NAME=planet-2014-06-23.osm.pbf
PLANET_URL=http://planet.osm.org/planet/experimental/$PLANET_NAME
OSM2PGSQL_OPTIONS='-C 8000 --number-processes 3'
# uncomment to make an sql archive and upload it to another server
#SERVER_PATH=user@example.com:planet/
# then load the archive with "gzip -dc db-12345.sql.gz | psql gis"

# Look no further

PLANET_PBF=$PLANET_PATH/$PLANET_NAME
PPREFIX=$REGION_PATH/$PREFIX
LAST_PBF=`ls -t $PPREFIX-*|head -n 1`
OSMCONVERT=./osmconvert
OSMUPDATE=./osmupdate

if [ ! -e $OSMCONVERT ]
then
	echo Building osmconvert
	if ! curl -s -L http://m.m.i24.cc/osmconvert.c | cc -x c - -lz -O3 -o $OSMCONVERT; then exit; fi
fi
if [ ! -e $OSMUPDATE ]
then
	echo Building osmupdate
	if ! curl -s -L http://m.m.i24.cc/osmupdate.c | cc -x c - -o $OSMUPDATE; then exit; fi
fi

if [ ! -e $PLANET_PBF ]
then
	echo Downloading $PLANET_URL
	if ! curl -L -o $PLANET_PBF $PLANET_URL; then exit; fi
	LAST_PBF=
fi

if [ ! -e "$LAST_PBF" ]
then
	echo Cutting out the polygon
	LAST_PBF=$PPREFIX-0.o5m
	if ! $OSMCONVERT $PLANET_PBF -B=$POLY -o=$LAST_PBF; then exit; fi
fi

echo Determining last change date
LAST_DATE=`$OSMCONVERT $LAST_PBF --out-statistics |grep "timestamp max" |cut -d' ' -f3`
DATE=`date +%y%m%d-%H%M`
REGION_PBF=$PPREFIX-$DATE.osm.pbf
TMP_FILE=$PPREFIX-tmp.o5m
DB_DUMP=$REGION_PATH/db-$DATE.sql.gz

echo Updating from $LAST_DATE to current
if ! $OSMUPDATE $LAST_PBF $LAST_DATE $TMP_FILE; then exit; fi
echo Cutting out the polygon - again
if ! $OSMCONVERT $TMP_FILE -B=$POLY -o=$REGION_PBF; then exit; fi
rm $TMP_FILE

echo Calling osm2pgsql
if ! osm2pgsql -d $DB --slim -S $STYLE $OSM2PGSQL_OPTIONS $REGION_PBF; then exit; fi

if [ -n "$SERVER_PATH" ]; then
	echo Creating database dump
	if pg_dump -c -t 'planet_osm*' $DB |gzip >$DB_DUMP
	then
		echo Uploading said database dump
		echo "put $DB_DUMP" | sftp $SERVER_PATH
		#rm $DB_DUMP
	fi
fi

#!/bin/sh

USER=osm
DATABASE=gis
#HSTORE=1
OSM2PGSQL_BUILD_PATH=~/osm2pgsql
OSM2PGSQL_EXTRA_OPTIONS="--cache 16000"

# Load PBF/OSM file to postgresql database
load() {
	if [ -n "$HSTORE" ]; then
		HSTORE_OPT=--hstore
	fi
	PROCS=$((`nproc`-1))
	osm2pgsql -d $DATABASE --slim $DROP_OPT $HSTORE_OPT --multi-geometry --number-processes $PROCS $OSM2PGSQL_EXTRA_OPTIONS --style $1 $2
}

# Remove slim tables from postgresql database
clean() {
	psql -d gis -c 'drop table if exists planet_osm_nodes, planet_osm_ways, planet_osm_rels;'
	psql -d gis -c 'vacuum freeze;'
}

# Create gzipped dump
dump() {
	if [ -n "$1" ]; then
		DUMP=$1
	else
		DUMP=db-$(date +%y%m%d-%H%M).sql.gz
	fi
	pg_dump -c -t 'planet_osm*' $DATABASE | gzip > $DUMP
}

# Send database over SSH, skipping dump step
transmit() {
	pg_dump -c -t 'planet_osm*' $DATABASE | gzip | ssh $1 'gzip -dc | psql gis'
}

# Determine Linux type and version and call appropriate function
init_all() {
	if [ -f /etc/fedora-release ]; then
		VERSION=$(grep -o '[0-9]\+' /etc/fedora-release)
		if [ "$VERSION" = "20" ]; then
			install_f20
		elif [ "$VERSION" = "19" ]; then
			install_f19
		else
			echo "Unsupported Fedora version: $VERSION"
			exit 1
		fi
	elif [ -f /etc/lsb-release -o -d /etc/lsb-release.d ]; then
		LINUX=$(lsb_release -i | cut -d: -f2 | sed s/\\t//)
		VERSION=$(lsb_release -r | cut -d: -f2 | sed s/\\t//)
		if [ "$LINUX" = "Ubuntu" ]; then
			if [ "$VERSION" = "14.04" ]; then
				install_u1404
			elif [ "$VERSION" = "12.04" ]; then
				install_u1204
			else
				echo "Unsupported Ubuntu version: $VERSION"
				exit 1
			fi
		else
			echo "Unknown Linux: $LINUX $VERSION"
			exit 1
		fi
	elif [ -f /etc/debian_version ]; then
		VERSION=$(cut /etc/debian_version -d. -f1)
		if [ "$VERSION" = "7" ]; then
			install_deb7
		else
			echo "Unsupported Debian version: $VERSION"
			exit 1
		fi
	else
		echo "Unsupported Linux OS"
		exit 1
	fi

	init_user
	init_pgsql
}

init_user() {
	useradd -m -s /bin/bash $USER
	cp $0 /home/$USER
	mv *.{style,osm,pbf}* /home/$USER
	echo "User account $USER has been created, you should use it for all operations."
	passwd $USER
}

init_pgsql() {
	if [ -n "$HSTORE" ]; then
		HSTORE_CMD='CREATE EXTENSION hstore;'
	fi
	su postgres -c "createuser -s $USER; createdb -E UTF8 -O $USER gis ; psql -d gis -c 'CREATE EXTENSION postgis;$HSTORE_CMD'"
}

build_osm2pgsql() {
	mkdir -p $OSM2PGSQL_BUILD_PATH
	git clone git://github.com/openstreetmap/osm2pgsql.git $OSM2PGSQL_BUILD_PATH
	cd $OSM2PGSQL_BUILD_PATH
	./autogen.sh
	./configure
	make
	make install
	cd -
}

# Enable overcommit (for faster importing)
overcommit() {
	echo "vm.overcommit_memory=1" > /etc/sysctl.d/60-overcommit.conf
	sysctl -p /etc/sysctl.d/60-overcommit.conf
}

# Install software on Fedora 20
install_f20() {
	yum install -y postgresql postgresql-contrib postgresql-server postgis osm2pgsql screen
	su postgres -c "initdb -E UTF8 -D /var/lib/pgsql/data"
	systemctl enable postgresql
	systemctl start postgresql
	overcommit
}

# Install software on Fedora 19 - the same as for F20 apparently
install_f19() {
	install_f20
}

# Install software on Ubuntu 14.04
install_u1404() {
	add-apt-repository -y ppa:kakrueger/openstreetmap
	apt-get update
	apt-get --no-install-recommends install -y postgresql-9.3-postgis-2.1 osm2pgsql
	# postgresql is automatically started
	overcommit
}

debian_postgis_from_ppa() {
	if ! grep postgresql.org /etc/apt/sources.list
	then
		echo "deb http://apt.postgresql.org/pub/repos/apt/ $1-pgdg main" >> /etc/apt/sources.list
		wget --quiet -O - http://apt.postgresql.org/pub/repos/apt/ACCC4CF8.asc | apt-key add -
	fi
}

install_u1204() {
	debian_postgis_from_ppa precise
	apt-get update
	apt-get --no-install-recommends install -y postgresql-9.3-postgis
	# for building osm2pgsql from source
	apt-get --no-install-recommends install -y build-essential libxml2-dev libgeos++-dev libpq-dev libbz2-dev proj libtool automake git libprotobuf-c0-dev protobuf-c-compiler
	build_osm2pgsql
	# postgresql is automatically started
	overcommit
}

install_deb7() {
	debian_postgis_from_ppa wheezy
	apt-get update
	apt-get --no-install-recommends install -y postgresql-9.3-postgis
	# for building osm2pgsql from source
	apt-get --no-install-recommends install -y build-essential libxml2-dev libgeos++-dev libpq-dev libbz2-dev libproj-dev libtool automake git libprotobuf-c0-dev protobuf-c-compiler
	build_osm2pgsql
	# postgresql is automatically started
	overcommit
}

check_user() {
	if [ -z "$1" ]; then
		TARGET=$USER
	else
		TARGET=$1
	fi
	if [ "$(whoami)" != "$TARGET" ]; then
		echo "Please run this task under user $TARGET"
		exit 1
	fi
}

case $1 in
	init)
		check_user root
		init_all
		;;
	load|loadc)
		check_user
		if [ ! -f "$2" -o ! -f "$3" ]; then
			echo "Please specify valid files for both parameters"
			exit 1
		fi
		if [ "$1" == "loadc" ]; then
			DROP_OPT=--drop
		fi
		load "$2" "$3"
		;;
	clear|clean)
		check_user
		clean
		;;
	dump)
		check_user
		dump $2
		;;
	transmit|send)
		check_user
		if [ -z "$2" ]; then
			echo "Please specify user@ip for the first parameter"
			exit 1
		fi
		transmit "$2" ;;
	*)
		echo "OSM Loader 1.0"
		echo ""
		echo "Usage: $0 init - install PostgreSQL and osm2pgsql, initialize database"
		echo "       $0 load <style> <file> - load OSM/PBF dump into the database"
		echo "       $0 loadc <style> <file> - load with --drop (eq. to clean option, but slightly faster)"
		echo "       $0 clean - drop slim tables (disables live updating)"
		echo "       $0 dump [file] - create a gzipped database dump (defaults to db-YYMMDD-HHMM.sql.gz)"
		echo "       $0 transmit user@ip - send database contents to a server via ssh, skipping dump step"
		;;
esac

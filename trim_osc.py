#!/usr/bin/python3
# Trim osmChange file to a bounding polygon and database contents
# Written by Ilya Zverev, licensed WTFPL

import argparse
import getpass
import gzip
import json
import psycopg2
import sys
import os
from lxml import etree
from shapely.geometry import shape, Polygon, Point


def poly_parse(fp):
    result = None
    start = fp.read(10)
    fp.seek(0)
    if '{' in start:
        # This is a GeoJSON
        geojson = json.load(fp)
        for feature in geojson['features']:
            poly = shape(feature['geometry'])
            if result:
                result = result.union(poly)
            else:
                result = poly
        return result

    poly = []
    data = False
    hole = False
    for l in fp:
        l = l.strip()
        if l == 'END' and data:
            if len(poly) > 0:
                if hole and result:
                    result = result.difference(Polygon(poly))
                elif not hole and result:
                    result = result.union(Polygon(poly))
                elif not hole:
                    result = Polygon(poly)
            poly = []
            data = False
        elif l == 'END' and not data:
            break
        elif len(l) > 0 and ' ' not in l and '\t' not in l:
            data = True
            hole = l[0] == '!'
        elif l and data:
            poly.append(list(map(lambda x: float(x.strip()), l.split()[:2])))
    return result


def box(x1, y1, x2, y2):
    return Polygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])

default_user = getpass.getuser()
default_prefix = 'planet_osm'

parser = argparse.ArgumentParser(description='Trim osmChange file to a polygon and a database data')
parser.add_argument('osc', type=argparse.FileType('rb'), help='input osc file, "-" for stdin')
parser.add_argument('output', help='output osc file, "-" for stdout')
parser.add_argument('-d', dest='dbname', help='database name')
parser.add_argument('--host', help='database host')
parser.add_argument('--port', type=int, help='database port')
parser.add_argument('--user', default=default_user,
                    help='user name for db (default: {0})'.format(default_user))
parser.add_argument('--password', action='store_true', help='ask for password', default=False)
parser.add_argument('-p', '--poly', type=argparse.FileType('r'), help='osmosis polygon file')
parser.add_argument('-b', '--bbox', nargs=4, type=float,
                    metavar=('Xmin', 'Ymin', 'Xmax', 'Ymax'), help='Bounding box')
parser.add_argument('-z', '--gzip', action='store_true', help='source and output files are gzipped')
parser.add_argument('-v', dest='verbose', action='store_true', help='display debug information')
parser.add_argument('-P', '--prefix',
                    default=default_prefix,
                    help='Prefix for table names (default: {0})'.format(default_prefix))
options = parser.parse_args()

# read poly
poly = None
if options.bbox:
    b = options.bbox
    poly = box(b[0], b[1], b[2], b[3])
if options.poly:
    tpoly = poly_parse(options.poly)
    poly = tpoly if not poly else poly.intersection(tpoly)

if poly is None or not options.dbname:
    parser.print_help()
    sys.exit()
    
prefix = options.prefix
    
# connect to database
passwd = ""
if options.password:
    passwd = os.getenv('PGPASSWORD') or getpass.getpass("Please enter your password: ")

try:
    db = psycopg2.connect(database=options.dbname, user=options.user, password=passwd,
                          host=options.host, port=options.port)
except Exception as e:
    print("Error connecting to database: {}".format(e))
    sys.exit(1)
cur = db.cursor()

# read the entire osc into memory
tree = etree.parse(options.osc if not options.gzip else gzip.GzipFile(fileobj=options.osc))
options.osc.close()
root = tree.getroot()

# NODES

nodes = {}  # True for nodes inside poly or referenced by good ways
nodesM = []  # List of modified nodes outside poly (temporary)
for node in root.iter('node'):
    if node.getparent().tag not in ['modify', 'create']:
        continue
    if 'lat' in node.keys() and 'lon' in node.keys():
        inside = poly.intersects(Point(float(node.get('lon')), float(node.get('lat'))))
        nodes[node.get('id')] = inside
        if node.getparent().tag == 'modify' and not inside:
            nodesM.append(int(node.get('id')))

# Save modified nodes that are already in the database
q1='select id from {0}_nodes where id = ANY(%s);'.format(prefix)
cur.execute(q1, (nodesM,))
for row in cur:
    nodes[str(row[0])] = True

# WAYS

ways = []  # List of ways (int id) with nodes inside poly or no known nodes
waysM = []  # List of modified ways with no nodes inside poly (temporary)
for way in root.iter('way'):
    if way.getparent().tag not in ['modify', 'create']:
        continue
    foundInside = False
    foundKnown = False
    for nd in way.iterchildren('nd'):
        if nd.get('ref') in nodes:
            foundKnown = True
            if nodes[nd.get('ref')] is True:
                foundInside = True
                break
    if foundInside:
        for nd in way.iterchildren('nd'):
            nodes[nd.get('ref')] = True
    else:
        wayId = int(way.get('id'))
        if foundKnown:
            ways.append(wayId)
            if way.getparent().tag == 'modify':
                waysM.append(wayId)

q2 = 'select id from {0}_ways where id = ANY(%s);'.format(prefix)
cur.execute(q2, (waysM,))
for row in cur:
    ways.remove(row[0])
    # iterate over osmChange/<mode>/way[id=<id>]/nd and set nodes[ref] to True
    for wy in root.xpath('//way'):
        if wy.attrib['id'] == row[0]:
            for nd in wy.xpath('nd'):
                nodes[nd.get('ref')] = True

# RELATIONS

relations = []  # List of modified relations that are not in the database
for rel in root.iter('relation'):
    if rel.getparent().tag == 'modify':
        relations.append(int(rel.get('id')))

q3 = 'select id from {0}_rels where id = ANY(%s);'.format(prefix)
cur.execute(q3, (relations,))
for row in cur:
    relations.remove(row[0])

cur.close()
db.close()

# filter tree
# 1. remove objects out of bounds
cnt = [0, 0, 0]
total = [0, 0, 0]
types = ['node', 'way', 'relation']
for obj in root.iter('node', 'way', 'relation'):
    idx = types.index(obj.tag)
    ident = obj.get('id')
    if obj.getparent().tag in ['modify', 'create']:
        total[idx] = total[idx] + 1
    if ((obj.tag == 'node' and ident in nodes and not nodes[ident]) or
            (obj.tag == 'way' and int(ident) in ways) or
            (obj.tag == 'relation' and int(ident) in relations)):
        obj.getparent().remove(obj)
    else:
        cnt[idx] = cnt[idx] + 1

if options.verbose:
    print('{} -> {}'.format('+'.join(map(str, total)), '+'.join(map(str, cnt))))

# 2. remove empty sections
for sec in root:
    if len(sec) == 0:
        root.remove(sec)

# save modified osc
of = sys.stdout.buffer if options.output == '-' else open(options.output, 'wb')
if options.gzip:
    of = gzip.GzipFile(fileobj=of)
of.write(etree.tostring(tree))

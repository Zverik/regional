#!/usr/bin/python3
# Trim osmChange file to a bounding polygon and database contents
# Stream-optimized version - avoids loading entire file into memory
# Written by Ilya Zverev, licensed WTFPL
# Stream processing optimization

import argparse
import getpass
import gzip
import json
import sys
import os
from io import BytesIO
from lxml import etree
from shapely.geometry import shape, Polygon, Point
import psycopg2


def poly_parse(fp):
    """Parse polygon file (GeoJSON or Osmosis format)"""
    result = None
    start = fp.read(10)
    fp.seek(0)
    if '{' in start:
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
    """Create bounding box polygon"""
    return Polygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])


class OSMChangeStreamProcessor:
    """
    Stream-based processor for osmChange files.
    Uses two-pass processing with minimal memory usage.
    """

    def __init__(self, poly, db_conn, prefix):
        self.poly = poly
        self.db_conn = db_conn
        self.prefix = prefix
        self.nodes = {}           # node_id -> bool (True = keep)
        self.ways_to_remove = set()   # way_id to remove
        self.relations_to_remove = set()  # relation_id to remove
        self.nodes_to_check = []   # nodes needing DB check
        self.ways_to_check = []    # ways needing DB check
        self.relations_to_check = [] # relations needing DB check
        self.verbose = False

    def set_verbose(self, verbose):
        self.verbose = verbose

    def process_and_write(self, input_path, output_path, input_gzipped=False, output_gzipped=False):
        """
        Process osmChange file with two-pass approach.
        Pass 1: Collect metadata (nodes, ways, relations to keep/remove)
        Pass 2: Write filtered output
        """
        # For non-seekable inputs (gzip, stdin), buffer the entire content
        # For regular files, we can do two passes by reopening
        needs_buffering = input_gzipped or input_path == '-'

        if needs_buffering:
            if self.verbose:
                print("Buffering input for two-pass processing...", file=sys.stderr)
            if input_path == '-':
                raw_data = sys.stdin.buffer.read()
            else:
                with gzip.open(input_path, 'rb') if input_gzipped else open(input_path, 'rb') as f:
                    raw_data = f.read()
            buffer = BytesIO(raw_data)
            input_source1 = buffer
            input_source2 = buffer
        else:
            input_source1 = input_path
            input_source2 = input_path

        # Setup output
        if output_path == '-':
            output_file = sys.stdout.buffer
            close_output = False
        else:
            if output_gzipped:
                output_file = gzip.open(output_path, 'wb')
            else:
                output_file = open(output_path, 'wb')
            close_output = True

        try:
            # Phase 1: Collect metadata
            if self.verbose:
                print("Phase 1: Collecting metadata...", file=sys.stderr)

            if needs_buffering:
                self._collect_metadata_from_buffer(input_source1)
            else:
                with open(input_source1, 'rb') as f:
                    self._collect_metadata_from_file(f)

            # Phase 2: Check database
            if self.verbose:
                print("Phase 2: Checking database...", file=sys.stderr)
            self._check_database()

            # Phase 3: Write filtered output
            if self.verbose:
                print("Phase 3: Writing filtered output...", file=sys.stderr)

            if needs_buffering:
                input_source2.seek(0)
                self._write_filtered(input_source2, output_file)
            else:
                with open(input_source2, 'rb') as f:
                    self._write_filtered(f, output_file)

            if self.verbose:
                print("Done.", file=sys.stderr)

        finally:
            if close_output:
                output_file.close()

    def _collect_metadata_from_file(self, file_obj):
        """Collect metadata from seekable file object"""
        input_iter = etree.iterparse(file_obj, events=('end',), tag=('node', 'way', 'relation'))
        self._process_metadata_iter(input_iter)

    def _collect_metadata_from_buffer(self, buffer_obj):
        """Collect metadata from BytesIO buffer"""
        buffer_obj.seek(0)
        input_iter = etree.iterparse(buffer_obj, events=('end',), tag=('node', 'way', 'relation'))
        self._process_metadata_iter(input_iter)

    def _process_metadata_iter(self, input_iter):
        """Process iterator and collect metadata"""
        current_mode = None

        for event, elem in input_iter:
            parent = elem.getparent()

            # Track current mode (modify/create/delete)
            if parent is not None:
                parent_tag = parent.tag
                if parent_tag in ('modify', 'create'):
                    current_mode = parent_tag
                elif parent_tag == 'delete':
                    current_mode = 'delete'

            tag = elem.tag
            oid = elem.get('id')

            if tag == 'node':
                if current_mode in ('modify', 'create'):
                    if 'lat' in elem.attrib and 'lon' in elem.attrib:
                        inside = self.poly.intersects(
                            Point(float(elem.get('lon')), float(elem.get('lat')))
                        )
                        self.nodes[oid] = inside
                        if current_mode == 'modify' and not inside:
                            self.nodes_to_check.append(int(oid))

            elif tag == 'way':
                if current_mode in ('modify', 'create'):
                    found_inside = False
                    found_known = False

                    for nd in elem.iterchildren('nd'):
                        ref = nd.get('ref')
                        if ref in self.nodes:
                            found_known = True
                            if self.nodes[ref] is True:
                                found_inside = True
                                break

                    if found_inside:
                        # Mark all nodes as keep
                        for nd in elem.iterchildren('nd'):
                            self.nodes[nd.get('ref')] = True
                    elif found_known:
                        # Way has no nodes inside poly - may need removal
                        way_id = int(oid)
                        self.ways_to_remove.add(way_id)
                        if current_mode == 'modify':
                            self.ways_to_check.append(way_id)

            elif tag == 'relation':
                if current_mode == 'modify':
                    rel_id = int(oid)
                    self.relations_to_remove.add(rel_id)
                    self.relations_to_check.append(rel_id)

            # Clear element to free memory
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

    def _check_database(self):
        """Check which elements exist in the database"""
        if not self.db_conn:
            return

        cur = self.db_conn.cursor()

        # Check nodes that are outside polygon but modified
        if self.nodes_to_check:
            q = f'SELECT id FROM {self.prefix}_nodes WHERE id = ANY(%s);'
            cur.execute(q, (self.nodes_to_check,))
            for row in cur:
                self.nodes[str(row[0])] = True

        # Check ways that have no nodes inside polygon
        ways_in_db = set()
        if self.ways_to_check:
            q = f'SELECT id FROM {self.prefix}_ways WHERE id = ANY(%s);'
            cur.execute(q, (self.ways_to_check,))
            for row in cur:
                ways_in_db.add(row[0])

        # Remove ways that exist in DB from removal list
        for way_id in ways_in_db:
            self.ways_to_remove.discard(way_id)

        # Check relations
        relations_in_db = set()
        if self.relations_to_check:
            q = f'SELECT id FROM {self.prefix}_rels WHERE id = ANY(%s);'
            cur.execute(q, (self.relations_to_check,))
            for row in cur:
                relations_in_db.add(row[0])

        # Remove relations that exist in DB from removal list
        for rel_id in relations_in_db:
            self.relations_to_remove.discard(rel_id)

        cur.close()

    def _write_filtered(self, input_file, output_file):
        """
        Write filtered XML output incrementally.
        """
        context = etree.iterparse(
            input_file,
            events=('start', 'end'),
            remove_blank_text=True
        )

        output_file.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        output_file.write(b'<osmChange version="0.6">\n')

        current_section = None
        section_has_content = False
        section_buffer = BytesIO()
        counts = {'node': [0, 0], 'way': [0, 0], 'relation': [0, 0]}

        for event, elem in context:
            tag = elem.tag
            oid = elem.get('id', '')

            if event == 'start':
                if tag in ('modify', 'create', 'delete'):
                    # Flush previous section if different
                    if current_section and current_section != tag:
                        if section_has_content:
                            output_file.write(section_buffer.getvalue())
                        section_buffer = BytesIO()
                        section_has_content = False

                    current_section = tag
                    output_file.write(f'<{tag}>'.encode())

            elif event == 'end':
                if tag in ('modify', 'create', 'delete'):
                    # Close section
                    if section_has_content:
                        output_file.write(section_buffer.getvalue())
                    output_file.write(f'</{tag}>\n'.encode())

                    # Reset for next section
                    section_buffer = BytesIO()
                    section_has_content = False
                    current_section = None

                elif tag in ('node', 'way', 'relation'):
                    counts[tag][0] += 1

                    # Determine if we should keep this element
                    should_remove = False

                    if current_section in ('modify', 'create'):
                        if tag == 'node' and oid in self.nodes:
                            should_remove = not self.nodes[oid]
                        elif tag == 'way' and int(oid) in self.ways_to_remove:
                            should_remove = True
                        elif tag == 'relation' and int(oid) in self.relations_to_remove:
                            should_remove = True

                    if not should_remove:
                        # Write element
                        xml_str = etree.tostring(elem, encoding='utf-8')
                        section_buffer.write(xml_str)
                        section_buffer.write(b'\n')
                        section_has_content = True
                        counts[tag][1] += 1

                    # Clean up
                    elem.clear()
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]

        output_file.write(b'</osmChange>\n')

        if self.verbose:
            total = '+'.join(str(counts[t][0]) for t in ('node', 'way', 'relation'))
            kept = '+'.join(str(counts[t][1]) for t in ('node', 'way', 'relation'))
            print(f'{total} -> {kept}', file=sys.stderr)


def main():
    default_user = getpass.getuser()
    default_prefix = 'planet_osm'

    parser = argparse.ArgumentParser(
        description='Trim osmChange file to a polygon (stream-optimized)'
    )
    parser.add_argument('osc', type=str, help='input osc file, "-" for stdin')
    parser.add_argument('output', help='output osc file, "-" for stdout')
    parser.add_argument('-d', dest='dbname', help='database name')
    parser.add_argument('--host', help='database host')
    parser.add_argument('--port', type=int, help='database port')
    parser.add_argument('--user', default=default_user,
                        help='user name for db (default: {0})'.format(default_user))
    parser.add_argument('--password', action='store_true', help='ask for password')
    parser.add_argument('-p', '--poly', type=argparse.FileType('r'), help='osmosis polygon file')
    parser.add_argument('-b', '--bbox', nargs=4, type=float,
                        metavar=('Xmin', 'Ymin', 'Xmax', 'Ymax'), help='Bounding box')
    parser.add_argument('-z', '--gzip', action='store_true',
                        help='source and output files are gzipped')
    parser.add_argument('-v', dest='verbose', action='store_true',
                        help='display debug information')
    parser.add_argument('-P', '--prefix', default=default_prefix,
                        help='Prefix for table names (default: {0})'.format(default_prefix))
    options = parser.parse_args()

    # Read polygon
    poly = None
    if options.bbox:
        b = options.bbox
        poly = box(b[0], b[1], b[2], b[3])
    if options.poly:
        tpoly = poly_parse(options.poly)
        poly = tpoly if not poly else poly.intersection(tpoly)

    if poly is None or not options.dbname:
        parser.print_help()
        sys.exit(1)

    # Connect to database
    passwd = ""
    if options.password:
        passwd = os.getenv('PGPASSWORD') or getpass.getpass("Password: ")

    try:
        db = psycopg2.connect(
            database=options.dbname,
            user=options.user,
            password=passwd,
            host=options.host,
            port=options.port
        )
    except Exception as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        sys.exit(1)

    # Process
    processor = OSMChangeStreamProcessor(poly, db, options.prefix)
    processor.set_verbose(options.verbose)

    try:
        processor.process_and_write(
            options.osc,
            options.output,
            input_gzipped=options.gzip,
            output_gzipped=options.gzip
        )
    finally:
        db.close()


if __name__ == '__main__':
    main()


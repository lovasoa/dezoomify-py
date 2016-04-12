#!/usr/bin/env python3
# coding=utf8

"""
TAKE A URL CONTAINING A PAGE CONTAINING A ZOOMIFY OBJECT, A ZOOMIFY BASE
DIRECTORY OR A LIST OF THESE, AND RECONSTRUCT THE FULL RESOLUTION IMAGE


====LICENSE=====================================================================

This software is licensed under the Expat License (also called the MIT license).
"""

import sys

if sys.version_info[0] < 3:
    sys.exit("ERROR: This program requires Python 3 to run.")

from math import ceil, floor
import argparse
import logging
import os
import re
import subprocess
import tempfile
import shutil
import urllib.error
import urllib.request
import urllib.parse
import platform
import itertools
import time
from multiprocessing.pool import ThreadPool

# Progressbar module is optional but recommended.
progressbar = None
try:
    import progressbar
except ImportError:
    pass

parser = argparse.ArgumentParser(
    description="Download and untile a Zoomify image.",
    epilog="More detailed help can be found at the project's wiki: http://sf.net/p/dezoomify/wiki/",
    usage='%(prog)s URL OUTPUT_FILE [options]'
)
parser.add_argument('url', metavar='URL', action='store',
                    help='the URL of a page containing a Zoomify object '
                         '(unless -b or -l flags are used)')
parser.add_argument('out', metavar='OUTPUT_FILE', action='store',
                    help='where to save the image')
parser.add_argument('-b', dest='base', action='store_true', default=False,
                    help='the URL is the base directory for the Zoomify tile structure (see wiki for more details)')
parser.add_argument('-l', dest='list', action='store_true', default=False,
                    help='batch mode: the URL parameter refers to a local file with a list of URL and filename pairs (one pair per line, separated by a tab). '
                         'The directory in which the images will be saved will be OUTPUT_FILE minus its extension. '
                         'Specifying a filename is optional, OUTPUT_FILE with numbers appended is used by default.')
parser.add_argument('-z', dest='zoom_level', action='store', default=-1, type=int,
                    help='Zoom level to grab the image at (defaults to maximum). '
                         'For positive zoom level values, the untiled image\' longest edge length is less or equal to (tile size) * 2^(zoom level). '
                         'For negative values, the untiled image\'s longest edge length equals (maximum length) / 2^(1 - zoom level).')
parser.add_argument('-s', dest='store', action='store_true', default=False,
                    help='save all tiles in the local directory instead of the system\'s temporary directory')
parser.add_argument('-x', dest='no_download', action='store_true', default=False,
                    help='create the image from previously downloaded files stored '
                         'with -s instead of downloading (can be useful when an error occurred during tile joining)')
parser.add_argument('-j', dest='jpegtran', action='store',
                    help='location of the jpegtran executable (assumed to be in the '
                         'same directory as this script by default)')
parser.add_argument('-t', dest='nthreads', action='store', default=16, type=int,
                    help='number of simultaneous tile downloads (default: 16)')
#parser.add_argument('-p', dest='protocol', action='store', default='zoomify',
#                    help='which image untiler protocol to use (options: zoomify. Default: zoomify)')
# This is commented out for now. Will probably reintroduce this option when Pillow is integrated.
# parser.add_argument('-a', dest='algorithm', action='store', default='jt_xl',
# choices=['jt_std', 'jt_xl'],
# help='which image untiler algorithm to use.'
# 'Options:'
# '    - jt_std (jpegtran standard classic - lossless)'
# '             Proven classic, slow for large images.'
# '    - jt_xl (jpegtran large image - lossless)'
# '            New, way faster for large images.'
# '    - pil (Python  Pillow - almost lossless - not yet implemented)'
# 'Default: jt_xl')
parser.add_argument('-v', dest='verbose', action='count', default=0,
                    help="increase verbosity (-vv for more)")

def open_url(url, retry=5):
    """
    Similar to urllib.request.urlopen,
    except some additional preparation is done on the URL and
    the user-agent and referrer are spoofed.

    Keyword arguments:
    url -- the URL to open
    retry -- the number of times to retry
    """

    # Escape the path part of the URL so spaces in it would not confuse the server.
    scheme, netloc, path, qs, anchor = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(path, '/%:|')
    qs = urllib.parse.quote_plus(qs, ':&=')
    safe_url = urllib.parse.urlunsplit((scheme, netloc, path, qs, anchor))

    # spoof the user-agent and referrer, in case that matters.
    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 6.2; WOW64; rv:24.0) Gecko/20100101 Firefox/24.0',
        'Referer': 'http://google.com'
    }
    # create a request object for the URL
    request = urllib.request.Request(safe_url, headers=req_headers)
    # create an opener object
    opener = urllib.request.build_opener()
    # open a connection and receive the http response headers + contents
    try:
        return opener.open(request)
    except urllib.error.URLError as e:
        if retry==0: raise e
        else:
            time.sleep(2**(5-retry))
            return open_url(url, retry-1)



def download_url(url, destination):
    """
    Copy a network object denoted by a URL to a local file.
    """
    with open_url(url) as response, open(destination, 'wb') as out_file:
        shutil.copyfileobj(response, out_file)

class JpegtranException(Exception):
    pass

class ZoomLevelError(Exception):
    pass

class ImageUntiler():
    def __init__(self, args):
        self.verbose = int(args.verbose)
        self.store = args.store
        self.out = args.out
        self.jpegtran = args.jpegtran
        self.no_download = args.no_download
        self.nthreads = args.nthreads
        self.base = args.base
        self.zoom_level = args.zoom_level
        # self.algorithm = args.algorithm
        self.ext = 'jpg'

        if self.no_download:
            self.store = True

        # Set up logging.
        log_level = logging.WARNING  # default
        if args.verbose == 0:
            # Disable the progressbar
            global progressbar
            progressbar = False
        elif args.verbose == 1:
            log_level = logging.INFO
        elif args.verbose >= 2:
            log_level = logging.DEBUG
        logging.basicConfig(level=log_level, format='%(levelname)s: %(message)s')
        self.log = logging.getLogger(__name__)

        # Set up jpegtran.
        if self.jpegtran == None:  # we need to locate jpegtran
            mod_dir = os.path.dirname(os.path.abspath(__file__))  # location of this script
            if platform.system() == 'Windows':
                jpegtran = os.path.join(mod_dir, 'jpegtran.exe')
            else:
                jpegtran = os.path.join(mod_dir, 'jpegtran')

            if os.path.exists(jpegtran):
                self.jpegtran = jpegtran
            else:
                self.log.error("No jpegtran excecutable found at the script's directory. "
                               "Use -j option to set its location.")
                raise JpegtranException

        # Check that jpegtran exists and has the lossless drop feature.
        if not os.path.exists(self.jpegtran):
            self.log.error("jpegtran excecutable not found. "
                           "Use -j option to set its location.")
            raise JpegtranException
        elif not os.access(self.jpegtran, os.X_OK):
            self.log.error("{} does not have execute permission."
            .format(self.jpegtran))
            raise JpegtranException

        try:
            with subprocess.Popen([self.jpegtran, '--help'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as subproc:
                jpegtran_help_info = str(subproc.communicate(timeout=5))
                if '-drop' not in jpegtran_help_info:
                    self.log.error("{} does not have the '-drop' feature. "
                                   "Either use the jpegtran supplied with Dezoomify or get it from "
                                   "http://jpegclub.org/jpegtran/ section \"3. Lossless crop 'n' drop (cut & paste)\" to fix the problem."
                    .format(self.jpegtran))
                    raise JpegtranException
        except Exception as e:
            self.log.error("Unable to start jpegtran: %s" % (e))

        self.tile_dir = None
        self.get_url_list(args.url, args.list)

        if len(self.image_urls) == 1:
            self.log.info("Processing image {})...".format(self.image_urls[0]))
            self.process_image(self.image_urls[0], self.out_names[0])
            self.log.info("Dezoomifed image created and saved to {}.".format(self.out_names[0]))
        else:
            for i, image_url in enumerate(self.image_urls):
                destination = self.out_names[i]
                self.log.info("[{}/{}] Processing image {}...".format(i + 1, len(self.image_urls), image_url))
                try:
                    self.process_image(image_url, destination)
                except Exception as e:
                    if not isinstance(e, (FileNotFoundError, JpegtranException, ZoomLevelError)):
                        self.log.warning("Unknown exception occurred while processing image {}: {} ()".format(image_url, e.__class__.__name__, e))
                self.log.info("Dezoomifed image created and saved to {}.".format(destination))

    def process_image(self, image_url, destination):
        """Scrapes image info and calls the untiler."""
        if not self.base:
            # locate the base directory of the zoomify tile images
            self.base_dir = self.get_base_directory(image_url)
        else:
            self.base_dir = image_url
            if self.base_dir.endswith('/ImageProperties.xml'):
                self.base_dir = urllib.parse.urljoin(self.base_dir, '.')
            self.base_dir = self.base_dir.rstrip('/') + '/'

        try:
            # inspect the ImageProperties.xml file to get properties, and derive the rest
            self.get_properties(self.base_dir, self.zoom_level)

            # create the directory where the tiles are stored
            self.setup_tile_directory(self.store, destination)

            # download and join tiles to create the dezoomified file
            self.untile_image(destination)

        finally:
            if not self.store and self.tile_dir:
                shutil.rmtree(self.tile_dir)
                self.log.debug("Erased the temporary directory and its contents")

    def untile_image(self, output_destination):
        """
        Downloads image tiles and joins them.
        These processes are done in parallel.
        """
        self.num_tiles = self.x_tiles * self.y_tiles
        self.num_downloaded = 0
        self.num_joined = 0

        # Progressbars for downloading and joining.
        download_progressbar = None
        joining_progressbar = None
        if progressbar:
            download_progressbar = progressbar.ProgressBar(
                widgets=['Downloading tiles: ',
                         progressbar.Counter(), '/', str(self.num_tiles), ' ',
                         progressbar.Bar('>', left='[', right=']'), ' ',
                         progressbar.ETA()],
                maxval=self.num_tiles
            )
            joining_progressbar = progressbar.ProgressBar(
                widgets=['Joining tiles: ',
                         progressbar.Counter(), '/', str(self.num_tiles), ' ',
                         progressbar.Bar('>', left='[', right=']'), ' ',
                         progressbar.ETA()],
                maxval=self.num_tiles
            )
            download_progressbar.start()
            if self.no_download:
                download_progressbar.finish()
                joining_progressbar.start()

        def update_progressbars():
            # Update UI info
            if progressbar:
                if self.num_downloaded < self.num_tiles:
                    download_progressbar.update(self.num_downloaded)
                elif not download_progressbar.finished:
                    download_progressbar.finish()
                    joining_progressbar.start()
                    joining_progressbar.update(self.num_joined)  # There are already images  joined!
                else:
                    joining_progressbar.update(self.num_joined)

        def local_tile_path(col, row):
            return os.path.join(self.tile_dir, "{}_{}.{}".format(col, row, self.ext))

        def download(tile_position):
            col, row = tile_position
            url = self.get_tile_url(col, row)
            destination = local_tile_path(col, row)
            if not progressbar:
                self.log.debug("Downloading tile (row {:3}, col {:3})".format(row, col))
            try:
                download_url(url, destination)
            except urllib.error.HTTPError as e:
                self.num_downloaded += 1
                self.log.warning(
                    "{}. Tile {} (row {}, col {}) does not exist on the server."
                    .format(e, url, row, col)
                )
                return (None, None)
            self.num_downloaded += 1
            return tile_position

        # Download tiles in self.nthreads parallel threads.
        tile_positions = itertools.product(range(self.x_tiles), range(self.y_tiles))
        if not self.no_download:
            pool = ThreadPool(processes=self.nthreads)
            self.downloaded_iterator = pool.imap(download, tile_positions)
        else:
            self.downloaded_iterator = tile_positions
            self.num_downloaded = self.num_tiles

        def jplarge(self, joining_progressbar):
            """
            Faster untilig algorithm, assembling columns separately,
            then assembling those into final image. Cuts down on the cost
            of constantly opening two huge final images.
            """
            # Do tile joining in parallel with the downloading.
            # Use 4 temporary files for the joining process.
            tmpimgs = []
            finalimage = []
            tempinfo = {'tmp_': tmpimgs, 'final_': finalimage}
            for i in range(2):
                for f in iter(tempinfo):
                    fhandle = tempfile.NamedTemporaryFile(suffix='.jpg', prefix=f, dir=self.tile_dir, delete=False)
                    tempinfo[f].append(fhandle.name)
                    fhandle.close()
                    self.log.debug("Created temporary image file: " + tempinfo[f][i])

            # The index of current_col temp image to be used for input, toggles between 0 and 1.
            active_tmp = 0
            active_final = 0

            # Join tiles into a single image in parallel to them being downloaded.
            try:
                subproc = None # Popen class of the most recently called subprocess.
                current_col = 0
                tile_in_column = 0
                for i, (col, row) in enumerate(self.downloaded_iterator):
                    if col is None:
                        self.log.debug("Missing col tile!")
                        continue # Tile failed to download.

                    if col == current_col:
                        if not progressbar:
                            self.log.debug("Adding tile (row {:3}, col {:3}) to the image".format(row, col))

                        # As the very first step create an (almost) empty temp column image,
                        # with the target column dimensions.
                        # Don't reuse old tempfile without overwriting it first -
                        # if the file is broken, we want an empty space instead of an image from previous iteration.
                        if tile_in_column == 0 and not current_col == self.x_tiles - 1:
                            subproc = subprocess.Popen([self.jpegtran,
                                '-copy', 'all',
                                '-crop', '{:d}x{:d}+0+0'.format(self.tile_size, self.height),
                                '-outfile', tmpimgs[active_tmp],
                                local_tile_path(col, row)
                            ])
                            subproc.wait()
                        # Last column may have different width - create tempfile with correct dimensions
                        elif tile_in_column == 0 and current_col == self.x_tiles - 1:
                            subproc = subprocess.Popen([self.jpegtran,
                                '-copy', 'all',
                                '-crop', '{:d}x{:d}+0+0'.format(self.width - ((self.x_tiles - 1) * self.tile_size), self.height),
                                '-outfile', tmpimgs[active_tmp],
                                local_tile_path(col, row)
                            ])
                            subproc.wait()
                        # Not working on a complete column - just keep adding images.
                        else:
                            subproc = subprocess.Popen([self.jpegtran,
                                '-perfect',
                                '-copy', 'all',
                                '-drop', '+{:d}+{:d}'.format(0, row * self.tile_size), local_tile_path(col, row),
                                '-outfile', tmpimgs[active_tmp],
                                tmpimgs[(active_tmp + 1) % 2]
                            ])
                            subproc.wait()

                        self.num_joined += 1
                        update_progressbars()

                        # After untiling of a first column,
                        # create a full sized temp image with the just untiled column
                        if tile_in_column == self.y_tiles - 1 and current_col == 0:
                            subproc = subprocess.Popen([self.jpegtran,
                                '-perfect',
                                '-copy', 'all',
                                '-crop', '{:d}x{:d}+0+0'.format(self.width, self.height),
                                '-outfile', finalimage[active_final],
                                tmpimgs[active_tmp]
                            ])
                            subproc.wait()
                            current_col += 1
                            tile_in_column = 0
                            active_final = (active_final + 1) % 2
                            active_tmp = (active_tmp + 1) % 2
                        # Drop just untiled column (other then first) into the full sized temp image.
                        elif tile_in_column == self.y_tiles - 1 and not current_col == 0:
                            subproc = subprocess.Popen([self.jpegtran,
                                '-perfect',
                                '-copy', 'all',
                                '-drop', '+{:d}+{:d}'.format(current_col * self.tile_size, 0), tmpimgs[active_tmp],
                                '-outfile', finalimage[active_final],
                                finalimage[(active_final + 1) % 2]
                            ])
                            subproc.wait()
                            current_col += 1
                            tile_in_column = 0
                            active_final = (active_final + 1) % 2
                            active_tmp = (active_tmp + 1) % 2
                        # No column completely untiled, keep working
                        else:
                            tile_in_column += 1
                            active_tmp = (active_tmp + 1) % 2  # toggle between the two temp images

                # Optimize the final  image and write it to destination
                subproc = subprocess.Popen([self.jpegtran,
                    '-copy', 'all',
                    '-optimize',
                    '-outfile', output_destination,
                    finalimage[(active_final + 1) % 2]
                ])
                subproc.wait()

                num_missing = self.num_tiles - self.num_joined
                if num_missing > 0:
                    self.log.warning(
                        "Image '{3}' is missing {0} tile{1}. "
                        "You might want to download the image at a different zoom level "
                        "(currently {2}) to get the missing part{1}."
                        .format(num_missing, '' if num_missing == 1 else 's', self.zoom_level,
                                output_destination)
                    )
                if progressbar and joining_progressbar.start_time is not None:
                    joining_progressbar.finish()

            except KeyboardInterrupt:
                # Kill the jpegtran subprocess.
                if subproc and subproc.poll() is None:
                    subproc.kill()
                raise
            finally:
                #Delete the temporary images.
                for i in range(2):
                    os.unlink(tmpimgs[i])
                    os.unlink(finalimage[i])

        # Select untiling algorithm
        # if self.algorithm == 'jt_xl':
            # jplarge(self, joining_progressbar)
        # elif self.algorithm == 'jt_std':
            # jpstandard(self, joining_progressbar)

        jplarge(self, joining_progressbar)

    def get_url_list(self, url, use_list):
        """
        Return a list of URLs to process and their respective output file names.
        """
        if not use_list:  # if we are dealing with a single object
            self.image_urls = [url]
            self.out_names = [self.out]

        else:  # if we are dealing with a file with a list of objects
            list_file = open(url, 'r')
            self.image_urls = []  # empty list of directories
            self.out_names = []

            i = 1
            for line in list_file:
                line = line.strip().split('\t', 1)
                if len(line[0]) > 0 and not line[0].isspace():    #Checks for empty lines - only eith newlines

                    if len(line) == 1:
                        root, ext = os.path.splitext(self.out)
                        self.out_names.append("{}_{:03d}{}".format(root, i, ext))
                        i += 1
                    elif len(line) == 2:
                        # allow filenames to lack extensions
                        m = re.search('\\.' + self.ext + '$', line[1])
                        if not m:
                            line[1] += '.' + self.ext
                        self.out_names.append(os.path.join(os.path.dirname(self.out), line[1]))
                    else:
                        continue

                    self.image_urls.append(line[0])

            list_file.close()

    def setup_tile_directory(self, in_local_dir, output_file_name=None):
        """
        Create the directory in which tile downloading & joining takes place.

        in_local_dir -- whether the directory should be placed in the working directory,
            will be created in the system's temp directory otherwise
        output_file_name -- the path of the final dezoomified image,
            used to derive the local directory's location
        """
        if in_local_dir:
            root = os.path.splitext(output_file_name)[0]

            if not os.path.exists(root):
                self.log.debug("Creating image storage directory: {}".format(root))
                os.makedirs(root)
            self.tile_dir = root
        else:
            self.tile_dir = tempfile.mkdtemp(prefix='dezoomify_')
            self.log.debug("Created temporary image storage directory: {}".format(self.tile_dir))


class UntilerDezoomify(ImageUntiler):
    def get_base_directory(self, url):
        """
        Gets the Zoomify image base directory for the image tiles. This function
        is called if the user does NOT supply a base directory explicitly. It works
        by parsing the HTML code of the given page and looking for
        zoomifyImagePath=....

        Keyword arguments
        url -- The URL of the page to look for the base directory on
        """

        try:
            with open_url(url) as handle:
                content = handle.read().decode(errors='ignore')
        except Exception as e:
            self.log.error(
                "Specified directory not found ({}).\n"
                "Check the URL: {}"
                .format(e, url)
            )
            raise FileNotFoundError

        image_path = None
        image_path_regexes = [
            ('zoomifyImagePath=([^\'"&]*)[\'"&]', 1),
            ('ZoomifyCache/[^\'"&.]+\\.\\d+x\\d+', 0),
            # For HTML5 Zoomify.
            ('(["\'])([^"\']+)/TileGroup0[^"\']*\\1', 2),
            # Another JavaScript/HTML5 Zoomify version (v1.8).
            ('showImage\\([^,]+, *(["\'])([^"\']+)\\1', 2)]
        for rx, group in image_path_regexes:
            m = re.search(rx, content)
            if m:
                image_path = m.group(group)
                break

        if not image_path:
            self.log.error("Zoomify base directory not found. "
                           "Ensure the given URL contains a Zoomify object.\n"
                           "If that does not work, see \"Troubleshooting\" (http://sourceforge.net/p/dezoomify/wiki/Troubleshooting/) for additional help.")
            raise FileNotFoundError

        self.log.debug("Found ZoomifyImagePath: {}".format(image_path))

        image_path = urllib.parse.unquote(image_path)
        base_dir = urllib.parse.urljoin(url, image_path)
        base_dir = base_dir.rstrip('/') + '/'
        return base_dir

    def get_properties(self, base_dir, zoom_level):
        """
        Retrieve the XML properties file and extract the needed information.

        Sets the relevant variables for the grabbing phase.

        Keyword arguments
        base_dir -- the Zoomify base directory
        zoom_level -- the level which we want to get
        """

        # READ THE XML FILE AND RETRIEVE THE ZOOMIFY PROPERTIES
        # NEEDED TO RECONSTRUCT (WIDTH, HEIGHT AND TILESIZE)

        # this file contains information about the image tiles
        xml_url = base_dir + 'ImageProperties.xml'

        self.log.debug("xml_url=" + xml_url)
        content = None
        try:
            with open_url(xml_url) as handle:
                content = handle.read().decode(errors='ignore')
        except Exception:
            self.log.error(
                "Could not open ImageProperties.xml ({}).\n"
                "URL: {}".format(sys.exc_info()[1], xml_url)
            )
            raise FileNotFoundError

        # example: <IMAGE_PROPERTIES WIDTH="2679" HEIGHT="4000" NUMTILES="241" NUMIMAGES="1" VERSION="1.8" TILESIZE="256"/>
        properties = dict(re.findall(r"\b(\w+)\s*=\s*[\"']([^\"']*)[\"']", content))
        self.max_width = int(properties["WIDTH"])
        self.max_height = int(properties["HEIGHT"])
        self.tile_size = int(properties["TILESIZE"])

        # PROCESS PROPERTIES TO GET ADDITIONAL DERIVABLE PROPERTIES

        self.get_zoom_levels()  # get one-indexed maximum zoom level
        self.max_zoom = len(self.levels) - 1

        # GET THE REQUESTED ZOOMLEVEL
        zoom_level = int(zoom_level)
        if 0 <= zoom_level <= self.max_zoom:
            self.zoom_level = zoom_level
        elif -self.max_zoom - 1 <= zoom_level <= -1:
            self.zoom_level = self.max_zoom + zoom_level + 1
        else:
            self.log.error(
                "The requested zoom level {} is not available. Possible values are {} to {}."
                .format(zoom_level, -self.max_zoom - 1, self.max_zoom)
            )
            raise ZoomLevelError

        # GET THE SIZE AT THE REQUESTED ZOOM LEVEL
        self.width  = int(self.max_width  / 2 ** (self.max_zoom - self.zoom_level))
        self.height = int(self.max_height / 2 ** (self.max_zoom - self.zoom_level))

        # GET THE NUMBER OF TILES AT THE REQUESTED ZOOM LEVEL
        self.maxx_tiles, self.maxy_tiles = self.levels[-1]
        self.x_tiles, self.y_tiles = self.levels[self.zoom_level]

        self.log.debug('\tMax zoom level:    {:d} (working zoom level: {:d})'.format(self.max_zoom, self.zoom_level))
        self.log.debug('\tWidth (overall):   {:d} (at given zoom level: {:d})'.format(self.max_width, self.width))
        self.log.debug('\tHeight (overall):  {:d} (at given zoom level: {:d})'.format(self.max_height, self.height))
        self.log.debug('\tTile size:         {:d}'.format(self.tile_size))
        self.log.debug('\tWidth (in tiles):  {:d} (at given level: {:d})'.format(self.maxx_tiles, self.x_tiles))
        self.log.debug('\tHeight (in tiles): {:d} (at given level: {:d})'.format(self.maxy_tiles, self.y_tiles))
        self.log.debug('\tTotal tiles:       {:d} (to be retrieved: {:d})'.format(self.maxx_tiles * self.maxy_tiles,
                                                                                 self.x_tiles * self.y_tiles))
        # self.log.debug("\tUsing {} joining algorithm.".format(self.algorithm))

    def get_zoom_levels(self):
        """Construct a list of all zoomlevels with sizes in tiles"""
        loc_width = self.max_width
        loc_height = self.max_height
        self.levels = []
        while True:
            width_in_tiles = int(ceil(loc_width / float(self.tile_size)))
            height_in_tiles = int(ceil(loc_height / float(self.tile_size)))
            self.levels.append((width_in_tiles, height_in_tiles))

            if width_in_tiles == 1 and height_in_tiles == 1:
                break

            loc_width = int(loc_width / 2.)
            loc_height = int(loc_height / 2.)

        # make the 0th level the smallest zoom, and higher levels, higher zoom
        self.levels.reverse()
        self.log.debug("self.levels = {}".format(self.levels))

    def get_tile_index(self, level, x, y):
        """
        Get the zoomify index of a tile in a given level, at given co-ordinates
        This is needed to get the tilegroup.

        Keyword arguments:
        level -- the zoomlevel of the tile
        x,y -- the co-ordinates of the tile in that level

        Returns -- the zoomify index
        """

        index = x + y * int(ceil(floor(self.width / pow(2, self.max_zoom - level)) / self.tile_size))

        for i in range(level):
            index += int(ceil(floor(self.width / pow(2, self.max_zoom - i)) / self.tile_size)) * \
                     int(ceil(floor(self.height / pow(2, self.max_zoom - i)) / self.tile_size))

        return index

    def get_tile_url(self, col, row):
        """
        Return the full URL of an image at a given position in the Zoomify structure.
        """
        tile_index = self.get_tile_index(self.zoom_level, col, row)
        tile_group = tile_index // self.tile_size
        url = self.base_dir + 'TileGroup{}/{}-{}-{}.{}'.format(tile_group, self.zoom_level, col, row, self.ext)
        return url


if __name__ == "__main__":
    args = parser.parse_args()
    try:
        UntilerDezoomify(args)
    except FileNotFoundError:
        pass
    except ZoomLevelError:
        pass
    except JpegtranException:
        pass

#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: fenc=utf-8 fileformat=unix:
# Author: 2004-2008 Gulácsi Tamás
#
# Ported from Josh Carter's Perl IPTCInfo.pm by Tamás Gulácsi
#
# IPTCInfo: extractor for IPTC metadata embedded in images
# Copyright (C) 2000-2004 Josh Carter <josh@multipart-mixed.com>
# Copyright (C) 2004-2008 Tamás Gulácsi <gthomas@gthomas.hu>
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the same terms as Python itself.
#
# VERSION = '1.9';
"""
IPTCInfo - Python module for extracting and modifying IPTC image meta-data
"""
import contextlib
import logging
import os
import shutil
import sys
import tempfile
from struct import pack, unpack

__version__ = '1.9.5-8'
__author__ = 'Gulácsi, Tamás'

SURELY_WRITE_CHARSET_INFO = False
debugMode = 0
#  Debug off for production use


logger = logging.getLogger('iptcinfo')
LOGDBG = logging.getLogger('iptcinfo.debug')


class EOFException(Exception):
    def __init__(self, *args):
        super().__init__(self)
        self._str = '\n'.join(args)

    def __str__(self):
        return self._str


@contextlib.contextmanager
def smart_open(path, *args, **kwargs):
    """
    Lets you treat a fild handler as if it were a file path.

    Based on https://stackoverflow.com/a/17603000/8049516
    """
    if hasattr(path, 'read'):
        fh = path
    else:
        fh = open(path, *args, **kwargs)

    try:
        yield fh
    finally:
        fh.close()


def duck_typed(obj, prefs):
    if isinstance(prefs, str):
        prefs = [prefs]
    for pref in prefs:
        if not hasattr(obj, pref):
            return False
    return True


def file_is_jpeg(fh):
    """
    Checks to see if this file is a Jpeg/JFIF or not.

    Will reset the file position back to 0 after it's done in either case.
    """
    fh.seek(0)
    if debugMode:  # pragma: no cover
        logger.info("Opening 16 bytes of file: %r", hex_dump(fh.read(16)))
        fh.seek(0)

    ered = False
    try:
        (ff, soi) = fh.read(2)
        if not (ff == 0xff and soi == 0xd8):
            ered = False
        else:
            # now check for APP0 marker. I'll assume that anything with a
            # SOI followed by APP0 is "close enough" for our purposes.
            # (We're not dinking with image data, so anything following
            # the Jpeg tagging system should work.)
            (ff, app0) = fh.read(2)
            ered = ff == 0xff
    finally:
        fh.seek(0)
        return ered


def read_exactly(fh, length):
    """
    Reads exactly `length` bytes and throws an exception if EOF is hit.
    """
    buf = fh.read(length)
    if buf is None or len(buf) < length:
        raise EOFException('read_exactly: %s' % str(fh))

    return buf


def seek_exactly(fh, length):
    """
    Seeks length bytes from the current position and checks the result
    """
    pos = fh.tell()
    fh.seek(length, 1)
    if fh.tell() - pos != length:
        raise EOFException('seek_exactly')


def hex_dump(dump):  # pragma: no cover
    """Very helpful when debugging."""
    if not debugMode:
        return

    length = len(dump)
    P = lambda z: ((ord3(z) >= 0x21 and ord3(z) <= 0x7e) and [z] or ['.'])[0]
    ROWLEN = 18
    res = ['\n']
    for j in range(length // ROWLEN + int(length % ROWLEN > 0)):
        row = dump[j * ROWLEN:(j + 1) * ROWLEN]
        if isinstance(row, list):
            row = b''.join(row)
        res.append(
            ('%02X ' * len(row) + '   ' * (ROWLEN - len(row)) + '| %s\n') %
            tuple(list(map(ord3, list(row))) + [''.join(map(P, row))]))
    return ''.join(res)


def jpegDebugScan(filename):  # pragma: no cover
    """Also very helpful when debugging."""
    assert isinstance(filename, str) and os.path.isfile(filename)
    with open(filename, 'wb') as fh:

        # Skip past start of file marker
        (ff, soi) = fh.read(2)
        if not (ord3(ff) == 0xff and ord3(soi) == 0xd8):
            logger.error("JpegScan: invalid start of file")
        else:
            # scan to 0xDA (start of scan), dumping the markers we see between
            # here and there.
            while True:
                marker = jpeg_next_marker(fh)
                if ord3(marker) == 0xda:
                    break

                if ord3(marker) == 0:
                    logger.warn("Marker scan failed")
                    break
                elif ord3(marker) == 0xd9:
                    logger.debug("Marker scan hit end of image marker")
                    break

                if not jpeg_skip_variable(fh):
                    logger.warn("JpegSkipVariable failed")
                    return None


def jpeg_get_variable_length(fh):
    """Gets length of current variable-length section. File position
    at start must be on the marker itself, e.g. immediately after call
    to JPEGNextMarker. File position is updated to just past the
    length field."""
    try:
        length = unpack('!H', read_exactly(fh, 2))[0]
    except EOFException:
        return 0
    logger.debug('JPEG variable length: %d', length)

    # Length includes itself, so must be at least 2
    if length < 2:
        logger.warn("jpeg_get_variable_length: erroneous JPEG marker length")
        return 0
    return length - 2


def jpeg_next_marker(fh):
    """Scans to the start of the next valid-looking marker. Return
    value is the marker id.

    TODO use .read instead of .readExactly
    """

    # Find 0xff byte. We should already be on it.
    try:
        byte = read_exactly(fh, 1)
    except EOFException:
        return None

    while ord3(byte) != 0xff:
        logger.warn("jpeg_next_marker: warning: bogus stuff in Jpeg file")
        try:
            byte = read_exactly(fh, 1)
        except EOFException:
            return None
    # Now skip any extra 0xffs, which are valid padding.
    while True:
        try:
            byte = read_exactly(fh, 1)
        except EOFException:
            return None
        if ord3(byte) != 0xff:
            break

    # byte should now contain the marker id.
    logger.debug("jpeg_next_marker: at marker %02X (%d)", ord3(byte), ord3(byte))
    return byte


def jpeg_skip_variable(fh, rSave=None):
    """Skips variable-length section of Jpeg block. Should always be
    called between calls to JpegNextMarker to ensure JpegNextMarker is
    at the start of data it can properly parse."""

    # Get the marker parameter length count
    length = jpeg_get_variable_length(fh)
    if length == 0:
        return None

    # Skip remaining bytes
    if rSave is not None or debugMode > 0:
        try:
            temp = read_exactly(fh, length)
        except EOFException:
            logger.error("jpeg_skip_variable: read failed while skipping var data")
            return None
    else:
        # Just seek
        try:
            seek_exactly(fh, length)
        except EOFException:
            logger.error("jpeg_skip_variable: read failed while skipping var data")
            return None

    return (rSave is not None and [temp] or [True])[0]


sys_enc = sys.getfilesystemencoding()


def ord3(x):
    return x if isinstance(x, int) else ord(x)


#####################################
# These names match the codes defined in ITPC's IIM record 2.
# This hash is for non-repeating data items; repeating ones
# are in %listdatasets below.
c_datasets = {
    # 0: 'record version',    # skip -- binary data
    5: 'object name',
    7: 'edit status',
    8: 'editorial update',
    10: 'urgency',
    12: 'subject reference',
    15: 'category',
    20: 'supplemental category',
    22: 'fixture identifier',
    25: 'keywords',
    26: 'content location code',
    27: 'content location name',
    30: 'release date',
    35: 'release time',
    37: 'expiration date',
    38: 'expiration time',
    40: 'special instructions',
    42: 'action advised',
    45: 'reference service',
    47: 'reference date',
    50: 'reference number',
    55: 'date created',
    60: 'time created',
    62: 'digital creation date',
    63: 'digital creation time',
    65: 'originating program',
    70: 'program version',
    75: 'object cycle',
    80: 'by-line',
    85: 'by-line title',
    90: 'city',
    92: 'sub-location',
    95: 'province/state',
    100: 'country/primary location code',
    101: 'country/primary location name',
    103: 'original transmission reference',
    105: 'headline',
    110: 'credit',
    115: 'source',
    116: 'copyright notice',
    118: 'contact',
    120: 'caption/abstract',
    121: 'local caption',
    122: 'writer/editor',
    # 125: 'rasterized caption', # unsupported (binary data)
    130: 'image type',
    131: 'image orientation',
    135: 'language identifier',
    200: 'custom1',  # These are NOT STANDARD, but are used by
    201: 'custom2',  # Fotostation. Use at your own risk. They're
    202: 'custom3',  # here in case you need to store some special
    203: 'custom4',  # stuff, but note that other programs won't
    204: 'custom5',  # recognize them and may blow them away if
    205: 'custom6',  # you open and re-save the file. (Except with
    206: 'custom7',  # Fotostation, of course.)
    207: 'custom8',
    208: 'custom9',
    209: 'custom10',
    210: 'custom11',
    211: 'custom12',
    212: 'custom13',
    213: 'custom14',
    214: 'custom15',
    215: 'custom16',
    216: 'custom17',
    217: 'custom18',
    218: 'custom19',
    219: 'custom20',
}

c_datasets_r = {v: k for k, v in c_datasets.items()}


class IPTCData(dict):
    """Dict with int/string keys from c_listdatanames"""
    def __init__(self, diction={}, *args, **kwds):
        super().__init__(self, *args, **kwds)
        self.update({self.keyAsInt(k): v for k, v in diction.items()})

    c_cust_pre = 'nonstandard_'

    @classmethod
    def keyAsInt(cls, key):
        if isinstance(key, int):
            return key
        elif isinstance(key, str) and key.lower() in c_datasets_r:
            return c_datasets_r[key.lower()]
        elif (key.startswith(cls.c_cust_pre) and key[len(cls.c_cust_pre):].isdigit()):
            return int(key[len(cls.c_cust_pre):])
        else:
            raise KeyError("Key %s is not in %s!" % (key, list(c_datasets_r.keys())))

    @classmethod
    def keyAsStr(cls, key):
        if isinstance(key, str) and key in c_datasets_r:
            return key
        elif key in c_datasets:
            return c_datasets[key]
        elif isinstance(key, int):
            return cls.c_cust_pre + str(key)
        else:
            raise KeyError("Key %s is not in %s!" % (key, list(c_datasets.keys())))

    def __getitem__(self, name):
        return self.get(self.keyAsInt(name), None)

    def __setitem__(self, name, value):
        key = self.keyAsInt(name)
        if key in self and isinstance(super().__getitem__(key), (tuple, list)):
            if isinstance(value, (tuple, list)):
                dict.__setitem__(self, key, value)
            else:
                raise ValueError("%s must be iterable" % name)
        else:
            dict.__setitem__(self, key, value)


class IPTCInfo:
    """info = IPTCInfo('image filename goes here')

    File can be a file-like object or a string. If it is a string, it is
    assumed to be a filename.

    Returns IPTCInfo object filled with metadata from the given image
    file. File on disk will be closed, and changes made to the IPTCInfo
    object will *not* be flushed back to disk.

    If force==True, than forces an object to always be returned. This
    allows you to start adding stuff to files that don't have IPTC info
    and then save it.

    If inp_charset is None, then no translation is done to unicode (except
    when charset is encoded in the image metadata). In this case you should
    be VERY careful to use bytestrings overall with the SAME ENCODING!
    """

    error = None

    def __init__(self, fobj, force=False, inp_charset=None, out_charset=None,
                 *args, **kwds):
        self._data = IPTCData({
            'supplemental category': [],
            'keywords': [],
            'contact': [],
        })
        if duck_typed(fobj, 'read'):  # DELETEME
            self._filename = None
            self._fh = fobj
        else:
            self._filename = fobj

        self.inp_charset = inp_charset
        self.out_charset = out_charset or inp_charset

        with smart_open(fobj, 'rb') as fh:
            datafound = self.scanToFirstIMMTag(fh)
            if datafound or force:
                # Do the real snarfing here
                if datafound:
                    self.collectIIMInfo(fh)
            else:
                logger.warn('No IPTC data found in %s', fobj)

    def _closefh(self, fh):  # DELETEME
        if fh and self._filename is not None:
            fh.close()

    def _getfh(self, mode='r'):  # DELETEME
        assert self._filename is not None or self._fh is not None
        if self._filename is not None:
            fh = open(self._filename, (mode + 'b').replace('bb', 'b'))
            if not fh:
                logger.error("Can't open file (%r)", self._filename)
                return None
            else:
                return fh
        else:
            return self._fh

    def save(self, options=None):
        """Saves Jpeg with IPTC data back to the same file it came from."""
        assert self._filename is not None
        return self.save_as(self._filename, options)

    def _filepos(self, fh):
        fh.flush()
        return fh.tell()

    def save_as(self, newfile, options=None):
        """Saves Jpeg with IPTC data to a given file name."""
        fh = self._getfh()
        assert fh
        fh.seek(0, 0)
        if not file_is_jpeg(fh):
            logger.error("Source file is not a Jpeg; I can only save Jpegs. Sorry.")
            return None

        ret = self.jpegCollectFileParts(fh, options)
        self._closefh(fh)
        if ret is None:
            logger.error("collectfileparts failed")
            raise Exception('collectfileparts failed')

        (start, end, adobe) = ret
        LOGDBG.debug('start: %d, end: %d, adobe:%d', *list(map(len, ret)))
        hex_dump(start)
        LOGDBG.debug('adobe1: %r', adobe)
        if options is not None and 'discardAdobeParts' in options:
            adobe = None
        LOGDBG.debug('adobe2: %r', adobe)

        LOGDBG.info('writing...')
        (tmpfd, tmpfn) = tempfile.mkstemp()
        if self._filename and os.path.exists(self._filename):
            shutil.copystat(self._filename, tmpfn)
        tmpfh = os.fdopen(tmpfd, 'wb')
        if not tmpfh:
            logger.error("Can't open output file %r", tmpfn)
            return None

        LOGDBG.debug('start=%d end=%d', len(start), len(end))
        tmpfh.write(start)
        # character set
        ch = self.c_charset_r.get(self.out_charset, None)
        # writing the character set is not the best practice
        # - couldn't find the needed place (record) for it yet!
        if SURELY_WRITE_CHARSET_INFO and ch is not None:
            tmpfh.write(pack("!BBBHH", 0x1c, 1, 90, 4, ch))

        LOGDBG.debug('pos: %d', self._filepos(tmpfh))
        data = self.photoshopIIMBlock(adobe, self.packedIIMData())
        LOGDBG.debug('data len=%d dmp=%r', len(data), hex_dump(data))
        tmpfh.write(data)
        LOGDBG.debug('pos: %d', self._filepos(tmpfh))
        tmpfh.write(end)
        LOGDBG.debug('pos: %d', self._filepos(tmpfh))
        tmpfh.flush()

        if hasattr(tmpfh, 'getvalue'):  # StringIO
            fh2 = open(newfile, 'wb')
            fh2.truncate()
            fh2.seek(0, 0)
            fh2.write(tmpfh.getvalue())
            fh2.flush()
            fh2.close()
            tmpfh.close()
            os.unlink(tmpfn)
        else:
            tmpfh.close()
            if os.path.exists(newfile):
                shutil.move(newfile, newfile + '~')
            shutil.move(tmpfn, newfile)
        return True

    def saveToBuf(self, buf, options=None):
        """
        Usage:
        import iptcinfo3
        from io import BytesIO

        iptc = iptcinfo3.IPTCInfo(src_jpeg)
        # change iptc data here..

        # Save JPEG with new IPTC to the buf:
        buf = BytesIO()
        iptc.saveToBuf(buf)

        # Save JPEG with new IPTC to a file:
        with open("/tmp/file.jpg", "wb") as f:
            iptc.saveToBuf(f)
        """

        fh = self._getfh()
        fh.seek(0, 0)
        if not file_is_jpeg(fh):
            self._closefh(fh)
            raise ValueError('Source file is not a valid JPEG')

        ret = self.jpegCollectFileParts(fh, options)
        self._closefh(fh)

        if ret is None:
            raise ValueError('No IPTC data found')

        (start, end, adobe) = ret
        if options is not None and 'discardAdobeParts' in options:
            adobe = None

        buf.write(start)
        ch = self.c_charset_r.get(self.out_charset, None)
        # writing the character set is not the best practice
        # - couldn't find the needed place (record) for it yet!
        if SURELY_WRITE_CHARSET_INFO and ch is not None:
            buf.write(pack("!BBBHH", 0x1c, 1, 90, 4, ch))

        data = self.photoshopIIMBlock(adobe, self.packedIIMData())
        buf.write(data)
        buf.write(end)

        return buf

    def __del__(self):
        """Called when object is destroyed.
        No action necessary in this case."""
        pass

    def __len__(self):
        return len(self._data)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def __str__(self):
        return ('charset: %s\n%s' % (self.inp_charset,
                str(dict((self._data.keyAsStr(k), v)
                         for k, v in list(self._data.items())))))

    def scanToFirstIMMTag(self, fh):
        """Scans to first IIM Record 2 tag in the file. The will either
        use smart scanning for Jpegs or blind scanning for other file
        types."""
        if file_is_jpeg(fh):
            logger.info("File is Jpeg, proceeding with JpegScan")
            return self.jpegScan(fh)
        else:
            logger.warn("File not a JPEG, trying blindScan")
            return self.blindScan(fh)

    c_marker_err = {0: "Marker scan failed",
                    0xd9:  "Marker scan hit end of image marker",
                    0xda: "Marker scan hit start of image data"}

    def jpegScan(self, fh):
        """Assuming the file is a Jpeg (see above), this will scan through
        the markers looking for the APP13 marker, where IPTC/IIM data
        should be found. While this isn't a formally defined standard, all
        programs have (supposedly) adopted Adobe's technique of putting
        the data in APP13."""
        # Skip past start of file marker
        try:
            (ff, soi) = read_exactly(fh, 2)
        except EOFException:
            return None

        if not (ord3(ff) == 0xff and ord3(soi) == 0xd8):
            self.error = "JpegScan: invalid start of file"
            logger.error(self.error)
            return None

        # Scan for the APP13 marker which will contain our IPTC info (I hope).
        while True:
            err = None
            marker = jpeg_next_marker(fh)
            if ord3(marker) == 0xed:
                break  # 237

            err = self.c_marker_err.get(ord3(marker), None)
            if err is None and jpeg_skip_variable(fh) == 0:
                err = "JpegSkipVariable failed"
            if err is not None:
                self.error = err
                logger.warn(err)
                return None

        # If were's here, we must have found the right marker.
        # Now blindScan through the data.
        return self.blindScan(fh, MAX=jpeg_get_variable_length(fh))

    c_charset = {100: 'iso8859_1', 101: 'iso8859_2', 109: 'iso8859_3',
                 110: 'iso8859_4', 111: 'iso8859_5', 125: 'iso8859_7',
                 127: 'iso8859_6', 138: 'iso8859_8',
                 196: 'utf_8'}
    c_charset_r = {v: k for k, v in c_charset.items()}

    def blindScan(self, fh, MAX=8192):
        """Scans blindly to first IIM Record 2 tag in the file. This
        method may or may not work on any arbitrary file type, but it
        doesn't hurt to check. We expect to see this tag within the first
        8k of data. (This limit may need to be changed or eliminated
        depending on how other programs choose to store IIM.)"""

        offset = 0
        # keep within first 8192 bytes
        # NOTE: this may need to change
        logger.debug('blindScan: starting scan, max length %d', MAX)

        # start digging
        while offset <= MAX:
            try:
                temp = read_exactly(fh, 1)
            except EOFException:
                logger.warn("BlindScan: hit EOF while scanning")
                return None
            # look for tag identifier 0x1c
            if ord3(temp) == 0x1c:
                # if we found that, look for record 2, dataset 0
                # (record version number)
                (record, dataset) = fh.read(2)
                if record == 1 and dataset == 90:
                    # found character set's record!
                    try:
                        temp = read_exactly(fh, jpeg_get_variable_length(fh))
                        try:
                            cs = unpack('!H', temp)[0]
                        except:
                            logger.warn('WARNING: problems with charset recognition (%r)', temp)
                            cs = None
                        if cs in self.c_charset:
                            self.inp_charset = self.c_charset[cs]
                        logger.info("BlindScan: found character set '%s' at offset %d",
                                    self.inp_charset, offset)
                    except EOFException:
                        pass

                elif record == 2:
                    # found it. seek to start of this tag and return.
                    logger.debug("BlindScan: found IIM start at offset %d", offset)
                    try:  # seek rel to current position
                        seek_exactly(fh, -3)
                    except EOFException:
                        return None
                    return offset

                else:
                    # didn't find it. back up 2 to make up for
                    # those reads above.
                    try:  # seek rel to current position
                        seek_exactly(fh, -2)
                    except EOFException:
                        return None

            # no tag, keep scanning
            offset += 1

        return False

    def collectIIMInfo(self, fh):
        """Assuming file is seeked to start of IIM data (using above),
        this reads all the data into our object's hashes"""
        # NOTE: file should already be at the start of the first
        # IPTC code: record 2, dataset 0.
        assert duck_typed(fh, 'read')
        while True:
            try:
                header = read_exactly(fh, 5)
            except EOFException:
                return None

            (tag, record, dataset, length) = unpack("!BBBH", header)
            # bail if we're past end of IIM record 2 data
            if not (tag == 0x1c and record == 2):
                return None

            alist = {'tag': tag, 'record': record, 'dataset': dataset,
                     'length': length}
            logger.debug('\n'.join('%s\t: %s' % (k, v) for k, v in list(alist.items())))
            value = fh.read(length)

            if self.inp_charset:
                try:
                    value = str(value, encoding=self.inp_charset, errors='strict')
                except:
                    logger.warn('Data "%r" is not in encoding %s!', value, self.inp_charset)
                    value = str(value, encoding=self.inp_charset, errors='replace')

            # try to extract first into _listdata (keywords, categories)
            # and, if unsuccessful, into _data. Tags which are not in the
            # current IIM spec (version 4) are currently discarded.
            if (dataset in self._data
                    and hasattr(self._data[dataset], 'append')):
                self._data[dataset].append(value)
            elif dataset != 0:
                self._data[dataset] = value

    #######################################################################
    # File Saving
    #######################################################################

    def jpegCollectFileParts(self, fh, discardAppParts=False):
        """Collects all pieces of the file except for the IPTC info that
        we'll replace when saving. Returns the stuff before the info,
        stuff after, and the contents of the Adobe Resource Block that the
        IPTC data goes in.

        Returns None if a file parsing error occured.
        """

        assert duck_typed(fh, ['seek', 'read'])
        adobeParts = b''
        start = []

        # Start at beginning of file
        fh.seek(0, 0)
        # Skip past start of file marker
        (ff, soi) = fh.read(2)
        if not (ord3(ff) == 0xff and ord3(soi) == 0xd8):
            self.error = "JpegScan: invalid start of file"
            logger.error(self.error)
            return None

        # Begin building start of file
        start.append(pack("BB", 0xff, 0xd8))

        # Get first marker in file. This will be APP0 for JFIF or APP1 for
        # EXIF.
        marker = jpeg_next_marker(fh)
        app0data = b''
        app0data = jpeg_skip_variable(fh, app0data)
        if app0data is None:
            self.error = 'jpegSkipVariable failed'
            logger.error(self.error)
            return None

        if ord3(marker) == 0xe0 or not discardAppParts:
            # Always include APP0 marker at start if it's present.
            start.append(pack('BB', 0xff, ord3(marker)))
            # Remember that the length must include itself (2 bytes)
            start.append(pack('!H', len(app0data) + 2))
            start.append(app0data)
        else:
            # Manually insert APP0 if we're trashing application parts, since
            # all JFIF format images should start with the version block.
            LOGDBG.debug('discardAppParts=%r', discardAppParts)
            start.append(pack("BB", 0xff, 0xe0))
            start.append(pack("!H", 16))    # length (including these 2 bytes)
            start.append("JFIF")            # format
            start.append(pack("BB", 1, 2))  # call it version 1.2 (current JFIF)
            start.append(pack('8B', 0))     # zero everything else

        # Now scan through all markers in file until we hit image data or
        # IPTC stuff.
        end = []
        while True:
            marker = jpeg_next_marker(fh)
            if marker is None or ord3(marker) == 0:
                self.error = "Marker scan failed"
                logger.error(self.error)
                return None
            # Check for end of image
            elif ord3(marker) == 0xd9:
                logger.debug("JpegCollectFileParts: saw end of image marker")
                end.append(pack("BB", 0xff, ord3(marker)))
                break
            # Check for start of compressed data
            elif ord3(marker) == 0xda:
                logger.debug("JpegCollectFileParts: saw start of compressed data")
                end.append(pack("BB", 0xff, ord3(marker)))
                break
            partdata = b''
            partdata = jpeg_skip_variable(fh, partdata)
            if not partdata:
                self.error = "JpegSkipVariable failed"
                logger.error(self.error)
                return None
            partdata = bytes(partdata)

            # Take all parts aside from APP13, which we'll replace
            # ourselves.
            if (discardAppParts and ord3(marker) >= 0xe0
                    and ord3(marker) <= 0xef):
                # Skip all application markers, including Adobe parts
                adobeParts = b''
            elif ord3(marker) == 0xed:
                # Collect the adobe stuff from part 13
                adobeParts = self.collectAdobeParts(partdata)
                break
            else:
                # Append all other parts to start section
                start.append(pack("BB", 0xff, ord3(marker)))
                start.append(pack("!H", len(partdata) + 2))
                start.append(partdata)

        # Append rest of file to end
        while True:
            buff = fh.read(8192)
            if buff is None or len(buff) == 0:
                break

            end.append(buff)

        return (b''.join(start), b''.join(end), adobeParts)

    def collectAdobeParts(self, data):
        """Part APP13 contains yet another markup format, one defined by
        Adobe.  See"File Formats Specification" in the Photoshop SDK
        (avail from www.adobe.com). We must take
        everything but the IPTC data so that way we can write the file back
        without losing everything else Photoshop stuffed into the APP13
        block."""
        assert isinstance(data, bytes)
        length = len(data)
        offset = 0
        out = []
        # Skip preamble
        offset = len('Photoshop 3.0 ')
        # Process everything
        while offset < length:
            # Get OSType and ID
            (ostype, id1, id2) = unpack("!LBB", data[offset:offset + 6])
            offset += 6
            if offset >= length:
                break

            # Get pascal string
            stringlen = unpack("B", data[offset:offset + 1])[0]
            offset += 1
            if offset >= length:
                break
            string = data[offset:offset + stringlen]
            offset += stringlen

            # round up if odd
            if (stringlen % 2 != 0):
                offset += 1
            # there should be a null if string len is 0
            if stringlen == 0:
                offset += 1
            if offset >= length:
                break

            # Get variable-size data
            size = unpack("!L", data[offset:offset + 4])[0]
            offset += 4
            if offset >= length:
                break

            var = data[offset:offset + size]
            offset += size
            if size % 2 != 0:
                offset += 1  # round up if odd

            # skip IIM data (0x0404), but write everything else out
            if not (id1 == 4 and id2 == 4):
                out.append(pack("!LBB", ostype, id1, id2))
                out.append(pack("B", stringlen))
                out.append(string)
                if stringlen == 0 or stringlen % 2 != 0:
                    out.append(pack("B", 0))
                out.append(pack("!L", size))
                out.append(var)
                out = [''.join(out)]
                if size % 2 != 0 and len(out[0]) % 2 != 0:
                    out.append(pack("B", 0))

        return b''.join(out)

    def _enc(self, text):
        """Recodes the given text from the old character set to utf-8"""
        res = text
        out_charset = self.out_charset or self.inp_charset
        if isinstance(text, str):
            res = text.encode(out_charset or 'utf8')
        elif isinstance(text, str) and out_charset:
            try:
                res = str(text, encoding=self.inp_charset).encode(
                    out_charset)
            except (UnicodeEncodeError, UnicodeDecodeError):
                logger.error("_enc: charset %s is not working for %s", self.inp_charset, text)
                res = str(text, encoding=self.inp_charset, errors='replace').encode(out_charset)
        elif isinstance(text, (list, tuple)):
            res = type(text)(list(map(self._enc, text)))
        return res

    def packedIIMData(self):
        """Assembles and returns our _data and _listdata into IIM format for
        embedding into an image."""
        out = []
        (tag, record) = (0x1c, 0x02)
        # Print record version
        # tag - record - dataset - len (short) - 4 (short)
        out.append(pack("!BBBHH", tag, record, 0, 2, 4))

        LOGDBG.debug('out=%r', hex_dump(out))
        # Iterate over data sets
        for dataset, value in list(self._data.items()):
            if len(value) == 0:
                continue
            if not (isinstance(dataset, int) and dataset in c_datasets):
                logger.warn("PackedIIMData: illegal dataname '%s' (%d)", dataset, dataset)
                continue
            logger.debug('packedIIMData %r -> %r', value, self._enc(value))
            value = self._enc(value)
            if not isinstance(value, list):
                value = bytes(value)
                out.append(pack("!BBBH", tag, record, dataset, len(value)))
                out.append(value)
            else:
                for v in map(bytes, value):
                    if v is None or len(v) == 0:
                        continue
                    out.append(pack("!BBBH", tag, record, dataset, len(v)))
                    out.append(v)

        return b''.join(out)

    def photoshopIIMBlock(self, otherparts, data):
        """Assembles the blob of Photoshop "resource data" that includes our
        fresh IIM data (from PackedIIMData) and the other Adobe parts we
        found in the file, if there were any."""
        out = []
        assert isinstance(data, bytes)
        resourceBlock = [b"Photoshop 3.0"]
        resourceBlock.append(pack("B", 0))
        # Photoshop identifier
        resourceBlock.append(b"8BIM")
        # 0x0404 is IIM data, 00 is required empty string
        resourceBlock.append(pack("BBBB", 0x04, 0x04, 0, 0))
        # length of data as 32-bit, network-byte order
        resourceBlock.append(pack("!L", len(data)))
        # Now tack data on there
        resourceBlock.append(data)
        # Pad with a blank if not even size
        if len(data) % 2 != 0:
            resourceBlock.append(pack("B", 0))
        # Finally tack on other data
        if otherparts is not None:
            resourceBlock.append(otherparts)
        resourceBlock = b''.join(resourceBlock)

        out.append(pack("BB", 0xff, 0xed))  # Jpeg start of block, APP13
        out.append(pack("!H", len(resourceBlock) + 2))  # length
        out.append(resourceBlock)

        return b''.join(out)


if __name__ == '__main__':  # pragma: no cover
    logging.basicConfig(level=logging.DEBUG)
    if len(sys.argv) > 1:
        info = IPTCInfo(sys.argv[1])
        print(info)

#!/usr/bin/env python
# coding: utf-8

"""
mpyq is a Python library for reading MPQ (MoPaQ) archives.
"""

from __future__ import print_function

import bz2
import os
import struct
import zlib
import tempfile, shutil, subprocess
from collections import namedtuple
from io import BytesIO


__author__ = "Aku Kotkavuo"
__version__ = "0.2.5"


MPQ_FILE_IMPLODE        = 0x00000100
MPQ_FILE_COMPRESS       = 0x00000200
MPQ_FILE_ENCRYPTED      = 0x00010000
MPQ_FILE_FIX_KEY        = 0x00020000
MPQ_FILE_SINGLE_UNIT    = 0x01000000
MPQ_FILE_DELETE_MARKER  = 0x02000000
MPQ_FILE_SECTOR_CRC     = 0x04000000
MPQ_FILE_EXISTS         = 0x80000000

from enum import Enum
CompressionType = Enum('CompressionType', dict(
    NONE = 0,
    DEFLATE = 2,    # zlib
    IMPLODE = 8,    # actually a TTComp compressed stream, see
                    # http://fileformats.archiveteam.org/wiki/TTComp_archive,
                    # same as IMPLODE which implements a Sannon-Fano-based
                    # algorithm, from the PKWare Inc. tools.
                    # C implementations (no pure python impl so far):
                    # https://github.com/ge0rg/libmpq/blob/master/libmpq/explode.c
                    # https://github.com/ladislav-zezula/StormLib/blob/master/src/pklib/explode.c
                    # http://www.exelana.com/techie/c/ttdecomp.html from
                    #  https://github.com/axx1611/lawine, an old Google project from
                    #  https://code.google.com/archive/p/lawine/
                    # Possible Python impl: https://github.com/Mohammed-Ashour/Shannon-Fano-Algorithm
                    # At the moment we use the external ttdecomp tool to process these.
    BZIP2 = 16,
    LZMA = 18,
    SPARSE = 32,
    ADPCM = 64,
    ADPCM_STEREO = 128,
))

MPQFileHeader = namedtuple('MPQFileHeader',
    '''
    magic
    header_size
    archive_size
    format_version
    sector_size_shift
    hash_table_offset
    block_table_offset
    hash_table_entries
    block_table_entries
    '''
)
MPQFileHeader.struct_format = '<4s2I2H4I'

MPQFileHeaderExt = namedtuple('MPQFileHeaderExt',
    '''
    extended_block_table_offset
    hash_table_offset_high
    block_table_offset_high
    '''
)
MPQFileHeaderExt.struct_format = 'q2h'

MPQUserDataHeader = namedtuple('MPQUserDataHeader',
    '''
    magic
    user_data_size
    mpq_header_offset
    user_data_header_size
    '''
)
MPQUserDataHeader.struct_format = '<4s3I'

MPQHashTableEntry = namedtuple('MPQHashTableEntry',
    '''
    hash_a
    hash_b
    locale
    platform
    block_table_index
    '''
)
MPQHashTableEntry.struct_format = '2I2HI'

MPQBlockTableEntry = namedtuple('MPQBlockTableEntry',
    '''
    offset
    archived_size
    size
    flags
    '''
)
MPQBlockTableEntry.struct_format = '4I'


class MPQArchive(object):

    def __init__(self, filename, listfile=True):
        """Create a MPQArchive object.

        You can skip reading the listfile if you pass listfile=False
        to the constructor. The 'files' attribute will be unavailable
        if you do this.
        """
        if hasattr(filename, 'read'):
            self.file = filename
        else:
            self.file = open(filename, 'rb')
        self.header = self.read_header()
        self.hash_table = self.read_table('hash')
        self.block_table = self.read_table('block')
        if listfile:
            self.files = self.read_file('(listfile)').splitlines()
        else:
            self.files = None

    def read_header(self):
        """Read the header of a MPQ archive."""

        def read_mpq_header(offset=None):
            if offset:
                self.file.seek(offset)
            data = self.file.read(32)
            header = MPQFileHeader._make(
                struct.unpack(MPQFileHeader.struct_format, data))
            header = header._asdict()
            if header['format_version'] == 1:
                data = self.file.read(12)
                extended_header = MPQFileHeaderExt._make(
                    struct.unpack(MPQFileHeaderExt.struct_format, data))
                header.update(extended_header._asdict())
            return header

        def read_mpq_user_data_header():
            data = self.file.read(16)
            header = MPQUserDataHeader._make(
                struct.unpack(MPQUserDataHeader.struct_format, data))
            header = header._asdict()
            header['content'] = self.file.read(header['user_data_header_size'])
            return header

        magic = self.file.read(4)
        self.file.seek(0)

        if magic == b'MPQ\x1a':
            header = read_mpq_header()
            header['offset'] = 0
        elif magic == b'MPQ\x1b':
            user_data_header = read_mpq_user_data_header()
            header = read_mpq_header(user_data_header['mpq_header_offset'])
            header['offset'] = user_data_header['mpq_header_offset']
            header['user_data_header'] = user_data_header
        else:
            raise ValueError("Invalid file header.")

        return header

    def read_table(self, table_type):
        """Read either the hash or block table of a MPQ archive."""

        if table_type == 'hash':
            entry_class = MPQHashTableEntry
        elif table_type == 'block':
            entry_class = MPQBlockTableEntry
        else:
            raise ValueError("Invalid table type.")

        table_offset = self.header['%s_table_offset' % table_type]
        table_entries = self.header['%s_table_entries' % table_type]
        key = self._hash('(%s table)' % table_type, 'TABLE')

        self.file.seek(table_offset + self.header['offset'])
        data = self.file.read(table_entries * 16)
        data = self._decrypt(data, key)

        def unpack_entry(position):
            entry_data = data[position*16:position*16+16]
            return entry_class._make(
                struct.unpack(entry_class.struct_format, entry_data))

        return [unpack_entry(i) for i in range(table_entries)]

    def get_hash_table_entry(self, filename):
        """Get the hash table entry corresponding to a given filename."""
        hash_a = self._hash(filename, 'HASH_A')
        hash_b = self._hash(filename, 'HASH_B')
        for entry in self.hash_table:
            if (entry.hash_a == hash_a and entry.hash_b == hash_b):
                return entry

    def get_file_key(self, filename, block_entry, strip=True):
        basename = os.path.basename(filename.replace('\\', '//')) if strip else filename
        key = self._hash(basename, 'TABLE')
        if block_entry.flags & MPQ_FILE_FIX_KEY:
            key += block_entry.offset - self.header['offset']
            key ^= block_entry.size
        return key

    def read_file(self, filename, force_decompress=False):
        """Read a file from the MPQ archive."""

        print('processing', filename, '...')
        def decompress(data):
            """Read the compression type and decompress file data."""

            try:
                compression_type, data = CompressionType(data[0]), data[1:]
            except ValueError as e:
                e.message = "warning: Unsupported compression type: {} for file {} (len={}).".format(hex(data[0]), filename, len(data))
                raise

            if compression_type == CompressionType.NONE:
                return data
            elif compression_type == CompressionType.DEFLATE:
                return zlib.decompress(data, 15)
            elif compression_type == CompressionType.BZIP2:
                return bz2.decompress(data)
            elif compression_type == CompressionType.IMPLODE:
                ttdecomp = shutil.which('ttdecomp')
                if ttdecomp:
                    tmpfd, tmpname = tempfile.mkstemp()
                    os.write(tmpfd, data)
                    os.close(tmpfd)
                    proc = subprocess.run([ttdecomp, tmpname, '/dev/stdout'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if proc.returncode != 0:
                        errmsg = 'Unable to decompress data: ' + data.hex()[:32] + '...'
                        err_output = proc.stderr
                        print('warning:', errmsg)
                        if err_output:
                            print('ttdecomp output:')
                            print(err_output.decode())
                    os.unlink(tmpname)
                    data = proc.stdout
                else:
                   print('warning: unable to find ttdecomp tool in $PATH to decompress the stream')
                return data
            elif compression_type == CompressionType.LZMA:
                print('warning: compression method {} not implemented'.format(hex(compression_type.value)))
                return data
            elif compression_type == CompressionType.SPARSE:
                print('warning: compression method {} not implemented'.format(hex(compression_type.value)))
                return data
            elif compression_type == CompressionType.ADPCM:
                print('warning: compression method {} not implemented'.format(hex(compression_type.value)))
                return data
            elif compression_type == CompressionType.ADPCM_STEREO:
                print('warning: compression method {} not implemented'.format(hex(compression_type.value)))
                return data

        hash_entry = self.get_hash_table_entry(filename)
        if hash_entry is None:
            return None
        block_entry = self.block_table[hash_entry.block_table_index]

        # Read the block.
        if block_entry.flags & MPQ_FILE_EXISTS:
            if block_entry.archived_size == 0:
                return None

            offset = block_entry.offset + self.header['offset']
            self.file.seek(offset)
            file_data = self.file.read(block_entry.archived_size)

            if block_entry.flags & MPQ_FILE_ENCRYPTED:
                key = self.get_file_key(filename, block_entry)

            if not block_entry.flags & MPQ_FILE_SINGLE_UNIT:
                # File consists of many sectors. They all need to be
                # decompressed separately and united.
                sector_size = 512 << self.header['sector_size_shift']
                sectors = block_entry.size // sector_size + 1
                if block_entry.flags & MPQ_FILE_SECTOR_CRC:
                    crc = True
                    sectors += 1
                else:
                    crc = False

                # The positions (Sector Offset Table) may be encrypted too.
                # The key value is base key - 1.
                n_offsets = sectors + 1
                offset_table = file_data[:4 * n_offsets]
                if block_entry.flags & MPQ_FILE_ENCRYPTED:
                    offset_table = self._decrypt(offset_table, key - 1)
                positions = struct.unpack('<%dI' % n_offsets, offset_table)

                result = BytesIO()
                sector_bytes_left = block_entry.size
                for i in range(len(positions) - (2 if crc else 1)):
                    sector = file_data[positions[i]:positions[i+1]]

                    if block_entry.flags & MPQ_FILE_ENCRYPTED:
                        # Data in each sector is encrypted with a key value of
                        # base key + index of sector.
                        sector = self._decrypt(sector, key + i)

                    # Some block entries are actually compressed, but do not
                    # give any indication. Fix it with a special case check.
                    flags = block_entry.flags
                    if sector.startswith(b'\x00\x06'):
                        flags |= MPQ_FILE_COMPRESS
                        sector = bytes([CompressionType.IMPLODE.value]) + sector

                    if (flags & MPQ_FILE_COMPRESS and
                        (force_decompress or sector_bytes_left > len(sector))):
                            sector = decompress(sector)

                    sector_bytes_left -= len(sector)
                    result.write(sector)
                file_data = result.getvalue()
            else:
                # Single unit files only need to be decompressed, but
                # compression only happens when at least one byte is gained.
                if (block_entry.flags & MPQ_FILE_COMPRESS and
                    (force_decompress or block_entry.size > block_entry.archived_size)):
                    file_data = decompress(file_data)

            return file_data

    def extract(self, files=None):
        """Extract all the files inside the MPQ archive in memory."""
        files = files or self.files
        if files:
            return dict((f, self.read_file(f)) for f in files)
        else:
            raise RuntimeError("Can't extract whole archive without listfile.")

    def extract_to_disk(self, files=None):
        """Extract all files and write them to disk."""
        archive_name, extension = os.path.splitext(os.path.basename(self.file.name))
        if not os.path.isdir(os.path.join(os.getcwd(), archive_name)):
            os.mkdir(archive_name)
        os.chdir(archive_name)
        for filename, data in self.extract(files).items():
            basename = os.path.basename(filename.replace('\\', '/'))
            f = open(basename, 'wb')
            f.write(data or b'')
            f.close()

    def extract_files(self, *filenames):
        """Extract given files from the archive to disk."""
        for filename in filenames:
            data = self.read_file(filename)
            basename = os.path.basename(filename.replace('\\', '/'))
            f = open(basename, 'wb')
            f.write(data or b'')
            f.close()

    def print_headers(self):
        print("MPQ archive header")
        print("------------------")
        for key, value in self.header.items():
            if key == "user_data_header":
                continue
            print("{0:30} {1!r}".format(key, value))
        if self.header.get('user_data_header'):
            print()
            print("MPQ user data header")
            print("--------------------")
            for key, value in self.header['user_data_header'].items():
                print("{0:30} {1!r}".format(key, value))
        print()

    def print_hash_table(self):
        print("MPQ archive hash table")
        print("----------------------")
        print(" Hash A   Hash B  Locl Plat BlockIdx")
        for entry in self.hash_table:
            print('{0:0>8X} {1:0>8X} {2:0>4X} {3:0>4X} {4:0>8X}'.format(*entry))
        print()

    def print_block_table(self):
        print("MPQ archive block table")
        print("-----------------------")
        print(" Offset  ArchSize RealSize  Flags")
        for entry in self.block_table:
            print('{0:0>8X} {1:>8} {2:>8} {3:>8X}'.format(*entry))
        print()

    def print_files(self):
        if self.files:
            print("Files")
            print("-----")
            width = max(len(name) for name in self.files) + 2
            for filename in self.files:
                hash_entry = self.get_hash_table_entry(filename)
                block_entry = self.block_table[hash_entry.block_table_index]
                print("{0:{width}} {1:>8} bytes".format(filename.decode(),
                                                        block_entry.size,
                                                        width=width))

    def _hash(self, string, hash_type):
        """Hash a string using MPQ's hash function."""
        hash_types = {
            'TABLE_OFFSET': 0,
            'HASH_A': 1,
            'HASH_B': 2,
            'TABLE': 3,
            'HASH_NUM': 4,
        }
        seed1 = 0x7FED7FED
        seed2 = 0xEEEEEEEE

        for ch in string.upper():
            if not isinstance(ch, int): ch = ord(ch)
            value = self.encryption_table[(hash_types[hash_type] << 8) + ch]
            seed1 = (value ^ (seed1 + seed2)) & 0xFFFFFFFF
            seed2 = ch + seed1 + seed2 + (seed2 << 5) + 3 & 0xFFFFFFFF

        return seed1

    def _decrypt(self, data, key):
        """Decrypt hash or block table or a sector."""
        seed1 = key
        seed2 = 0xEEEEEEEE
        result = BytesIO()

        for i in range(len(data) // 4):
            seed2 += self.encryption_table[0x400 + (seed1 & 0xFF)]
            seed2 &= 0xFFFFFFFF
            value = struct.unpack("<I", data[i*4:i*4+4])[0]
            value = (value ^ (seed1 + seed2)) & 0xFFFFFFFF

            seed1 = ((~seed1 << 0x15) + 0x11111111) | (seed1 >> 0x0B)
            seed1 &= 0xFFFFFFFF
            seed2 = value + seed2 + (seed2 << 5) + 3 & 0xFFFFFFFF

            result.write(struct.pack("<I", value))

        return result.getvalue()

    def _prepare_encryption_table():
        """Prepare encryption table for MPQ hash function."""
        seed = 0x00100001
        crypt_table = {}

        for i in range(256):
            index = i
            for j in range(5):
                seed = (seed * 125 + 3) % 0x2AAAAB
                temp1 = (seed & 0xFFFF) << 0x10

                seed = (seed * 125 + 3) % 0x2AAAAB
                temp2 = (seed & 0xFFFF)

                crypt_table[index] = (temp1 | temp2)

                index += 0x100

        return crypt_table

    encryption_table = _prepare_encryption_table()


def main():
    import argparse
    description = "mpyq reads and extracts MPQ archives."
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("file", action="store", help="path to the archive")
    parser.add_argument("-I", "--headers", action="store_true", dest="headers",
                        help="print header information from the archive")
    parser.add_argument("-H", "--hash-table", action="store_true",
                        dest="hash_table", help="print hash table"),
    parser.add_argument("-b", "--block-table", action="store_true",
                        dest="block_table", help="print block table"),
    parser.add_argument("-s", "--skip-listfile", action="store_true",
                        dest="skip_listfile", help="skip reading (listfile)"),
    parser.add_argument("-t", "--list-files", action="store_true", dest="list",
                        help="list files inside the archive")
    parser.add_argument("-x", "--extract", action="store_true", dest="extract",
                        help="extract files from the archive")
    parser.add_argument("-L", "--listfile", action="store",
                        help="path to the external listfile")
    args = parser.parse_args()
    if args.file:
        if not args.skip_listfile:
            archive = MPQArchive(args.file)
        else:
            archive = MPQArchive(args.file, listfile=False)
        if args.headers:
            archive.print_headers()
        if args.hash_table:
            archive.print_hash_table()
        if args.block_table:
            archive.print_block_table()
        if args.list:
            archive.print_files()
        if args.extract:
            with open(args.listfile, 'r') as fp:
                files = fp.read().splitlines()
            archive.extract_to_disk(files=files)


if __name__ == '__main__':
    main()

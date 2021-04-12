# Copyright 2011 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

"""Utilties for manipulating WebAssembly binaries from python.
"""

from collections import namedtuple
from enum import IntEnum
import logging
import os
import sys

from . import shared

sys.path.append(shared.path_from_root('third_party'))

import leb128

logger = logging.getLogger('shared')


# For the Emscripten-specific WASM metadata section, follows semver, changes
# whenever metadata section changes structure.
# NB: major version 0 implies no compatibility
# NB: when changing the metadata format, we should only append new fields, not
#     reorder, modify, or remove existing ones.
EMSCRIPTEN_METADATA_MAJOR, EMSCRIPTEN_METADATA_MINOR = (0, 3)
# For the JS/WASM ABI, specifies the minimum ABI version required of
# the WASM runtime implementation by the generated WASM binary. It follows
# semver and changes whenever C types change size/signedness or
# syscalls change signature. By semver, the maximum ABI version is
# implied to be less than (EMSCRIPTEN_ABI_MAJOR + 1, 0). On an ABI
# change, increment EMSCRIPTEN_ABI_MINOR if EMSCRIPTEN_ABI_MAJOR == 0
# or the ABI change is backwards compatible, otherwise increment
# EMSCRIPTEN_ABI_MAJOR and set EMSCRIPTEN_ABI_MINOR = 0.
EMSCRIPTEN_ABI_MAJOR, EMSCRIPTEN_ABI_MINOR = (0, 29)

WASM_PAGE_SIZE = 65536


def toLEB(num):
  return leb128.u.encode(num)


def readULEB(iobuf):
  return leb128.u.decode_reader(iobuf)[0]


def readSLEB(iobuf):
  return leb128.i.decode_reader(iobuf)[0]


def add_emscripten_metadata(wasm_file):
  mem_size = shared.Settings.INITIAL_MEMORY // WASM_PAGE_SIZE
  global_base = shared.Settings.GLOBAL_BASE

  logger.debug('creating wasm emscripten metadata section with mem size %d' % mem_size)
  name = b'\x13emscripten_metadata' # section name, including prefixed size
  contents = (
    # metadata section version
    toLEB(EMSCRIPTEN_METADATA_MAJOR) +
    toLEB(EMSCRIPTEN_METADATA_MINOR) +

    # NB: The structure of the following should only be changed
    #     if EMSCRIPTEN_METADATA_MAJOR is incremented
    # Minimum ABI version
    toLEB(EMSCRIPTEN_ABI_MAJOR) +
    toLEB(EMSCRIPTEN_ABI_MINOR) +

    # Wasm backend, always 1 now
    toLEB(1) +

    toLEB(mem_size) +
    toLEB(0) +
    toLEB(global_base) +
    toLEB(0) +
    # dynamictopPtr, always 0 now
    toLEB(0) +

    # tempDoublePtr, always 0 in wasm backend
    toLEB(0) +

    toLEB(int(shared.Settings.STANDALONE_WASM))

    # NB: more data can be appended here as long as you increase
    #     the EMSCRIPTEN_METADATA_MINOR
  )

  orig = open(wasm_file, 'rb').read()
  with open(wasm_file, 'wb') as f:
    f.write(orig[0:8]) # copy magic number and version
    # write the special section
    f.write(b'\0') # user section is code 0
    # need to find the size of this section
    size = len(name) + len(contents)
    f.write(toLEB(size))
    f.write(name)
    f.write(contents)
    f.write(orig[8:])


class SecType(IntEnum):
  CUSTOM = 0
  TYPE = 1
  IMPORT = 2
  FUNCTION = 3
  TABLE = 4
  MEMORY = 5
  EVENT = 13
  GLOBAL = 6
  EXPORT = 7
  START = 8
  ELEM = 9
  DATACOUNT = 12
  CODE = 10
  DATA = 11


class ExternType(IntEnum):
  FUNC = 0
  TABLE = 1
  MEMORY = 2
  GLOBAL = 3
  EVENT = 4


Section = namedtuple('Section', ['type', 'size', 'offset'])
Limits = namedtuple('Limits', ['flags', 'initial', 'maximum'])
Import = namedtuple('Import', ['type', 'mod', 'field'])


class Module:

  """Extremely minimal wasm module reader.  Currently only used
  for parsing the dylink section."""
  def __init__(self, filename):
    self.size = os.path.getsize(filename)
    self.buf = open(filename, 'rb')
    self.sections = []
    self.section_map = {}
    magic = self.buf.read(4)
    version = self.buf.read(4)
    assert magic == b'\0asm'
    assert version == b'\x01\0\0\0'
    self.readSectionHeaders()

  def __del__(self):
    self.buf.close()

  def readByte(self):
    return self.buf.read(1)[0]

  def readULEB(self):
    return readULEB(self.buf)

  def readSLEB(self):
    return readSLEB(self.buf)

  def readString(self):
    size = self.readULEB()
    return self.buf.read(size).decode('utf-8')

  def readLimits(self):
    flags = self.readByte()
    initial = self.readULEB()
    maximum = 0
    if flags & 0x1:
      maximum = self.readULEB()
    return Limits(flags, initial, maximum)

  def readSectionHeaders(self):
    while self.buf.tell() != self.size:
      section_type = self.readByte()
      section_size = self.readULEB()
      offset = self.buf.tell()
      section_info = Section(section_type, section_size, offset)
      self.sections.append(section_info)
      if section_type != SecType.CUSTOM:
        assert section_type not in self.section_map
        self.section_map[section_type] = section_info
      # Seek to start of next section
      self.seek(offset + section_size)

  def seek(self, offset):
    self.buf.seek(offset)


def parse_dylink_section(wasm_file):
  module = Module(wasm_file)

  dylink_section = module.sections[0]
  assert dylink_section.type == SecType.CUSTOM
  section_size = dylink_section.size
  section_offset = dylink_section.offset
  section_end = section_offset + section_size
  module.seek(section_offset)
  # section name
  section_name = module.readString()
  assert section_name == 'dylink'
  mem_size = module.readULEB()
  mem_align = module.readULEB()
  table_size = module.readULEB()
  table_align = module.readULEB()

  needed = []
  needed_count = module.readULEB()
  while needed_count:
    libname = module.readString()
    needed.append(libname)
    needed_count -= 1

  return (mem_size, mem_align, table_size, table_align, section_end, needed)


def get_imports(wasm_file):
  module = Module(wasm_file)
  import_section = module.section_map.get(SecType.IMPORT)
  if not import_section:
    return []

  module.seek(import_section.offset)
  num_imports = module.readULEB()
  imports = []
  for i in range(num_imports):
    mod = module.readString()
    field = module.readString()
    kind = ExternType(module.readByte())
    imports.append(Import(kind, mod, field))
    if kind == ExternType.FUNC:
      sig = module.readULEB()
    elif kind == ExternType.GLOBAL:
      global_type = module.readSLEB()
      mutable = module.readByte()
    elif kind == ExternType.MEMORY:
      module.readLimits()
    elif kind == ExternType.TABLE:
      table_type = module.readSLEB()
      module.readLimits()
    else:
      assert False

  return imports


def update_dylink_section(wasm_file, extra_dynlibs):
  # A wasm shared library has a special "dylink" section, see tools-conventions repo.
  # This function updates this section, adding extra dynamic library dependencies.

  mem_size, mem_align, table_size, table_align, section_end, needed = parse_dylink_section(wasm_file)

  section_name = b'\06dylink' # section name, including prefixed size
  contents = (toLEB(mem_size) + toLEB(mem_align) +
              toLEB(table_size) + toLEB(0))

  # we extend "dylink" section with information about which shared libraries
  # our shared library needs. This is similar to DT_NEEDED entries in ELF.
  #
  # In theory we could avoid doing this, since every import in wasm has
  # "module" and "name" attributes, but currently emscripten almost always
  # uses just "env" for "module". This way we have to embed information about
  # required libraries for the dynamic linker somewhere, and "dylink" section
  # seems to be the most relevant place.
  #
  # Binary format of the extension:
  #
  #   needed_dynlibs_count        varuint32       ; number of needed shared libraries
  #   needed_dynlibs_entries      dynlib_entry*   ; repeated dynamic library entries as described below
  #
  # dynlib_entry:
  #
  #   dynlib_name_len             varuint32       ; length of dynlib_name_str in bytes
  #   dynlib_name_str             bytes           ; name of a needed dynamic library: valid UTF-8 byte sequence
  #
  # a proposal has been filed to include the extension into "dylink" specification:
  # https://github.com/WebAssembly/tool-conventions/pull/77
  needed += extra_dynlibs
  contents += toLEB(len(needed))
  for dyn_needed in needed:
    dyn_needed = dyn_needed.encode('utf-8')
    contents += toLEB(len(dyn_needed))
    contents += dyn_needed

  orig = open(wasm_file, 'rb').read()
  file_header = orig[:8]
  file_remainder = orig[section_end:]

  section_size = len(section_name) + len(contents)
  with open(wasm_file, 'wb') as f:
    # copy magic number and version
    f.write(file_header)
    # write the special section
    f.write(b'\0') # user section is code 0
    f.write(toLEB(section_size))
    f.write(section_name)
    f.write(contents)
    # copy rest of binary
    f.write(file_remainder)

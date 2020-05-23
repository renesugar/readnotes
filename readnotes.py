import re
import os
import argparse
import sys
import errno
import optparse
import sqlite3
import uuid

import email
import email.utils
from email.message import EmailMessage
from email.parser import BytesParser, Parser
from email.policy import default

from bs4 import BeautifulSoup

from datetime import datetime
from datetime import timedelta

import hashlib

import zlib
import binascii

import notesdb
import common

import urllib
from biplist import *

from notes2html import ReadAttachments, ProcessNoteBodyBlob, DefaultCss, PrintAttachments

'''
   Copyright (c) 2017 Yogesh Khatri 

   This file is part of mac_apt (macOS Artifact Parsing Tool).
   Usage or distribution of this software/code is subject to the 
   terms of the MIT License.
'''
# https://github.com/ydkhatri/mac_apt/blob/master/plugins/notes.py

#
# MIT License
#
# https://opensource.org/licenses/MIT
#
# Copyright 2020 Rene Sugar
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

#
# Description:
#
# This program reads MacOS/iOS Notes into a mac_apt Notes SQLite database.
#
# When the URL of a web page is saved as a note, it is saved as a public.url
# attachment in the note data blob.
#
# mac_apt currently does not read ZURLSTRING for public.url attachments in
# the note data blob.
#
# https://github.com/ydkhatri/mac_apt/blob/master/plugins/notes.py
#

global __name__, __author__, __email__, __version__, __license__
__program_name__ = 'ReadNotesHighSierra'
__author__ = 'Rene Sugar'
__email__ = 'rene.sugar@gmail.com'
__version__ = '1.00'
__license__ = 'MIT License (https://opensource.org/licenses/MIT)'
__website__ = 'https://github.com/renesugar'
__db_schema_version__ = '1'
__db_schema_min_version__ = '1'

def _log_error(msg):
  raise print('ERROR: %s' % (msg, ))

def _log_warning(msg):
  print('WARNING: %s' % (msg, ))

def ReadAttPathFromPlist(plist_blob):
  '''For NotesV2, read plist and get path'''
  try:
    plist = readPlistFromString(plist_blob)
    try:
      path = plist['$objects'][2]
      return path
    except (KeyError, IndexError):
      _log_error('Could not fetch attachment path from plist')
  except (InvalidPlistException, IOError) as e:
    _log_error("Invalid plist in table." + str(e) )
  return ''

def ReadMacAbsoluteTime(mac_abs_time): # Mac Absolute time is time epoch beginning 2001/1/1
  '''Returns datetime object, or empty string upon error'''
  if mac_abs_time not in ( 0, None, ''):
    try:
      if isinstance(mac_abs_time, str):
        mac_abs_time = float(mac_abs_time)
      if mac_abs_time > 0xFFFFFFFF: # more than 32 bits, this should be nano-second resolution timestamp (seen only in HighSierra)
        return datetime(2001, 1, 1) + timedelta(seconds=mac_abs_time/1000000000.)
      return datetime(2001, 1, 1) + timedelta(seconds=mac_abs_time)
    except (ValueError, OverflowError, TypeError) as ex:
      _log_error("ReadMacAbsoluteTime() Failed to convert timestamp from value " + str(mac_abs_time) + " Error was: " + str(ex))
  return ''

def GetUncompressedData(compressed):
  if compressed == None:
    return None
  data = None
  try:
    data = zlib.decompress(compressed, 15 + 32)
  except zlib.error:
    _log_error('Zlib Decompression failed!')
  return data

def ReadLengthField(blob):
  '''Returns a tuple (length, skip) where skip is number of bytes read'''
  length = 0
  skip = 0
  try:
    data_length = int(blob[0])
    length = data_length & 0x7F
    while data_length > 0x7F:
      skip += 1
      data_length = int(blob[skip])
      length = ((data_length & 0x7F) << (skip * 7)) + length
  except (IndexError, ValueError):
    _log_error('Error trying to read length field in note data blob')
  skip += 1
  return length, skip

def ProcessBasicNoteBodyBlob(blob):
  data = b''
  if blob == None: return data
  try:
    pos = 0
    if blob[0:3] != b'\x08\x00\x12': # header
        _log_error('Unexpected bytes in header pos 0 - ' + binascii.hexlify(blob[0:3]) + '  Expected 080012')
        return ''
    pos += 3
    length, skip = ReadLengthField(blob[pos:])
    pos += skip

    if blob[pos:pos+3] != b'\x08\x00\x10': # header 2
        _log_error('Unexpected bytes in header pos {0}:{0}+3'.format(pos))
        return '' 
    pos += 3
    length, skip = ReadLengthField(blob[pos:])
    pos += skip

    # Now text data begins
    if blob[pos] != 0x1A:
        _log_error('Unexpected byte in text header pos {} - byte is 0x{:X}'.format(pos, blob[pos]))
        return ''
    pos += 1
    length, skip = ReadLengthField(blob[pos:])
    pos += skip
    # Read text tag next
    if blob[pos] != 0x12:
        _log_error('Unexpected byte in pos {} - byte is 0x{:X}'.format(pos, blob[pos]))
        return ''
    pos += 1
    length, skip = ReadLengthField(blob[pos:])
    pos += skip
    data = blob[pos : pos + length].decode('utf-8')
    # Skipping the formatting Tags
  except (IndexError, ValueError):
    _log_error('Error processing note data blob')
  return data

def ExecuteQuery(db, query):
  '''Run query, return tuple (cursor, error_message)'''
  try:
    db.row_factory = sqlite3.Row
    cursor = db.execute(query)
    return cursor, ""
  except sqlite3.Error as ex:
    error = str(ex)
  return None, error

def ReadNotesHighSierra(db, source, user, css, attachments, odb, blob_path):
  '''Read Notestore.sqlite'''
  try:
    query = " SELECT n.Z_PK, n.ZNOTE as note_id, n.ZDATA as data, " \
            " c3.ZFILESIZE, "\
            " c4.ZFILENAME, c4.ZIDENTIFIER as att_uuid,  "\
            " c1.ZTITLE1 as title, c1.ZSNIPPET as snippet, c1.ZIDENTIFIER as noteID, "\
            " c1.ZCREATIONDATE1 as created, c1.ZLASTVIEWEDMODIFICATIONDATE, c1.ZMODIFICATIONDATE1 as modified, "\
            " c2.ZACCOUNT3, c2.ZTITLE2 as folderName, c2.ZIDENTIFIER as folderID, "\
            " c5.ZNAME as acc_name, c5.ZIDENTIFIER as acc_identifier, c5.ZACCOUNTTYPE "\
            " FROM ZICNOTEDATA as n "\
            " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c1 ON c1.ZNOTEDATA = n.Z_PK  "\
            " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c2 ON c2.Z_PK = c1.ZFOLDER "\
            " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c3 ON c3.ZNOTE= n.ZNOTE "\
            " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c4 ON c4.ZATTACHMENT1= c3.Z_PK "\
            " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c5 ON c5.Z_PK = c1.ZACCOUNT2  "\
            " ORDER BY note_id  "
    db.row_factory = sqlite3.Row
    cursor = db.execute(query)
    for row in cursor:
      try:
        att_path = ''
        if row['att_uuid'] != None:
          if user:
            att_path = '/Users/' + user + '/Library/Group Containers/group.com.apple.notes/Media/' + row['att_uuid'] + '/' + row['ZFILENAME']
          else:
            att_path = 'Media/' + row['att_uuid'] + '/' + row['ZFILENAME']
        data = GetUncompressedData(row['data'])
        if blob_path is not None:
          with open(os.path.join(blob_path, str(row['note_id'])), 'wb') as f:
            if data is None:
              f.write(b'')
            else:
              f.write(data)
            f.close()
        try:
          text_content = ProcessNoteBodyBlob(data, css, attachments)
        except KeyError:
          _log_warning('Could not find version number; only processing text ' + data.hex())
          text_content = ProcessBasicNoteBodyBlob(data)
        columns = {}
        columns["apple_id"] = row['note_id']
        columns["apple_title"] = row['title']
        columns["apple_snippet"] = row['snippet']
        columns["apple_folder"] = row['folderName']
        columns["apple_created"] = ReadMacAbsoluteTime(row['created'])
        columns["apple_last_modified"] = ReadMacAbsoluteTime(row['modified'])
        columns["apple_data"] = text_content
        columns["apple_attachment_id"] = row['att_uuid']
        columns["apple_attachment_path"] = att_path
        columns["apple_account_description"] = row['acc_name']
        columns["apple_account_identifier"] = row['acc_identifier']
        columns["apple_account_username"] = ''
        columns["apple_version"] = 'NoteStore'
        columns["apple_user"] = user
        columns["apple_source"] = source
        process_note(columns, odb)
      except sqlite3.Error:
        _log_error('Error fetching row data')
  except sqlite3.Error:
    _log_error('Query  execution failed. Query was: ' + query)

def IsHighSierraDb(db):
  '''Returns false if Z_xxNOTE is a table where xx is a number'''
  try:
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%NOTE%'")
    for row in cursor:
      if row[0].startswith('Z_') and row[0].endswith('NOTES'):
        return False
  except sqlite3.Error as ex:
    _log_error("Failed to list tables of db. Error Details:{}".format(str(ex)) )
  return True

def ReadQueryResults(cursor, user, source, css, attachments, odb):
  for row in cursor:
    try:
      att_path = ''
      if row['media_id'] != None:
          att_path = row['ZFILENAME']
      data = GetUncompressedData(row['data'])

      try:
        text_content = ProcessNoteBodyBlob(data, css, attachments)
      except KeyError:
        _log_warning('Could not find version number; only processing text')
        text_content = ProcessBasicNoteBodyBlob(data)

      columns = {}
      columns["apple_id"] = row['note_id']
      columns["apple_title"] = row['title']
      columns["apple_snippet"] = row['snippet']
      columns["apple_folder"] = row['folder']
      columns["apple_created"] = ReadMacAbsoluteTime(row['created'])
      columns["apple_last_modified"] = ReadMacAbsoluteTime(row['modified'])
      columns["apple_data"] = text_content
      columns["apple_attachment_id"] = row['att_uuid']
      columns["apple_attachment_path"] = att_path
      columns["apple_account_description"] = row['acc_name']
      columns["apple_account_identifier"] = row['acc_identifier']
      columns["apple_account_username"] = ''
      columns["apple_version"] = 'NoteStore'
      columns["apple_user"] = user
      columns["apple_source"] = source
      process_note(columns, odb)
    except sqlite3.Error:
      _log_error('Error fetching row data')

def ReadNotes(db, source, user, css, odb, blob_path):
  '''Read Notestore.sqlite'''
  attachments = {}
  ReadAttachments(db, attachments, source, user)

  if IsHighSierraDb(db):
    ReadNotesHighSierra(db, source, user, css, attachments, odb, blob_path)
    return

  query1 = " SELECT n.Z_12FOLDERS as folder_id , n.Z_9NOTES as note_id, d.ZDATA as data, " \
          " c2.ZTITLE2 as folder, c2.ZDATEFORLASTTITLEMODIFICATION as folder_title_modified, " \
          " c1.ZCREATIONDATE as created, c1.ZMODIFICATIONDATE1 as modified, c1.ZSNIPPET as snippet, c1.ZTITLE1 as title, c1.ZACCOUNT2 as acc_id, " \
          " c5.ZACCOUNTTYPE as acc_type, c5.ZIDENTIFIER as acc_identifier, c5.ZNAME as acc_name, " \
          " c3.ZMEDIA as media_id, c3.ZFILESIZE as att_filesize, c3.ZMODIFICATIONDATE as att_modified, c3.ZPREVIEWUPDATEDATE as att_previewed, c3.ZTITLE as att_title, c3.ZTYPEUTI, c3.ZIDENTIFIER as att_uuid, " \
          " c4.ZFILENAME, c4.ZIDENTIFIER as media_uuid " \
          " FROM Z_12NOTES as n " \
          " LEFT JOIN ZICNOTEDATA as d ON d.ZNOTE = n.Z_9NOTES " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c1 ON c1.Z_PK = n.Z_9NOTES " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c2 ON c2.Z_PK = n.Z_12FOLDERS " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c3 ON c3.ZNOTE = n.Z_9NOTES " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c4 ON c3.ZMEDIA = c4.Z_PK " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c5 ON c5.Z_PK = c1.ZACCOUNT2 " \
          " ORDER BY note_id "
  query2 = " SELECT n.Z_11FOLDERS as folder_id , n.Z_8NOTES as note_id, d.ZDATA as data, " \
          " c2.ZTITLE2 as folder, c2.ZDATEFORLASTTITLEMODIFICATION as folder_title_modified, " \
          " c1.ZCREATIONDATE as created, c1.ZMODIFICATIONDATE1 as modified, c1.ZSNIPPET as snippet, c1.ZTITLE1 as title, c1.ZACCOUNT2 as acc_id, " \
          " c5.ZACCOUNTTYPE as acc_type, c5.ZIDENTIFIER as acc_identifier, c5.ZNAME as acc_name, " \
          " c3.ZMEDIA as media_id, c3.ZFILESIZE as att_filesize, c3.ZMODIFICATIONDATE as att_modified, c3.ZPREVIEWUPDATEDATE as att_previewed, c3.ZTITLE as att_title, c3.ZTYPEUTI, c3.ZIDENTIFIER as att_uuid, " \
          " c4.ZFILENAME, c4.ZIDENTIFIER as media_uuid " \
          " FROM Z_11NOTES as n " \
          " LEFT JOIN ZICNOTEDATA as d ON d.ZNOTE = n.Z_8NOTES " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c1 ON c1.Z_PK = n.Z_8NOTES " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c2 ON c2.Z_PK = n.Z_11FOLDERS " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c3 ON c3.ZNOTE = n.Z_8NOTES " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c4 ON c3.ZMEDIA = c4.Z_PK " \
          " LEFT JOIN ZICCLOUDSYNCINGOBJECT as c5 ON c5.Z_PK = c1.ZACCOUNT2 " \
          " ORDER BY note_id "
  cursor, error1 = ExecuteQuery(db, query1)
  if cursor:
    ReadQueryResults(cursor, user, source, css, attachments, odb)
  else: # Try query2
    cursor, error2 = ExecuteQuery(db, query2)
    if cursor:
      ReadQueryResults(cursor, user, source, css, attachments, odb)
    else:
      _log_error('Query execution failed.\n Query 1 error: {}\n Query 2 error: {}'.format(error1, error2))

def ReadNotesV2_V4_V6(db, version, source, user, odb):
  '''Reads NotesVx.storedata, where x= 2,4,6,7'''
  try:
    query = "SELECT n.Z_PK as note_id, n.ZDATECREATED as created, n.ZDATEEDITED as edited, n.ZTITLE as title, "\
            " (SELECT ZNAME from ZFOLDER where n.ZFOLDER=ZFOLDER.Z_PK) as folder, "\
            " (SELECT zf2.ZACCOUNT from ZFOLDER as zf1  LEFT JOIN ZFOLDER as zf2 on (zf1.ZPARENT=zf2.Z_PK) where n.ZFOLDER=zf1.Z_PK) as folder_parent_id, "\
            " ac.ZEMAILADDRESS as email, ac.ZACCOUNTDESCRIPTION as acc_desc, ac.ZUSERNAME as username, b.ZHTMLSTRING as data, "\
            " att.ZCONTENTID as att_id, att.ZFILEURL as file_url "\
            " FROM ZNOTE as n "\
            " LEFT JOIN ZNOTEBODY as b ON b.ZNOTE = n.Z_PK "\
            " LEFT JOIN ZATTACHMENT as att ON att.ZNOTE = n.Z_PK "\
            " LEFT JOIN ZACCOUNT as ac ON ac.Z_PK = folder_parent_id"
    db.row_factory = sqlite3.Row
    cursor = db.execute(query)
    for row in cursor:
      try:
        att_path = ''
        if row['file_url'] != None:
          att_path = ReadAttPathFromPlist(row['file_url'])

        columns = {}
        columns["apple_id"] = row['note_id']
        columns["apple_title"] = row['title']
        columns["apple_snippet"] = ''
        columns["apple_folder"] = row['folder']
        columns["apple_created"] = ReadMacAbsoluteTime(row['created'])
        columns["apple_last_modified"] = ReadMacAbsoluteTime(row['edited'])
        columns["apple_data"] = row['data']
        columns["apple_attachment_id"] = row['att_id']
        columns["apple_attachment_path"] = att_path
        columns["apple_account_description"] = row['acc_desc']
        columns["apple_account_identifier"] = row['email']
        columns["apple_account_username"] = row['username']
        columns["apple_version"] = 'NoteStore'
        columns["apple_user"] = user
        columns["apple_source"] = source
        process_note(columns, odb)
      except (sqlite3.Error, KeyError):
        _log_error('Error fetching row data')
  except sqlite3.Error:
    _log_error('Query  execution failed. Query was: ' + query)

def loadfile(file):
  data = ''
  with open(file, 'r') as f:
    lines = f.readlines()
    data=''.join(lines)
  return data

def _get_option_parser():
    parser = optparse.OptionParser('%prog [options]',
                                   version='%prog ' + __version__)
    parser.add_option("", "--user",
                      action="store", dest="user_name", default=None,
                      help="User name")
    parser.add_option("", "--input",
                      action="store", dest="input_path", default=None,
                      help="Path to input Notes SQLite file")
    parser.add_option("", "--css",
                      action="store", dest="css_path", default=None,
                      help="Path to CSS file")
    parser.add_option('', "--output",
                      action="store", dest="output_path", default=None,
                      help="Path to output notes SQLite directory")
    parser.add_option("--blob",
                      action="store_true", dest="output_blob", default=False,
                      help="Write BLOBs to 'blob' directory in output directory")
    return parser

def process_note(columns, sqlconn):
  # note_title
  note_title = ''
  if columns["apple_title"] is None:
    note_title = "New Note"
  else:
    note_title = common.remove_line_breakers(columns["apple_title"]).strip()

  print("processing '%s'" % (note_title,))

  notesdb.add_macapt_note(sqlconn, columns)
  sqlconn.commit()

def main(args):
  parser = _get_option_parser()
  (options, args) = parser.parse_args(args)

  userName = ''

  if hasattr(options, 'user_name') and options.user_name:
    userName = options.user_name
  else:
    common.error("user name not specified.")

  inputPath = ''

  if hasattr(options, 'input_path') and options.input_path:
    inputPath = os.path.abspath(os.path.expanduser(options.input_path))
    if os.path.isfile(inputPath) == False:
      # Check if input file exists
      common.error("input file '%s' does not exist." % (inputPath,))
  else:
    common.error("input file not specified.")

  cssPath = ''

  if hasattr(options, 'css_path') and options.css_path:
    cssPath = os.path.abspath(os.path.expanduser(options.css_path))
    if os.path.isfile(cssPath) == False:
      # Check if CSS file exists
      common.error("CSS file '%s' does not exist." % (cssPath,))

  outputPath = ''

  if hasattr(options, 'output_path') and options.output_path:
    outputPath = os.path.abspath(os.path.expanduser(options.output_path))
    if os.path.isdir(outputPath) == False:
      # Check if output directory exists
      common.error("output path '%s' does not exist." % (outputPath,))
  else:
    common.error("output path not specified.")

  blobPath = ''

  if hasattr(options, 'output_blob') and options.output_blob:
    blobPath = os.path.join(outputPath, 'blob')
    if os.path.isdir(blobPath) == False:
      # Check if BLOB directory exists
      common.error("BLOB path '%s' does not exist." % (blobPath,))
  else:
    blobPath = None

  macosdbfile = options.input_path

  notesdbfile = os.path.join(options.output_path, 'mac_apt.db')

  if not os.path.isfile(macosdbfile):
    common.error("input file does not exist")

  new_database = (not os.path.isfile(notesdbfile))

  print("input database '%s'" % (macosdbfile,))

  macos_sqlconn = sqlite3.connect(macosdbfile) #,
  #  detect_types=sqlite3.PARSE_DECLTYPES)
  macos_sqlconn.row_factory = sqlite3.Row

  sqlconn = sqlite3.connect(notesdbfile,
    detect_types=sqlite3.PARSE_DECLTYPES)

  if (new_database):
    notesdb.create_macapt_database(sqlconn=sqlconn)

  if cssPath == '':
    css = DefaultCss()
  else:
    css = loadfile(cssPath)

  if sqlconn != None:
    filename = os.path.basename(macosdbfile)
    if filename.find('V2') > 0:
        ReadNotesV2_V4_V6(macos_sqlconn, 'V2', macosdbfile, userName, sqlconn)
    elif filename.find('V1') > 0:
        ReadNotesV2_V4_V6(macos_sqlconn, 'V1', macosdbfile, userName, sqlconn)
    elif filename.find('V4') > 0:
        ReadNotesV2_V4_V6(macos_sqlconn, 'V4', macosdbfile, userName, sqlconn)
    elif filename.find('V6') > 0:
        ReadNotesV2_V4_V6(macos_sqlconn, 'V6', macosdbfile, userName, sqlconn)
    elif filename.find('V7') > 0:
        ReadNotesV2_V4_V6(macos_sqlconn, 'V7', macosdbfile, userName, sqlconn)
    elif filename.find('NoteStore') >= 0:
        ReadNotes(macos_sqlconn, macosdbfile, userName, css, sqlconn, blobPath)
    else:
        _log_error('Unknown database type, not a recognized file name')
    sqlconn.commit()
    sqlconn.close()

if __name__ == "__main__":
  main(sys.argv[1:])


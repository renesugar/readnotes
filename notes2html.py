#!/usr/bin/env python3
import os, sqlite3, json, struct, re, zipfile, sys
import zlib
import xml.etree.ElementTree as ET
import urllib

# https://github.com/dunhamsteve/notesutils
#
# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <http://unlicense.org>

# https://github.com/dunhamsteve/notesutils/blob/master/notes.md

def GetUncompressedData(compressed):
  if compressed == None:
    return None
  data = None
  data = zlib.decompress(compressed, 15 + 32)
  return data

# HTML construction utils

def append(rval,a):
    "append a to rval and return a"
    if a is None:
      a = ''
    if isinstance(a,str):
        i = len(rval)-1
        if i<0:
            rval.text = (rval.text or "")+a
        else:
            rval[i].tail = (rval[i].tail or "")+a
    elif isinstance(a,ET.Element):
        rval.append(a)
    elif isinstance(a,dict):
        rval.attrib.update(a)
    else:
        raise Exception(f"unhandled type {type(a)}")
    return a

def E(tag,*args):
    tag,*cc = tag.split('.')
    rval = ET.Element(tag)
    tail = None
    if cc: rval.set('class',' '.join(cc))
    for a in args:
        append(rval,a)
    return rval

# protobuf parser

def uvarint(data,pos):
    x = s = 0
    while True:
        b = data[pos]
        pos += 1
        x = x | ((b&0x7f)<<s)
        if b < 0x80: return x,pos
        s += 7

def readbytes(data,pos):
    l,pos = uvarint(data,pos)
    return data[pos:pos+l], pos+l

def readstruct(fmt,l):
    return lambda data,pos: (struct.unpack_from(fmt,data,pos)[0],pos+l)

readers = [ uvarint, readstruct('<d',8), readbytes, None, None, readstruct('<f',4) ]

def parse(data, schema):
    if data is None:
      data = b''
    "parses a protobuf"
    obj = {}
    pos = 0
    while pos < len(data):
        val,pos = uvarint(data,pos)
        typ = val & 7
        key = val >> 3
        val, pos = readers[typ](data,pos)
        if key not in schema: 
            continue
        name, repeated, typ = schema[key]
        if isinstance(typ, dict):
            val = parse(val, typ)
        if typ == 'string':
            val = val.decode('utf8')
        if repeated:
            val = obj.get(name,[]) + [val]
        obj[name] = val
    return obj

def svg(drawing):
    "Convert note drawing to SVG"
    width = drawing['bounds']['width']
    height = drawing['bounds']['height']
    rval = E('svg',{'width':str(width),'height':str(height)})
    inks = drawing.get('inks')
    for stroke in drawing.get('strokes',[]):
        if stroke.get('hidden'):
            continue
        if 'points' in stroke:
            swidth=1
            ink = inks[stroke['inkIndex']]
            c = ink['color']
            red = int(c['red']*255)
            green = int(c['green']*255)
            blue = int(c['blue']*255)
            alpha = c['alpha']
            if ink['identifier'] == 'com.apple.ink.marker':
                swidth = 15
                alpha = 0.5

            color = f'rgba({red},{green},{blue},{alpha})'
            path = ''
            for _,x,y,*rest in struct.iter_unpack('<3f5H2B',stroke['points']):
                path += f"L{x:.2f} {y:.2f}"
            path = "M"+path[1:]
            
            rval.append(E('path',{'d':"M"+path[1:],'stroke':color,'stroke-width':str(swidth),'stroke-cap':'round','fill':'none'}))
            if 'transform' in stroke:
                rval[-1].set('transform',"matrix({a} {b} {c} {d} {tx:.2f} {ty:.2f})".format(**stroke['transform']))
    return rval

def render_html(note,attachments):
  if note is None:
    return ""
  "Convert note attributed string to HTML"
  styles = {0:'h1',1:'h2',4:'pre',100:'li',101:'li',102:'li',103:'li'}
  rval = E('div')
  txt = note['string']
  pos = 0
  par = None
  for run in note.get('attributeRun',[]):
    l = run['length']
    for frag in re.findall(r'\n|[^\n]+',txt[pos:pos+l]):
      if par is None: # start paragraph
        pstyle = run.get('paragraphStyle',{}).get('style',-1)
        indent = run.get('paragraphStyle',{}).get('indent',0)
        if pstyle > 100: # this mess handles merging todo lists
          tag = ['ul','ul','ol','ul'][pstyle - 100]
          par = rval
          while indent > 0:
            last = list(par)[-1]
            if last.tag != tag:
              break
            par = last
            indent -= 1
          while indent >= 0:
            par = append(par,E(tag))
            indent -= 1
          par = append(par,E('li'))
        elif pstyle == 4 and list(rval)[-1].tag == 'pre':
          par = list(rval)[-1]
          append(par,"\n")
        else:
          par = append(rval,E(styles.get(pstyle,'p')))
        if pstyle == 103:
          par.append(E('input',{"type":"checkbox"}))
          if run.get('todo',{}).get('done'):
            par[0].put('checked','')
      if frag == '\n':
        par = None
      else:
        link = run.get('link')
        if link:
          frag = E('a',{'href':link},link)
        info = run.get('attachmentInfo')
        style = run.get('fontHints',0) + 4*run.get('underline',0) + 8*run.get('strikethrough',0)
        if style & 1: frag = E('b',frag)
        if style & 2: frag = E('em',frag)
        if style & 4: frag = E('u',frag)
        if style & 8: frag = E('strike',frag)
        if info:
          #print("ATTACHMENT: '%s'" % (info.get('attachmentIdentifier')))
          attach = attachments.get(info.get('attachmentIdentifier'))
          if attach is not None and attach.get('html') is not None:
            frag = attach.get('html')
            #print("HTML: %s" % (ET.tostring(frag,method='html')))
          else:
            root  = '/Users/' + 'none' + '/Library/Group Containers/group.com.apple.notes'
            fn = os.path.join(root,'Media',info.get('attachmentIdentifier'),'missing.txt')
            att_url = urllib.parse.urlunsplit(('file', '', fn, '', ''))
            frag = E('a',{'href':att_url},att_url)
            #print("(NOT FOUND) ATTACHMENT: '%s'" % (info.get('attachmentIdentifier')))
        append(par,frag)
    pos += l
  return rval

def process_archive(table):
  "Decode a 'CRArchive'"
  objects = []

  def dodict(v):
    rval = {}
    for e in v.get('element',[]):
      rval[coerce(e['key'])] = coerce(e['value'])
    return rval

  def coerce(o):
    [(k,v)]= o.items()
    if 'custom' == k:
      rval = dict((table['keyItem'][e['key']],coerce(e['value'])) for e in v['mapEntry'])
      typ = table['typeItem'][v['type']]
      if typ == 'com.apple.CRDT.NSUUID':
        return table['uuidItem'][rval['UUIDIndex']]
      if typ == 'com.apple.CRDT.NSString':
        return rval['self']
      return rval
    if k == 'objectIndex':
      return coerce(table['object'][v])
    if k == 'registerLatest':
      return coerce(v['contents'])
    if k == 'orderedSet':
      elements = dodict(v['elements'])
      contents = dodict(v['ordering']['contents'])
      rval = []
      for a in v['ordering']['array']['attachments']:
        value = contents[a['uuid']]
        if value not in rval and a['uuid'] in elements:
          rval.append(value)
      return rval
    if k == 'dictionary':
      return dodict(v)
    if k in ('stringValue','unsignedIntegerValue','string'):
      return v
    raise Exception(f"unhandled type {k}")

  return coerce(table['object'][0])

def render_table(table):
  "Render a table to html"
  table = process_archive(table)
  rval = E('table')
  # table header
  thead = E('thead')
  tr = E('tr')
  for col in table['crColumns']:
    th = E('th')
    tr.append(th)
  thead.append(tr)
  rval.append(thead)
  # table rows
  for row in table['crRows']:
    tr = E('tr')
    for col in table['crColumns']:
      cell = table.get('cellColumns').get(col,{}).get(row)
      td = E('td',render_html(cell,{}))
      tr.append(td)
    rval.append(tr)
  return rval

s_string = {
    2: [ "string", 0, "string"],
    5: [ "attributeRun", 1, {
        1: ["length",0,0],
        2: ["paragraphStyle", 0, {
            1: ["style", 0,0],
            4: ["indent",0,0],
            5: ["todo",0,{ 
                1: ["todoUUID", 0, "bytes"],
                2: ["done",0,0]
            }]
        }],
        5: ["fontHints",0,0],
        6: ["underline",0,0],
        7: ["strikethrough",0,0],
        9: ["link",0,"string"],
        12: [ "attachmentInfo", 0, {
            1: [ "attachmentIdentifier", 0, "string"],
            2: [ "typeUTI", 0, "string"]
        }]
    }]
}

s_doc = { 2: ["version", 1, { 3: ["data", 0, s_string ]}]}

s_drawing = { 2: ["version", 1, { 3: ["data", 0, {
            4: ["inks",1, {
                1:["color",0,{1:["red",0,0],2:["green",0,0],3:["blue",0,0],4:["alpha",0,0]}],
                2:["identifier",0,"string"]
            }],
            5: ["strokes",1, {
                3:["inkIndex",0,0],
                5:["points",0,"bytes"],
                9:["hidden",0,0],
                10: ["transform",0,{1:["a",0,0],2:["b",0,0],3:["c",0,0],4:["d",0,0],5:["tx",0,0],6:["ty",0,0]}]
            }],
            8: ["bounds", 0, {1:["originX",0,0],2:["originY",0,0],3:["width",0,0],4:["height",0,0]}]
        }]
    }
]}

# this essentially is a variant type
s_oid = { 2:["unsignedIntegerValue",0,0], 4:["stringValue",0,'string'], 6:["objectIndex",0,0] }
s_dictionary = {1:["element",1,{ 1:["key",0,s_oid], 2:["value",0,s_oid]}]}
s_table = { 2: ["version", 1, { 3: ["data", 0, {
    3: ["object",1,{
        1:["registerLatest",0,{2:["contents",0,s_oid]}],
        6:["dictionary",0,s_dictionary],
        10:["string",0,s_string],
        13:["custom",0,{
            1:["type",0,0],
            3:["mapEntry",1,{
                1:["key",0,0],
                2:["value",0,s_oid]
            }]
        }],
        16:["orderedSet",0,{
            1: ["ordering",0, {
                1:["array",0,{
                    1:["contents",0,s_string],
                    2:["attachments",1,{1:["index",0,0],2:["uuid",0,0]}]
                }],
                2:["contents",0,s_dictionary]
            }],
            2: ["elements",0,s_dictionary]
        }]
    }],
    4:["keyItem",1,"string"],
    5:["typeItem",1,"string"],
    6:["uuidItem",1,"bytes"]
}]}]}

def write(data,*path):
    path = os.path.join(*path)
    os.makedirs(os.path.dirname(path),exist_ok=True)
    open(path,'wb').write(data)

def DefaultCss():
  css = '''
.underline { text-decoration: underline; }
.strikethrough { text-decoration: line-through; }
.todo { list-style-type: none; margin-left: -20px; }
.dashitem { list-style-type: none; }
.dashitem:before { content: "-"; text-indent: -5px }
'''
  return css

# attachments = {}
def ReadAttachments(db, attachments, source, user):
  root  = '/Users/' + user + '/Library/Group Containers/group.com.apple.notes'
  mquery = '''select a.zidentifier, a.zmergeabledata, a.ztypeuti, b.zidentifier, b.zfilename, a.zurlstring,a.ztitle
    from ziccloudsyncingobject a left join ziccloudsyncingobject b on a.zmedia = b.z_pk
    where a.zcryptotag is null and a.ztypeuti is not null'''
  for id, data, typ, id2, fname, url,title in db.execute(mquery):
    if url is None:
      url = ''
    if title is None:
      title = ''
    if typ == 'com.apple.drawing' and data:
      doc = parse(GetUncompressedData(data),s_drawing)
      attachments[id] = {'html': svg(doc['version'][0]['data'])}
    elif typ == 'com.apple.notes.table' and data:
      doc = parse(GetUncompressedData(data),s_table)
      attachments[id] = {'html': render_table(doc['version'][0]['data']) }
    elif typ == 'public.url':
      # there is a preview image somewhere too, but not sure I care
      attachments[id] = {'html': E('a',{'href':url},title)}
    elif fname:
      fn = os.path.join(root,'Media',id2,fname)
      att_url = urllib.parse.urlunsplit(('file', '', fn, '', ''))
      if typ in ['public.tiff','public.jpeg','public.png']:
        attachments[id] = {'html': E('img',{'src':att_url})}
      else:
        # e.g. com.adobe.pdf
        attachments[id] = {'html': E('a',{'href':att_url},fname)}
    else:
      fn = os.path.join(root,'FallbackImages',id+'.jpg')
      att_url = urllib.parse.urlunsplit(('file', '', fn, '', ''))
      if os.path.exists(fn):
        attachments[id] = {'html': E('img',{'src':att_url})}
      else:
        fn = os.path.join(root,'Media',id,'missing.txt')
        att_url = urllib.parse.urlunsplit(('file', '', fn, '', ''))
        attachments[id] = {'html': E('a',{'href':att_url},att_url)}
    # html = attachments[id]['html']
    # if html is None:
    #   html = 'None'
    #print("ATTACHMENT: %s HTML: %s" % (id, ET.tostring(html,method='html')))

def PrintAttachments(attachments):
  for key in attachments.keys():
    attach = attachments.get(key)
    print("=====>ATTACH ")
    print(attach)
    print(attach.get('html'))
    if attach is not None and attach.get('html') is not None:
      frag = attach.get('html')
      print("=====>HTML: %s" % (ET.tostring(frag,method='html')))
    else:
      print("=====>(NOT FOUND) ATTACHMENT: '%s'" % (key))
    html = attachments[key]['html']
    if html is None:
      html = 'None'
    print("=====>ATTACHMENT: %s HTML: %s" % (key, ET.tostring(html,method='html')))

# nquery = '''select a.zidentifier, n.zdata from zicnotedata n join ziccloudsyncingobject a on a.znotedata = n.z_pk 
#     where n.zcryptotag is null and zdata is not null'''

# for id,data in db.execute(nquery):

def ProcessNoteBodyBlob(blob, css, attachments):
  if blob is None:
    return ''
  pb = blob
  doc = parse(pb,s_doc)['version'][0]['data']
  section = render_html(doc,attachments)
  section.tag = 'section'
  hdoc = E('html',E('head',E('style',css)),E('body',section))
  return ET.tostring(hdoc,method='html')

# About
**readnotes** reads Apple Notes from the Notes app databases into a database allowing migration to:

* GMail Apple Notes
* Joplin

The Apple Notes app does not provide a ***move all*** action to move all Apple Notes in a folder to GMail.


[gyb](https://github.com/jay0lee/got-your-back/wiki) is used to import and export note email messages to and from GMail.

[mac_apt](https://github.com/ydkhatri/mac_apt/wiki) is used to extract Apple Notes from iOS device backups.

[movenotes](https://github.com/renesugar/movenotes) is used to move Apple notes, emails and bookmarks to GMail or Joplin.

# Usage

## Extract Apple Notes

Currently, *mac_apt* does not extract *public.url* attachments in Apple Notes (see [<https://github.com/ydkhatri/mac_apt/issues/33>).

There are several other attachment types in the Apple Notes BLOB format (e.g. TODOs, SVG drawings, tables, public.tiff, public.jpeg, public.png, public.url, com.adobe.pdf, etc.) that *mac_apt* currently does not extract.

### Extract MacOS notes

This workaround will only extract the first public.url attachment from the note BLOB (see [movenotes](https://github.com/renesugar/movenotes) README for more details on what files to process for different Mac OS versions.).

```
python3 -B readnotes.py  --user rene --input "$HOME/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite" --output ~/notes_macos
```
### Extract iOS notes

After using *mac_apt* to extract the device backup, rename **4f98687d8ab0d6d1a371110e6b7300f6e465bef2** from iOS backup to ***NoteStore.sqlite*** (see [movenotes](https://github.com/renesugar/movenotes) README for more details.).

```
python3 -B readnotes.py  --user yourusername --input "$HOME/output/NoteStore.sqlite" --output ~/notes_ios
```

#!/usr/bin/env python3
"""Rewrite repository tarballs deterministically.

repo-add stamps every tar member with the wall clock, so identical package
sets rebuild with different bytes and the checkpoint store refuses the
collision. Each named tarball is rewritten in place with member mtimes pinned
to SOURCE_DATE_EPOCH, numeric ownership zeroed, and a timestamp-free gzip
header. Content and member order are preserved exactly.
"""

import gzip
import io
import os
import sys
import tarfile


def main() -> int:
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    for path in sys.argv[1:]:
        source = tarfile.open(path, "r:gz")
        buffer = io.BytesIO()
        out = tarfile.open(fileobj=buffer, mode="w", format=source.format)
        for member in source:
            data = source.extractfile(member) if member.isreg() else None
            member.mtime = epoch
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            out.addfile(member, data)
        out.close()
        source.close()
        temporary = path + ".normalized"
        with open(temporary, "wb") as handle:
            with gzip.GzipFile(
                filename="", fileobj=handle, mode="wb", mtime=0
            ) as compressed:
                compressed.write(buffer.getvalue())
        os.replace(temporary, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

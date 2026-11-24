"""Builds a Windows resource object (.syso) holding an icon, for the Go linker.

usage: make_syso.py app.ico out.syso
"""
import struct
import sys

RT_ICON, RT_GROUP_ICON = 3, 14
LANG = 0x0409


def read_ico(path):
    data = open(path, "rb").read()
    _, typ, count = struct.unpack_from("<HHH", data, 0)
    assert typ == 1
    images, entries = [], []
    for i in range(count):
        w, h, colors, res, planes, bits, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        images.append(data[off:off + size])
        entries.append((w, h, colors, res, planes, bits, size, i + 1))
    group = struct.pack("<HHH", 0, 1, count) + b"".join(struct.pack("<BBBBHHIH", *e) for e in entries)
    return images, group


def build(ico, out):
    images, group = read_ico(ico)
    # tree: type -> [(id, blob)]
    tree = [(RT_ICON, [(i + 1, b) for i, b in enumerate(images)]), (RT_GROUP_ICON, [(1, group)])]

    def dir_size(n):
        return 16 + 8 * n

    # layout offsets
    off = dir_size(len(tree))
    type_dirs = []
    for _, items in tree:
        type_dirs.append(off)
        off += dir_size(len(items))
    name_dirs = []
    for _, items in tree:
        nd = []
        for _ in items:
            nd.append(off)
            off += dir_size(1)
        name_dirs.append(nd)
    data_entries = []
    for _, items in tree:
        de = []
        for _ in items:
            de.append(off)
            off += 16
        data_entries.append(de)
    blobs = []
    for _, items in tree:
        bl = []
        for _, blob in items:
            off = (off + 7) & ~7
            bl.append(off)
            off += len(blob)
        blobs.append(bl)
    sec = bytearray(off)

    def put_dir(at, ids_targets):
        struct.pack_into("<IIHHHH", sec, at, 0, 0, 0, 0, 0, len(ids_targets))
        for k, (ident, target, is_dir) in enumerate(ids_targets):
            struct.pack_into("<II", sec, at + 16 + 8 * k, ident, target | (0x80000000 if is_dir else 0))

    put_dir(0, [(t, type_dirs[i], True) for i, (t, _) in enumerate(tree)])
    relocs = []
    for i, (_, items) in enumerate(tree):
        put_dir(type_dirs[i], [(ident, name_dirs[i][j], True) for j, (ident, _) in enumerate(items)])
        for j, (ident, blob) in enumerate(items):
            put_dir(name_dirs[i][j], [(LANG, data_entries[i][j], False)])
            struct.pack_into("<IIII", sec, data_entries[i][j], blobs[i][j], len(blob), 0, 0)
            relocs.append(data_entries[i][j])
            sec[blobs[i][j]:blobs[i][j] + len(blob)] = blob

    hdr_size, sh_size = 20, 40
    raw_ptr = hdr_size + sh_size
    reloc_ptr = raw_ptr + len(sec)
    sym_ptr = reloc_ptr + 10 * len(relocs)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, sym_ptr, 1, 0, 0)
    sh = struct.pack("<8sIIIIIIHHI", b".rsrc", 0, 0, len(sec), raw_ptr, reloc_ptr, 0, len(relocs), 0, 0x40000040)
    rel = b"".join(struct.pack("<IIH", r, 0, 3) for r in relocs)  # IMAGE_REL_AMD64_ADDR32NB
    sym = struct.pack("<8sIhHBB", b".rsrc", 0, 1, 0, 3, 0)
    strtab = struct.pack("<I", 4)
    open(out, "wb").write(coff + sh + bytes(sec) + rel + sym + strtab)


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])

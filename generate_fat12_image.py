#!/usr/bin/env python3
"""
generate_fat12_image.py
Generates a FAT12 disk image (.mem format for Vivado BRAM initialization).
Preset .txt files appear in the root directory when the drive is mounted on Windows.

Layout (256 sectors = 128 KB BRAM):
  LBA 0      : MBR
  LBA 1      : BPB  (FAT12 volume boot record; partition starts here)
  LBA 2      : FAT1
  LBA 3-4    : Root directory  (32 entries × 32 bytes = 2 sectors)
  LBA 5+     : Data area  (cluster 2 at LBA 5, then every 8 sectors per cluster)

Hardware:
  Xilinx Artix-7 75T (Enigma X1)
  BRAM: 128 KB = 256 sectors
  PCILeech NVMe emulation firmware
"""

import struct, os

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────
SECTOR_SIZE         = 512
TOTAL_BRAM_SECTORS  = 256          # 128 KB physical BRAM
SECTORS_PER_CLUSTER = 8            # 4 KB clusters
RESERVED_SECTORS    = 1            # just the BPB sector
NUM_FATS            = 1
SECTORS_PER_FAT     = 1            # 1 sector holds ~341 FAT12 entries
ROOT_ENTRIES        = 32           # 2 sectors of root directory
ROOT_SECTORS        = ROOT_ENTRIES * 32 // SECTOR_SIZE   # = 2
VOLUME_LABEL        = "Eevee      "   # exactly 11 ASCII chars
VOLUME_SERIAL       = 0xCAFEBABE
MBR_DISK_SIGNATURE  = 0x19861019
PARTITION_SECTORS   = TOTAL_BRAM_SECTORS - 1             # = 255

# Derived layout
DATA_START_REL = RESERVED_SECTORS + NUM_FATS * SECTORS_PER_FAT + ROOT_SECTORS  # = 4
DATA_START_ABS = 1 + DATA_START_REL                                             # = 5
MAX_DATA_CLUSTERS = (TOTAL_BRAM_SECTORS - DATA_START_ABS) // SECTORS_PER_CLUSTER  # = 31

# FAT date/time: 2024-01-01 00:00:00
# date bits: [15:9]=year-1980  [8:5]=month  [4:0]=day
FAT_DATE = (44 << 9) | (1 << 5) | 1
FAT_TIME = 0

# ─────────────────────────────────────────────
#  Preset files
#  Keys  : "NAME.EXT"  (uppercased, truncated to 8.3 automatically)
#  Values: str or bytes  (max ~4 KB per file; larger files use multiple clusters)
# ─────────────────────────────────────────────
PRESET_FILES = {
    "README.TXT": (
        "Welcome to Eevee!\r\n"
        "\r\n"
        "This drive is emulated by PCILeech firmware on an Artix-7 75T FPGA.\r\n"
    ),
    "INFO.TXT": (
        "Device  : NVMe Pcileech(spoofed)\r\n"
        "FPGA    : Xilinx Artix-7 75T (Enigma X1)\r\n"
        "BRAM    : 128 KB = 256 sectors\r\n"
        "Cluster : 4 KB (8 sectors)\r\n"
    ),
}

# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def le16(b, o, v): struct.pack_into("<H", b, o, v)
def le32(b, o, v): struct.pack_into("<I", b, o, v)

def fat12_set(fat, idx, val):
    """Write a 12-bit FAT12 entry at logical index idx."""
    pos = (idx * 3) // 2
    if idx % 2 == 0:
        fat[pos]     =  val        & 0xFF
        fat[pos + 1] = (fat[pos + 1] & 0xF0) | ((val >> 8) & 0x0F)
    else:
        fat[pos]     = (fat[pos] & 0x0F) | ((val & 0x0F) << 4)
        fat[pos + 1] =  (val >> 4) & 0xFF

def name_83(filename):
    """'README.TXT' → (b'README  ', b'TXT')."""
    filename = filename.upper()
    base, _, ext = filename.partition('.')
    return base[:8].ljust(8).encode('ascii'), ext[:3].ljust(3).encode('ascii')

def make_dirent(name8, ext3, cluster, size, attr=0x20):
    """Return a 32-byte FAT directory entry."""
    d = bytearray(32)
    d[0:8]  = name8
    d[8:11] = ext3
    d[11]   = attr
    le16(d, 22, FAT_TIME)
    le16(d, 24, FAT_DATE)
    le16(d, 26, cluster)    # FstClusLO  (FstClusHI at d[20] stays 0 for FAT12)
    le32(d, 28, size)
    return d

# ─────────────────────────────────────────────
#  MBR  (LBA 0)
# ─────────────────────────────────────────────
def build_mbr():
    b = bytearray(512)
    le32(b, 0x1B8, MBR_DISK_SIGNATURE)
    b[446] = 0x80                       # bootable flag
    b[450] = 0x01                       # partition type: FAT12
    le32(b, 454, 1)                     # partition start LBA
    le32(b, 458, PARTITION_SECTORS)     # partition size in sectors
    b[510] = 0x55; b[511] = 0xAA
    return b

# ─────────────────────────────────────────────
#  BPB / Volume Boot Record  (LBA 1)
# ─────────────────────────────────────────────
def build_bpb():
    b = bytearray(512)
    b[0] = 0xEB; b[1] = 0x3C; b[2] = 0x90  # short jump + NOP
    b[3:11] = b"MSDOS5.0"
    le16(b, 11, SECTOR_SIZE)
    b[13]  = SECTORS_PER_CLUSTER
    le16(b, 14, RESERVED_SECTORS)
    b[16]  = NUM_FATS
    le16(b, 17, ROOT_ENTRIES)           # RootEntryCount (FAT12/16 field)
    le16(b, 19, PARTITION_SECTORS)      # TotalSectors16
    b[21]  = 0xF8                       # MediaType: fixed disk
    le16(b, 22, SECTORS_PER_FAT)
    le16(b, 24, 63)                     # SectorsPerTrack
    le16(b, 26, 255)                    # NumHeads
    le32(b, 28, 1)                      # HiddenSectors
    # TotalSectors32 at offset 32 stays 0 (TotalSectors16 is used)
    b[36]  = 0x80                       # DriveNumber
    b[38]  = 0x29                       # BootSignature
    le32(b, 39, VOLUME_SERIAL)
    b[43:54] = VOLUME_LABEL.encode('ascii')[:11]
    b[54:62] = b"FAT12   "
    b[510] = 0x55; b[511] = 0xAA
    return b

# ─────────────────────────────────────────────
#  FAT1  (LBA 2)
# ─────────────────────────────────────────────
def build_fat(allocs):
    """allocs: list of (start_cluster, num_clusters) for each file."""
    b = bytearray(SECTORS_PER_FAT * SECTOR_SIZE)
    fat12_set(b, 0, 0xFF8)  # media descriptor
    fat12_set(b, 1, 0xFFF)  # reserved end-of-chain
    for start, count in allocs:
        for i in range(count - 1):
            fat12_set(b, start + i, start + i + 1)
        fat12_set(b, start + count - 1, 0xFFF)  # last cluster → end-of-chain
    return b

# ─────────────────────────────────────────────
#  Root directory  (LBA 3-4)
# ─────────────────────────────────────────────
def build_root_dir(entries):
    b = bytearray(ROOT_SECTORS * SECTOR_SIZE)
    for i, e in enumerate(entries):
        b[i * 32: i * 32 + 32] = e
    return b

# ─────────────────────────────────────────────
#  Assemble full image
# ─────────────────────────────────────────────
def build_image():
    cluster_size = SECTORS_PER_CLUSTER * SECTOR_SIZE
    next_cluster = 2
    allocs      = []
    dir_entries = []
    data_map    = {}  # cluster_index → bytes (one cluster worth)

    for fname, content in PRESET_FILES.items():
        if isinstance(content, str):
            content = content.encode('ascii')
        num_clusters = max(1, (len(content) + cluster_size - 1) // cluster_size)
        assert next_cluster + num_clusters - 1 < 2 + MAX_DATA_CLUSTERS, \
            f"{fname}: not enough BRAM clusters (max {MAX_DATA_CLUSTERS})"

        name8, ext3 = name_83(fname)
        dir_entries.append(make_dirent(name8, ext3, next_cluster, len(content)))
        allocs.append((next_cluster, num_clusters))

        for i in range(num_clusters):
            chunk = content[i * cluster_size: (i + 1) * cluster_size]
            data_map[next_cluster + i] = chunk.ljust(cluster_size, b'\x00')

        next_cluster += num_clusters

    # Volume label entry (attribute 0x08, no cluster, no size)
    vol_label_bytes = VOLUME_LABEL.encode('ascii')[:11]
    vol_entry = make_dirent(vol_label_bytes[:8], vol_label_bytes[8:11],
                            cluster=0, size=0, attr=0x08)
    all_entries = [vol_entry] + dir_entries

    image = bytearray(TOTAL_BRAM_SECTORS * SECTOR_SIZE)
    image[0 * 512: 1 * 512] = build_mbr()
    image[1 * 512: 2 * 512] = build_bpb()
    image[2 * 512: 3 * 512] = build_fat(allocs)

    root = build_root_dir(all_entries)
    image[3 * 512: 3 * 512 + len(root)] = root

    for cluster, data in data_map.items():
        lba    = DATA_START_ABS + (cluster - 2) * SECTORS_PER_CLUSTER
        offset = lba * SECTOR_SIZE
        image[offset: offset + len(data)] = data

    return image

# ─────────────────────────────────────────────
#  Write .mem file  (32-bit little-endian hex words)
# ─────────────────────────────────────────────
def write_mem(image, output_path):
    dwords = [struct.unpack_from('<I', image, i * 4)[0]
              for i in range(len(image) // 4)]
    assert len(dwords) == 32768, f"Unexpected image size: {len(dwords)} dwords"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w') as f:
        for dw in dwords:
            f.write(f"{dw:08X}\n")

# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    cluster_size = SECTORS_PER_CLUSTER * SECTOR_SIZE
    used_clusters = sum(
        max(1, (len(c.encode('ascii') if isinstance(c, str) else c) + cluster_size - 1) // cluster_size)
        for c in PRESET_FILES.values()
    )

    print("=== FAT12 Image Generator ===")
    print(f"  BRAM          : {TOTAL_BRAM_SECTORS * SECTOR_SIZE // 1024} KB  ({TOTAL_BRAM_SECTORS} sectors)")
    print(f"  FAT type      : FAT12")
    print(f"  Cluster size  : {cluster_size // 1024} KB  ({SECTORS_PER_CLUSTER} sectors)")
    print(f"  Data clusters : {used_clusters} used / {MAX_DATA_CLUSTERS} available")
    print()
    print("  Layout (absolute LBA):")
    print(f"    MBR         : LBA 0")
    print(f"    BPB         : LBA 1")
    print(f"    FAT1        : LBA 2")
    print(f"    Root dir    : LBA 3-{3 + ROOT_SECTORS - 1}  ({ROOT_ENTRIES} entries)")
    print(f"    Data area   : LBA {DATA_START_ABS}+  (cluster 2 at LBA {DATA_START_ABS})")
    print()
    print("  Preset files:")
    for fname, content in PRESET_FILES.items():
        data = content.encode('ascii') if isinstance(content, str) else content
        print(f"    {fname:<16}  {len(data)} bytes")
    print()

    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "nvme_fat12_image.mem"
    )
    image = build_image()
    write_mem(image, output_path)
    print(f"  Output: {output_path}")

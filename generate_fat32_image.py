#!/usr/bin/env python3
"""
generate_fat12_image.py
Generates a FAT32 disk image (.mem format for Vivado BRAM initialization).

Spoofs a 1 TB NVMe drive with visible preset .txt files.

How it works (Option C — dual BRAM window):
  The firmware serves BRAM data for two LBA ranges:
    Window A  LBA 0–8       → BRAM sectors 0–8   (MBR, BPB, FSInfo, FAT sector 0–3)
    Window B  LBA DATA_ABS+ → BRAM sectors 9–32  (root dir + file clusters)
  All other LBAs return zeros from firmware:
    LBA 9–255     → zeros  → FAT sectors 4–250    → clusters 512–32k are free
    LBA 256–DATA_ABS-1 → zeros → remaining FAT    → clusters 32k–244M are free
  Total free ≈ 244 M clusters × 4 KB ≈ 999 GB ≈ 1 TB displayed.

BRAM layout (256 sectors = 128 KB):
  Sectors  0      MBR
  Sectors  1      BPB  (FAT32, TotalSectors=1TB, SectorsPerFAT=1,907,251)
  Sectors  2      FSInfo
  Sectors  3–4    reserved / padding (zeros)
  Sectors  5–8    FAT1  (4 sectors: entries for clusters 0–511, 0–4 set, rest zero)
  Sectors  9–16   Cluster 2 = root directory   ← served for LBA DATA_ABS
  Sectors 17–24   Cluster 3 = file 1 data      ← served for LBA DATA_ABS+8
  Sectors 25–32   Cluster 4 = file 2 data      ← served for LBA DATA_ABS+16
  Sectors 33–255  zeros (unused)

Firmware constants that MUST match this layout (nvme_pkg.sv):
  FS_LBA_END        = 9
  VIRTUAL_LBA_BASE  = DATA_ABS   (computed below, printed on run)
  VIRTUAL_LBA_COUNT = 24         (3 clusters × 8 sectors)
  VIRTUAL_BRAM_BASE = 4608       (= 9 × 512)

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
TOTAL_BRAM_SECTORS  = 256
SECTORS_PER_CLUSTER = 8            # 4 KB clusters
RESERVED_SECTORS    = 4            # BPB + FSInfo + 2 padding
NUM_FATS            = 1
FAT_BRAM_SECTORS    = 4            # FAT sectors physically in BRAM (covers clusters 0–511)
ROOT_CLUSTER        = 2
TOTAL_DISK_SECTORS  = 1_953_125_000  # spoofed 1 TB  (1 × 10¹² ÷ 512)
VOLUME_LABEL        = "Eevee      "  # exactly 11 ASCII chars
VOLUME_SERIAL       = 0xCAFEBABE
MBR_DISK_SIGNATURE  = 0x19861019

# FAT date/time: 2024-01-01 00:00:00
FAT_DATE = (44 << 9) | (1 << 5) | 1
FAT_TIME = 0

# ── Auto-calculate SectorsPerFAT (correct for 1 TB; Windows validates this) ──
_spf_est     = 100_000
_data_rel_est = RESERVED_SECTORS + NUM_FATS * _spf_est
_total_clust  = (TOTAL_DISK_SECTORS - _data_rel_est) // SECTORS_PER_CLUSTER
SECTORS_PER_FAT = (_total_clust * 4 + SECTOR_SIZE - 1) // SECTOR_SIZE

# ── Derived layout ────────────────────────────────────────────────────────────
ABS_FAT1  = 1 + RESERVED_SECTORS                             # = 5
DATA_REL  = RESERVED_SECTORS + NUM_FATS * SECTORS_PER_FAT   # = 1,907,255
DATA_ABS  = 1 + DATA_REL                                     # = 1,907,256  ← VIRTUAL_LBA_BASE

# BRAM layout for cluster data (Window B)
FS_LBA_END        = ABS_FAT1 + FAT_BRAM_SECTORS             # = 9
VIRTUAL_BRAM_BASE = FS_LBA_END * SECTOR_SIZE                 # = 4608

# Maximum file clusters that fit in BRAM and in VIRTUAL_LBA_COUNT=24
VIRTUAL_LBA_COUNT  = 24                                      # must match nvme_pkg.sv
MAX_FILE_CLUSTERS  = VIRTUAL_LBA_COUNT // SECTORS_PER_CLUSTER - 1  # 3 total − 1 root = 2 files

# ── Sanity: SV constants that must match ─────────────────────────────────────
_SV_VIRTUAL_LBA_BASE  = 1_907_256
_SV_FS_LBA_END        = 9
_SV_VIRTUAL_BRAM_BASE = 4608
assert DATA_ABS        == _SV_VIRTUAL_LBA_BASE,  f"VIRTUAL_LBA_BASE mismatch: {DATA_ABS}"
assert FS_LBA_END      == _SV_FS_LBA_END,        f"FS_LBA_END mismatch: {FS_LBA_END}"
assert VIRTUAL_BRAM_BASE == _SV_VIRTUAL_BRAM_BASE, f"VIRTUAL_BRAM_BASE mismatch: {VIRTUAL_BRAM_BASE}"

# ─────────────────────────────────────────────
#  Preset files
#  Keys  : "NAME.EXT"  (uppercased, truncated to 8.3 automatically)
#  Values: str or bytes  (one cluster = 4 KB max per file; add more → increase
#          VIRTUAL_LBA_COUNT by 8 in nvme_pkg.sv and re-synthesize)
# ─────────────────────────────────────────────
PRESET_FILES = {
    "README.TXT": "Welcome to Eevee!\r\n",
    "INFO.URL": (
        "[InternetShortcut]\r\n"
        "URL=https://discord.gg/TnJREgxC8t\r\n"
    ),
}

# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def le16(b, o, v): struct.pack_into("<H", b, o, v)
def le32(b, o, v): struct.pack_into("<I", b, o, v)

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
    le16(d, 20, (cluster >> 16) & 0xFFFF)  # FstClusHI
    le16(d, 22, FAT_TIME)
    le16(d, 24, FAT_DATE)
    le16(d, 26,  cluster        & 0xFFFF)  # FstClusLO
    le32(d, 28, size)
    return d

# ─────────────────────────────────────────────
#  MBR  (LBA 0)
# ─────────────────────────────────────────────
def build_mbr():
    b = bytearray(512)
    le32(b, 0x1B8, MBR_DISK_SIGNATURE)
    b[446] = 0x80                        # bootable flag
    b[450] = 0x0C                        # FAT32 with LBA
    le32(b, 454, 1)                      # partition start LBA
    le32(b, 458, TOTAL_DISK_SECTORS)     # spoofed 1 TB
    b[510] = 0x55; b[511] = 0xAA
    return b

# ─────────────────────────────────────────────
#  BPB / Volume Boot Record  (LBA 1)
# ─────────────────────────────────────────────
def build_bpb():
    b = bytearray(512)
    b[0] = 0xEB; b[1] = 0x58; b[2] = 0x90   # short jump + NOP
    b[3:11] = b"MSDOS5.0"
    le16(b, 11, SECTOR_SIZE)
    b[13]  = SECTORS_PER_CLUSTER
    le16(b, 14, RESERVED_SECTORS)
    b[16]  = NUM_FATS
    le16(b, 17, 0)                       # RootEntryCount = 0 (FAT32)
    le16(b, 19, 0)                       # TotalSectors16 = 0 (FAT32)
    b[21]  = 0xF8                        # MediaType: fixed disk
    le16(b, 22, 0)                       # SectorsPerFAT16 = 0 (FAT32)
    le16(b, 24, 63)                      # SectorsPerTrack
    le16(b, 26, 255)                     # NumHeads
    le32(b, 28, 1)                       # HiddenSectors
    le32(b, 32, TOTAL_DISK_SECTORS)      # TotalSectors32 = 1 TB ★
    le32(b, 36, SECTORS_PER_FAT)         # SectorsPerFAT32 (correct for 1 TB) ★
    le16(b, 40, 0)                       # ExtFlags
    le16(b, 42, 0)                       # FSVersion 0.0
    le32(b, 44, ROOT_CLUSTER)            # RootCluster = 2
    le16(b, 48, 1)                       # FSInfo at partition sector 1 (abs LBA 2)
    le16(b, 50, 0)                       # BkBootSec = 0 (no backup)
    b[64]  = 0x80                        # DriveNumber
    b[66]  = 0x29                        # BootSignature
    le32(b, 67, VOLUME_SERIAL)
    b[71:82] = VOLUME_LABEL.encode('ascii')[:11]
    b[82:90] = b"FAT32   "
    b[510] = 0x55; b[511] = 0xAA
    return b

# ─────────────────────────────────────────────
#  FSInfo  (LBA 2 = partition sector 1)
# ─────────────────────────────────────────────
def build_fsinfo():
    b = bytearray(512)
    le32(b, 0,   0x41615252)   # LeadSig
    le32(b, 484, 0x61417272)   # StrucSig
    le32(b, 488, 0xFFFFFFFF)   # FreeCount = unknown (Windows will count from FAT)
    le32(b, 492, 0xFFFFFFFF)   # NextFree  = unknown
    le32(b, 508, 0xAA550000)   # TrailSig
    return b

# ─────────────────────────────────────────────
#  FAT1 — only FAT_BRAM_SECTORS physically in BRAM (LBA 5–8)
#  Covers clusters 0–511; entries 0–4 are set, rest are zeros = free.
#  FAT sectors beyond BRAM (LBA 9+ → firmware zeros) = all remaining clusters free.
# ─────────────────────────────────────────────
def build_fat(allocs):
    """allocs: list of (start_cluster, num_clusters) for file clusters."""
    b = bytearray(FAT_BRAM_SECTORS * SECTOR_SIZE)
    le32(b, 0, 0x0FFFFFF8)   # cluster 0: media descriptor
    le32(b, 4, 0x0FFFFFFF)   # cluster 1: reserved EOC
    le32(b, 8, 0x0FFFFFFF)   # cluster 2: root dir EOC
    for start, count in allocs:
        for i in range(count - 1):
            le32(b, (start + i) * 4, start + i + 1)
        le32(b, (start + count - 1) * 4, 0x0FFFFFFF)
    return b

# ─────────────────────────────────────────────
#  Root directory cluster  (BRAM sectors 9–16, served at LBA DATA_ABS)
# ─────────────────────────────────────────────
def build_root_cluster(entries):
    b = bytearray(SECTORS_PER_CLUSTER * SECTOR_SIZE)
    for i, e in enumerate(entries):
        b[i * 32: i * 32 + 32] = e
    return b

# ─────────────────────────────────────────────
#  Assemble full 128 KB BRAM image
# ─────────────────────────────────────────────
def build_image():
    cluster_size = SECTORS_PER_CLUSTER * SECTOR_SIZE
    next_cluster = ROOT_CLUSTER + 1   # cluster 2 = root dir; files start at 3
    allocs      = []
    dir_entries = []
    data_map    = {}   # cluster → bytes (padded to cluster_size)

    for fname, content in PRESET_FILES.items():
        if isinstance(content, str):
            content = content.encode('ascii')
        num_clusters = max(1, (len(content) + cluster_size - 1) // cluster_size)
        assert next_cluster - ROOT_CLUSTER + num_clusters - 1 < VIRTUAL_LBA_COUNT // SECTORS_PER_CLUSTER, \
            f"{fname}: exceeds virtual cluster window — increase VIRTUAL_LBA_COUNT in nvme_pkg.sv"

        name8, ext3 = name_83(fname)
        dir_entries.append(make_dirent(name8, ext3, next_cluster, len(content)))
        allocs.append((next_cluster, num_clusters))

        for i in range(num_clusters):
            chunk = content[i * cluster_size: (i + 1) * cluster_size]
            data_map[next_cluster + i] = chunk.ljust(cluster_size, b'\x00')

        next_cluster += num_clusters

    # Volume label entry (attr 0x08, no cluster, no size)
    vol_label_bytes = VOLUME_LABEL.encode('ascii')[:11]
    vol_entry = make_dirent(vol_label_bytes[:8], vol_label_bytes[8:11],
                            cluster=0, size=0, attr=0x08)
    root_entries = [vol_entry] + dir_entries

    image = bytearray(TOTAL_BRAM_SECTORS * SECTOR_SIZE)

    # Window A: filesystem structures (LBA 0–8 → BRAM sectors 0–8)
    image[0 * 512: 1 * 512] = build_mbr()
    image[1 * 512: 2 * 512] = build_bpb()
    image[2 * 512: 3 * 512] = build_fsinfo()
    # sectors 3–4: zeros (reserved padding) — already zero
    fat = build_fat(allocs)
    image[ABS_FAT1 * 512: ABS_FAT1 * 512 + len(fat)] = fat   # BRAM sectors 5–8

    # Window B: cluster data (LBA DATA_ABS+ → BRAM sectors 9–32)
    # Root dir cluster 2
    root = build_root_cluster(root_entries)
    image[VIRTUAL_BRAM_BASE: VIRTUAL_BRAM_BASE + len(root)] = root

    # File clusters 3, 4, …
    for cluster, data in data_map.items():
        bram_offset = VIRTUAL_BRAM_BASE + (cluster - ROOT_CLUSTER) * cluster_size
        image[bram_offset: bram_offset + len(data)] = data

    return image

# ─────────────────────────────────────────────
#  Write .mem file  (32-bit little-endian hex words, one per line)
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
    file_clusters = sum(
        max(1, (len(c.encode('ascii') if isinstance(c, str) else c) + cluster_size - 1) // cluster_size)
        for c in PRESET_FILES.values()
    )
    count_of_clusters = (TOTAL_DISK_SECTORS - DATA_REL) // SECTORS_PER_CLUSTER
    free_gb = (count_of_clusters - 1 - file_clusters) * cluster_size / 1e9

    print("=== FAT32 1 TB + Files Image Generator ===")
    print(f"  Spoofed size   : {TOTAL_DISK_SECTORS * SECTOR_SIZE / 1e12:.2f} TB")
    print(f"  SectorsPerFAT  : {SECTORS_PER_FAT:,}  (correct — Windows accepts volume)")
    print(f"  CountOfClusters: {count_of_clusters:,}")
    print(f"  Free space     : ~{free_gb:.1f} GB  (zeros beyond BRAM = free FAT entries)")
    print()
    print("  BRAM layout (256 sectors = 128 KB):")
    print(f"    MBR          : sector 0     → LBA 0")
    print(f"    BPB          : sector 1     → LBA 1")
    print(f"    FSInfo       : sector 2     → LBA 2")
    print(f"    FAT1 (BRAM)  : sectors 5–8  → LBA {ABS_FAT1}–{ABS_FAT1 + FAT_BRAM_SECTORS - 1}  ({FAT_BRAM_SECTORS} sectors)")
    print(f"    Root dir     : sectors 9–16 → LBA {DATA_ABS}–{DATA_ABS + SECTORS_PER_CLUSTER - 1}  (cluster 2)")
    cl = ROOT_CLUSTER + 1
    for fname in PRESET_FILES:
        lba = DATA_ABS + (cl - ROOT_CLUSTER) * SECTORS_PER_CLUSTER
        bram_s = FS_LBA_END + (cl - ROOT_CLUSTER) * SECTORS_PER_CLUSTER
        print(f"    {fname:<12}  : sectors {bram_s}–{bram_s+SECTORS_PER_CLUSTER-1} → LBA {lba}–{lba+SECTORS_PER_CLUSTER-1}  (cluster {cl})")
        cl += 1
    print()
    print(f"  Firmware constants (nvme_pkg.sv) — must match:")
    print(f"    FS_LBA_END        = {FS_LBA_END}")
    print(f"    VIRTUAL_LBA_BASE  = {DATA_ABS}")
    print(f"    VIRTUAL_LBA_COUNT = {VIRTUAL_LBA_COUNT}")
    print(f"    VIRTUAL_BRAM_BASE = {VIRTUAL_BRAM_BASE}")
    print()

    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "nvme_fat12_image.mem"
    )
    image = build_image()
    write_mem(image, output_path)
    print(f"  Output: {output_path}")

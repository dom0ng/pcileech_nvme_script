#!/usr/bin/env python3
"""
generate_exfat_1tb.py  –  exFAT 1 TB SSD image for Vivado BRAM

Why exFAT?
   NTFS needs the full 128 KB $UpCase table → impossible in 128 KB BRAM.
   FAT32 works but is dated for an SSD.
   exFAT supports a *compressed* up-case table (~60 B for ASCII-only) and
   gives a modern look for the disk.

The trick:
   exFAT requires the Allocation Bitmap and Up-Case Table data to be
   readable at fixed cluster locations referenced from the root dir.
   We pick FAT geometry so that clusters 2, 3 and 4 all START inside
   our 256-sector BRAM.  Their tails (and the rest of the cluster heap)
   live outside BRAM and read as zeros — but only DataLength bytes are
   ever consulted, so that's fine.

Disk layout (all critical bytes within LBA 0..255):
   LBA   0      : MBR  (partition 1, type 0x07, covers 1 TB - 1 sectors)
   LBA   1      : Main Boot Sector
   LBA   2- 9   : Extended Boot Sectors  (each ends 0xAA550000)
   LBA  10      : OEM Parameters
   LBA  11      : Reserved
   LBA  12      : Boot Checksum   (computed over LBA 1..11)
   LBA  13- 24  : Backup boot region (identical copy of LBA 1..12)
   LBA  25-126  : FAT (102 sectors → 13056 entries → ClusterCount=13054)
   LBA 127-190  : cluster 2  (root directory, 32 KB)
   LBA 191-254  : cluster 3  (allocation bitmap, 32 KB)
   LBA 255      : cluster 4 first sector (compressed Up-Case Table)
   LBA 256-...  : cluster 4 tail + rest of heap (zeros, never consulted)

Reported volume size to Windows ≈ 13054 × 32 KB ≈ 408 MB on a 1 TB disk.
"""

import struct, os

# ─── Disk geometry ─────────────────────────────────────────────────────────
SECTOR_SIZE   = 512
BRAM_SECTORS  = 256                  # 128 KB BRAM
TOTAL_SECTORS = 2_147_483_648        # 1 TB

MBR_SIGNATURE = 0x19861019
VOLUME_SERIAL = 0xCAFEBABE
VOLUME_LABEL  = "NVME1TB"            # ≤ 11 UTF-16 chars

# ─── exFAT BPB choices (chosen so clusters 2/3/4 start inside BRAM) ────────
PARTITION_OFFSET         = 1                          # LBA where vol starts
VOLUME_LENGTH            = TOTAL_SECTORS - 1          # vol size, in sectors

BYTES_PER_SECTOR_SHIFT   = 9                          # 2^9  = 512  B
SECTORS_PER_CLUSTER_SHIFT= 6                          # 2^6  = 64 sec = 32 KB
SECTORS_PER_CLUSTER      = 1 << SECTORS_PER_CLUSTER_SHIFT
CLUSTER_SIZE             = SECTORS_PER_CLUSTER * SECTOR_SIZE

FAT_OFFSET               = 24                         # volume sector
FAT_LENGTH               = 102                        # volume sectors
CLUSTER_HEAP_OFFSET      = FAT_OFFSET + FAT_LENGTH    # 126
CLUSTER_COUNT            = FAT_LENGTH * (SECTOR_SIZE // 4) - 2   # 13054

ROOT_CLUSTER             = 2
BITMAP_CLUSTER           = 3
UPCASE_CLUSTER           = 4

# ─── little-endian helpers ─────────────────────────────────────────────────
def p8 (b, o, v): b[o] = v & 0xFF
def p16(b, o, v): struct.pack_into("<H", b, o, v)
def p32(b, o, v): struct.pack_into("<I", b, o, v)
def p64(b, o, v): struct.pack_into("<Q", b, o, v)

# ─── Up-Case Table (compressed, ASCII-only, 60 bytes) ──────────────────────
def build_upcase_table() -> bytes:
    """
    Minimal valid compressed Up-Case Table:
      0xFFFF, 0x0061 → identity for code points 0x0000..0x0060
      0x0041..0x005A → maps 'a'..'z' to 'A'..'Z'  (positions 0x61..0x7A)
      0xFFFF, 0xFF85 → identity for 0x007B..0xFFFF
    """
    entries = [0xFFFF, 0x0061]
    entries += list(range(0x41, 0x5B))     # 'A'..'Z'
    entries += [0xFFFF, 0xFF85]
    return b"".join(struct.pack("<H", e) for e in entries)

def exfat_checksum(data: bytes, skip_offsets=()) -> int:
    """exFAT rotate-right + add checksum (used for boot + up-case)."""
    cs = 0
    for i, byte in enumerate(data):
        if i in skip_offsets:
            continue
        cs = ((cs >> 1) | ((cs & 1) << 31)) + byte
        cs &= 0xFFFFFFFF
    return cs

# ─── MBR ───────────────────────────────────────────────────────────────────
def build_mbr() -> bytes:
    b = bytearray(SECTOR_SIZE)
    p32(b, 0x1B8, MBR_SIGNATURE)
    b[446] = 0x80                          # bootable
    b[447] = 0x00; b[448] = 0x02; b[449] = 0x00   # CHS start (legacy)
    b[450] = 0x07                          # type 0x07 = exFAT/NTFS
    b[451] = 0xFE; b[452] = 0xFF; b[453] = 0xFF   # CHS end (legacy)
    p32(b, 454, PARTITION_OFFSET)          # start LBA
    p32(b, 458, TOTAL_SECTORS - 1)         # sector count
    b[510] = 0x55; b[511] = 0xAA
    return bytes(b)

# ─── Main Boot Sector (volume sector 0 = disk LBA 1) ───────────────────────
def build_main_boot_sector() -> bytes:
    b = bytearray(SECTOR_SIZE)
    b[0:3]  = b"\xEB\x76\x90"              # JumpBoot
    b[3:11] = b"EXFAT   "                  # FileSystemName
    # bytes 11..63 must be zero
    p64(b,  64, PARTITION_OFFSET)
    p64(b,  72, VOLUME_LENGTH)
    p32(b,  80, FAT_OFFSET)
    p32(b,  84, FAT_LENGTH)
    p32(b,  88, CLUSTER_HEAP_OFFSET)
    p32(b,  92, CLUSTER_COUNT)
    p32(b,  96, ROOT_CLUSTER)
    p32(b, 100, VOLUME_SERIAL)
    p16(b, 104, 0x0100)                    # FileSystemRevision = 1.00
    p16(b, 106, 0x0000)                    # VolumeFlags (clean)
    b[108] = BYTES_PER_SECTOR_SHIFT
    b[109] = SECTORS_PER_CLUSTER_SHIFT
    b[110] = 1                             # NumberOfFats
    b[111] = 0x80                          # DriveSelect
    b[112] = 0xFF                          # PercentInUse (don't track)
    # 113..119 reserved; 120..509 boot code (zero)
    b[510] = 0x55; b[511] = 0xAA           # BootSignature
    return bytes(b)

def build_extended_boot_sector() -> bytes:
    b = bytearray(SECTOR_SIZE)
    p32(b, 508, 0xAA550000)                # ExtendedBootSignature
    return bytes(b)

def build_oem_params_sector() -> bytes:
    return bytes(SECTOR_SIZE)              # all zeros = no OEM params

def build_reserved_sector() -> bytes:
    return bytes(SECTOR_SIZE)

def build_boot_checksum_sector(checksum: int) -> bytes:
    b = bytearray(SECTOR_SIZE)
    for off in range(0, SECTOR_SIZE, 4):
        struct.pack_into("<I", b, off, checksum)
    return bytes(b)

# ─── FAT first sector (entries 0..4) ───────────────────────────────────────
def build_fat_first_sector() -> bytes:
    b = bytearray(SECTOR_SIZE)
    p32(b,  0, 0xFFFFFFF8)                 # FAT[0] = media descriptor
    p32(b,  4, 0xFFFFFFFF)                 # FAT[1] = end-of-chain
    p32(b,  8, 0xFFFFFFFF)                 # FAT[2] = root → EOC (1 cluster)
    p32(b, 12, 0xFFFFFFFF)                 # FAT[3] = bitmap → EOC
    p32(b, 16, 0xFFFFFFFF)                 # FAT[4] = up-case → EOC
    return bytes(b)

# ─── Allocation Bitmap (cluster 3) ─────────────────────────────────────────
def build_allocation_bitmap() -> bytes:
    """
    One bit per cluster in the cluster heap, starting at cluster 2.
    Clusters 2, 3, 4 are in use → bits 0, 1, 2 set → byte 0 = 0x07.
    """
    nbytes = (CLUSTER_COUNT + 7) // 8
    b = bytearray(nbytes)
    b[0] = 0x07
    return bytes(b)

# ─── Root directory (cluster 2) ────────────────────────────────────────────
def build_root_directory(upcase_checksum: int,
                         bitmap_data_length: int,
                         upcase_data_length: int) -> bytes:
    """
    Root directory entries.  Three primary entries totalling 96 bytes.
    Rest of the cluster is zero, where the leading 0x00 byte marks
    end-of-directory.
    """
    b = bytearray(CLUSTER_SIZE)            # 32 KB cluster

    # ── Volume Label entry  (type 0x83, 32 bytes) ────────────────────
    label_utf16 = VOLUME_LABEL.encode("utf-16-le")
    b[0]   = 0x83
    b[1]   = len(VOLUME_LABEL)
    b[2:2+len(label_utf16)] = label_utf16
    # bytes 0x18..0x1F reserved (zero)

    # ── Allocation Bitmap entry  (type 0x81, 32 bytes) ───────────────
    p8 (b, 32+0x00, 0x81)
    p8 (b, 32+0x01, 0x00)                  # BitmapFlags (first/only bitmap)
    # 0x02..0x13 reserved
    p32(b, 32+0x14, BITMAP_CLUSTER)        # FirstCluster
    p64(b, 32+0x18, bitmap_data_length)    # DataLength

    # ── Up-Case Table entry  (type 0x82, 32 bytes) ───────────────────
    p8 (b, 64+0x00, 0x82)
    # 0x01..0x03 reserved
    p32(b, 64+0x04, upcase_checksum)       # TableChecksum
    # 0x08..0x13 reserved
    p32(b, 64+0x14, UPCASE_CLUSTER)        # FirstCluster
    p64(b, 64+0x18, upcase_data_length)    # DataLength

    # rest of cluster: zero (b[96] == 0x00 ⇒ end of directory)
    return bytes(b)

# ─── Build complete BRAM image ─────────────────────────────────────────────
def build_image() -> bytearray:
    image = bytearray(BRAM_SECTORS * SECTOR_SIZE)

    # Pre-compute up-case data and checksum
    upcase_data       = build_upcase_table()
    upcase_checksum   = exfat_checksum(upcase_data)
    upcase_dlen       = len(upcase_data)
    bitmap_data       = build_allocation_bitmap()
    bitmap_dlen       = len(bitmap_data)

    # ─── Boot region: 12 sectors covering LBA 1..12 ──────────────────────
    boot_region = bytearray(12 * SECTOR_SIZE)
    boot_region[0:512]               = build_main_boot_sector()
    for i in range(1, 9):
        boot_region[i*512:(i+1)*512] = build_extended_boot_sector()
    boot_region[ 9*512:10*512]       = build_oem_params_sector()
    boot_region[10*512:11*512]       = build_reserved_sector()
    # Boot checksum: covers sectors 0..10, skipping VolumeFlags (106,107)
    # and PercentInUse (112) of sector 0
    cs = exfat_checksum(boot_region[:11*SECTOR_SIZE],
                        skip_offsets=(106, 107, 112))
    boot_region[11*512:12*512]       = build_boot_checksum_sector(cs)

    # MBR
    image[0:SECTOR_SIZE] = build_mbr()

    # Main boot region    : disk LBA 1..12
    image[1*SECTOR_SIZE : 13*SECTOR_SIZE] = boot_region
    # Backup boot region  : disk LBA 13..24  (identical copy)
    image[13*SECTOR_SIZE : 25*SECTOR_SIZE] = boot_region

    # FAT first sector at disk LBA 25 (= volume sector 24)
    fat_lba = PARTITION_OFFSET + FAT_OFFSET
    image[fat_lba*SECTOR_SIZE : (fat_lba+1)*SECTOR_SIZE] = build_fat_first_sector()

    # Cluster 2 (root dir) starts at disk LBA = PARTITION_OFFSET + ClusterHeapOffset
    root_lba = PARTITION_OFFSET + CLUSTER_HEAP_OFFSET
    root_dir = build_root_directory(upcase_checksum, bitmap_dlen, upcase_dlen)
    # Only write the part that fits in BRAM (the rest is zero anyway)
    write_len = min(len(root_dir), len(image) - root_lba*SECTOR_SIZE)
    image[root_lba*SECTOR_SIZE : root_lba*SECTOR_SIZE + write_len] = root_dir[:write_len]

    # Cluster 3 (allocation bitmap)
    bitmap_lba = root_lba + SECTORS_PER_CLUSTER
    write_len  = min(len(bitmap_data), len(image) - bitmap_lba*SECTOR_SIZE)
    if write_len > 0:
        image[bitmap_lba*SECTOR_SIZE : bitmap_lba*SECTOR_SIZE + write_len] = bitmap_data[:write_len]

    # Cluster 4 (up-case table)
    upcase_lba = root_lba + 2 * SECTORS_PER_CLUSTER
    write_len  = min(len(upcase_data), len(image) - upcase_lba*SECTOR_SIZE)
    if write_len > 0:
        image[upcase_lba*SECTOR_SIZE : upcase_lba*SECTOR_SIZE + write_len] = upcase_data[:write_len]
    else:
        raise RuntimeError(
            f"Up-Case cluster (LBA {upcase_lba}) is outside BRAM "
            f"({BRAM_SECTORS} sectors).  Reduce FAT_LENGTH.")

    return image

# ─── .mem writer ───────────────────────────────────────────────────────────
def write_mem(image: bytearray, path: str):
    dwords = len(image) // 4
    assert dwords == 32768, f"Image is {dwords} DWORDs, expected 32768"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as f:
        for i in range(dwords):
            f.write(f"{struct.unpack_from('<I', image, i*4)[0]:08X}\n")

# ─── entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    image = build_image()

    # Stats
    fat_lba    = PARTITION_OFFSET + FAT_OFFSET
    root_lba   = PARTITION_OFFSET + CLUSTER_HEAP_OFFSET
    bitmap_lba = root_lba + SECTORS_PER_CLUSTER
    upcase_lba = root_lba + 2 * SECTORS_PER_CLUSTER

    print("=" * 60)
    print("  exFAT 1 TB SSD Image  (compact, fits in 128 KB BRAM)")
    print("=" * 60)
    print(f"  Disk size            : {TOTAL_SECTORS * SECTOR_SIZE / 1024**4:.2f} TB")
    print(f"  Filesystem           : exFAT")
    print(f"  Cluster size         : {CLUSTER_SIZE // 1024} KB ({SECTORS_PER_CLUSTER} sectors)")
    print(f"  ClusterCount         : {CLUSTER_COUNT:,}")
    print(f"  Reported volume size : "
          f"{CLUSTER_COUNT * CLUSTER_SIZE / 1024**2:.0f} MB")
    print(f"  Volume label         : {VOLUME_LABEL}")
    print(f"  Volume serial        : 0x{VOLUME_SERIAL:08X}")
    print()
    print(f"  Layout (absolute LBA):")
    print(f"    LBA   0          : MBR (partition type 0x07)")
    print(f"    LBA   1          : Main Boot Sector")
    print(f"    LBA   2- 9       : Extended Boot Sectors")
    print(f"    LBA  10          : OEM Parameters")
    print(f"    LBA  11          : Reserved")
    print(f"    LBA  12          : Boot Checksum")
    print(f"    LBA  13-24       : Backup boot region")
    print(f"    LBA  {fat_lba:3d}-{fat_lba+FAT_LENGTH-1:3d}     : FAT")
    print(f"    LBA  {root_lba:3d}-{root_lba+SECTORS_PER_CLUSTER-1:3d}     : Cluster 2 — root directory")
    print(f"    LBA  {bitmap_lba:3d}-{bitmap_lba+SECTORS_PER_CLUSTER-1:3d}     : Cluster 3 — allocation bitmap")
    print(f"    LBA  {upcase_lba:3d}+         : Cluster 4 — Up-Case Table"
          f" (first sector in BRAM, rest = zeros, OK)")
    print()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "ip", "nvme_fat12_image.mem")
    write_mem(image, out)
    print(f"  ✓  Written : {out}")
    print("=" * 60)

#!/usr/bin/env python3
"""
generate_fat32_100gb.py
Generates a FAT32 spoof-100GB image (.mem format for Vivado BRAM initialization)

How it works:
  - TotalSectors32  = 209715200 (100 GB)
  - SectorsPerFAT   = 204798   (correct value for 100 GB, passes Windows validation)
  - NumFATs         = 1        (single FAT to maximize BRAM space for FAT1)
  - FAT1 first 223 sectors live in BRAM; beyond that the firmware returns all-zeros
    which Windows interprets as free clusters
  - Root dir (cluster 2) is beyond BRAM → firmware returns all-zeros → valid empty dir
  - Windows sees: 100 GB disk, ~99.9 GB free, empty root directory

Hardware:
  - Xilinx Artix-7 75T (Enigma X1)
  - BRAM: 128 KB = 256 sectors
  - PCILeech NVMe Samsung 980 1 TB emulation firmware
"""

import struct
import os

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────
SECTOR_SIZE          = 512
TOTAL_BRAM_SECTORS   = 256          # Physical BRAM limit (128 KB)
SECTORS_PER_CLUSTER  = 8            # 4 KB per cluster
VOLUME_LABEL         = "Eevee"
TOTAL_DISK_SECTORS   = 209_715_200  # Spoofed disk size = 100 GB
MBR_DISK_SIGNATURE   = 0x19861019
VOLUME_SERIAL        = 0xCAFEBABE

RESERVED_SECTORS     = 32           # FAT32 standard
NUM_FATS             = 1            # Single FAT — saves BRAM for FAT data
ROOT_CLUSTER         = 2

# ─────────────────────────────────────────────
#  Auto-calculate SectorsPerFAT
#  (must match TotalSectors32 so Windows accepts the volume)
# ─────────────────────────────────────────────
_spf_est        = 100_000
_data_rel       = RESERVED_SECTORS + NUM_FATS * _spf_est
_total_clust    = (TOTAL_DISK_SECTORS - _data_rel) // SECTORS_PER_CLUSTER
SECTORS_PER_FAT = (_total_clust * 4 + SECTOR_SIZE - 1) // SECTOR_SIZE

# Final layout (absolute LBA; partition starts at LBA 1)
ABS_FAT1          = 1 + RESERVED_SECTORS        # = 33
DATA_REL          = RESERVED_SECTORS + NUM_FATS * SECTORS_PER_FAT
ABS_DATA          = 1 + DATA_REL                # beyond BRAM — firmware returns zeros
BRAM_FAT_SECTORS  = TOTAL_BRAM_SECTORS - ABS_FAT1  # = 223 sectors of FAT fit in BRAM

# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def le16(b, o, v): struct.pack_into("<H", b, o, v)
def le32(b, o, v): struct.pack_into("<I", b, o, v)

# ─────────────────────────────────────────────
#  MBR  (LBA 0)
# ─────────────────────────────────────────────
def build_mbr():
    b = bytearray(512)
    le32(b, 0x1B8, MBR_DISK_SIGNATURE)
    b[446] = 0x80                          # bootable flag
    b[447] = 0x00; b[448] = 0x02; b[449] = 0x00
    b[450] = 0x0C                          # partition type: FAT32 with LBA
    b[451] = 0xFE; b[452] = 0xFF; b[453] = 0xFF
    le32(b, 454, 1)                        # partition start LBA
    le32(b, 458, TOTAL_DISK_SECTORS)       # partition size in sectors
    b[510] = 0x55; b[511] = 0xAA
    return b

# ─────────────────────────────────────────────
#  BPB / Volume Boot Record  (LBA 1, backup at LBA 7)
# ─────────────────────────────────────────────
def build_bpb():
    b = bytearray(512)
    b[0] = 0xEB; b[1] = 0x58; b[2] = 0x90   # jump + NOP
    b[3:11] = b"MSDOS5.0"                    # OEM name
    le16(b, 11, SECTOR_SIZE)                 # BytesPerSector
    b[13]  = SECTORS_PER_CLUSTER             # SectorsPerCluster
    le16(b, 14, RESERVED_SECTORS)            # ReservedSectors
    b[16]  = NUM_FATS                        # NumFATs
    le16(b, 17, 0)                           # RootEntryCount = 0 (FAT32)
    le16(b, 19, 0)                           # TotalSectors16 = 0 (FAT32)
    b[21]  = 0xF8                            # MediaType
    le16(b, 22, 0)                           # SectorsPerFAT16 = 0 (FAT32)
    le16(b, 24, 63)                          # SectorsPerTrack
    le16(b, 26, 255)                         # NumHeads
    le32(b, 28, 1)                           # HiddenSectors
    le32(b, 32, TOTAL_DISK_SECTORS)          # TotalSectors32 = 100 GB ★
    le32(b, 36, SECTORS_PER_FAT)             # SectorsPerFAT32 (correct value) ★
    le16(b, 40, 0)                           # ExtFlags
    le16(b, 42, 0)                           # FSVersion 0.0
    le32(b, 44, ROOT_CLUSTER)                # RootCluster = 2
    le16(b, 48, 1)                           # FSInfo at relative sector 1
    le16(b, 50, 6)                           # BackupBoot at relative sector 6
    b[64]  = 0x80                            # DriveNumber
    b[66]  = 0x29                            # BootSignature
    le32(b, 67, VOLUME_SERIAL)               # VolumeSerialNumber
    b[71:82] = VOLUME_LABEL.encode("ascii").ljust(11)[:11]
    b[82:90] = b"FAT32   "
    b[510] = 0x55; b[511] = 0xAA
    return b

# ─────────────────────────────────────────────
#  FSInfo  (LBA 2, backup at LBA 8)
# ─────────────────────────────────────────────
def build_fsinfo():
    b = bytearray(512)
    le32(b, 0,   0x41615252)    # LeadSig
    le32(b, 484, 0x61417272)    # StrucSig
    le32(b, 488, 0xFFFFFFFF)    # FreeCount = unknown (let Windows scan)
    le32(b, 492, 0xFFFFFFFF)    # NextFree  = unknown
    le32(b, 508, 0xAA550000)    # TrailSig
    return b

# ─────────────────────────────────────────────
#  FAT1  (LBA 33 — only write the 223 sectors that fit in BRAM)
#
#  The remaining ~204k sectors of FAT1 live beyond BRAM.
#  The firmware returns all-zeros for those reads, which
#  Windows treats as free clusters — giving ~99.9 GB free space.
# ─────────────────────────────────────────────
def build_fat_partial():
    b = bytearray(BRAM_FAT_SECTORS * SECTOR_SIZE)
    le32(b, 0, 0x0FFFFFF8)   # cluster 0: media descriptor
    le32(b, 4, 0x0FFFFFFF)   # cluster 1: end-of-chain (reserved)
    le32(b, 8, 0x0FFFFFFF)   # cluster 2: root dir — end-of-chain
    # clusters 3 … 28543 : zero = free (stored in BRAM)
    # clusters 28544+    : beyond BRAM → firmware returns zero = free ✓
    return b

# ─────────────────────────────────────────────
#  Assemble image
# ─────────────────────────────────────────────
def build_image():
    image = bytearray(TOTAL_BRAM_SECTORS * SECTOR_SIZE)

    image[0*512 : 1*512] = build_mbr()
    image[1*512 : 2*512] = build_bpb()        # BPB
    image[2*512 : 3*512] = build_fsinfo()     # FSInfo
    image[7*512 : 8*512] = build_bpb()        # Backup BPB  (abs LBA 7)
    image[8*512 : 9*512] = build_fsinfo()     # Backup FSInfo (abs LBA 8)

    fat = build_fat_partial()
    image[ABS_FAT1*512 : ABS_FAT1*512 + len(fat)] = fat

    # Root directory (abs LBA ~204k) is beyond BRAM.
    # Firmware returns all-zeros → Windows sees a valid empty directory.
    # No data needs to be written here.

    return image

# ─────────────────────────────────────────────
#  Write .mem file
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
    total_clusters = (TOTAL_DISK_SECTORS - DATA_REL) // SECTORS_PER_CLUSTER
    free_gb        = (total_clusters - 1) * SECTORS_PER_CLUSTER * SECTOR_SIZE / 1024**3

    print("=== FAT32 100 GB Image Generator ===")
    print(f"  Disk size      : {TOTAL_DISK_SECTORS * SECTOR_SIZE / 1024**3:.2f} GB")
    print(f"  SectorsPerFAT  : {SECTORS_PER_FAT:,}")
    print(f"  NumFATs        : {NUM_FATS}")
    print(f"  Total clusters : {total_clusters:,}")
    print(f"  Windows free   : ~{free_gb:.2f} GB")
    print()
    print("  Layout (absolute LBA):")
    print(f"    MBR          : LBA 0")
    print(f"    BPB          : LBA 1")
    print(f"    FSInfo       : LBA 2")
    print(f"    Backup BPB   : LBA 7")
    print(f"    FAT1         : LBA {ABS_FAT1} ~ {ABS_FAT1 + SECTORS_PER_FAT - 1}")
    print(f"      In BRAM    : LBA {ABS_FAT1} ~ {TOTAL_BRAM_SECTORS - 1}  ({BRAM_FAT_SECTORS} sectors)")
    print(f"      Beyond BRAM: firmware returns zeros → free clusters ✓")
    print(f"    Root dir     : LBA {ABS_DATA} → beyond BRAM → zeros → empty dir ✓")
    print()

    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "nvme_fat32_100gb.mem"
    )
    image = build_image()
    write_mem(image, output_path)

    print(f"  Output : {output_path}")
    print()
    print("  Usage:")
    print("    Replace nvme_fat12_image.mem in your Vivado project with nvme_fat32_100gb.mem")
    print("    Re-synthesize → flash → Windows shows a 100 GB FAT32 disk ✓")
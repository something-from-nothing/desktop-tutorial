#!/bin/bash
# Turns a finished system folder into DeskOS.bin (a whole disk: GPT, an EFI
# partition with GRUB, and the system) and DeskOS.zip. Needs no mounting or root
# loop devices, so it runs in a plain container.
#   make-disk.sh <system folder> <scratch folder>
set -euo pipefail
cd "$(dirname "$0")"
root=$1
work=$2

# Boot loader: one GRUB file with its settings inside, so the EFI partition holds
# nothing else and no install step is needed.
cat > "$work/grub.cfg" <<CFG
set timeout=0
search --no-floppy --label DESKOS --set=root
linux /vmlinuz root=LABEL=DESKOS ro quiet loglevel=3 vt.global_cursor_default=0 ${DESKOS_CMDLINE_EXTRA:-}
initrd /initrd.img
boot
CFG
grub-mkstandalone -O x86_64-efi -o "$work/BOOTX64.EFI" \
  --modules="part_gpt ext2 search_label linux normal gzio" "boot/grub/grub.cfg=$work/grub.cfg"

# The EFI partition, made as a file without mounting anything.
truncate -s 64M "$work/esp.img"
mkfs.vfat -F 32 -n DESKOS-EFI "$work/esp.img"
mmd -i "$work/esp.img" ::/EFI ::/EFI/BOOT
mcopy -i "$work/esp.img" "$work/BOOTX64.EFI" ::/EFI/BOOT/BOOTX64.EFI

# The system partition, filled straight from the folder, with 1.5 GB free for chats.
used=$(du -sm "$root" | cut -f1)
size=$(( used + 1536 ))
mkfs.ext4 -q -L DESKOS -O ^orphan_file,^metadata_csum_seed -d "$root" "$work/root.img" "${size}M"

# The whole disk: GPT, the EFI partition, then the system.
disk=DeskOS.bin
rm -f "$disk"
truncate -s $(( 1 + 64 + size + 1 ))M "$disk"
sgdisk -o -n 1:2048:+64M -t 1:ef00 -c 1:DESKOS-EFI -n 2:0:0 -t 2:8300 -c 2:DESKOS "$disk"
start2=$(sgdisk -i 2 "$disk" | sed -n 's/^First sector: \([0-9]*\).*/\1/p')
dd if="$work/esp.img" of="$disk" bs=512 seek=2048 conv=notrunc status=none
dd if="$work/root.img" of="$disk" bs=512 seek="$start2" conv=notrunc status=none
sgdisk -v "$disk"
ls -la "$disk"
zip -1 DeskOS.zip "$disk"
ls -la DeskOS.zip
# GitHub release files can't be over 2 GB.
[ "$(stat -c%s DeskOS.zip)" -lt 2000000000 ] || { echo "DeskOS.zip is over 2 GB"; exit 1; }

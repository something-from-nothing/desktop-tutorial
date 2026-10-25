#!/bin/bash
# Builds Desk OS: a small Debian system that starts straight into Desk Agent, with
# the AI engine and the Qwen 3 1.7B model on the disk. The result is DeskOS.bin, a whole
# disk image for a USB stick (the Chromebook Recovery Utility can write it).
#
# Runs as root inside a privileged debian:trixie container (see build-os.yml), and
# needs the engine already built into ./engine and the model at ./model.gguf.
set -euo pipefail
cd "$(dirname "$0")"
work=$PWD/work
rm -rf "$work" && mkdir -p "$work"

apt-get update
apt-get install -y --no-install-recommends mmdebstrap grub-efi-amd64-bin grub-common mtools dosfstools \
  e2fsprogs gdisk zip ca-certificates

# The system itself. firmware-* are for Chromebook Wi-Fi and graphics.
mmdebstrap --mode=root --variant=minbase --components="main contrib non-free-firmware" \
  --include=linux-image-amd64,initramfs-tools,systemd-sysv,libpam-systemd,udev,dbus,kmod,sudo,curl,ca-certificates,\
cage,chromium,fonts-roboto-unhinted,fonts-noto-color-emoji,network-manager,wpasupplicant,\
firmware-iwlwifi,firmware-realtek,firmware-atheros,firmware-intel-graphics,firmware-amd-graphics,\
libinput-bin,libgl1-mesa-dri \
  trixie "$work/root" http://deb.debian.org/debian

root=$work/root
cp -a overlay/. "$root/"
mkdir -p "$root/opt/deskos/app" "$root/opt/deskos/models" "$root/opt/deskos/engine"
cp ../android/app/src/main/assets/index.html "$root/opt/deskos/app/index.html"
cp -a engine/. "$root/opt/deskos/engine/"
cp model.gguf "$root/opt/deskos/models/model.gguf"
chmod +x "$root/opt/deskos/session" "$root/opt/deskos/engine/llama-server"

chroot "$root" /bin/bash -euxc '
  echo deskos > /etc/hostname
  printf "127.0.0.1 localhost\n127.0.1.1 deskos\n" > /etc/hosts
  printf "LABEL=DESKOS / ext4 defaults,noatime 0 1\n" > /etc/fstab
  useradd -m -s /bin/bash -G video,input,render,netdev desk
  passwd -d desk
  printf "desk ALL=(ALL) NOPASSWD: /usr/bin/nmcli, /usr/sbin/poweroff, /usr/sbin/reboot\n" > /etc/sudoers.d/desk
  cat /opt/deskos/profile >> /home/desk/.bash_profile
  chown desk:desk /home/desk/.bash_profile
  systemctl enable deskos-engine.service NetworkManager.service
  systemctl set-default multi-user.target
  apt-get clean
  rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*
'

./make-disk.sh "$root" "$work"

#!/bin/bash
# Builds Desk OS: a Debian desktop (Xfce) made for the HP Chromebook 14, with Desk
# Agent as an app, an app store (Flathub), a USB stick writer, an installer for the
# computer's own drive, and the AI engine with the Qwen 3 1.7B model on the disk. The result is DeskOS.bin, a whole
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

# The system itself. firmware-* are for Chromebook Wi-Fi, graphics and sound.
mmdebstrap --mode=root --variant=minbase --components="main contrib non-free-firmware" \
  --include=linux-image-amd64,initramfs-tools,systemd-sysv,libpam-systemd,udev,dbus,dbus-x11,kmod,sudo,curl,busybox,ca-certificates,\
xserver-xorg-core,xserver-xorg-input-libinput,xinit,lightdm,lightdm-gtk-greeter,\
xfce4-session,xfwm4,xfdesktop4,xfce4-panel,xfce4-settings,xfconf,xfce4-whiskermenu-plugin,xfce4-pulseaudio-plugin,\
xfce4-power-manager,xfce4-power-manager-plugins,xfce4-notifyd,xfce4-terminal,xfce4-screenshooter,xfce4-appfinder,\
thunar,thunar-volman,gvfs,udisks2,mousepad,ristretto,galculator,xarchiver,thunar-archive-plugin,\
chromium,gnome-software,gnome-software-plugin-flatpak,flatpak,gnome-disk-utility,zenity,polkitd,pkexec,\
network-manager,network-manager-gnome,wpasupplicant,pipewire-pulse,wireplumber,pavucontrol,alsa-ucm-conf,\
arc-theme,papirus-icon-theme,fonts-noto-core,fonts-noto-color-emoji,fonts-roboto-unhinted,\
cloud-guest-utils,gdisk,parted,e2fsprogs,tzdata,\
firmware-iwlwifi,firmware-realtek,firmware-atheros,firmware-intel-graphics,firmware-sof-signed,\
libinput-bin,libgl1-mesa-dri \
  trixie "$work/root" http://deb.debian.org/debian

root=$work/root
cp -a overlay/. "$root/"
mkdir -p "$root/opt/deskos/app" "$root/opt/deskos/models" "$root/opt/deskos/engine"
cp ../android/app/src/main/assets/index.html "$root/opt/deskos/app/index.html"
cp -r ../android/app/src/main/assets/pdfjs "$root/opt/deskos/app/pdfjs"
cp -a engine/. "$root/opt/deskos/engine/"
cp model.gguf "$root/opt/deskos/models/model.gguf"
chmod +x "$root/opt/deskos/engine/llama-server" "$root"/opt/deskos/{desk-agent,grow-root,install-helper,install-to-drive}

chroot "$root" /bin/bash -euxc '
  echo deskos > /etc/hostname
  ln -sf /usr/share/zoneinfo/Europe/London /etc/localtime && echo Europe/London > /etc/timezone
  printf "127.0.0.1 localhost\n127.0.1.1 deskos\n" > /etc/hosts
  printf "LABEL=DESKOS / ext4 defaults,noatime 0 1\n" > /etc/fstab
  useradd -m -s /bin/bash -c "Desk OS" -G video,input,render,netdev,audio,plugdev desk
  passwd -d desk
  systemctl enable deskos-engine.service deskos-app.service deskos-grow.service NetworkManager.service lightdm.service
  systemctl set-default graphical.target
  systemd-hwdb update
  flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || echo "::warning::Flathub not added; the app store can add it later"
  xdg-settings set default-web-browser chromium.desktop 2>/dev/null || true
  apt-get clean
  rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*
'

./make-disk.sh "$root" "$work"

<# : Windows runs the lines below as a batch file, which hands the rest to PowerShell.
@echo off
set "RUN_OS_FILE=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-Expression ([IO.File]::ReadAllText('%~f0'))"
pause
exit /b
#>

# Run-OS: tests an operating system in a window on this PC, without touching Windows.
# Double click it to run Desk OS (DeskOS.zip in Downloads), or drag any .zip, .bin,
# .img or .iso onto it to try that instead. The first time, it installs QEMU (a
# free program that runs a computer inside a window) into your own user folder.

$ErrorActionPreference = "Stop"
$home_ = Join-Path $env:LOCALAPPDATA "RunOS"
$qemuDir = Join-Path $home_ "qemu"
$qemu = Join-Path $qemuDir "qemu-system-x86_64.exe"
New-Item -ItemType Directory -Force $home_ | Out-Null
function Say($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan }

# 1. QEMU
if (-not (Test-Path $qemu)) {
  Say "First time: getting QEMU (about 150 MB). This happens once."
  $list = (Invoke-WebRequest "https://qemu.weilnetz.de/w64/" -UseBasicParsing).Content
  $name = ([regex]::Matches($list, 'qemu-w64-setup-\d{8}\.exe') | ForEach-Object Value | Sort-Object -Unique | Select-Object -Last 1)
  if (-not $name) { throw "Couldn't find the QEMU download. Check the internet connection." }
  $setup = Join-Path $home_ $name
  Invoke-WebRequest "https://qemu.weilnetz.de/w64/$name" -OutFile $setup -UseBasicParsing
  Say "Installing QEMU. If Windows asks 'Do you want to allow this app to make changes', click Yes."
  Start-Process $setup -ArgumentList "/S", "/D=$qemuDir" -Wait
  if (-not (Test-Path $qemu)) { throw "QEMU didn't install. Try again, and click Yes when Windows asks." }
}

# 2. Which OS to run
$file = $env:RUN_OS_FILE
if (-not $file) {
  $file = Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads\DeskOS.zip"
  if (-not (Test-Path $file)) {
    throw "DeskOS.zip isn't in Downloads. Download it first, or drag an OS file onto Run-OS."
  }
}
$file = (Resolve-Path $file).Path
$ext = [IO.Path]::GetExtension($file).ToLower()

# A .zip (like DeskOS.zip) holds a disk image: unpack it once and reuse it after.
if ($ext -eq ".zip") {
  $stamp = (Get-Item $file).LastWriteTimeUtc.Ticks
  $out = Join-Path $home_ ("disk-" + [IO.Path]::GetFileNameWithoutExtension($file) + "-$stamp")
  if (-not (Test-Path $out)) {
    Say "Unpacking $([IO.Path]::GetFileName($file)). This takes a few minutes the first time."
    Get-ChildItem $home_ -Directory -Filter ("disk-" + [IO.Path]::GetFileNameWithoutExtension($file) + "-*") | Remove-Item -Recurse -Force
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($file, $out)
  }
  $file = (Get-ChildItem $out -File | Where-Object { $_.Extension -in ".bin", ".img", ".iso", ".raw" } | Select-Object -First 1).FullName
  if (-not $file) { throw "That zip has no disk image inside." }
  $ext = [IO.Path]::GetExtension($file).ToLower()
}

# 3. Start it, like a computer with a USB stick (or a DVD for .iso files) plugged in.
$share = Join-Path $qemuDir "share"
$code = Join-Path $share "edk2-x86_64-code.fd"
$vars = Join-Path $home_ "efi-vars.fd"
if (-not (Test-Path $vars)) { Copy-Item (Join-Path $share "edk2-i386-vars.fd") $vars }
$memory = [math]::Min(4096, [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB / 2))
$args_ = @(
  "-accel", "whpx,kernel-irqchip=off", "-accel", "tcg",
  "-machine", "q35", "-m", "$memory", "-smp", "2",
  "-drive", "if=pflash,format=raw,readonly=on,file=$code",
  "-drive", "if=pflash,format=raw,file=$vars",
  "-device", "virtio-vga", "-display", "sdl",
  "-device", "qemu-xhci", "-device", "usb-tablet", "-device", "usb-kbd",
  "-nic", "user,model=virtio-net-pci",
  "-name", "Run OS: $([IO.Path]::GetFileName($file))"
)
if ($ext -eq ".iso") { $args_ += @("-cdrom", $file) }
else { $args_ += @("-drive", "file=$file,format=raw,if=none,id=stick", "-device", "usb-storage,drive=stick") }

Say "Starting $([IO.Path]::GetFileName($file)) in a window."
Write-Host "Click inside the window to use it. Ctrl + Alt + G lets go of the mouse. Close the window to switch it off."
Write-Host "Your changes are kept for next time."
& $qemu @args_

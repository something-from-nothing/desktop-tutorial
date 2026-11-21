//go:build windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

type dataBlob struct {
	size uint32
	data *byte
}

var (
	crypt32  = syscall.NewLazyDLL("crypt32.dll")
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")
)

// protect and unprotect use Windows DPAPI: only this Windows user on this PC can decrypt.
func dpapi(proc string, b []byte) ([]byte, error) {
	if len(b) == 0 {
		return nil, nil
	}
	in := dataBlob{size: uint32(len(b)), data: &b[0]}
	var out dataBlob
	const uiForbidden = 0x1
	r, _, err := crypt32.NewProc(proc).Call(uintptr(unsafe.Pointer(&in)), 0, 0, 0, 0, uiForbidden, uintptr(unsafe.Pointer(&out)))
	if r == 0 {
		return nil, err
	}
	defer kernel32.NewProc("LocalFree").Call(uintptr(unsafe.Pointer(out.data)))
	return append([]byte(nil), unsafe.Slice(out.data, out.size)...), nil
}

func protect(b []byte) ([]byte, error)   { return dpapi("CryptProtectData", b) }
func unprotect(b []byte) ([]byte, error) { return dpapi("CryptUnprotectData", b) }

func screenSize() (int, int) {
	gsm := user32.NewProc("GetSystemMetrics")
	// SM_CXMAXIMIZED / SM_CYMAXIMIZED: the usable area above the taskbar.
	w, _, _ := gsm.Call(61)
	h, _, _ := gsm.Call(62)
	return int(w), int(h)
}

func fatal(msg string) {
	mb := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString("Boot.dev Dashboard")
	m, _ := syscall.UTF16PtrFromString(msg)
	mb.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), 0x10)
	os.Exit(1)
}

// ensureShortcut puts "Boot.dev Dashboard" on the desktop and in the Start menu.
// It only runs again if the EXE moves, so a shortcut you delete stays deleted.
func ensureShortcut() {
	flag := filepath.Join(appDir(), "shortcuts.txt")
	exe, err := os.Executable()
	if err != nil {
		return
	}
	if b, err := os.ReadFile(flag); err == nil && strings.TrimSpace(string(b)) == exe {
		return
	}
	q := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }
	one := func(folder string) string {
		return "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('" + folder + "')) 'Boot.dev Dashboard.lnk'));" +
			"$s.TargetPath=" + q(exe) + ";" +
			"$s.WorkingDirectory=" + q(filepath.Dir(exe)) + ";" +
			"$s.IconLocation=" + q(exe+",0") + ";" +
			"$s.Description='Boot.dev with a live dashboard and AI explanations';" +
			"$s.Save();"
	}
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", one("Desktop")+one("Programs"))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	if cmd.Run() == nil {
		_ = os.WriteFile(flag, []byte(exe), 0o600)
	}
}

// killOldCopies ends other copies of this app and the dashboard window they opened.
func killOldCopies(dir string) {
	pid := os.Getpid()
	ps := "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'BootDevDashboard*.exe' -and $_.ProcessId -ne " + itoa(pid) + ") -or ($_.CommandLine -like '*" + strings.ReplaceAll(filepath.Join(dir, "dashboard-window"), "'", "''") + "*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	_ = cmd.Run()
}

func showFolder(dir string) { _ = exec.Command("explorer.exe", dir).Start() }

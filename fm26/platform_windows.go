//go:build windows

package main

import (
	"errors"
	"image"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"unsafe"
)

var (
	crypt32  = syscall.NewLazyDLL("crypt32.dll")
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")
	gdi32    = syscall.NewLazyDLL("gdi32.dll")
	advapi32 = syscall.NewLazyDLL("advapi32.dll")
	dwmapi   = syscall.NewLazyDLL("dwmapi.dll")

	pEnumWindows                = user32.NewProc("EnumWindows")
	pIsWindowVisible            = user32.NewProc("IsWindowVisible")
	pIsIconic                   = user32.NewProc("IsIconic")
	pGetWindowThreadProcessId   = user32.NewProc("GetWindowThreadProcessId")
	pGetWindowTextW             = user32.NewProc("GetWindowTextW")
	pGetClientRect              = user32.NewProc("GetClientRect")
	pClientToScreen             = user32.NewProc("ClientToScreen")
	pGetForegroundWindow        = user32.NewProc("GetForegroundWindow")
	pGetDC                      = user32.NewProc("GetDC")
	pReleaseDC                  = user32.NewProc("ReleaseDC")
	pPrintWindow                = user32.NewProc("PrintWindow")
	pRegisterHotKey             = user32.NewProc("RegisterHotKey")
	pGetMessageW                = user32.NewProc("GetMessageW")
	pGetSystemMetrics           = user32.NewProc("GetSystemMetrics")
	pGetDpiForSystem            = user32.NewProc("GetDpiForSystem")
	pCreateCompatibleDC         = gdi32.NewProc("CreateCompatibleDC")
	pCreateCompatibleBitmap     = gdi32.NewProc("CreateCompatibleBitmap")
	pSelectObject               = gdi32.NewProc("SelectObject")
	pBitBlt                     = gdi32.NewProc("BitBlt")
	pGetDIBits                  = gdi32.NewProc("GetDIBits")
	pDeleteObject               = gdi32.NewProc("DeleteObject")
	pDeleteDC                   = gdi32.NewProc("DeleteDC")
	pOpenProcess                = kernel32.NewProc("OpenProcess")
	pQueryFullProcessImageNameW = kernel32.NewProc("QueryFullProcessImageNameW")
	pCloseHandle                = kernel32.NewProc("CloseHandle")
	pRegGetValueW               = advapi32.NewProc("RegGetValueW")
	_                           = dwmapi
)

func init() {
	// Per monitor DPI awareness, so the capture is sharp and lines up on scaled screens.
	if p := user32.NewProc("SetProcessDpiAwarenessContext"); p.Find() == nil {
		p.Call(^uintptr(3)) // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4)
	} else if p := user32.NewProc("SetProcessDPIAware"); p.Find() == nil {
		p.Call()
	}
}

func platformName() string { return "windows" }

type dataBlob struct {
	size uint32
	data *byte
}

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

// screenSize is the usable area above the taskbar, in the units browser windows use.
func screenSize() (int, int) {
	w, _, _ := pGetSystemMetrics.Call(61) // SM_CXMAXIMIZED
	h, _, _ := pGetSystemMetrics.Call(62) // SM_CYMAXIMIZED
	dpi := uintptr(96)
	if pGetDpiForSystem.Find() == nil {
		if d, _, _ := pGetDpiForSystem.Call(); d > 0 {
			dpi = d
		}
	}
	return int(w * 96 / dpi), int(h * 96 / dpi)
}

func fatal(msg string) {
	mb := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString("FM26 Dashboard")
	m, _ := syscall.UTF16PtrFromString(msg)
	mb.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), 0x10)
	os.Exit(1)
}

func hiddenPS(script string) *exec.Cmd {
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	return cmd
}

// ensureShortcut puts "FM26 Dashboard" on the desktop and in the Start menu.
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
		return "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('" + folder + "')) 'FM26 Dashboard.lnk'));" +
			"$s.TargetPath=" + q(exe) + ";" +
			"$s.WorkingDirectory=" + q(filepath.Dir(exe)) + ";" +
			"$s.IconLocation=" + q(exe+",0") + ";" +
			"$s.Description='Live dashboard for the Football Manager 26 Demo';" +
			"$s.Save();"
	}
	if hiddenPS(one("Desktop")+one("Programs")).Run() == nil {
		_ = os.WriteFile(flag, []byte(exe), 0o600)
	}
}

// killOldCopies ends other copies of this app and the dashboard window they opened.
func killOldCopies(dir string) {
	pid := os.Getpid()
	ps := "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'FM26Dashboard*.exe' -and $_.ProcessId -ne " + itoa(pid) + ") -or ($_.CommandLine -like '*" + strings.ReplaceAll(filepath.Join(dir, "dashboard-window"), "'", "''") + "*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
	_ = hiddenPS(ps).Run()
}

func showFolder(dir string) { _ = exec.Command("explorer.exe", dir).Start() }

// ---- Steam's registry values (read only) ----

const hkcu, hklm = 0x80000001, 0x80000002

func regDWORD(root uintptr, key, name string) (uint32, bool) {
	k, _ := syscall.UTF16PtrFromString(key)
	n, _ := syscall.UTF16PtrFromString(name)
	var v uint32
	size := uint32(4)
	r, _, _ := pRegGetValueW.Call(root, uintptr(unsafe.Pointer(k)), uintptr(unsafe.Pointer(n)), 0x10, 0, uintptr(unsafe.Pointer(&v)), uintptr(unsafe.Pointer(&size)))
	return v, r == 0
}

func regString(root uintptr, key, name string) string {
	k, _ := syscall.UTF16PtrFromString(key)
	n, _ := syscall.UTF16PtrFromString(name)
	buf := make([]uint16, 1024)
	size := uint32(len(buf) * 2)
	r, _, _ := pRegGetValueW.Call(root, uintptr(unsafe.Pointer(k)), uintptr(unsafe.Pointer(n)), 0x2|0x4, 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)))
	if r != 0 {
		return ""
	}
	return syscall.UTF16ToString(buf)
}

func steamPath() string {
	if p := os.Getenv("FM26_STEAM_PATH"); p != "" {
		return p
	}
	if p := regString(hkcu, `Software\Valve\Steam`, "SteamPath"); p != "" {
		return filepath.Clean(strings.ReplaceAll(p, "/", `\`))
	}
	if p := regString(hklm, `SOFTWARE\WOW6432Node\Valve\Steam`, "InstallPath"); p != "" {
		return p
	}
	return ""
}

func activeSteamUser() uint32 {
	v, _ := regDWORD(hkcu, `Software\Valve\Steam\ActiveProcess`, "ActiveUser")
	return v
}

// demoRunningRegistry reads RunningAppID (and the demo's own Running flag). known is false if
// Steam's registry key isn't there at all.
func demoRunningRegistry() (running, known bool) {
	id, ok := regDWORD(hkcu, `Software\Valve\Steam`, "RunningAppID")
	if !ok {
		if regString(hkcu, `Software\Valve\Steam`, "SteamPath") == "" {
			return false, false
		}
	}
	if id == demoAppID {
		return true, true
	}
	if r, ok := regDWORD(hkcu, `Software\Valve\Steam\Apps\`+demoAppStr, "Running"); ok && r == 1 {
		return true, true
	}
	return false, true
}

// ---- Finding and capturing the Football Manager window (and nothing else) ----

type rect struct{ L, T, R, B int32 }
type point struct{ X, Y int32 }

func processPath(pid uint32) string {
	h, _, _ := pOpenProcess.Call(0x1000, 0, uintptr(pid)) // PROCESS_QUERY_LIMITED_INFORMATION
	if h == 0 {
		return ""
	}
	defer pCloseHandle.Call(h)
	buf := make([]uint16, 1024)
	size := uint32(len(buf))
	if r, _, _ := pQueryFullProcessImageNameW.Call(h, 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size))); r == 0 {
		return ""
	}
	return syscall.UTF16ToString(buf[:size])
}

func windowTitle(hwnd uintptr) string {
	buf := make([]uint16, 256)
	pGetWindowTextW.Call(hwnd, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
	return syscall.UTF16ToString(buf)
}

func windowPID(hwnd uintptr) uint32 {
	var pid uint32
	pGetWindowThreadProcessId.Call(hwnd, uintptr(unsafe.Pointer(&pid)))
	return pid
}

// isGameWindow: the window belongs to a program inside the demo's install folder. If Steam's
// manifest can't be read, fall back to the window title.
func isGameWindow(hwnd uintptr, dir string) bool {
	if dir != "" {
		p := strings.ToLower(processPath(windowPID(hwnd)))
		return p != "" && strings.HasPrefix(p, strings.ToLower(dir)+`\`)
	}
	return strings.Contains(windowTitle(hwnd), "Football Manager")
}

var (
	enumMu   sync.Mutex
	enumBest uintptr
	enumArea int64
	enumDir  string
	enumCB   = syscall.NewCallback(enumProc)
)

func enumProc(hwnd, _ uintptr) uintptr {
	if v, _, _ := pIsWindowVisible.Call(hwnd); v == 0 {
		return 1
	}
	if !isGameWindow(hwnd, enumDir) {
		return 1
	}
	var r rect
	pGetClientRect.Call(hwnd, uintptr(unsafe.Pointer(&r)))
	area := int64(r.R-r.L) * int64(r.B-r.T)
	if ic, _, _ := pIsIconic.Call(hwnd); ic != 0 {
		area = 1 // minimised: remember it, but prefer a real window
	}
	if area > enumArea {
		enumBest, enumArea = hwnd, area
	}
	return 1
}

func findGameWindow() uintptr {
	enumMu.Lock()
	defer enumMu.Unlock()
	enumBest, enumArea, enumDir = 0, 0, gameDir(steamPath())
	pEnumWindows.Call(enumCB, 0)
	return enumBest
}

func gameIsForeground() bool {
	fg, _, _ := pGetForegroundWindow.Call()
	return fg != 0 && isGameWindow(fg, gameDir(steamPath()))
}

// captureGame grabs the client area of the Football Manager window only. It asks Windows to
// draw that one window (PrintWindow), so other windows on top don't show. If that comes back
// blank (some full screen games), it copies the same rectangle from the screen, but only while
// Football Manager is the active window, so the picture is still only the game.
func captureGame() (image.Image, string, error) {
	hwnd := findGameWindow()
	if hwnd == 0 {
		return nil, "", errors.New("Football Manager isn't open (no Football Manager 26 Demo window found).")
	}
	if ic, _, _ := pIsIconic.Call(hwnd); ic != 0 {
		return nil, "", errors.New("Football Manager is minimised. Bring it back up and try again.")
	}
	title := windowTitle(hwnd)
	var r rect
	pGetClientRect.Call(hwnd, uintptr(unsafe.Pointer(&r)))
	w, h := int(r.R-r.L), int(r.B-r.T)
	if w < 50 || h < 50 {
		return nil, title, errors.New("The Football Manager window is too small to read.")
	}
	screen, _, _ := pGetDC.Call(0)
	defer pReleaseDC.Call(0, screen)
	mem, _, _ := pCreateCompatibleDC.Call(screen)
	defer pDeleteDC.Call(mem)
	bmp, _, _ := pCreateCompatibleBitmap.Call(screen, uintptr(w), uintptr(h))
	if bmp == 0 {
		return nil, title, errors.New("Windows couldn't make room for the capture.")
	}
	defer pDeleteObject.Call(bmp)
	old, _, _ := pSelectObject.Call(mem, bmp)
	const pwClientOnly, pwRenderFull = 1, 2
	pPrintWindow.Call(hwnd, mem, pwClientOnly|pwRenderFull)
	img := readBitmap(mem, bmp, w, h)
	if isBlank(fingerprintOf(img)) {
		fg, _, _ := pGetForegroundWindow.Call()
		if fg == hwnd {
			var p point
			pClientToScreen.Call(hwnd, uintptr(unsafe.Pointer(&p)))
			pBitBlt.Call(mem, 0, 0, uintptr(w), uintptr(h), screen, uintptr(p.X), uintptr(p.Y), 0x00CC0020)
			img = readBitmap(mem, bmp, w, h)
		}
	}
	pSelectObject.Call(mem, old)
	return img, title, nil
}

type bitmapInfoHeader struct {
	Size          uint32
	Width         int32
	Height        int32
	Planes        uint16
	BitCount      uint16
	Compression   uint32
	SizeImage     uint32
	XPelsPerMeter int32
	YPelsPerMeter int32
	ClrUsed       uint32
	ClrImportant  uint32
}

func readBitmap(dc, bmp uintptr, w, h int) *image.RGBA {
	bi := bitmapInfoHeader{Size: 40, Width: int32(w), Height: -int32(h), Planes: 1, BitCount: 32}
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	// GetDIBits needs the bitmap not selected into a DC on some drivers; it's fine with a memory DC.
	pGetDIBits.Call(dc, bmp, 0, uintptr(h), uintptr(unsafe.Pointer(&img.Pix[0])), uintptr(unsafe.Pointer(&bi)), 0)
	for i := 0; i < len(img.Pix); i += 4 {
		img.Pix[i], img.Pix[i+2] = img.Pix[i+2], img.Pix[i] // BGRA to RGBA
		img.Pix[i+3] = 255
	}
	return img
}

// ---- Ctrl+Shift+E from anywhere ----

var (
	hkMu     sync.Mutex
	hkStatus = "starting"
)

func hotkeyStatus() string {
	hkMu.Lock()
	defer hkMu.Unlock()
	return hkStatus
}

type winMsg struct {
	Hwnd    uintptr
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      point
	Private uint32
}

func startHotkey(fn func()) {
	runtime.LockOSThread()
	const modCtrl, modShift, modNoRepeat = 0x2, 0x4, 0x4000
	if r, _, _ := pRegisterHotKey.Call(0, 1, modCtrl|modShift|modNoRepeat, 'E'); r == 0 {
		hkMu.Lock()
		hkStatus = "Ctrl+Shift+E is already used by another program, so the hotkey is off. Use the Explain button."
		hkMu.Unlock()
		return
	}
	hkMu.Lock()
	hkStatus = "ok"
	hkMu.Unlock()
	var m winMsg
	for {
		r, _, _ := pGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			return
		}
		if m.Message == 0x0312 { // WM_HOTKEY
			fn()
		}
	}
}

//go:build windows

package main

import (
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// FC26 Dashboard installs itself, like a normal Windows app: the first time you open the EXE
// from anywhere else (Downloads, say), it copies itself to
// %LOCALAPPDATA%\Programs\FC26 Dashboard, puts shortcuts on the desktop and in the Start menu,
// adds itself to Settings > Apps > Installed apps (so it can be uninstalled from there), and
// starts the installed copy. No admin rights are needed. Your settings stay in
// %APPDATA%\FC26Dashboard, so reinstalling or updating keeps them.

const uninstallKey = `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\FC26Dashboard`

func installDir() string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		base = filepath.Join(os.Getenv("USERPROFILE"), "AppData", "Local")
	}
	return filepath.Join(base, "Programs", "FC26 Dashboard")
}

func installedExe() string { return filepath.Join(installDir(), "FC26Dashboard.exe") }

func hidden(name string, args ...string) *exec.Cmd {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	return cmd
}

// maybeInstall installs (or updates) the app if this EXE isn't the installed copy. It returns
// true when it has started the installed copy and this one should exit.
func maybeInstall() bool {
	for _, a := range os.Args[1:] {
		if a == "--uninstall" {
			uninstall()
			os.Exit(0)
		}
	}
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	exe, _ = filepath.EvalSymlinks(exe)
	target := installedExe()
	if strings.EqualFold(filepath.Clean(exe), filepath.Clean(target)) || os.Getenv("FC26DASH_NO_INSTALL") != "" {
		return false
	}
	// Close a running copy so its EXE can be replaced.
	killOldCopies(appDir())
	if err := os.MkdirAll(installDir(), 0o755); err != nil {
		return false
	}
	if err := copyFile(exe, target); err != nil {
		return false // can't install: just run from here
	}
	registerUninstall(target)
	removeOldShortcuts()
	_ = os.Remove(filepath.Join(appDir(), "shortcuts.txt")) // make the installed copy write fresh shortcuts
	_ = os.Remove(filepath.Join(appDir(), "instance.txt"))
	if err := exec.Command(target).Start(); err != nil {
		return false
	}
	return true
}

func copyFile(src, dst string) error {
	var last error
	for i := 0; i < 20; i++ { // the old copy may take a moment to let go of its EXE
		last = func() error {
			in, err := os.Open(src)
			if err != nil {
				return err
			}
			defer in.Close()
			tmp := dst + ".new"
			out, err := os.Create(tmp)
			if err != nil {
				return err
			}
			if _, err := io.Copy(out, in); err != nil {
				out.Close()
				return err
			}
			if err := out.Close(); err != nil {
				return err
			}
			_ = os.Remove(dst)
			return os.Rename(tmp, dst)
		}()
		if last == nil {
			return nil
		}
		time.Sleep(250 * time.Millisecond)
	}
	return last
}

func registerUninstall(target string) {
	size := int64(0)
	if fi, err := os.Stat(target); err == nil {
		size = fi.Size() / 1024
	}
	vals := [][3]string{
		{"DisplayName", "REG_SZ", "FC26 Dashboard"},
		{"DisplayIcon", "REG_SZ", target + ",0"},
		{"DisplayVersion", "REG_SZ", version},
		{"Publisher", "REG_SZ", "Pete Williams"},
		{"InstallLocation", "REG_SZ", installDir()},
		{"UninstallString", "REG_SZ", `"` + target + `" --uninstall`},
		{"NoModify", "REG_DWORD", "1"},
		{"NoRepair", "REG_DWORD", "1"},
		{"EstimatedSize", "REG_DWORD", strconv.FormatInt(size, 10)},
	}
	for _, v := range vals {
		_ = hidden("reg", "add", uninstallKey, "/v", v[0], "/t", v[1], "/d", v[2], "/f").Run()
	}
}

// removeOldShortcuts tidies up after the earlier FM26 Dashboard, which this app replaces.
func removeOldShortcuts() {
	for _, folder := range []string{"Desktop", "Programs"} {
		out, err := hiddenPS("[Environment]::GetFolderPath('" + folder + "')").Output()
		if err != nil {
			continue
		}
		dir := strings.TrimSpace(string(out))
		if dir == "" {
			continue
		}
		_ = os.Remove(filepath.Join(dir, "FM26 Dashboard.lnk"))
	}
}

// uninstall removes the shortcuts, the Installed apps entry and the program folder. Your
// settings folder (%APPDATA%\FC26Dashboard) is left in place.
func uninstall() {
	killOldCopies(appDir())
	for _, folder := range []string{"Desktop", "Programs"} {
		if out, err := hiddenPS("[Environment]::GetFolderPath('" + folder + "')").Output(); err == nil {
			_ = os.Remove(filepath.Join(strings.TrimSpace(string(out)), "FC26 Dashboard.lnk"))
		}
	}
	_ = hidden("reg", "delete", uninstallKey, "/f").Run()
	_ = os.Remove(filepath.Join(appDir(), "shortcuts.txt"))
	// The running EXE can't delete itself, so a hidden command removes the folder a moment later.
	_ = hidden("cmd", "/c", "ping 127.0.0.1 -n 3 >nul & rmdir /s /q \""+installDir()+"\"").Start()
}

//go:build !windows

package main

import (
	"errors"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"os"
)

// Development and test builds only (Linux/macOS): no DPAPI, so secrets are stored unencrypted,
// and Steam and the game window come from test files named in environment variables.

func platformName() string               { return "other" }
func protect(b []byte) ([]byte, error)   { return append([]byte(nil), b...), nil }
func unprotect(b []byte) ([]byte, error) { return append([]byte(nil), b...), nil }
func screenSize() (int, int)             { return 1536, 864 }
func ensureShortcut()                    {}
func fatal(msg string)                   { fmt.Fprintln(os.Stderr, msg); os.Exit(1) }
func killOldCopies(dir string)           {}
func showFolder(dir string)              { fmt.Println("folder:", dir) }
func steamPath() string                  { return os.Getenv("FM26_STEAM_PATH") }
func activeSteamUser() uint32            { return 0 }
func hotkeyStatus() string               { return "Windows only" }
func startHotkey(fn func())              {}

func demoRunningRegistry() (bool, bool) {
	switch os.Getenv("FM26_FAKE_RUNNING") {
	case "1":
		return true, true
	case "0":
		return false, true
	}
	return false, false
}

func gameIsForeground() bool { return os.Getenv("FM26_FAKE_CAPTURE") != "" }

// captureGame reads the picture named in FM26_FAKE_CAPTURE (a sample screenshot).
func captureGame() (image.Image, string, error) {
	p := os.Getenv("FM26_FAKE_CAPTURE")
	if p == "" {
		return nil, "", errors.New("Football Manager isn't open (screen capture works on Windows only).")
	}
	f, err := os.Open(p)
	if err != nil {
		return nil, "", err
	}
	defer f.Close()
	img, _, err := image.Decode(f)
	return img, "Football Manager 26 Demo (test picture)", err
}

// Test builds can point the app at fake servers. The Windows build has no such switch.
func init() {
	if v := os.Getenv("FM26_STEAM_API"); v != "" {
		steamAPI = v
	}
	if v := os.Getenv("FM26_CLAUDE_URL"); v != "" {
		claudeURL = v
	}
	if v := os.Getenv("FM26_BRIDGE_URL"); v != "" {
		bridgeBase = v
	}
}

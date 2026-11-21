//go:build !windows

package main

import (
	"fmt"
	"os"
)

// Development builds only (Linux/macOS): no DPAPI, so secrets are stored unencrypted.
func protect(b []byte) ([]byte, error)   { return append([]byte(nil), b...), nil }
func unprotect(b []byte) ([]byte, error) { return append([]byte(nil), b...), nil }
func screenSize() (int, int)             { return 1536, 864 }
func ensureShortcut()                    {}
func fatal(msg string)                   { fmt.Fprintln(os.Stderr, msg); os.Exit(1) }

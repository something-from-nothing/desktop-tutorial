//go:build windows

package main

import (
	"encoding/base64"
	"errors"
	"os"
	"strings"
	"sync"
	"time"
	"unicode/utf16"
)

// Windows Firewall and the phone link. Windows blocks phones from reaching an app unless a rule
// allows it, and on a network Windows calls "Public" the usual first time prompt rule doesn't
// apply. This checks for that, and the Fix button adds one rule (after Windows asks you to
// approve it): inbound, this EXE only, devices on your own network only (LocalSubnet).

const fwRuleName = "FC26 Dashboard phone link"

var (
	fwMu   sync.Mutex
	fwLast map[string]any
	fwAt   time.Time
	fwBusy bool
)

func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

func firewallInfo() map[string]any {
	fwMu.Lock()
	if fwLast != nil && time.Since(fwAt) < time.Minute || fwBusy {
		out := fwLast
		fwMu.Unlock()
		if out == nil {
			return map[string]any{"supported": true, "checking": true}
		}
		return out
	}
	fwBusy = true
	fwMu.Unlock()
	go func() {
		exe, _ := os.Executable()
		script := "$c=(Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object {$_.IPv4Connectivity -ne 'Disconnected'} | ForEach-Object { $_.Name + '=' + $_.NetworkCategory }) -join ';';" +
			"$r=@(Get-NetFirewallRule -DisplayName " + psQuote(fwRuleName) + " -ErrorAction SilentlyContinue | Where-Object {$_.Enabled -eq 'True'}).Count;" +
			"$b=@(Get-NetFirewallApplicationFilter -Program " + psQuote(exe) + " -ErrorAction SilentlyContinue | Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {$_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True'}).Count;" +
			"Write-Output ($c + '|' + $r + '|' + $b)"
		raw, err := hiddenPS(script).Output()
		info := map[string]any{"supported": true, "checked": time.Now().Unix()}
		if err == nil {
			parts := strings.Split(strings.TrimSpace(string(raw)), "|")
			if len(parts) == 3 {
				info["networks"] = parts[0]
				info["public"] = strings.Contains(parts[0], "=Public")
				info["rule"] = parts[1] != "0"
				info["blocked"] = parts[2] != "0"
			}
		} else {
			info["error"] = "couldn't check the firewall"
		}
		fwMu.Lock()
		fwLast, fwAt, fwBusy = info, time.Now(), false
		fwMu.Unlock()
		bumpSeq()
	}()
	fwMu.Lock()
	defer fwMu.Unlock()
	if fwLast == nil {
		return map[string]any{"supported": true, "checking": true}
	}
	return fwLast
}

// fixFirewall removes any rules blocking this EXE and adds the allow rule. Windows shows its
// own approval prompt (UAC) first; nothing changes unless you click Yes.
func fixFirewall() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	inner := "Get-NetFirewallApplicationFilter -Program " + psQuote(exe) + " -ErrorAction SilentlyContinue | Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {$_.Direction -eq 'Inbound' -and $_.Action -eq 'Block'} | Remove-NetFirewallRule -ErrorAction SilentlyContinue;" +
		"Remove-NetFirewallRule -DisplayName " + psQuote(fwRuleName) + " -ErrorAction SilentlyContinue;" +
		"New-NetFirewallRule -DisplayName " + psQuote(fwRuleName) + " -Direction Inbound -Program " + psQuote(exe) + " -Action Allow -Profile Any -RemoteAddress LocalSubnet | Out-Null"
	u := utf16.Encode([]rune(inner))
	b := make([]byte, len(u)*2)
	for i, c := range u {
		b[2*i], b[2*i+1] = byte(c), byte(c>>8)
	}
	enc := base64.StdEncoding.EncodeToString(b)
	outer := "try { Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList '-NoProfile -NonInteractive -EncodedCommand " + enc + "'; exit 0 } catch { exit 5 }"
	if err := hiddenPS(outer).Run(); err != nil {
		return errors.New("Windows didn't apply the firewall rule (the approval was probably declined)")
	}
	fwMu.Lock()
	fwLast, fwAt = nil, time.Time{}
	fwMu.Unlock()
	firewallInfo()
	return nil
}

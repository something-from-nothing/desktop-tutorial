package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// The phone link. Off until you pair a phone. It only answers devices on your home network
// (private addresses such as 192.168.x.x), it is encrypted (TLS with this PC's own certificate,
// which the phone remembers when it pairs), and every request needs the phone's own token.
// Nothing is opened to the internet: no port forwarding, no UPnP, no cloud relay.

const (
	lanPort       = 47830
	discoveryPort = 47831
	discoveryAsk  = "FM26DASH?1"
)

var (
	lanMu       sync.Mutex
	lanLn       net.Listener
	lanAddr     string
	lanFP       string
	lanCert     tls.Certificate
	pairCode    string
	pairExpires time.Time
	pairTries   int
	udpConn     *net.UDPConn
)

// privateIP is true for home network addresses only.
func privateIP(ip net.IP) bool {
	if ip == nil {
		return false
	}
	return ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLoopback()
}

func remoteIP(addr string) net.IP {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		host = addr
	}
	return net.ParseIP(host)
}

// lanCertificate loads this PC's certificate, or makes one the first time.
func lanCertificate() (tls.Certificate, string, error) {
	path := filepath.Join(appDir(), "phone-link-cert.json")
	var saved struct {
		Cert   string `json:"cert"`
		KeyEnc string `json:"keyEnc"`
	}
	if b, err := os.ReadFile(path); err == nil && json.Unmarshal(b, &saved) == nil {
		if keyPEM := unseal(saved.KeyEnc); keyPEM != "" {
			if c, err := tls.X509KeyPair([]byte(saved.Cert), []byte(keyPEM)); err == nil {
				return c, certFP(c), nil
			}
		}
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 62))
	tpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: "FM26 Dashboard on " + hostName()},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().AddDate(20, 0, 0),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	kd, _ := x509.MarshalECPrivateKey(key)
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kd})
	enc, err := seal(string(keyPEM))
	if err != nil {
		return tls.Certificate{}, "", err
	}
	saved.Cert, saved.KeyEnc = string(certPEM), enc
	b, _ := json.Marshal(saved)
	_ = os.WriteFile(path, b, 0o600)
	c, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	return c, certFP(c), nil
}

func certFP(c tls.Certificate) string {
	if len(c.Certificate) == 0 {
		return ""
	}
	h := sha256.Sum256(c.Certificate[0])
	return hex.EncodeToString(h[:])
}

func hostName() string {
	h, _ := os.Hostname()
	if h == "" {
		h = "this PC"
	}
	return h
}

func hashToken(t string) string {
	h := sha256.Sum256([]byte(t))
	return hex.EncodeToString(h[:])
}

// startLAN opens the phone link if it's on. Safe to call more than once.
func startLAN(handler http.Handler) error {
	lanMu.Lock()
	defer lanMu.Unlock()
	if lanLn != nil {
		return nil
	}
	c, fp, err := lanCertificate()
	if err != nil {
		return err
	}
	lanCert, lanFP = c, fp
	var ln net.Listener
	for p := lanPort; p < lanPort+10; p++ {
		ln, err = net.Listen("tcp", fmt.Sprintf(":%d", p))
		if err == nil {
			break
		}
	}
	if err != nil {
		return errors.New("couldn't open the phone link: " + err.Error())
	}
	lanAddr = ln.Addr().String()
	tl := tls.NewListener(privateOnly{ln}, &tls.Config{Certificates: []tls.Certificate{c}, MinVersion: tls.VersionTLS12})
	lanLn = tl
	srv := &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	go func() { _ = srv.Serve(tl) }()
	go discoveryLoop()
	return nil
}

func lanPortNum() int {
	lanMu.Lock()
	defer lanMu.Unlock()
	if a, ok := lanLnAddr(); ok {
		return a
	}
	return 0
}

func lanLnAddr() (int, bool) {
	if lanLn == nil {
		return 0, false
	}
	_, p, err := net.SplitHostPort(lanAddr)
	if err != nil {
		return 0, false
	}
	var n int
	fmt.Sscan(p, &n)
	return n, true
}

// privateOnly drops connections from anything that isn't on the home network.
type privateOnly struct{ net.Listener }

func (l privateOnly) Accept() (net.Conn, error) {
	for {
		c, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		if privateIP(remoteIP(c.RemoteAddr().String())) {
			return c, nil
		}
		c.Close()
	}
}

// discoveryLoop answers "where are you?" from phones on the home network, so the phone finds
// this PC even after its address changes.
func discoveryLoop() {
	pc, err := net.ListenUDP("udp4", &net.UDPAddr{Port: discoveryPort})
	if err != nil {
		return
	}
	lanMu.Lock()
	udpConn = pc
	lanMu.Unlock()
	buf := make([]byte, 512)
	for {
		n, from, err := pc.ReadFromUDP(buf)
		if err != nil {
			return
		}
		if !privateIP(from.IP) || strings.TrimSpace(string(buf[:n])) != discoveryAsk {
			continue
		}
		reply, _ := json.Marshal(map[string]any{"app": "fm26dash", "name": hostName(), "port": lanPortNum(), "fp": lanFP, "pairing": pairingActive()})
		_, _ = pc.WriteToUDP(reply, from)
	}
}

// ---- pairing ----

func newPairCode() (string, time.Time) {
	n, _ := rand.Int(rand.Reader, big.NewInt(1000000))
	lanMu.Lock()
	defer lanMu.Unlock()
	pairCode = fmt.Sprintf("%06d", n.Int64())
	pairExpires = time.Now().Add(5 * time.Minute)
	pairTries = 0
	return pairCode, pairExpires
}

func pairingActive() bool {
	lanMu.Lock()
	defer lanMu.Unlock()
	return pairCode != "" && time.Now().Before(pairExpires)
}

func pairingInfo() map[string]any {
	lanMu.Lock()
	defer lanMu.Unlock()
	out := map[string]any{"on": lanLn != nil, "fp": lanFP, "addresses": localAddrs()}
	if p, ok := lanLnAddr(); ok {
		out["port"] = p
	}
	if pairCode != "" && time.Now().Before(pairExpires) {
		out["code"] = pairCode
		out["expires"] = pairExpires.Unix()
	}
	return out
}

func localAddrs() []string {
	var out []string
	ifs, _ := net.Interfaces()
	for _, i := range ifs {
		if i.Flags&net.FlagUp == 0 || i.Flags&net.FlagLoopback != 0 {
			continue
		}
		as, _ := i.Addrs()
		for _, a := range as {
			if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil && n.IP.IsPrivate() {
				out = append(out, n.IP.String())
			}
		}
	}
	return out
}

// checkPair checks a code from a phone. Five wrong tries and the code is thrown away.
func checkPair(code string) bool {
	lanMu.Lock()
	defer lanMu.Unlock()
	if pairCode == "" || time.Now().After(pairExpires) {
		return false
	}
	if subtle.ConstantTimeCompare([]byte(strings.TrimSpace(code)), []byte(pairCode)) == 1 {
		pairCode = ""
		return true
	}
	pairTries++
	if pairTries >= 5 {
		pairCode = ""
	}
	return false
}

func handlePair(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Code   string `json:"code"`
		Device string `json:"device"`
	}
	_ = json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&in)
	if !checkPair(in.Code) {
		w.WriteHeader(http.StatusForbidden)
		writeJSON(w, map[string]any{"ok": false, "error": "That code didn't match, or it has expired. Make a new code on the PC."})
		return
	}
	tb := make([]byte, 32)
	_, _ = rand.Read(tb)
	tok := hex.EncodeToString(tb)
	name := strings.TrimSpace(in.Device)
	if name == "" || len(name) > 60 {
		name = "Phone"
	}
	err := updateSettings(func(s *settings) error {
		s.Devices = append(s.Devices, pairedDevice{Name: name, TokenHash: hashToken(tok), Added: time.Now().Unix(), LastSeen: time.Now().Unix()})
		if len(s.Devices) > 5 {
			s.Devices = s.Devices[len(s.Devices)-5:]
		}
		s.PhoneLink = "on"
		return nil
	})
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	s := snapshotSettings()
	// The phone gets the Steam ID and Web API key so it can show Steam stats when this PC is
	// off. They travel inside the encrypted link and the phone stores them in its keystore.
	writeJSON(w, map[string]any{"ok": true, "token": tok, "pc": hostName(), "fp": lanFP,
		"steamId": s.SteamID, "steamName": s.SteamName, "steamKey": unseal(s.SteamKeyEnc)})
	bumpSeq()
}

// phoneAuthed checks the phone's token and notes when it was last seen.
func phoneAuthed(r *http.Request) bool {
	if !privateIP(remoteIP(r.RemoteAddr)) {
		return false
	}
	tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if tok == "" {
		return false
	}
	h := hashToken(tok)
	s := snapshotSettings()
	for i, d := range s.Devices {
		if subtle.ConstantTimeCompare([]byte(d.TokenHash), []byte(h)) == 1 {
			if time.Now().Unix()-d.LastSeen > 60 {
				idx := i
				go updateSettings(func(n *settings) error {
					if idx < len(n.Devices) {
						n.Devices[idx].LastSeen = time.Now().Unix()
					}
					return nil
				})
			}
			return true
		}
	}
	return false
}

func forgetPhones() {
	_ = updateSettings(func(s *settings) error {
		s.Devices = nil
		s.PhoneLink = "off"
		return nil
	})
	lanMu.Lock()
	if lanLn != nil {
		lanLn.Close()
		lanLn = nil
	}
	if udpConn != nil {
		udpConn.Close()
		udpConn = nil
	}
	pairCode = ""
	lanMu.Unlock()
}

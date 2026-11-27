// Brioche's private stdin/stdout bridge. No HTTP management port or system VPN.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/tsnet"
)

const discoveryPort = "47994"
const streamPort = "47995"
const udpStreamPort = "47997"

var pinPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)
var tokenPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)

type share struct {
	Port    int    `json:"port"`
	UdpPort int    `json:"udp_port,omitempty"`
	Pin     string `json:"pin"`
	Token   string `json:"token"`
	Product string `json:"product"`
	Busy    bool   `json:"busy"`
}
type request struct {
	ID     int    `json:"id"`
	Op     string `json:"op"`
	IP     string `json:"ip"`
	Port   int    `json:"port"`
	Tunnel string `json:"tunnel"`
	Share  share  `json:"share"`
}
type udpAuthEntry struct {
	ok      bool
	expires time.Time
}
type bridge struct {
	server      *tsnet.Server
	client      *local.Client
	mu          sync.Mutex
	shared      share
	tunnels     map[string]net.Listener
	udpTunnels  map[string]io.Closer
	connections map[net.Conn]bool
	udpAuth     map[string]udpAuthEntry
	serial      int
}

func validIP(value string) bool {
	ip, err := netip.ParseAddr(value)
	return err == nil && ip.Is4() && netip.MustParsePrefix("100.64.0.0/10").Contains(ip)
}
func validShare(s share) bool {
	return s.Port > 0 && s.Port < 65536 && pinPattern.MatchString(s.Pin) && tokenPattern.MatchString(s.Token)
}
func sameOwner(own, other int64, tagged bool) bool { return own > 0 && own == other && !tagged }
func (b *bridge) authorized(ctx context.Context, address string) bool {
	status, err := b.client.StatusWithoutPeers(ctx)
	if err != nil || status.Self == nil {
		return false
	}
	who, err := b.client.WhoIs(ctx, address)
	return err == nil && who.Node != nil && who.UserProfile != nil && sameOwner(int64(status.Self.UserID), int64(who.UserProfile.ID), len(who.Node.Tags) > 0 || (status.Self.Tags != nil && status.Self.Tags.Len() > 0))
}
func (b *bridge) info(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if r.Method != "GET" || (r.URL.Path != "/info" && r.URL.Path != "/presence") {
		http.NotFound(w, r)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
	defer cancel()
	if !b.authorized(ctx, r.RemoteAddr) {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	b.mu.Lock()
	s := b.shared
	b.mu.Unlock()
	if r.URL.Path == "/presence" {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"product": "Brioche/0.6", "sharing": validShare(s), "busy": s.Busy})
		return
	}
	if !validShare(s) {
		http.Error(w, "not sharing", http.StatusConflict)
		return
	}
	s.Port = 47995 // Native host stays private on loopback; this is its tailnet endpoint.
	json.NewEncoder(w).Encode(s)
}
func tuneTCP(conn net.Conn) {
	if tc, ok := conn.(*net.TCPConn); ok {
		_ = tc.SetNoDelay(true)
		_ = tc.SetKeepAlive(true)
		_ = tc.SetKeepAlivePeriod(15 * time.Second)
	}
}
func (b *bridge) copyConnection(a, c net.Conn, inbound bool) {
	tuneTCP(a)
	tuneTCP(c)
	b.mu.Lock()
	b.connections[a] = inbound
	b.connections[c] = inbound
	b.mu.Unlock()
	defer func() {
		a.Close()
		c.Close()
		b.mu.Lock()
		delete(b.connections, a)
		delete(b.connections, c)
		b.mu.Unlock()
	}()
	done := make(chan struct{}, 2)
	go func() { io.Copy(a, c); done <- struct{}{} }()
	go func() { io.Copy(c, a); done <- struct{}{} }()
	<-done
}
func (b *bridge) acceptStreams(listener net.Listener) {
	for {
		incoming, err := listener.Accept()
		if err != nil {
			return
		}
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			if !b.authorized(ctx, incoming.RemoteAddr().String()) {
				incoming.Close()
				return
			}
			b.mu.Lock()
			s := b.shared
			b.mu.Unlock()
			if !validShare(s) {
				incoming.Close()
				return
			}
			target, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", fmt.Sprint(s.Port)), 4*time.Second)
			if err != nil {
				incoming.Close()
				return
			}
			b.copyConnection(incoming, target, true)
		}()
	}
}
func (b *bridge) authorizedCached(address string) bool {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		host = address
	}
	now := time.Now()
	b.mu.Lock()
	if entry, ok := b.udpAuth[host]; ok && now.Before(entry.expires) {
		b.mu.Unlock()
		return entry.ok
	}
	b.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	ok := b.authorized(ctx, address)
	b.mu.Lock()
	if b.udpAuth == nil {
		b.udpAuth = map[string]udpAuthEntry{}
	}
	b.udpAuth[host] = udpAuthEntry{ok: ok, expires: now.Add(30 * time.Second)}
	b.mu.Unlock()
	return ok
}

// acceptUdpStreams bridges tailnet UDP datagrams on :47997 to local brioche-core UDP port.
func (b *bridge) acceptUdpStreams(pc net.PacketConn) {
	type udpPeer struct {
		localConn *net.UDPConn
		lastSeen  time.Time
	}
	var mu sync.Mutex
	peers := map[string]*udpPeer{}
	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			cutoff := time.Now().Add(-45 * time.Second)
			mu.Lock()
			for k, p := range peers {
				if p.lastSeen.Before(cutoff) {
					p.localConn.Close()
					delete(peers, k)
				}
			}
			mu.Unlock()
		}
	}()
	buf := make([]byte, 2048)
	for {
		n, remoteAddr, err := pc.ReadFrom(buf)
		if err != nil {
			return
		}
		if n <= 0 || !b.authorizedCached(remoteAddr.String()) {
			continue
		}
		b.mu.Lock()
		s := b.shared
		b.mu.Unlock()
		if !validShare(s) || s.UdpPort <= 0 || s.UdpPort >= 65536 {
			continue
		}
		key := remoteAddr.String()
		mu.Lock()
		p, exists := peers[key]
		if !exists {
			raddr, derr := net.ResolveUDPAddr("udp", fmt.Sprintf("127.0.0.1:%d", s.UdpPort))
			if derr != nil {
				mu.Unlock()
				continue
			}
			lc, derr := net.DialUDP("udp", nil, raddr)
			if derr != nil {
				mu.Unlock()
				continue
			}
			p = &udpPeer{localConn: lc, lastSeen: time.Now()}
			peers[key] = p
			go func(peerKey string, rAddr net.Addr, local *net.UDPConn) {
				defer func() {
					local.Close()
					mu.Lock()
					delete(peers, peerKey)
					mu.Unlock()
				}()
				rbuf := make([]byte, 2048)
				for {
					rn, rerr := local.Read(rbuf)
					if rerr != nil {
						return
					}
					if rn > 0 {
						pc.WriteTo(rbuf[:rn], rAddr)
					}
				}
			}(key, remoteAddr, lc)
		} else {
			p.lastSeen = time.Now()
		}
		localConn := p.localConn
		mu.Unlock()
		localConn.Write(buf[:n])
	}
}

func (b *bridge) services() {
	listener, err := b.server.Listen("tcp", ":"+discoveryPort)
	if err != nil {
		return
	}
	defer listener.Close()
	go (&http.Server{Handler: http.HandlerFunc(b.info), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 8 * time.Second, WriteTimeout: 8 * time.Second, IdleTimeout: 10 * time.Second, MaxHeaderBytes: 8192}).Serve(listener)
	if udpPC, uerr := b.server.ListenPacket("udp", ":"+udpStreamPort); uerr == nil {
		defer udpPC.Close()
		go b.acceptUdpStreams(udpPC)
	}
	streams, err := b.server.Listen("tcp", ":"+streamPort)
	if err != nil {
		return
	}
	defer streams.Close()
	b.acceptStreams(streams)
}
func (b *bridge) unpublish() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.shared = share{}
	for connection, inbound := range b.connections {
		if inbound {
			connection.Close()
		}
	}
}
func (b *bridge) clear() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.shared = share{}
	for _, listener := range b.tunnels {
		listener.Close()
	}
	b.tunnels = map[string]net.Listener{}
	for _, closer := range b.udpTunnels {
		closer.Close()
	}
	b.udpTunnels = map[string]io.Closer{}
	for connection := range b.connections {
		connection.Close()
	}
}

// udpTunnel opens a local loopback UDP port bridged to the peer's tailnet :47997/udp endpoint.
func (b *bridge) udpTunnel(ctx context.Context, ip string, port int) (map[string]any, error) {
	if !validIP(ip) || port != 47997 {
		return nil, errors.New("invalid_endpoint")
	}
	if !b.authorized(ctx, net.JoinHostPort(ip, fmt.Sprint(port))) {
		return nil, errors.New("different_account")
	}
	localPC, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0})
	if err != nil {
		return nil, errors.New("local_udp_listener_failed")
	}
	remoteConn, err := b.server.Dial(ctx, "udp", net.JoinHostPort(ip, fmt.Sprint(port)))
	if err != nil {
		localPC.Close()
		return nil, errors.New("remote_udp_dial_failed")
	}
	b.mu.Lock()
	b.serial++
	id := "udp-" + fmt.Sprint(b.serial)
	if b.udpTunnels == nil {
		b.udpTunnels = map[string]io.Closer{}
	}
	b.udpTunnels[id] = localPC
	b.mu.Unlock()

	var clientAddrMu sync.Mutex
	var clientAddr *net.UDPAddr

	go func() {
		defer func() {
			localPC.Close()
			remoteConn.Close()
			b.mu.Lock()
			delete(b.udpTunnels, id)
			b.mu.Unlock()
		}()
		buf := make([]byte, 2048)
		for {
			n, addr, rerr := localPC.ReadFromUDP(buf)
			if rerr != nil {
				return
			}
			clientAddrMu.Lock()
			clientAddr = addr
			clientAddrMu.Unlock()
			if n > 0 {
				if _, werr := remoteConn.Write(buf[:n]); werr != nil {
					return
				}
			}
		}
	}()
	go func() {
		defer localPC.Close()
		buf := make([]byte, 2048)
		for {
			n, rerr := remoteConn.Read(buf)
			if rerr != nil {
				return
			}
			clientAddrMu.Lock()
			dst := clientAddr
			clientAddrMu.Unlock()
			if n > 0 && dst != nil {
				localPC.WriteToUDP(buf[:n], dst)
			}
		}
	}()
	return map[string]any{"port": localPC.LocalAddr().(*net.UDPAddr).Port, "tunnel": id}, nil
}
func (b *bridge) tunnel(ctx context.Context, ip string, port int) (map[string]any, error) {
	if !validIP(ip) || port != 47995 {
		return nil, errors.New("invalid_endpoint")
	}
	// Validate peer ownership on outgoing paths too, not just the advertised list.
	if !b.authorized(ctx, net.JoinHostPort(ip, fmt.Sprint(port))) {
		return nil, errors.New("different_account")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, errors.New("local_listener_failed")
	}
	b.mu.Lock()
	b.serial++
	id := fmt.Sprint(b.serial)
	b.tunnels[id] = listener
	b.mu.Unlock()
	go func() {
		defer func() { listener.Close(); b.mu.Lock(); delete(b.tunnels, id); b.mu.Unlock() }()
		listener.(*net.TCPListener).SetDeadline(time.Now().Add(30 * time.Second))
		incoming, err := listener.Accept()
		if err != nil {
			return
		}
		listener.Close()
		dialCtx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		target, err := b.server.Dial(dialCtx, "tcp", net.JoinHostPort(ip, fmt.Sprint(port)))
		if err != nil {
			incoming.Close()
			return
		}
		b.copyConnection(incoming, target, false)
	}()
	return map[string]any{"port": listener.Addr().(*net.TCPAddr).Port, "tunnel": id}, nil
}

// Probe the application, not just the coordination server's cached online flag.
func (b *bridge) presence(parent context.Context, ip string) (bool, bool, bool) {
	ctx, cancel := context.WithTimeout(parent, 1800*time.Millisecond)
	defer cancel()
	transport := &http.Transport{DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
		return b.server.Dial(ctx, network, address)
	}, DisableKeepAlives: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	for _, path := range []string{"/presence", "/info"} {
		request, _ := http.NewRequestWithContext(ctx, "GET", "http://"+net.JoinHostPort(ip, discoveryPort)+path, nil)
		response, err := client.Do(request)
		if err != nil {
			return false, false, false
		}
		if response.StatusCode == http.StatusNotFound {
			response.Body.Close()
			continue
		}
		if path == "/info" && response.StatusCode == http.StatusConflict {
			response.Body.Close()
			return true, false, false
		}
		if response.StatusCode != http.StatusOK {
			response.Body.Close()
			return false, false, false
		}
		if path == "/info" {
			var info share
			err = json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&info)
			response.Body.Close()
			return err == nil && validShare(info), err == nil && validShare(info), info.Busy
		}
		var info struct {
			Product string `json:"product"`
			Sharing bool   `json:"sharing"`
			Busy    bool   `json:"busy"`
		}
		err = json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&info)
		response.Body.Close()
		if err != nil || !strings.HasPrefix(info.Product, "Brioche/") {
			return false, false, false
		}
		return true, info.Sharing, info.Busy
	}
	return false, false, false
}

func (b *bridge) command(ctx context.Context, r request) (any, error) {
	switch r.Op {
	case "status", "login", "devices":
		if r.Op == "login" {
			if err := b.client.StartLoginInteractive(ctx); err != nil {
				return nil, errors.New("login_failed")
			}
		}
		status, err := b.client.Status(ctx)
		if err != nil {
			return nil, errors.New("status_unavailable")
		}
		peers := []map[string]any{}
		ips := []string{}
		if status.Self != nil {
			for _, ip := range status.TailscaleIPs {
				if ip.Is4() {
					ips = append(ips, ip.String())
				}
			}
			for _, peer := range status.Peer {
				if !strings.HasPrefix(peer.HostName, "brioche-") {
					continue
				}
				if !sameOwner(int64(status.Self.UserID), int64(peer.UserID), (peer.Tags != nil && peer.Tags.Len() > 0) || (status.Self.Tags != nil && status.Self.Tags.Len() > 0)) {
					continue
				}
				for _, ip := range peer.TailscaleIPs {
					if ip.Is4() {
						path := "待检测"
						if peer.Active {
							if peer.PeerRelay != "" {
								path = "节点中继"
							} else if peer.CurAddr != "" {
								path = "直连"
							} else if peer.Relay != "" {
								path = "DERP 中继"
							}
						}
						peers = append(peers, map[string]any{"ip": ip.String(), "name": strings.TrimPrefix(strings.TrimSuffix(peer.HostName, "."), "brioche-"), "online": peer.Online, "path": path})
					}
				}
			}
		}
		if r.Op == "devices" {
			if len(peers) > 32 {
				peers = peers[:32]
			}
			probeCtx, probeCancel := context.WithTimeout(ctx, 6*time.Second)
			defer probeCancel()
			var wait sync.WaitGroup
			limit := make(chan struct{}, 6)
			for _, peer := range peers {
				peer["reported_online"] = peer["online"]
				peer["reachable"], peer["sharing"], peer["busy"] = false, false, false
				wait.Add(1)
				go func(entry map[string]any) {
					defer wait.Done()
					select {
					case limit <- struct{}{}:
						defer func() { <-limit }()
					case <-probeCtx.Done():
						return
					}
					reachable, sharing, busy := b.presence(probeCtx, entry["ip"].(string))
					entry["reachable"], entry["sharing"], entry["busy"] = reachable, sharing, busy
				}(peer)
			}
			wait.Wait()
		}
		return map[string]any{"state": status.BackendState, "ips": ips, "peers": peers, "auth_url": status.AuthURL}, nil
	case "share":
		if !validShare(r.Share) {
			return nil, errors.New("invalid_share")
		}
		r.Share.Product = "Brioche/0.6"
		b.mu.Lock()
		b.shared = r.Share
		b.mu.Unlock()
		return map[string]bool{"ok": true}, nil
	case "clear":
		b.mu.Lock()
		matches := r.Share.Token == "" || r.Share.Token == b.shared.Token
		b.mu.Unlock()
		if matches {
			b.unpublish()
		}
		return map[string]bool{"ok": true}, nil
	case "logout":
		b.clear()
		if err := b.client.Logout(ctx); err != nil {
			return nil, errors.New("logout_failed")
		}
		return map[string]bool{"ok": true}, nil
	case "close_tunnel":
		b.mu.Lock()
		listener := b.tunnels[r.Tunnel]
		udpCloser := b.udpTunnels[r.Tunnel]
		b.mu.Unlock()
		if listener != nil {
			listener.Close()
		}
		if udpCloser != nil {
			udpCloser.Close()
		}
		return map[string]bool{"ok": true}, nil
	case "connect":
		if !validIP(r.IP) {
			return nil, errors.New("invalid_endpoint")
		}
		if !b.authorized(ctx, net.JoinHostPort(r.IP, discoveryPort)) {
			return nil, errors.New("different_account")
		}
		transport := &http.Transport{DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			return b.server.Dial(ctx, network, address)
		}, DisableKeepAlives: true}
		defer transport.CloseIdleConnections()
		client := &http.Client{Transport: transport, Timeout: 8 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		req, _ := http.NewRequestWithContext(ctx, "GET", "http://"+net.JoinHostPort(r.IP, discoveryPort)+"/info", nil)
		response, err := client.Do(req)
		if err != nil {
			return nil, errors.New("peer_unavailable")
		}
		defer response.Body.Close()
		if response.StatusCode != 200 {
			return nil, errors.New("peer_not_sharing")
		}
		var s share
		if json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&s) != nil || !validShare(s) || !strings.HasPrefix(s.Product, "Brioche/") {
			return nil, errors.New("invalid_peer")
		}
		result, err := b.tunnel(ctx, r.IP, 47995)
		if err != nil {
			return nil, err
		}
		result["pin"] = s.Pin
		result["token"] = s.Token
		return result, nil
	case "tunnel":
		return b.tunnel(ctx, r.IP, r.Port)
	case "udp_tunnel":
		port := r.Port
		if port == 0 {
			port = 47997
		}
		return b.udpTunnel(ctx, r.IP, port)
	}
	return nil, errors.New("unknown_command")
}
func installFallbackDNS(raw string) {
	var servers []string
	for _, part := range strings.Split(raw, ",") {
		s := strings.TrimSpace(part)
		if s == "" {
			continue
		}
		if _, _, err := net.SplitHostPort(s); err != nil {
			s = net.JoinHostPort(s, "53")
		}
		servers = append(servers, s)
	}
	if len(servers) == 0 {
		return
	}
	net.DefaultResolver = &net.Resolver{
		PreferGo: true,
		Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
			d := net.Dialer{Timeout: 4 * time.Second}
			var lastErr error
			for _, srv := range servers {
				conn, err := d.DialContext(ctx, network, srv)
				if err == nil {
					return conn, nil
				}
				lastErr = err
			}
			return nil, lastErr
		},
	}
}

func main() {
	state := flag.String("state-dir", "", "private node state")
	hostFlag := flag.String("hostname", "", "optional custom node hostname")
	dnsFlag := flag.String("dns", "", "comma-separated fallback DNS servers")
	flag.Parse()
	if *state == "" {
		os.Exit(2)
	}
	if *dnsFlag != "" {
		installFallbackDNS(*dnsFlag)
	}
	if err := os.MkdirAll(*state, 0700); err != nil {
		os.Exit(2)
	}
	os.Chmod(*state, 0700)
	name := strings.TrimSpace(*hostFlag)
	if name == "" {
		name, _ = os.Hostname()
	}
	if name == "" {
		name = "device"
	}
	server := &tsnet.Server{Dir: *state, Hostname: "brioche-" + name, Logf: func(string, ...any) {}, UserLogf: func(string, ...any) {}}
	log.SetOutput(io.Discard)
	if err := server.Start(); err != nil {
		os.Exit(3)
	}
	defer server.Close()
	localClient, err := server.LocalClient()
	if err != nil {
		os.Exit(3)
	}
	b := &bridge{server: server, client: localClient, tunnels: map[string]net.Listener{}, udpTunnels: map[string]io.Closer{}, connections: map[net.Conn]bool{}, udpAuth: map[string]udpAuthEntry{}}
	defer b.clear()
	go b.services()
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 16384)
	output := json.NewEncoder(os.Stdout)
	for scanner.Scan() {
		var r request
		if json.Unmarshal(scanner.Bytes(), &r) != nil {
			break
		}
		if r.Op == "shutdown" {
			output.Encode(map[string]any{"id": r.ID, "result": map[string]bool{"ok": true}})
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
		result, err := b.command(ctx, r)
		cancel()
		response := map[string]any{"id": r.ID, "result": result}
		if err != nil {
			response["error"] = err.Error()
		}
		if output.Encode(response) != nil {
			return
		}
	}
}

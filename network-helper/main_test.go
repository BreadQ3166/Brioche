package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"tailscale.com/net/netns"
	"tailscale.com/tsnet"
	"tailscale.com/tstest/integration"
	"tailscale.com/tstest/integration/testcontrol"
	"tailscale.com/types/logger"
)

func TestEndpointAndOwnership(t *testing.T) {
	for _, ip := range []string{"127.0.0.1", "1.1.1.1", "example.com", "100.64.0.1:80", "::1"} {
		if validIP(ip) {
			t.Fatalf("accepted %s", ip)
		}
	}
	if !validIP("100.64.0.1") {
		t.Fatal("tailnet IP rejected")
	}
	if !sameOwner(1, 1, false) || sameOwner(1, 2, false) || sameOwner(0, 0, false) || sameOwner(1, 1, true) {
		t.Fatal("ownership validation failed")
	}
	if validShare(share{Port: 22, Pin: "x", Token: "x"}) {
		t.Fatal("invalid credentials accepted")
	}
}
func TestEmbeddedTransport(t *testing.T) {
	for _, same := range []bool{true, false} {
		t.Run(fmt.Sprint("same_owner_", same), func(t *testing.T) {
			netns.SetEnabled(false)
			defer netns.SetEnabled(true)
			control := &testcontrol.Server{DERPMap: integration.RunDERPAndSTUN(t, logger.Discard, "127.0.0.1"), AllNodesSameUser: same, Logf: logger.Discard}
			web := httptest.NewServer(control)
			defer web.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
			defer cancel()
			create := func(name string) *bridge {
				server := &tsnet.Server{Dir: t.TempDir(), ControlURL: web.URL, Hostname: name, Logf: logger.Discard, UserLogf: logger.Discard}
				t.Cleanup(func() { server.Close() })
				if _, err := server.Up(ctx); err != nil {
					t.Fatal(err)
				}
				client, err := server.LocalClient()
				if err != nil {
					t.Fatal(err)
				}
				b := &bridge{server: server, client: client, tunnels: map[string]net.Listener{}, connections: map[net.Conn]bool{}}
				t.Cleanup(b.clear)
				go b.services()
				return b
			}
			first, second := create("brioche-first"), create("brioche-second")
			status, err := second.client.Status(ctx)
			if err != nil {
				t.Fatal(err)
			}
			ip := status.TailscaleIPs[0].String()
			// Wait for the peer map, not an arbitrary sleep before checking authorization.
			for {
				s, err := first.client.Status(ctx)
				if err == nil && len(s.Peer) > 0 {
					break
				}
				select {
				case <-ctx.Done():
					t.Fatal("peer map timeout")
				case <-time.After(20 * time.Millisecond):
				}
			}
			if same {
				for {
					reachable, sharing, _ := first.presence(ctx, ip)
					if reachable {
						if sharing {
							t.Fatal("unshared node advertised credentials")
						}
						break
					}
					select {
					case <-ctx.Done():
						t.Fatal("presence timeout")
					case <-time.After(20 * time.Millisecond):
					}
				}
			}
			if !same {
				if _, err := first.command(ctx, request{Op: "connect", IP: ip}); err == nil || err.Error() != "different_account" {
					t.Fatalf("cross-account connect accepted: %v", err)
				}
				return
			}
			echo, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			defer echo.Close()
			go func() {
				c, err := echo.Accept()
				if err == nil {
					defer c.Close()
					io.Copy(c, c)
				}
			}()
			credentials := share{Port: echo.Addr().(*net.TCPAddr).Port, Pin: strings.Repeat("a", 64), Token: strings.Repeat("b", 43)}
			if _, err := second.command(ctx, request{Op: "share", Share: credentials}); err != nil {
				t.Fatal(err)
			}
			devices, err := first.command(ctx, request{Op: "devices"})
			if err != nil {
				t.Fatal(err)
			}
			peers := devices.(map[string]any)["peers"].([]map[string]any)
			if len(peers) != 1 || peers[0]["reachable"] != true || peers[0]["sharing"] != true {
				t.Fatalf("bad application presence: %v", peers)
			}
			encoded, _ := json.Marshal(devices)
			if strings.Contains(string(encoded), credentials.Token) || strings.Contains(string(encoded), credentials.Pin) {
				t.Fatal("presence exposed share credentials")
			}
			var result any
			for {
				result, err = first.command(ctx, request{Op: "connect", IP: ip})
				if err == nil {
					break
				}
				select {
				case <-ctx.Done():
					t.Fatalf("connect timeout: %v", err)
				case <-time.After(30 * time.Millisecond):
				}
			}
			reply := result.(map[string]any)
			if reply["pin"] != credentials.Pin || reply["token"] != credentials.Token {
				t.Fatal("discovery credentials changed")
			}
			conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", fmt.Sprint(reply["port"])), time.Second)
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			conn.SetDeadline(time.Now().Add(10 * time.Second))
			payload := strings.Repeat("Brioche TLS/H264 opaque bytes\x00\xff", 4096)
			go io.WriteString(conn, payload)
			data := make([]byte, len(payload))
			if _, err := io.ReadFull(conn, data); err != nil {
				t.Fatal(err)
			}
			if string(data) != payload {
				t.Fatal("stream changed")
			}
			second.unpublish()
			conn.SetReadDeadline(time.Now().Add(2 * time.Second))
			if _, err := conn.Read(make([]byte, 1)); err == nil {
				t.Fatal("unpublished connection not closed")
			}
			if _, err := first.command(ctx, request{Op: "connect", IP: ip}); err == nil || err.Error() != "peer_not_sharing" {
				t.Fatalf("unpublished service accepted: %v", err)
			}
		})
	}
}

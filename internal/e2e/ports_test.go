//go:build e2e

package e2e

import (
	"net"
	"strconv"
	"testing"

	"github.com/xiaosq2000/sing-box-manager/internal/singbox"
)

// These checks need only loopback sockets, not an installed sbc or services.
func TestE2EPortSelection(t *testing.T) {
	t.Run("UDP-only-occupancy", func(t *testing.T) {
		busy := freePort(t)
		address := net.JoinHostPort("127.0.0.1", strconv.Itoa(busy))
		udp, err := net.ListenPacket("udp", address)
		if err != nil {
			t.Fatal(err)
		}
		defer udp.Close()
		// A TCP-only probe accepts this port, but sbc port must reject it.
		tcp, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatal(err)
		}
		tcp.Close()
		port, err := singbox.FreePort(busy)
		if err != nil {
			t.Fatal(err)
		}
		if port == busy {
			t.Fatal("selected a port occupied by UDP")
		}
		assertBothTransportsFree(t, port)
	})
	t.Run("fixture-port", func(t *testing.T) {
		assertBothTransportsFree(t, freePort(t))
	})
}

func assertBothTransportsFree(t *testing.T, port int) {
	t.Helper()
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	tcp, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatalf("fixture port %d is unavailable for TCP: %v", port, err)
	}
	defer tcp.Close()
	udp, err := net.ListenPacket("udp", address)
	if err != nil {
		t.Fatalf("fixture port %d is unavailable for UDP: %v", port, err)
	}
	udp.Close()
}

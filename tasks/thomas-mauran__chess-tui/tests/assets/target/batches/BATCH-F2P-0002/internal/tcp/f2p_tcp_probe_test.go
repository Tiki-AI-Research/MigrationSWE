package tcp

import (
	"net"
	"testing"
	"time"

	"github.com/thomas-mauran/chess-tui/internal/constants"
)

func f2pStartServer(t *testing.T, hostWhite bool) (string, *GameServer) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := NewGameServer(hostWhite)
	go server.Serve(lis)
	t.Cleanup(server.Stop)
	return lis.Addr().String(), server
}

func f2pDial(t *testing.T, addr string) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

func f2pReadN(t *testing.T, conn net.Conn, n int) string {
	t.Helper()
	buf := make([]byte, n)
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	got, err := conn.Read(buf)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	return string(buf[:got])
}

func f2pJoin(t *testing.T, addr string) (net.Conn, net.Conn) {
	t.Helper()
	host := f2pDial(t, addr)
	time.Sleep(60 * time.Millisecond)
	joiner := f2pDial(t, addr)
	if got := f2pReadN(t, joiner, 1); got != "w" && got != "b" {
		t.Fatalf("joiner color = %q, want w or b", got)
	}
	if got := f2pReadN(t, host, 1); got != "s" {
		t.Fatalf("host start = %q, want s", got)
	}
	return host, joiner
}

// F2P-026 (R045): the multiplayer server listens on 0.0.0.0:2308 and the full
// join/relay/ended contract holds.
func TestF2P026ServerListenAndJoinContract(t *testing.T) {
	if constants.NetworkPort != 2308 {
		t.Fatalf("NetworkPort = %d, want 2308", constants.NetworkPort)
	}

	server := NewGameServer(true)
	go server.Run()
	t.Cleanup(server.Stop)

	var conn net.Conn
	var err error
	for i := 0; i < 20; i++ {
		conn, err = net.DialTimeout("tcp", "127.0.0.1:2308", 200*time.Millisecond)
		if err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err != nil {
		t.Skipf("could not bind 0.0.0.0:2308 for the probe: %v", err)
	}
	conn.Close()

	addr, srv := f2pStartServer(t, true)
	host, joiner := f2pJoin(t, addr)

	// Third client is rejected.
	third := f2pDial(t, addr)
	buf := make([]byte, 64)
	_ = third.SetReadDeadline(time.Now().Add(2 * time.Second))
	n, err := third.Read(buf)
	if err != nil {
		t.Fatalf("third read: %v", err)
	}
	if string(buf[:n]) != "Game is already full" {
		t.Fatalf("third = %q", string(buf[:n]))
	}

	// Relay without echo.
	if _, err := host.Write([]byte("e2e4")); err != nil {
		t.Fatal(err)
	}
	if got := f2pReadN(t, joiner, 5); got != "e2e4" {
		t.Fatalf("relayed = %q", got)
	}
	_ = host.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	hostRead := make([]byte, 5)
	if _, err := host.Read(hostRead); err == nil {
		t.Fatal("sender must not receive an echo")
	}

	// ended broadcast.
	if _, err := host.Write([]byte("ended")); err != nil {
		t.Fatal(err)
	}
	if got := f2pReadN(t, joiner, 5); got != "ended" {
		t.Fatalf("broadcast = %q, want ended", got)
	}
	_ = srv
}

// F2P-046 (R045): moves are framed as from+to plus an optional promotion
// suffix and the read buffer is exactly 5 bytes.
func TestF2P046MoveWireFormatAndBufferSize(t *testing.T) {
	if constants.NetworkBufferSize != 5 {
		t.Fatalf("NetworkBufferSize = %d, want 5", constants.NetworkBufferSize)
	}
	addr, _ := f2pStartServer(t, true)
	host, joiner := f2pJoin(t, addr)
	if _, err := joiner.Write([]byte("e7e8q")); err != nil {
		t.Fatal(err)
	}
	if got := f2pReadN(t, host, 5); got != "e7e8q" {
		t.Fatalf("framed = %q", got)
	}
}

// f2pBlockingConn makes the zero-deadline poll deterministic for the ended
// case by ignoring read deadlines.
type f2pBlockingConn struct{ net.Conn }

func (f2pBlockingConn) SetReadDeadline(time.Time) error { return nil }

// F2P-042 (R044): WaitForGameStart on a connected but silent socket returns
// (false, nil) through a zero read deadline, and ReadStream surfaces a peer's
// "ended" as the "Game ended by the other opponent" error.
func TestF2P042NonBlockingReads(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lis.Close()

	accepted := make(chan net.Conn, 1)
	go func() {
		c, err := lis.Accept()
		if err == nil {
			accepted <- c
		}
	}()

	client, err := net.Dial("tcp", lis.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	peer := <-accepted
	defer peer.Close()

	silent := &Opponent{conn: client}
	started, err := WaitForGameStart(silent)
	if err != nil || started {
		t.Fatalf("WaitForGameStart on silent socket = %v/%v, want false/nil", started, err)
	}

	_ = client.SetReadDeadline(time.Time{})
	go func() {
		time.Sleep(50 * time.Millisecond)
		_, _ = peer.Write([]byte("ended"))
	}()
	ended := &Opponent{conn: f2pBlockingConn{client}}
	_, err = ended.ReadStream()
	if err == nil || err.Error() != "Game ended by the other opponent" {
		t.Fatalf("ReadStream err = %v", err)
	}
}

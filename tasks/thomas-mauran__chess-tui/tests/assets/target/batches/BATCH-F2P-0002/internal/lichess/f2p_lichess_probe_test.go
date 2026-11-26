package lichess

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func f2pCollect(t *testing.T, ch <-chan string, n int) []string {
	t.Helper()
	var out []string
	for i := 0; i < n; i++ {
		select {
		case msg := <-ch:
			out = append(out, msg)
		case <-time.After(3 * time.Second):
			t.Fatalf("timed out after %d messages: %v", len(out), out)
		}
	}
	return out
}

// F2P-038 (R046): GetOngoingGames GETs <base>/account/playing with the
// preserved User-Agent and bearer auth, and decodes gameId/color/isMyTurn.
func TestF2P038OngoingGamesRequestContract(t *testing.T) {
	var gotUA, gotAuth, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotUA = r.Header.Get("User-Agent")
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"nowPlaying":[{"gameId":"abc","fullId":"abcfull","color":"white","fen":"startpos","opponent":{"id":"u","username":"bob","rating":1200},"isMyTurn":true}]}`)
	}))
	defer srv.Close()

	c := NewClient("token123")
	c.BaseURL = srv.URL
	games, err := c.GetOngoingGames()
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/account/playing" {
		t.Fatalf("path = %q", gotPath)
	}
	if gotUA != "chess-tui (https://github.com/thomas-mauran/chess-tui)" {
		t.Fatalf("user-agent = %q", gotUA)
	}
	if gotAuth != "Bearer token123" {
		t.Fatalf("authorization = %q", gotAuth)
	}
	if len(games) != 1 || games[0].GameID != "abc" || games[0].Color != "white" || !games[0].IsMyTurn {
		t.Fatalf("games = %+v", games)
	}
}

// F2P-041 (R046): the same request/decoding contract for the ongoing-games
// surface, asserted on a second response (black, not my turn).
func TestF2P041OngoingGamesDecoding(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		if r.Header.Get("User-Agent") != "chess-tui (https://github.com/thomas-mauran/chess-tui)" {
			t.Errorf("user-agent = %q", r.Header.Get("User-Agent"))
		}
		if r.Header.Get("Authorization") != "Bearer tok" {
			t.Errorf("authorization = %q", r.Header.Get("Authorization"))
		}
		fmt.Fprint(w, `{"nowPlaying":[{"gameId":"xyz","color":"black","isMyTurn":false}]}`)
	}))
	defer srv.Close()

	c := NewClient("tok")
	c.BaseURL = srv.URL
	games, err := c.GetOngoingGames()
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/account/playing" {
		t.Fatalf("path = %q", gotPath)
	}
	if len(games) != 1 || games[0].GameID != "xyz" || games[0].Color != "black" || games[0].IsMyTurn {
		t.Fatalf("games = %+v", games)
	}
}

// F2P-036 (R047): seek errors map to the exact documented strings.
func TestF2P036SeekErrorMapping(t *testing.T) {
	cases := []struct {
		status int
		want   string
	}{
		{403, "Token missing permissions. Please generate a new token with 'board:play' scope enabled."},
		{429, "Rate limit exceeded. Please wait a minute before trying again."},
		{401, "Invalid token. Please check your token or generate a new one."},
	}
	for _, tc := range cases {
		err := seekError(tc.status, "")
		if err == nil || err.Error() != tc.want {
			t.Fatalf("status %d = %v, want %q", tc.status, err, tc.want)
		}
	}
	err := seekError(400, "bad body")
	if err == nil || err.Error() != "Invalid seek parameters: bad body" {
		t.Fatalf("400 = %v", err)
	}
}

// F2P-033 (R053): the event stream detects a gameStart whose id was not in the
// initial ongoing set and treats blank lines as keep-alive.
func TestF2P033EventStreamDetection(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		fmt.Fprint(w, "\n")
		fmt.Fprint(w, `{"type":"gameStart","game":{"gameId":"newgame","color":"black"}}`+"\n")
	}))
	defer srv.Close()

	c := NewClient("t")
	c.BaseURL = srv.URL
	cancel := &atomic.Bool{}
	found := make(chan GameFound, 1)
	c.SpawnEventStream(map[string]struct{}{"old": {}}, cancel, found)

	select {
	case res := <-found:
		if res.Err != nil {
			t.Fatalf("found err = %v", res.Err)
		}
		if res.GameID != "newgame" {
			t.Fatalf("game id = %q, want newgame", res.GameID)
		}
		if res.Color != ParseGameColor("black") {
			t.Fatalf("color = %v", res.Color)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for game start")
	}
}

// F2P-043 (R048): the board stream emits only newly arrived moves and maps
// status, and a 404 stops the stream.
func TestF2P043BoardStreamParsing(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		flusher, _ := w.(http.Flusher)
		fmt.Fprint(w, `{"type":"gameFull","id":"g1","white":{"id":"me"},"black":{"id":"op"},"state":{"moves":"e2e4 e7e5","status":"started"}}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"started"}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"mate"}`+"\n")
		if flusher != nil {
			flusher.Flush()
		}
	}))
	defer srv.Close()

	c := NewClient("t")
	c.BaseURL = srv.URL
	ch := make(chan string, 16)
	f2p2StartContractStream(t, c, ch)
	f2p2ExpectBoardMessages(t, ch, []string{"g1f3", "GAME_STATUS:checkmate"}, "INIT_MOVES:2")

	notFound := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer notFound.Close()
	c2 := NewClient("t")
	c2.BaseURL = notFound.URL
	ch2 := make(chan string, 4)
	f2p2StartContractStream(t, c2, ch2)
	select {
	case msg := <-ch2:
		t.Fatalf("404 must not emit a message, got %q", msg)
	case <-time.After(400 * time.Millisecond):
	}
	_ = strings.TrimSpace
}

// StreamGame may read synchronously or start a background reader. Both satisfy
// the public stream contract; await observable messages independently of return.
func f2p2StartContractStream(t *testing.T, c *Client, messages chan string) {
	t.Helper()
	returned := make(chan error, 1)
	go func() { returned <- c.StreamGame("g1", messages, nil) }()
	t.Cleanup(func() {
		select {
		case err := <-returned:
			if err != nil {
				t.Errorf("StreamGame returned an error: %v", err)
			}
		default:
			// A live synchronous reader is still following the stream.
		}
	})
}

// Bootstrap bookkeeping is optional; live moves, their order and the status
// transition are public behavior. A bootstrap token is allowed only once,
// before the first live message. Unexpected or repeated messages fail.
func f2p2ExpectBoardMessages(t *testing.T, ch <-chan string, want []string, bootstrap ...string) {
	t.Helper()
	allowed := make(map[string]bool)
	for _, msg := range bootstrap { allowed[msg] = true }
	deadline := time.NewTimer(2 * time.Second)
	defer deadline.Stop()
	for i := 0; i < len(want); {
		select {
		case got := <-ch:
			if i == 0 && allowed[got] { delete(allowed, got); continue }
			if got != want[i] { t.Fatalf("message %d = %q, want %q", i, got, want[i]) }
			i++
		case <-deadline.C:
			t.Fatalf("timed out waiting for live message %d: %v", i, want)
		}
	}
	select {
	case extra := <-ch: t.Fatalf("unexpected repeated message %q", extra)
	case <-time.After(50 * time.Millisecond):
	}
}

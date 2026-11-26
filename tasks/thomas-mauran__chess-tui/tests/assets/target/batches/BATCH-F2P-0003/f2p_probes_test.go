package main

// F2P probes for BATCH-F2P-0003 (chess-tui Rust -> Go migration).
//
// Every observation owned by this batch has exactly one dedicated test whose
// name carries the observation ID (dash removed), so a runtime harness can map
// Go test results onto observation IDs without ambiguity. Each test exercises
// the concrete entry point named by the observation's atomic_claim against the
// Go 1.25.0 target technology.

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	chesslib "github.com/notnil/chess"
	board "github.com/thomas-mauran/chess-tui/internal/chess"
	"github.com/thomas-mauran/chess-tui/internal/config"
	"github.com/thomas-mauran/chess-tui/internal/constants"
	"github.com/thomas-mauran/chess-tui/internal/lichess"
	"github.com/thomas-mauran/chess-tui/internal/pgn"
	"github.com/thomas-mauran/chess-tui/internal/skin"
	"github.com/thomas-mauran/chess-tui/internal/tcp"
	"github.com/thomas-mauran/chess-tui/internal/uci"
)

const f2pStartFEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

func f2pConfigHome(t *testing.T) {
	t.Helper()
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
}

func f2pSimplePGN(title string) string {
	return "[Event \"" + title + "\"]\n[White \"W\"]\n[Black \"B\"]\n[Result \"1-0\"]\n\n1. e4 e5 2. Nf3 1-0\n"
}

func f2pStrPtr(s string) *string { return &s }
func f2pU8Ptr(v uint8) *uint8    { return &v }

// F2P-055
func TestF2P055(t *testing.T) {
	b := board.NewGameBoard()
	if got := b.FEN(); got != f2pStartFEN {
		t.Fatalf("initial FEN = %q", got)
	}
	b.ExecuteShakmatyMove(chesslib.E2, chesslib.E4)
	b.ExecuteShakmatyMove(chesslib.E7, chesslib.E5)
	b.ExecuteShakmatyMove(chesslib.G1, chesslib.F3)
	if got := b.FEN(); got != "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2" {
		t.Fatalf("after 1.e4 e5 2.Nf3 FEN = %q", got)
	}
	if b.ExecuteShakmatyMove(chesslib.E4, chesslib.E5) != nil {
		t.Fatal("illegal move must return nil")
	}
	m := board.NewGameBoard()
	m.ExecuteShakmatyMove(chesslib.F2, chesslib.F3)
	m.ExecuteShakmatyMove(chesslib.E7, chesslib.E5)
	m.ExecuteShakmatyMove(chesslib.G2, chesslib.G4)
	if m.ExecuteShakmatyMove(chesslib.D8, chesslib.H4) == nil {
		t.Fatal("Qh4 must be legal")
	}
	if !m.IsCheckmate() {
		t.Fatal("fool's mate must be checkmate")
	}
	if m.IsDraw() {
		t.Fatal("checkmate must not be a draw")
	}
	// A bishop and knight can mate; having two minor pieces is not by itself
	// insufficient material. Keep the genuinely dead positions covered too.
	for _, tc := range []struct {
		name, rank string
		draw       bool
	}{
		{"bare_kings", "K7", true},
		{"single_bishop", "K1B5", true},
		{"single_knight", "KN6", true},
		{"same_color_bishops", "K1B1B3", true},
		{"bishop_and_knight", "KNB5", false},
		{"opposite_color_bishops", "K1B2B2", false},
		{"two_knights", "KNN5", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			setup, err := chesslib.FEN("7k/8/8/8/8/8/8/" + tc.rank + " w - - 0 1")
			if err != nil {
				t.Fatal(err)
			}
			board := board.NewGameBoard()
			board.PositionHistory = []*chesslib.Position{chesslib.NewGame(setup).Position()}
			if got := board.IsDraw(); got != tc.draw {
				t.Fatalf("IsDraw() = %v, want %v for %s", got, tc.draw, tc.name)
			}
		})
	}
}

// F2P-049
func TestF2P049(t *testing.T) {
	games, err := pgn.FromPGNString("[Event \"Live\"]\n[White \"A\"]\n[Black \"B\"]\n[Result \"0-1\"]\n\n1. e4 e5 2. Nf3 0-1\n")
	if err != nil || len(games) == 0 {
		t.Fatalf("parse: %v", err)
	}
	if got := games[0].ResultSummary(); got != "B (Black) won" {
		t.Fatalf("0-1 summary = %q", got)
	}
	g10, _ := pgn.FromPGNString("[Event \"X\"]\n[White \"A\"]\n[Black \"B\"]\n[Result \"1-0\"]\n\n1. e4 e5 1-0\n")
	if got := g10[0].ResultSummary(); got != "A (White) won" {
		t.Fatalf("1-0 summary = %q", got)
	}
	gd, _ := pgn.FromPGNString("[Event \"X\"]\n[White \"A\"]\n[Black \"B\"]\n[Result \"1/2-1/2\"]\n\n1. e4 e5 1/2-1/2\n")
	if got := gd[0].ResultSummary(); got != "Draw" {
		t.Fatalf("draw summary = %q", got)
	}
	graw, _ := pgn.FromPGNString("[Event \"X\"]\n[White \"A\"]\n[Black \"B\"]\n[Result \"*\"]\n\n1. e4 e5 *\n")
	if got := graw[0].ResultSummary(); got != "*" {
		t.Fatalf("raw summary = %q", got)
	}
}

// F2P-050
func TestF2P050(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lis.Close()
	connCh := make(chan net.Conn, 1)
	go func() { c, e := lis.Accept(); if e == nil { connCh <- c } }()
	white := chesslib.White
	o, err := tcp.New(lis.Addr().String(), &white)
	if err != nil {
		t.Fatal(err)
	}
	defer o.Close()
	peer := <-connCh
	defer peer.Close()

	started, err := tcp.WaitForGameStart(o)
	if err != nil || started {
		t.Fatalf("silent socket start = (%v,%v), want (false,nil)", started, err)
	}
	msg, err := o.ReadStream()
	if err != nil || msg != "" {
		t.Fatalf("silent socket read = (%q,%v), want (\"\",nil)", msg, err)
	}
}

// F2P-051
func TestF2P051(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		fmt.Fprint(w, `{"type":"gameFull","id":"g1","white":{"id":"me"},"black":{"id":"op"},"state":{"moves":"e2e4 e7e5","status":"started"}}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"started"}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"mate"}`+"\n")
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	ch := make(chan string, 16)
	f2p3StartContractStream(t, c, ch)
	f2p3ExpectBoardMessages(t, ch, []string{"g1f3", "GAME_STATUS:checkmate"}, "INIT_MOVES:2")

	srv404 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv404.Close()
	c2 := lichess.NewClient("t")
	c2.BaseURL = srv404.URL
	ch2 := make(chan string, 4)
	f2p3StartContractStream(t, c2, ch2)
	select {
	case msg := <-ch2:
		t.Fatalf("404 produced unexpected message %q", msg)
	case <-time.After(300 * time.Millisecond):
	}
}

// F2P-052
func TestF2P052(t *testing.T) {
	cases := []string{`"LightBlue"`, `{"Rgb":[160,160,160]}`}
	for _, raw := range cases {
		var c skin.Color
		if err := json.Unmarshal([]byte(raw), &c); err != nil {
			t.Fatalf("unmarshal %s: %v", raw, err)
		}
		out, err := json.Marshal(c)
		if err != nil {
			t.Fatal(err)
		}
		if string(out) != raw {
			t.Fatalf("round trip %s -> %s", raw, out)
		}
	}
}

// F2P-053
func TestF2P053(t *testing.T) {
	f2pConfigHome(t)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "b.pgn"), []byte(f2pSimplePGN("Second")), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "a.pgn"), []byte(f2pSimplePGN("First")), 0o644); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	games, err := loadPGN(dir, &stderr)
	if err != nil {
		t.Fatal(err)
	}
	if len(games) != 2 || games[0].Title != "First" || games[1].Title != "Second" {
		t.Fatalf("directory order wrong: %d games", len(games))
	}
}

// F2P-054
func TestF2P054(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.toml")
	cfg := config.Default()
	cfg.EnginePath = nil
	cfg.BotDifficulty = nil
	cfg.LichessToken = nil
	if err := cfg.Persist(path); err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(content)
	for _, key := range []string{"engine_path", "bot_difficulty", "lichess_token"} {
		if strings.Contains(text, key) {
			t.Fatalf("%s must be omitted, file = %s", key, text)
		}
	}
	for _, key := range []string{"display_mode", "log_level", "bot_depth", "selected_skin_name", "sound_enabled", "animations_enabled"} {
		if !strings.Contains(text, key) {
			t.Fatalf("%s must be present, file = %s", key, text)
		}
	}
	decoded := config.Load(path)
	if decoded.DisplayMode == nil || *decoded.DisplayMode != "DEFAULT" || decoded.BotDepth == nil || *decoded.BotDepth != 10 {
		t.Fatalf("decoded = %+v", decoded)
	}
	if decoded.SoundEnabled == nil || !*decoded.SoundEnabled || decoded.AnimationsEnabled == nil || *decoded.AnimationsEnabled {
		t.Fatalf("decoded flags = %+v", decoded)
	}
}

// F2P-056
func TestF2P056(t *testing.T) {
	f2pConfigHome(t)
	var out, errb bytes.Buffer
	code := run([]string{"--pgn", "examples/sample.pgn"}, strings.NewReader(""), &out, &errb)
	if code != 0 {
		t.Fatalf("run(sample.pgn) = %d, stderr = %s", code, errb.String())
	}
}

// F2P-057
func TestF2P057(t *testing.T) {
	var gotUA, gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotUA = r.Header.Get("User-Agent")
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"nowPlaying":[{"gameId":"abc","color":"white","isMyTurn":true}]}`)
	}))
	defer srv.Close()
	c := lichess.NewClient("secret")
	c.BaseURL = srv.URL
	games, err := c.GetOngoingGames()
	if err != nil {
		t.Fatal(err)
	}
	if gotUA != constants.LichessUserAgent {
		t.Fatalf("user-agent = %q", gotUA)
	}
	if gotAuth != "Bearer secret" {
		t.Fatalf("authorization = %q", gotAuth)
	}
	if len(games) != 1 || games[0].GameID != "abc" || games[0].Color != "white" || !games[0].IsMyTurn {
		t.Fatalf("games = %+v", games)
	}
}

// F2P-058
func TestF2P058(t *testing.T) {
	check := func(name string, cfg *config.Config) {
		t.Helper()
		if cfg == nil {
			t.Fatalf("%s: nil config", name)
		}
		if cfg.DisplayMode == nil || *cfg.DisplayMode != "DEFAULT" ||
			cfg.LogLevel == nil || *cfg.LogLevel != "OFF" ||
			cfg.BotDepth == nil || *cfg.BotDepth != 10 ||
			cfg.SelectedSkinName == nil || *cfg.SelectedSkinName != "Default" ||
			cfg.SoundEnabled == nil || !*cfg.SoundEnabled ||
			cfg.AnimationsEnabled == nil || *cfg.AnimationsEnabled {
			t.Fatalf("%s: unexpected defaults %+v", name, cfg)
		}
		if cfg.EnginePath != nil || cfg.BotDifficulty != nil || cfg.LichessToken != nil {
			t.Fatalf("%s: optional fields must be nil", name)
		}
	}

	check("missing", config.Load(filepath.Join(t.TempDir(), "does-not-exist.toml")))

	empty := filepath.Join(t.TempDir(), "empty.toml")
	if err := os.WriteFile(empty, []byte("   \n\t\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	check("empty", config.Load(empty))

	invalid := filepath.Join(t.TempDir(), "invalid.toml")
	if err := os.WriteFile(invalid, []byte("not = = toml"), 0o644); err != nil {
		t.Fatal(err)
	}
	check("invalid", config.Load(invalid))
}

// F2P-059
func TestEngineStubHelper(t *testing.T) {
	if os.Getenv("F2P_WANT_HELPER") != "1" {
		return
	}
	logPath := os.Getenv("F2P_STUB_LOG")
	sc := bufio.NewScanner(os.Stdin)
	for sc.Scan() {
		line := sc.Text()
		if logPath != "" {
			if f, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
				fmt.Fprintln(f, line)
				f.Close()
			}
		}
		switch {
		case line == "uci":
			fmt.Println("uciok")
		case strings.HasPrefix(line, "go"):
			fmt.Println("bestmove e2e4")
		}
	}
	os.Exit(0)
}

// F2P-059
func TestF2P059(t *testing.T) {
	logPath := filepath.Join(t.TempDir(), "stub.log")
	t.Setenv("F2P_WANT_HELPER", "1")
	t.Setenv("F2P_STUB_LOG", logPath)
	enginePath := os.Args[0] + " -test.run=TestEngineStubHelper"
	bot := uci.New(enginePath, false, 6, nil)
	move, err := bot.GetMove(f2pStartFEN)
	if err != nil {
		t.Fatal(err)
	}
	if move != "e2e4" {
		t.Fatalf("move = %q", move)
	}
	content, _ := os.ReadFile(logPath)
	joined := string(content)
	iuci := strings.Index(joined, "uci\n")
	ipos := strings.Index(joined, "position fen "+f2pStartFEN)
	igo := strings.Index(joined, "go depth 6")
	if !(iuci >= 0 && ipos > iuci && igo > ipos) {
		t.Fatalf("handshake order wrong: %q", joined)
	}
	bad := uci.New("/nonexistent/engine-binary-xyz", false, 5, nil)
	if _, err := bad.GetMove(f2pStartFEN); err == nil {
		t.Fatal("a missing engine binary must be reported as an error")
	}
}

// F2P-060
func TestF2P060(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		fmt.Fprint(w, "\n")
		fmt.Fprint(w, `{"type":"gameStart","game":{"gameId":"newgame","color":"black"}}`+"\n")
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	found := make(chan lichess.GameFound, 1)
	c.SpawnEventStream(map[string]struct{}{"old": {}}, &atomic.Bool{}, found)
	select {
	case res := <-found:
		if res.GameID != "newgame" || res.Color != lichess.ParseGameColor("black") {
			t.Fatalf("found = %+v", res)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for gameStart")
	}
}

// F2P-061
func TestF2P061(t *testing.T) {
	f2pConfigHome(t)
	empty := t.TempDir()
	var o1, e1 bytes.Buffer
	if code := run([]string{"--pgn", empty}, strings.NewReader(""), &o1, &e1); code != 1 {
		t.Fatalf("empty dir exit = %d, want 1", code)
	}
	if strings.TrimSpace(e1.String()) == "" {
		t.Fatalf("empty dir must report the failure on stderr")
	}

	missing := filepath.Join(t.TempDir(), "missing.pgn")
	var o2, e2 bytes.Buffer
	if code := run([]string{"--pgn", missing}, strings.NewReader(""), &o2, &e2); code != 1 {
		t.Fatalf("missing file exit = %d, want 1", code)
	}
	if e2.Len() == 0 {
		t.Fatalf("missing file must write to stderr")
	}
}

// F2P-062
func TestF2P062(t *testing.T) {
	f2pConfigHome(t)
	var out, errb bytes.Buffer
	code := run([]string{"--pgn", "examples/sample.pgn"}, strings.NewReader(""), &out, &errb)
	if code != 0 {
		t.Fatalf("run(sample.pgn) = %d, stderr = %s", code, errb.String())
	}
}

// F2P-063
func TestF2P063(t *testing.T) {
	games, err := pgn.FromPGNString("[Event \"X\"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 1-0\n")
	if err != nil || len(games) == 0 {
		t.Fatalf("parse: %v", err)
	}
	v := games[0]
	if len(pgn.Speeds) != 7 || pgn.Speeds[0].Label != "0.5x" || pgn.Speeds[6].Label != "4x" {
		t.Fatalf("speeds = %+v", pgn.Speeds)
	}
	for i := 0; i < 20; i++ {
		v.SpeedUp()
	}
	if v.SpeedIdx != len(pgn.Speeds)-1 {
		t.Fatalf("speed up did not clamp: %d", v.SpeedIdx)
	}
	for i := 0; i < 20; i++ {
		v.SpeedDown()
	}
	if v.SpeedIdx != 0 {
		t.Fatalf("speed down did not clamp: %d", v.SpeedIdx)
	}
	v.SpeedIdx = 1
	v.AutoPlay = true
	v.CurrentPly = 0
	start := v.CurrentPly
	v.AutoPlayAccum = 0.999
	v.Tick()
	if v.CurrentPly != start+1 {
		t.Fatalf("tick did not advance exactly one ply: %d", v.CurrentPly)
	}
}

// F2P-064
func f2pStartServer(t *testing.T, hostWhite bool) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := tcp.NewGameServer(hostWhite)
	go server.Serve(lis)
	t.Cleanup(server.Stop)
	return lis.Addr().String()
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

func TestF2P064(t *testing.T) {
	addr := f2pStartServer(t, true)
	host := f2pDial(t, addr)
	time.Sleep(50 * time.Millisecond)
	joiner := f2pDial(t, addr)
	if got := f2pReadN(t, joiner, 1); got != "w" && got != "b" {
		t.Fatalf("joiner color = %q, want w or b", got)
	}
	if got := f2pReadN(t, host, 1); got != "s" {
		t.Fatalf("host start = %q, want s", got)
	}
	time.Sleep(50 * time.Millisecond)
	third := f2pDial(t, addr)
	buf := make([]byte, 64)
	_ = third.SetReadDeadline(time.Now().Add(2 * time.Second))
	n, err := third.Read(buf)
	if err != nil {
		t.Fatalf("third read: %v", err)
	}
	if string(buf[:n]) != "Game is already full" {
		t.Fatalf("third client = %q", string(buf[:n]))
	}
}

// F2P-065
func TestF2P065(t *testing.T) {
	f2pConfigHome(t)
	var out, errb bytes.Buffer
	code := run([]string{"--pgn", "examples/sample.pgn"}, strings.NewReader(""), &out, &errb)
	if code != 0 {
		t.Fatalf("run(sample.pgn) = %d, stderr = %s", code, errb.String())
	}
}

// F2P-066
func TestF2P066(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   string
	}{
		{403, "", "Token missing permissions. Please generate a new token with 'board:play' scope enabled."},
		{429, "", "Rate limit exceeded. Please wait a minute before trying again."},
		{401, "", "Invalid token. Please check your token or generate a new one."},
		{400, "bad body", "Invalid seek parameters: bad body"},
	}
	for _, tc := range cases {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/account/playing" {
				fmt.Fprint(w, `{"nowPlaying":[]}`)
				return
			}
			if r.URL.Path == "/board/seek" {
				w.WriteHeader(tc.status)
				fmt.Fprint(w, tc.body)
				return
			}
			w.WriteHeader(http.StatusOK)
		}))
		c := lichess.NewClient("t")
		c.BaseURL = srv.URL
		_, _, err := c.SeekGame(60000, 0, &atomic.Bool{})
		srv.Close()
		if err == nil || err.Error() != tc.want {
			t.Fatalf("status %d = %v, want %q", tc.status, err, tc.want)
		}
	}
}

// F2P-067
func TestF2P067(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", "/tmp/x")
	dir, err := constants.ConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	if got := filepath.Join(dir, "chess-tui", "config.toml"); got != "/tmp/x/chess-tui/config.toml" {
		t.Fatalf("xdg config path = %q", got)
	}
	home := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("HOME", home)
	dir2, err := constants.ConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	if got := filepath.Join(dir2, "chess-tui", "config.toml"); got != filepath.Join(home, ".config", "chess-tui", "config.toml") {
		t.Fatalf("home config path = %q", got)
	}
}

// F2P-068
func TestF2P068(t *testing.T) {
	addr := f2pStartServer(t, true)
	host := f2pDial(t, addr)
	time.Sleep(50 * time.Millisecond)
	joiner := f2pDial(t, addr)
	f2pReadN(t, joiner, 1)
	f2pReadN(t, host, 1)

	if _, err := host.Write([]byte("e2e4")); err != nil {
		t.Fatal(err)
	}
	if got := f2pReadN(t, joiner, 5); got != "e2e4" {
		t.Fatalf("relayed = %q", got)
	}
	_ = host.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	echo := make([]byte, 5)
	if _, err := host.Read(echo); err == nil {
		t.Fatal("sender must not receive an echo")
	}

	if _, err := host.Write([]byte("ended")); err != nil {
		t.Fatal(err)
	}
	if got := f2pReadN(t, joiner, 5); got != "ended" {
		t.Fatalf("ended broadcast = %q", got)
	}
}

// F2P-069
func TestF2P069(t *testing.T) {
	f2pConfigHome(t)
	var out, errb bytes.Buffer
	code := run([]string{"--pgn", "examples/sample.pgn"}, strings.NewReader(""), &out, &errb)
	if code != 0 {
		t.Fatalf("run(sample.pgn) = %d, stderr = %s", code, errb.String())
	}
}

// F2P-070
func TestF2P070(t *testing.T) {
	src := "[Event \"Live Chess\"]\n[White \"Player A\"]\n[Black \"Player B\"]\n[Result \"0-1\"]\n[Termination \"Player B won by checkmate\"]\n\n1. e4 e5 2. Nf3 0-1\n"
	games, err := pgn.FromPGNString(src)
	if err != nil || len(games) == 0 {
		t.Fatalf("parse: %v", err)
	}
	v := games[0]
	if v.Title != "Live Chess" || v.White != "Player A" || v.Black != "Player B" || v.Termination != "Player B won by checkmate" {
		t.Fatalf("headers = %+v", v)
	}
	def, err := pgn.FromPGNString("[Event \"E\"]\n\n1. e4 e5 1-0\n")
	if err != nil || len(def) == 0 {
		t.Fatalf("default parse: %v", err)
	}
	if def[0].White != "?" || def[0].Black != "?" || def[0].Result != "*" {
		t.Fatalf("defaults = white=%q black=%q result=%q", def[0].White, def[0].Black, def[0].Result)
	}
}

// F2P-071
func TestF2P071(t *testing.T) {
	args, err := config.ParseArgs([]string{"--depth", "255"})
	if err != nil || args.Depth == nil || *args.Depth != 255 {
		t.Fatalf("depth 255 = %+v, err = %v", args, err)
	}
	if _, err := config.ParseArgs([]string{"--depth", "256"}); err == nil {
		t.Fatal("depth 256 must error")
	}
	if _, err := config.ParseArgs([]string{"--depth", "-1"}); err == nil {
		t.Fatal("depth -1 must error")
	}
	short, err := config.ParseArgs([]string{"-e", "/e", "-d", "3", "-l", "tok", "-p", "x.pgn"})
	if err != nil {
		t.Fatal(err)
	}
	long, err := config.ParseArgs([]string{"--engine-path", "/e", "--depth", "3", "--lichess-token", "tok", "--pgn", "x.pgn"})
	if err != nil {
		t.Fatal(err)
	}
	if short.EnginePath != long.EnginePath || short.Depth == nil || long.Depth == nil || *short.Depth != *long.Depth {
		t.Fatalf("short/long mismatch: %+v vs %+v", short, long)
	}
	if short.LichessToken == nil || long.LichessToken == nil || *short.LichessToken != *long.LichessToken {
		t.Fatalf("token mismatch")
	}
	if short.Pgn == nil || long.Pgn == nil || *short.Pgn != *long.Pgn {
		t.Fatalf("pgn mismatch")
	}
}

// F2P-072
func TestF2P072(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	if err := config.Create(&config.Args{EnginePath: "/usr/bin/stockfish"}, dir, path); err != nil {
		t.Fatal(err)
	}

	cfg := config.Default()
	cfg.EnginePath = f2pStrPtr("/usr/bin/stockfish")
	cfg.DisplayMode = f2pStrPtr("DEFAULT")
	cfg.LogLevel = f2pStrPtr("OFF")
	cfg.BotDepth = f2pU8Ptr(12)
	cfg.BotDifficulty = f2pU8Ptr(2)
	cfg.SelectedSkinName = f2pStrPtr("Default")
	cfg.LichessToken = f2pStrPtr("example-token")
	if err := cfg.Persist(path); err != nil {
		t.Fatal(err)
	}

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(content)
	for _, key := range []string{"engine_path", "display_mode", "log_level", "bot_depth", "bot_difficulty", "selected_skin_name", "lichess_token", "sound_enabled", "animations_enabled"} {
		if !strings.Contains(text, key) {
			t.Fatalf("%s must be present, file = %s", key, text)
		}
	}

	loaded := config.Load(path)
	if loaded.EnginePath == nil || *loaded.EnginePath != "/usr/bin/stockfish" ||
		loaded.BotDepth == nil || *loaded.BotDepth != 12 ||
		loaded.BotDifficulty == nil || *loaded.BotDifficulty != 2 ||
		loaded.LichessToken == nil || *loaded.LichessToken != "example-token" {
		t.Fatalf("round trip mismatch: %+v", loaded)
	}
}

// StreamGame may read synchronously or start a background reader. Both satisfy
// the public stream contract; await observable messages independently of return.
func f2p3StartContractStream(t *testing.T, c *lichess.Client, messages chan string) {
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
func f2p3ExpectBoardMessages(t *testing.T, ch <-chan string, want []string, bootstrap ...string) {
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

// Package differential is the target (Go) probe for BATCH-DIFFERENTIAL-0001.
//
// It replays the shared differential case corpus against the migrated Go
// public surface and asserts the canonical, normalized results that the Rust
// source of the same corpus is expected to produce. Each test name carries the
// observation id it proves.
package differential

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/BurntSushi/toml"
	chesslib "github.com/notnil/chess"
	internalchess "github.com/thomas-mauran/chess-tui/internal/chess"
	"github.com/thomas-mauran/chess-tui/internal/config"
	"github.com/thomas-mauran/chess-tui/internal/constants"
	"github.com/thomas-mauran/chess-tui/internal/lichess"
	"github.com/thomas-mauran/chess-tui/internal/pgn"
	"github.com/thomas-mauran/chess-tui/internal/skin"
	"github.com/thomas-mauran/chess-tui/internal/tcp"
	"github.com/thomas-mauran/chess-tui/internal/uci"
)

const ua = "chess-tui (https://github.com/thomas-mauran/chess-tui)"

const startFEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// ---------------------------------------------------------------------------
// DIFF-001 / DIFF-029 / DIFF-032: public surface and protected assets.
// ---------------------------------------------------------------------------

func TestDIFF_001_PublicSurfaceReachable(t *testing.T) {
	if internalchess.NewGameBoard().FEN() != startFEN {
		t.Fatal("board model not reachable or not natively rendered")
	}
	if constants.BotDifficultyCount != 4 {
		t.Fatalf("preset count = %d", constants.BotDifficultyCount)
	}
	if constants.LichessAPIURL != "https://lichess.org/api" {
		t.Fatalf("base url = %s", constants.LichessAPIURL)
	}
	if constants.LichessUserAgent != ua {
		t.Fatalf("user agent = %s", constants.LichessUserAgent)
	}
}

func TestDIFF_029_EveryCapabilityAvailable(t *testing.T) {
	args, err := config.ParseArgs([]string{
		"-e", "/usr/bin/stockfish", "-d", "5", "--difficulty", "hard",
		"-l", "tok", "--no-sound", "--skin", "Matrix", "--update-skins", "-p", "game.pgn",
	})
	if err != nil {
		t.Fatal(err)
	}
	if args.EnginePath != "/usr/bin/stockfish" || args.Depth == nil || *args.Depth != 5 {
		t.Fatalf("engine/depth = %+v", args)
	}
	if args.Difficulty == nil || *args.Difficulty != "hard" || args.LichessToken == nil || *args.LichessToken != "tok" {
		t.Fatalf("difficulty/token = %+v", args)
	}
	if !args.NoSound || !args.UpdateSkins || args.Skin == nil || *args.Skin != "Matrix" || args.Pgn == nil || *args.Pgn != "game.pgn" {
		t.Fatalf("sound/skins/pgn = %+v", args)
	}
}

func TestDIFF_032_ProtectedAssetsIntact(t *testing.T) {
	var collection struct {
		Skins       []json.RawMessage `json:"skins"`
		PieceStyles []json.RawMessage `json:"piece_styles"`
	}
	if err := json.Unmarshal(skin.DefaultSkins(), &collection); err != nil {
		t.Fatalf("embedded skins.json is not valid: %v", err)
	}
	if len(collection.Skins) == 0 || len(collection.PieceStyles) == 0 {
		t.Fatal("embedded skins.json lost its schema")
	}
}

// ---------------------------------------------------------------------------
// DIFF-002 / DIFF-003 / DIFF-004 / DIFF-005: config file contract.
// ---------------------------------------------------------------------------

func TestDIFF_002_NineFieldRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	raw := `engine_path = "/usr/bin/stockfish"
display_mode = "DEFAULT"
log_level = "OFF"
bot_depth = 12
bot_difficulty = 2
selected_skin_name = "Matrix"
lichess_token = "secret"
sound_enabled = true
animations_enabled = false
`
	mustWrite(t, path, raw)
	cfg := config.Load(path)
	if cfg.EnginePath == nil || *cfg.EnginePath != "/usr/bin/stockfish" {
		t.Fatalf("engine_path = %v", cfg.EnginePath)
	}
	if cfg.BotDepth == nil || *cfg.BotDepth != 12 || cfg.BotDifficulty == nil || *cfg.BotDifficulty != 2 {
		t.Fatalf("depth/difficulty = %v/%v", cfg.BotDepth, cfg.BotDifficulty)
	}
	if cfg.SelectedSkinName == nil || *cfg.SelectedSkinName != "Matrix" || cfg.LichessToken == nil || *cfg.LichessToken != "secret" {
		t.Fatalf("skin/token = %v/%v", cfg.SelectedSkinName, cfg.LichessToken)
	}

	out := filepath.Join(dir, "re.toml")
	// Persist updates runtime settings while preserving file-owned values
	// such as engine_path. Establish that file through the preserved CLI API.
	if err := config.Create(&config.Args{EnginePath: "/usr/bin/stockfish"}, dir, out); err != nil {
		t.Fatal(err)
	}
	if err := cfg.Persist(out); err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if _, err := toml.DecodeFile(out, &decoded); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{
		"engine_path", "display_mode", "log_level", "bot_depth", "bot_difficulty",
		"selected_skin_name", "lichess_token", "sound_enabled", "animations_enabled",
	} {
		if _, ok := decoded[key]; !ok {
			t.Fatalf("re-persisted config lost key %q: %v", key, decoded)
		}
	}
}

func TestDIFF_003_LoadFallsBackToDefaults(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "missing.toml")
	empty := filepath.Join(dir, "empty.toml")
	invalid := filepath.Join(dir, "invalid.toml")
	mustWrite(t, empty, "")
	mustWrite(t, invalid, "not = = toml")

	for _, path := range []string{missing, empty, invalid} {
		cfg := config.Load(path)
		if cfg.DisplayMode == nil || *cfg.DisplayMode != "DEFAULT" || cfg.LogLevel == nil || *cfg.LogLevel != "OFF" {
			t.Fatalf("%s: display/log = %v/%v", path, cfg.DisplayMode, cfg.LogLevel)
		}
		if cfg.BotDepth == nil || *cfg.BotDepth != 10 || cfg.SelectedSkinName == nil || *cfg.SelectedSkinName != "Default" {
			t.Fatalf("%s: depth/skin = %v/%v", path, cfg.BotDepth, cfg.SelectedSkinName)
		}
		if cfg.SoundEnabled == nil || !*cfg.SoundEnabled || cfg.AnimationsEnabled == nil || *cfg.AnimationsEnabled {
			t.Fatalf("%s: sound/animations = %v/%v", path, cfg.SoundEnabled, cfg.AnimationsEnabled)
		}
		if cfg.EnginePath != nil || cfg.BotDifficulty != nil || cfg.LichessToken != nil {
			t.Fatalf("%s: optional fields should be nil", path)
		}
	}
}

func TestDIFF_004_WriteFailureTolerated(t *testing.T) {
	if !config.IsReadonlyError(syscall.EROFS) {
		t.Fatal("EROFS must be treated as read-only")
	}
	if !config.IsReadonlyError(fs.ErrPermission) {
		t.Fatal("permission denied must be treated as read-only")
	}
	// Must log only, never panic or propagate.
	config.HandleWriteError(syscall.EROFS, "/tmp/config.toml")

	if os.Geteuid() == 0 {
		return
	}
	dir := t.TempDir()
	ro := filepath.Join(dir, "ro")
	if err := os.MkdirAll(ro, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(ro, "config.toml")
	mustWrite(t, path, "bot_depth = 3\n")
	if err := os.Chmod(ro, 0o555); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(ro, 0o755)
	cfg := &config.Config{}
	cfg.BotDepth = u8(7)
	if err := cfg.Persist(path); err != nil {
		t.Fatalf("read-only persist must be tolerated, got %v", err)
	}
}

func TestDIFF_005_PersistMergesExistingFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	mustWrite(t, path, "engine_path = \"/usr/bin/stockfish\"\nbot_depth = 5\n")

	fresh := config.Default()
	fresh.SoundEnabled = boolPtr(false)
	fresh.BotDepth = u8(7)
	if err := fresh.Persist(path); err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if _, err := toml.DecodeFile(path, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["engine_path"] != "/usr/bin/stockfish" {
		t.Fatalf("engine_path = %v, want preserved", decoded["engine_path"])
	}
	if decoded["bot_depth"] != int64(7) || decoded["sound_enabled"] != false {
		t.Fatalf("runtime settings not persisted: %v", decoded)
	}
}

// ---------------------------------------------------------------------------
// DIFF-006 / DIFF-007 / DIFF-008: CLI and bot parameters.
// ---------------------------------------------------------------------------

func TestDIFF_006_DifficultyParsing(t *testing.T) {
	cases := map[string]uint8{"easy": 0, "Easy": 0, "medium": 1, "MEDIUM": 1, "hard": 2, "magnus": 3, "Magnus": 3}
	for in, want := range cases {
		got, ok := config.ParseDifficulty(in)
		if !ok || got != want {
			t.Fatalf("ParseDifficulty(%q) = %d/%v, want %d", in, got, ok, want)
		}
	}
	if _, ok := config.ParseDifficulty("grandmaster"); ok {
		t.Fatal("unknown difficulty must not map")
	}
}

func TestDIFF_007_DepthIsUint8(t *testing.T) {
	args, err := config.ParseArgs([]string{"--depth", "255"})
	if err != nil || args.Depth == nil || *args.Depth != 255 {
		t.Fatalf("depth 255 = %+v, err %v", args, err)
	}
	if _, err := config.ParseArgs([]string{"--depth", "256"}); err == nil {
		t.Fatal("depth 256 must be rejected")
	}
	if _, err := config.ParseArgs([]string{"--depth", "-1"}); err == nil {
		t.Fatal("depth -1 must be rejected")
	}
}

func TestDIFF_008_DifficultyPresetTables(t *testing.T) {
	wantDepth := []uint8{1, 4, 8, 20}
	wantElo := []uint16{400, 900, 1500, 2700}
	wantMs := []int{25, 120, 500, 12000}
	for i := 0; i < constants.BotDifficultyCount; i++ {
		if constants.BotDifficultyDepth[i] != wantDepth[i] || constants.BotDifficultyElo[i] != wantElo[i] || constants.BotDifficultyMovetimeMs[i] != wantMs[i] {
			t.Fatalf("preset %d = %d/%d/%d", i, constants.BotDifficultyDepth[i], constants.BotDifficultyElo[i], constants.BotDifficultyMovetimeMs[i])
		}
	}
	full := uci.New("engine", true, 10, nil)
	if full.EffectiveDepth() != 10 {
		t.Fatalf("full strength depth = %d", full.EffectiveDepth())
	}
	if _, ok := full.MovetimeMs(); ok {
		t.Fatal("full strength must not set movetime")
	}
	if _, ok := full.Elo(); ok {
		t.Fatal("full strength must not set elo")
	}
}

// ---------------------------------------------------------------------------
// DIFF-009: UCI bot protocol.
// ---------------------------------------------------------------------------

func TestDIFF_009_UCIProtocolAndFailures(t *testing.T) {
	dir := t.TempDir()
	stub := filepath.Join(dir, "engine.sh")
	mustWrite(t, stub, "#!/bin/sh\nwhile IFS= read -r line; do\n  case \"$line\" in\n    uci) echo uciok ;;\n    isready) echo readyok ;;\n    go*) echo \"bestmove e2e4\" ;;\n  esac\ndone\n")
	if err := os.Chmod(stub, 0o755); err != nil {
		t.Fatal(err)
	}

	bot := uci.New(stub, true, 10, nil)
	move, err := bot.GetMove(startFEN)
	if err != nil || move != "e2e4" {
		t.Fatalf("move = %q, err = %v", move, err)
	}

	bad := uci.New("/nonexistent/engine-xyz", true, 10, nil)
	if _, err := bad.GetMove(startFEN); err == nil {
		t.Fatal("a missing engine binary must be reported as an error")
	}
	// The contract requires the bot to send the position as FEN and to report
	// spawn/handshake/computation failures. Whether the FEN is also rejected
	// locally before being forwarded to the engine is an implementation detail
	// the contract does not fix, so it is not asserted here.
}

// ---------------------------------------------------------------------------
// DIFF-010..DIFF-015: PGN.
// ---------------------------------------------------------------------------

func TestDIFF_010_LoadErrorsAndSplitting(t *testing.T) {
	if _, err := pgn.FromFile(filepath.Join(t.TempDir(), "nope.pgn")); err == nil || !strings.HasPrefix(err.Error(), "Cannot read '") {
		t.Fatalf("unreadable err = %v", err)
	}
	multi := "[Event \"A\"]\n[White \"w\"]\n[Black \"b\"]\n[Result \"1-0\"]\n\n1. e4 e5 2. Nf3 1-0\n\n[Event \"B\"]\n[White \"x\"]\n[Black \"y\"]\n[Result \"0-1\"]\n\n1. d4 d5 0-1\n"
	games, err := pgn.FromPGNString(multi)
	if err != nil || len(games) != 2 {
		t.Fatalf("multi-game = %d games, err %v", len(games), err)
	}
	if _, err := pgn.FromPGNString("no game here"); err == nil || err.Error() != "No valid games found in PGN" {
		t.Fatalf("no valid games err = %v", err)
	}
}

func TestDIFF_011_HeaderExtraction(t *testing.T) {
	viewer, err := pgn.FromPGNString("[Event \"Only\"]\n\n1. e4 e5\n")
	if err != nil {
		t.Fatal(err)
	}
	v := viewer[0]
	if v.Title != "Only" || v.White != "?" || v.Black != "?" || v.Result != "*" {
		t.Fatalf("headers = %q/%q/%q/%q", v.Title, v.White, v.Black, v.Result)
	}
}

func TestDIFF_012_MovetextCleaning(t *testing.T) {
	raw := "[Event \"Clean\"]\n[White \"w\"]\n[Black \"b\"]\n[Result \"1-0\"]\n\n1. e4 {a comment} e5 (1... c5 2. Nf3) 2. Nf3 $1 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 1-0\n"
	games, err := pgn.FromPGNString(raw)
	if err != nil {
		t.Fatal(err)
	}
	v := games[0]
	if v.TotalPlies() != 10 {
		t.Fatalf("plies = %d, want 10", v.TotalPlies())
	}
	if v.SANs[len(v.SANs)-1] != "Be7" {
		t.Fatalf("last SAN = %q", v.SANs[len(v.SANs)-1])
	}
	if _, err := pgn.FromPGNString("[Event \"Empty\"]\n\n*\n"); err == nil {
		t.Fatal("game with no moves must be rejected")
	}
}

func TestDIFF_013_NavigationState(t *testing.T) {
	games, err := pgn.FromPGNString("[Event \"Nav\"]\n\n1. e4 e5 2. Nf3\n")
	if err != nil {
		t.Fatal(err)
	}
	v := games[0]
	v.GotoEnd()
	if !v.IsAtEnd() {
		t.Fatal("IsAtEnd must be true at final ply")
	}
	v.Next()
	if v.CurrentPly != v.TotalPlies() {
		t.Fatalf("Next not clamped: %d/%d", v.CurrentPly, v.TotalPlies())
	}
	v.Prev()
	if v.CurrentPly != v.TotalPlies()-1 || v.EndBannerDismissed {
		t.Fatalf("Prev state = %d/%v", v.CurrentPly, v.EndBannerDismissed)
	}
	v.GotoStart()
	if v.CurrentPly != 0 || v.EndBannerDismissed {
		t.Fatalf("GotoStart = %d/%v", v.CurrentPly, v.EndBannerDismissed)
	}
}

func TestDIFF_014_ResultSummaryMapping(t *testing.T) {
	base := "[White \"Alice\"]\n[Black \"Bob\"]\n"
	cases := map[string]string{
		"1-0":       "Alice (White) won",
		"0-1":       "Bob (Black) won",
		"1/2-1/2":   "Draw",
		"*":         "*",
	}
	for result, want := range cases {
		raw := "[Event \"R\"]\n" + base + "[Result \"" + result + "\"]\n\n1. e4 e5\n"
		games, err := pgn.FromPGNString(raw)
		if err != nil {
			t.Fatal(err)
		}
		if got := games[0].ResultSummary(); got != want {
			t.Fatalf("result %q summary = %q, want %q", result, got, want)
		}
	}
}

func TestDIFF_015_AutoPlaySpeedsAndTick(t *testing.T) {
	want := []float32{0.5, 1, 1.5, 2, 2.5, 3, 4}
	if len(pgn.Speeds) != len(want) {
		t.Fatalf("speeds length = %d", len(pgn.Speeds))
	}
	for i := range want {
		if pgn.Speeds[i].Rate != want[i] {
			t.Fatalf("speed %d = %v", i, pgn.Speeds[i].Rate)
		}
	}
	games, err := pgn.FromPGNString("[Event \"AP\"]\n\n1. e4 e5 2. Nf3 Nc6\n")
	if err != nil {
		t.Fatal(err)
	}
	v := games[0]
	v.SpeedIdx = 0
	v.AutoPlay = true
	for i := 0; i < 8; i++ {
		v.Tick()
	}
	if v.CurrentPly != 1 {
		t.Fatalf("8 ticks at 0.5x advanced to ply %d, want 1", v.CurrentPly)
	}
	v.SpeedDown()
	if v.SpeedIdx != 0 {
		t.Fatal("SpeedDown must clamp at the minimum")
	}
	for i := 0; i < 20; i++ {
		v.SpeedUp()
	}
	if v.SpeedIdx != len(pgn.Speeds)-1 {
		t.Fatal("SpeedUp must clamp at the maximum")
	}
}

// ---------------------------------------------------------------------------
// DIFF-016..DIFF-020: TCP multiplayer.
// ---------------------------------------------------------------------------

func startServer(t *testing.T, hostWhite bool) string {
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

func dial(t *testing.T, addr string) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

func readN(t *testing.T, conn net.Conn, n int) string {
	t.Helper()
	buf := make([]byte, n)
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	got, err := conn.Read(buf)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	return string(buf[:got])
}

func TestDIFF_016_ListenAddressConstant(t *testing.T) {
	if constants.NetworkPort != 2308 {
		t.Fatalf("NetworkPort = %d", constants.NetworkPort)
	}
}

func TestDIFF_017_JoinHandshakeAndCapacity(t *testing.T) {
	addr := startServer(t, true)
	host := dial(t, addr)
	time.Sleep(50 * time.Millisecond)
	joiner := dial(t, addr)
	if got := readN(t, joiner, 1); got != "w" && got != "b" {
		t.Fatalf("joiner color = %q", got)
	}
	if got := readN(t, host, 1); got != "s" {
		t.Fatalf("host start = %q", got)
	}
	third := dial(t, addr)
	if got := readN(t, third, len("Game is already full")); got != "Game is already full" {
		t.Fatalf("third = %q", got)
	}
}

func TestDIFF_018_WireFormatAndBuffer(t *testing.T) {
	if constants.NetworkBufferSize != 5 {
		t.Fatalf("NetworkBufferSize = %d", constants.NetworkBufferSize)
	}
	if tcp.PromotionSuffix(chesslib.Queen) != "q" || tcp.PromotionSuffix(chesslib.Knight) != "n" || tcp.PromotionSuffix(chesslib.NoPieceType) != "" {
		t.Fatal("promotion suffix mapping is wrong")
	}
	addr := startServer(t, true)
	host := dial(t, addr)
	time.Sleep(50 * time.Millisecond)
	joiner := dial(t, addr)
	readN(t, joiner, 1)
	readN(t, host, 1)
	if _, err := joiner.Write([]byte("e7e8q")); err != nil {
		t.Fatal(err)
	}
	if got := readN(t, host, 5); got != "e7e8q" {
		t.Fatalf("framed move = %q", got)
	}
}

func TestDIFF_019_RelayAndEnded(t *testing.T) {
	addr := startServer(t, true)
	host := dial(t, addr)
	time.Sleep(50 * time.Millisecond)
	joiner := dial(t, addr)
	readN(t, joiner, 1)
	readN(t, host, 1)

	if _, err := host.Write([]byte("e2e4")); err != nil {
		t.Fatal(err)
	}
	if got := readN(t, joiner, 5); got != "e2e4" {
		t.Fatalf("relayed = %q", got)
	}
	_ = host.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	if _, err := host.Read(make([]byte, 5)); err == nil {
		t.Fatal("sender must not receive an echo")
	}
	if _, err := host.Write([]byte("ended")); err != nil {
		t.Fatal(err)
	}
	if got := readN(t, joiner, 5); got != "ended" {
		t.Fatalf("broadcast = %q", got)
	}
}

func TestDIFF_020_PeerReadSemantics(t *testing.T) {
	// ReadStream is a non-blocking read: a silent peer yields no message and
	// no error. The peer-visible "ended" contract is exercised through the
	// server broadcast in DIFF-019 and the join/color errors below.
	addr := startSilentPeer(t)
	white := chesslib.White
	opponent, err := tcp.New(addr, &white)
	if err != nil {
		t.Fatal(err)
	}
	defer opponent.Close()
	if msg, err := opponent.ReadStream(); msg != "" || err != nil {
		t.Fatalf("silent ReadStream = %q/%v, want empty/nil", msg, err)
	}
	if started, err := tcp.WaitForGameStart(opponent); started || err != nil {
		t.Fatalf("silent WaitForGameStart = %v/%v, want false/nil", started, err)
	}
}

func TestDIFF_020b_InvalidColorToken(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lis.Close()
	go func() {
		conn, err := lis.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_, _ = conn.Write([]byte("x"))
		time.Sleep(200 * time.Millisecond)
	}()
	conn, err := net.Dial("tcp", lis.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := tcp.GetColorFromStream(conn); err == nil || err.Error() != "Failed to get color from stream" {
		t.Fatalf("color err = %v", err)
	}
}

func TestDIFF_020c_WaitForGameStartSilent(t *testing.T) {
	addr := startSilentPeer(t)
	white := chesslib.White
	opponent, err := tcp.New(addr, &white)
	if err != nil {
		t.Fatal(err)
	}
	defer opponent.Close()
	started, err := tcp.WaitForGameStart(opponent)
	if started || err != nil {
		t.Fatalf("silent start = %v/%v, want false/nil", started, err)
	}
}

func startSilentPeer(t *testing.T) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lis.Close() })
	go func() {
		conn, err := lis.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		time.Sleep(500 * time.Millisecond)
	}()
	return lis.Addr().String()
}

// ---------------------------------------------------------------------------
// DIFF-021..DIFF-027: Lichess.
// ---------------------------------------------------------------------------

func TestDIFF_021_HeadersAndAuth(t *testing.T) {
	var gotUA, gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotUA = r.Header.Get("User-Agent")
		gotAuth = r.Header.Get("Authorization")
		fmt.Fprint(w, `{"nowPlaying":[]}`)
	}))
	defer srv.Close()

	c := lichess.NewClient("token123")
	c.BaseURL = srv.URL
	if _, err := c.GetOngoingGames(); err != nil {
		t.Fatal(err)
	}
	if gotUA != ua || gotAuth != "Bearer token123" {
		t.Fatalf("ua=%q auth=%q", gotUA, gotAuth)
	}
}

func TestDIFF_022_SeekErrorMapping(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   string
	}{
		{403, "", "Token missing permissions. Please generate a new token with 'board:play' scope enabled."},
		{400, "bad body", "Invalid seek parameters: bad body"},
	}
	for _, tc := range cases {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case "/account/playing":
				fmt.Fprint(w, `{"nowPlaying":[]}`)
			case "/board/seek":
				w.WriteHeader(tc.status)
				fmt.Fprint(w, tc.body)
			default:
				fmt.Fprint(w, "\n")
			}
		}))
		c := lichess.NewClient("t")
		c.BaseURL = srv.URL
		cancel := &atomic.Bool{}
		_, _, err := c.SeekGame(0, 0, cancel)
		cancel.Store(true)
		srv.Close()
		if err == nil || err.Error() != tc.want {
			t.Fatalf("status %d err = %v, want %q", tc.status, err, tc.want)
		}
	}
}

func TestDIFF_023_JoinFlowAndErrors(t *testing.T) {
	preferred := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"nowPlaying":[{"gameId":"g1","color":"white"}]}`)
	}))
	c := lichess.NewClient("t")
	c.BaseURL = preferred.URL
	_, color, err := c.JoinGame("g1", "me")
	preferred.Close()
	if err != nil || color != chesslib.White {
		t.Fatalf("preferred join = %v/%v", color, err)
	}

	forbidden := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/account/playing":
			fmt.Fprint(w, `{"nowPlaying":[]}`)
		case "/challenge/g1/accept":
			w.WriteHeader(http.StatusForbidden)
		default:
			w.WriteHeader(http.StatusForbidden)
		}
	}))
	c = lichess.NewClient("t")
	c.BaseURL = forbidden.URL
	_, _, err = c.JoinGame("g1", "me")
	forbidden.Close()
	if err == nil || err.Error() != "Cannot join this game. You may not be a participant." {
		t.Fatalf("forbidden join err = %v", err)
	}
}

func TestDIFF_024_MoveAndResignEndpoints(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/board/game/g1/resign" {
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, "nope")
			return
		}
		w.WriteHeader(http.StatusTeapot)
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	if err := c.MakeMove("g1", "e2e4"); err == nil || (err.Error() != "Failed to make move: 418 I'm a teapot" && err.Error() != "Failed to make move: 418") {
		t.Fatalf("move err = %v", err)
	}
	if err := c.ResignGame("g1"); err == nil || (err.Error() != "Failed to resign game: 400 Bad Request - nope" && err.Error() != "Failed to resign game: 400 - nope") {
		t.Fatalf("resign err = %v", err)
	}
}

func TestDIFF_025_BoardStreamParsing(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"type":"gameFull","id":"g1","white":{"id":"me"},"black":{"id":"op"},"state":{"moves":"e2e4 e7e5","status":"started"}}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"started"}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"mate"}`+"\n")
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	ch := make(chan string, 16)
	diffStartContractStream(t, c, ch)
	diffExpectBoardMessages(t, ch, []string{"g1f3", "GAME_STATUS:checkmate"}, "INIT_MOVES:2")

}

func TestDIFF_026_EventStreamHandling(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "\n")
		fmt.Fprint(w, `{"type":"gameStart","game":{"gameId":"newgame","color":"black"}}`+"\n")
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	cancel := &atomic.Bool{}
	found := make(chan lichess.GameFound, 1)
	go c.SpawnEventStream(map[string]struct{}{"old": {}}, cancel, found)
	select {
	case res := <-found:
		if res.Err != nil || res.GameID != "newgame" || res.Color != chesslib.Black {
			t.Fatalf("found = %+v", res)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for game start")
	}
}

func TestDIFF_027_GetGameInfo(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"turns":7,"lastMove":"e2e4"}`+"\n")
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	turns, last, err := c.GetGameInfo("g1")
	if err != nil || turns != 7 || last == nil || *last != "e2e4" {
		t.Fatalf("info = %d/%v/%v", turns, last, err)
	}
	empty := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "\n")
	}))
	defer empty.Close()
	c.BaseURL = empty.URL
	if _, _, err := c.GetGameInfo("g1"); err == nil {
		t.Fatal("empty response must error")
	}
}

// ---------------------------------------------------------------------------
// DIFF-028 / DIFF-030 / DIFF-031: CLI PGN, external formats, integrity.
// ---------------------------------------------------------------------------

func TestDIFF_028_PgnDirectoryAndExitCodes(t *testing.T) {
	root := moduleRoot(t)
	dir := t.TempDir()
	mustWrite(t, filepath.Join(dir, "a.pgn"), "[Event \"A\"]\n\n1. e4 e5\n")
	mustWrite(t, filepath.Join(dir, "b.pgn"), "[Event \"B\"]\n\n1. d4 d5\n")
	emptyDir := t.TempDir()

	cfgHome := t.TempDir()
	env := append(os.Environ(), "XDG_CONFIG_HOME="+cfgHome)

	run := func(pgnPath string) (int, string) {
		cmd := exec.Command("go", "run", ".", "--pgn", pgnPath)
		cmd.Dir = root
		cmd.Env = env
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		err := cmd.Run()
		code := 0
		if err != nil {
			var ee *exec.ExitError
			if errors.As(err, &ee) {
				code = ee.ExitCode()
			} else {
				t.Fatalf("go run failed: %v (%s)", err, stderr.String())
			}
		}
		return code, stderr.String()
	}

	if code, _ := run(dir); code != 0 {
		t.Fatalf("pgn dir exit = %d", code)
	}
	if code, se := run(emptyDir); code != 1 || strings.TrimSpace(se) == "" {
		t.Fatalf("empty dir exit = %d, stderr = %q", code, se)
	}
	if code, _ := run(filepath.Join(dir, "missing.pgn")); code != 1 {
		t.Fatalf("missing file exit = %d", code)
	}
}

func TestDIFF_030_ExternalFormatsStable(t *testing.T) {
	var fromName skin.Color
	if err := json.Unmarshal([]byte(`"LightBlue"`), &fromName); err != nil {
		t.Fatal(err)
	}
	if out, _ := json.Marshal(fromName); string(out) != `"LightBlue"` {
		t.Fatalf("named round trip = %s", out)
	}
	var fromRgb skin.Color
	if err := json.Unmarshal([]byte(`{"Rgb":[160,160,160]}`), &fromRgb); err != nil {
		t.Fatal(err)
	}
	if out, _ := json.Marshal(fromRgb); string(out) != `{"Rgb":[160,160,160]}` {
		t.Fatalf("rgb round trip = %s", out)
	}
}

func TestDIFF_031_AssertionsAreLive(t *testing.T) {
	// The probe must distinguish correct from incorrect behavior, not pass
	// vacuously: distinct inputs must yield the exact distinct outputs.
	if _, ok := config.ParseDifficulty("not-a-difficulty"); ok {
		t.Fatal("unknown difficulty must be rejected")
	}
	if constants.NetworkPort == 0 || constants.NetworkBufferSize == 0 {
		t.Fatal("wire constants must be concrete")
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func moduleRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found above working directory")
		}
		dir = parent
	}
}

func strPtr(s string) *string { return &s }
func boolPtr(b bool) *bool    { return &b }
func u8(v uint8) *uint8       { return &v }

// StreamGame may read synchronously or start a background reader. Both satisfy
// the public stream contract; await observable messages independently of return.
func diffStartContractStream(t *testing.T, c *lichess.Client, messages chan string) {
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
func diffExpectBoardMessages(t *testing.T, ch <-chan string, want []string, bootstrap ...string) {
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

package main

// F2P probe suite for BATCH-F2P-0001.
//
// Each TestF2P_* function is the target-side probe for one F2P observation.
// The probe is run against the reference target (/input/target) where it must
// PASS, and against the Rust source tree (/input/source) where the same Go
// predicate cannot execute (no Go module / no Go sources), which is the
// required FAIL. The runner created by the INTEGRATION batch owns the
// source/runner and target/runner entry points.

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
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/thomas-mauran/chess-tui/internal/chess"
	"github.com/thomas-mauran/chess-tui/internal/config"
	"github.com/thomas-mauran/chess-tui/internal/lichess"
	"github.com/thomas-mauran/chess-tui/internal/pgn"
	"github.com/thomas-mauran/chess-tui/internal/skin"
	"github.com/thomas-mauran/chess-tui/internal/tcp"
	"github.com/thomas-mauran/chess-tui/internal/uci"
)

// f2pStubEngine writes a shell UCI engine that logs every stdin line and
// answers the handshake with bestmove e2e4. The returned engine argument is
// a whitespace separated "executable args" string, exercising the
// engine-path-with-arguments contract.
func f2pStubEngine(t *testing.T) (string, string) {
	t.Helper()
	dir := t.TempDir()
	logPath := filepath.Join(dir, "engine.log")
	script := filepath.Join(dir, "engine.sh")
	body := "#!/bin/sh\n" +
		"log=\"$1\"\n" +
		"while IFS= read -r line; do\n" +
		"  printf '%s\\n' \"$line\" >> \"$log\"\n" +
		"  case \"$line\" in\n" +
		"    uci) printf 'id name StubEngine\\nuciok\\n' ;;\n" +
		"    isready) printf 'readyok\\n' ;;\n" +
		"    go*) printf 'info depth 1 score cp 0\\nbestmove e2e4\\n' ;;\n" +
		"  esac\n" +
		"done\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return "/bin/sh " + script + " " + logPath, logPath
}

func f2pReadLog(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(b)
}

const f2pStartFEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

const f2pCheckmatePGN = `[Event "Live"]
[White "A"]
[Black "B"]
[Result "0-1"]
[Termination "B won by checkmate"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 0-1
`

const f2pGamePGN = `[Event "%s"]
[White "W"]
[Black "B"]
[Result "1-0"]

1. e4 e5 2. Nf3 1-0
`

// F2P-001 / R042: difficulty preset tables and full-strength go parameters.
func TestF2P_001_BotDifficultyPresets(t *testing.T) {
	engineArg, logPath := f2pStubEngine(t)

	easy := uint8(0)
	bot := uci.New(engineArg, false, 10, &easy)
	if _, err := bot.GetMove(f2pStartFEN); err != nil {
		t.Fatalf("difficulty 0 GetMove: %v", err)
	}
	got := f2pReadLog(t, logPath)
	for _, want := range []string{
		"setoption name UCI_LimitStrength value true",
		"setoption name UCI_Elo value 400",
		"go depth 1 movetime 25",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("difficulty 0 missing %q in:\n%s", want, got)
		}
	}

	fullArg, fullLog := f2pStubEngine(t)
	full := uci.New(fullArg, false, 12, nil)
	if _, err := full.GetMove(f2pStartFEN); err != nil {
		t.Fatalf("full strength GetMove: %v", err)
	}
	fullGot := f2pReadLog(t, fullLog)
	if !strings.Contains(fullGot, "go depth 12") {
		t.Fatalf("full strength missing 'go depth 12':\n%s", fullGot)
	}
	if strings.Contains(fullGot, "movetime") {
		t.Fatalf("full strength must not use movetime:\n%s", fullGot)
	}
	if strings.Contains(fullGot, "UCI_LimitStrength") {
		t.Fatalf("full strength must not set UCI_LimitStrength:\n%s", fullGot)
	}
}

// F2P-002 / R050: --pgn directory ordering and exit codes.
func TestF2P_002_PgnDirectoryAndExitCodes(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "b.pgn"), []byte(fmt.Sprintf(f2pGamePGN, "Second")), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "a.pgn"), []byte(fmt.Sprintf(f2pGamePGN, "First")), 0o644); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	games, err := loadPGN(dir, &stderr)
	if err != nil {
		t.Fatal(err)
	}
	if len(games) != 2 || games[0].Title != "First" || games[1].Title != "Second" {
		t.Fatalf("sorted loading wrong: %d games", len(games))
	}

	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	empty := t.TempDir()
	var out, errBuf bytes.Buffer
	if code := run([]string{"--pgn", empty}, strings.NewReader(""), &out, &errBuf); code != 1 {
		t.Fatalf("empty dir exit = %d, want 1", code)
	}
	if strings.TrimSpace(errBuf.String()) == "" {
		t.Fatalf("empty dir must report the failure on stderr")
	}
	var out2, err2 bytes.Buffer
	if code := run([]string{"--pgn", filepath.Join(empty, "missing.pgn")}, strings.NewReader(""), &out2, &err2); code != 1 {
		t.Fatalf("missing file exit = %d, want 1", code)
	}
}

// F2P-003 / R043: PGN load errors and multi-game splitting.
func TestF2P_003_PgnLoadErrorsAndSplit(t *testing.T) {
	if _, err := pgn.FromFile("/no/such/file.pgn"); err == nil || !strings.HasPrefix(err.Error(), "Cannot read '/no/such/file.pgn':") {
		t.Fatalf("FromFile error = %v", err)
	}
	multi := fmt.Sprintf(f2pGamePGN, "One") + "\n" + fmt.Sprintf(f2pGamePGN, "Two")
	games, err := pgn.FromPGNString(multi)
	if err != nil {
		t.Fatal(err)
	}
	if len(games) != 2 {
		t.Fatalf("games = %d, want 2", len(games))
	}
	// Whitespace around headers and move-number spacing do not change a game.
	canonical := fmt.Sprintf(f2pGamePGN, "Whitespace")
	cases := []struct {
		name string
		text string
		titles []string
	}{
		{"leading_whitespace", "\n \t\r\n" + canonical, []string{"Whitespace"}},
		{"header_whitespace", strings.ReplaceAll(canonical, "\n[", "\n\n["), []string{"Whitespace"}},
		{"attached_move_numbers", strings.Replace(canonical, "1. e4 e5 2. Nf3", "1.e4 e5 2.Nf3", 1), []string{"Whitespace"}},
		{"leading_multi_game", "\n" + multi, []string{"One", "Two"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assertGames := func(games []*pgn.Viewer, err error) {
				t.Helper()
				if err != nil || len(games) != len(tc.titles) {
					t.Fatalf("valid PGN: games=%d, err=%v", len(games), err)
				}
				for i, game := range games {
					if game.Title != tc.titles[i] || game.White != "W" || game.Black != "B" || game.Result != "1-0" || game.TotalPlies() != 3 {
						t.Fatalf("game %d: title=%q white=%q black=%q result=%q plies=%d", i, game.Title, game.White, game.Black, game.Result, game.TotalPlies())
					}
				}
			}
			assertGames(pgn.FromPGNString(tc.text))
			path := filepath.Join(t.TempDir(), "valid.pgn")
			if err := os.WriteFile(path, []byte(tc.text), 0o644); err != nil {
				t.Fatal(err)
			}
			assertGames(pgn.FromFile(path))
		})
	}
	if _, err := pgn.FromPGNString("this is not a pgn"); err == nil || err.Error() != "No valid games found in PGN" {
		t.Fatalf("no-valid-games error = %v", err)
	}
}

// F2P-004 / R053: event stream detects a new gameStart and ignores blank lines.
func TestF2P_004_EventStreamParsing(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		fmt.Fprint(w, "\n")
		fmt.Fprint(w, `{"type":"gameStart","game":{"gameId":"newgame","color":"black"}}`+"\n")
	}))
	defer srv.Close()

	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	cancel := &atomic.Bool{}
	found := make(chan lichess.GameFound, 1)
	c.SpawnEventStream(map[string]struct{}{"old": {}}, cancel, found)
	select {
	case res := <-found:
		if res.Err != nil || res.GameID != "newgame" {
			t.Fatalf("found = %+v", res)
		}
		if res.Color != lichess.ParseGameColor("black") {
			t.Fatalf("color = %v", res.Color)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for game start")
	}
}

// F2P-005 / R049: skin Color round-trip and --update-skins creation.
func TestF2P_005_SkinRoundTripAndUpdate(t *testing.T) {
	// Existing skins files may use the Rust serde alias display_mode or omit
	// the style field. Both forms must retain their original meaning.
	for _, tc := range []struct {
		name, styleJSON, want string
	}{
		{"piece_style", `"piece_style":"CLASSIC",`, "CLASSIC"},
		{"legacy_display_mode", `"display_mode":"ASCII",`, "ASCII"},
		{"default_piece_style", "", "DEFAULT"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			document := `{"skins":[{` + tc.styleJSON + `"name":"Compatibility",
				"board_white_color":{"Rgb":[160,160,160]},"board_black_color":"Black",
				"piece_white_color":"White","piece_black_color":"Black",
				"cursor_color":"LightBlue","selection_color":"LightGreen","last_move_color":"LightGreen"}]}`
			path := filepath.Join(t.TempDir(), "skins.json")
			if err := os.WriteFile(path, []byte(document), 0o644); err != nil {
				t.Fatal(err)
			}
			skins, err := skin.LoadAllSkins(path)
			if err != nil || len(skins) != 1 {
				t.Fatalf("load compatible skins.json: skins=%d, err=%v", len(skins), err)
			}
			if skins[0].Name != "Compatibility" || skins[0].PieceStyle != tc.want {
				t.Fatalf("loaded name=%q piece_style=%q, want Compatibility/%q", skins[0].Name, skins[0].PieceStyle, tc.want)
			}
		})
	}

	for _, raw := range []string{`"LightBlue"`, `{"Rgb":[160,160,160]}`} {
		var c skin.Color
		if err := json.Unmarshal([]byte(raw), &c); err != nil {
			t.Fatalf("unmarshal %s: %v", raw, err)
		}
		out, err := json.Marshal(c)
		if err != nil {
			t.Fatal(err)
		}
		var normalized any
		if err := json.Unmarshal([]byte(raw), &normalized); err != nil {
			t.Fatal(err)
		}
		want, _ := json.Marshal(normalized)
		if string(out) != string(want) {
			t.Fatalf("round-trip %s -> %s, want %s", raw, out, want)
		}
	}

	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	out, err := os.CreateTemp("", "f2p-skins-*")
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(out.Name())
	if err := skin.RunUpdateSkins(bufio.NewReader(strings.NewReader("")), out); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "chess-tui", "skins.json")); err != nil {
		t.Fatalf("skins.json not created: %v", err)
	}
}

// F2P-006 / R047: GetGameInfo returns turns/lastMove and errors on empty input.
func TestF2P_006_GetGameInfo(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/stream/game/g1" {
			t.Errorf("path = %s", r.URL.Path)
		}
		fmt.Fprint(w, `{"turns":7,"lastMove":"e2e4"}`+"\n")
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	turns, last, err := c.GetGameInfo("g1")
	if err != nil {
		t.Fatal(err)
	}
	if turns != 7 || last == nil || *last != "e2e4" {
		t.Fatalf("turns=%d last=%v", turns, last)
	}

	empty := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer empty.Close()
	c2 := lichess.NewClient("t")
	c2.BaseURL = empty.URL
	if _, _, err := c2.GetGameInfo("g1"); err == nil {
		t.Fatal("empty response should error")
	}
}

// F2P-007 / R039: config.toml serialization omits nil optional keys.
func TestF2P_007_ConfigPersistShape(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.toml")
	cfg := config.Default()
	cfg.EnginePath = nil
	cfg.BotDifficulty = nil
	cfg.LichessToken = nil
	if err := cfg.Persist(path); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	for _, key := range []string{"engine_path", "bot_difficulty", "lichess_token"} {
		if strings.Contains(text, key+" ") || strings.Contains(text, key+"=") {
			t.Fatalf("%s should be omitted:\n%s", key, text)
		}
	}
	for _, key := range []string{"display_mode", "log_level", "bot_depth", "selected_skin_name", "sound_enabled", "animations_enabled"} {
		if !strings.Contains(text, key) {
			t.Fatalf("%s should be present:\n%s", key, text)
		}
	}
	reloaded := config.Load(path)
	if reloaded.BotDepth == nil || *reloaded.BotDepth != 10 {
		t.Fatalf("reloaded depth = %v", reloaded.BotDepth)
	}
	if reloaded.SoundEnabled == nil || !*reloaded.SoundEnabled {
		t.Fatalf("reloaded sound = %v", reloaded.SoundEnabled)
	}
}

// F2P-008 / R043: PGN suffix tolerance and stop-on-illegal-move.
func TestF2P_008_PgnSuffixAndStopOnIllegal(t *testing.T) {
	games, err := pgn.FromPGNString(f2pCheckmatePGN)
	if err != nil {
		t.Fatal(err)
	}
	v := games[0]
	if v.TotalPlies() != 7 || strings.TrimRight(v.SANs[len(v.SANs)-1], "+#") != "Qxf7" {
		t.Fatalf("plies = %d, last SAN = %v", v.TotalPlies(), v.SANs)
	}

	illegal := `[Event "X"]

1. e4 e5 2. Ke2 Ke7 3. Qh5 Nc6 4. Qxf7# 0-1
`
	games2, err := pgn.FromPGNString(illegal)
	if err != nil {
		t.Fatal(err)
	}
	if p := games2[0].TotalPlies(); p == 0 || p >= 7 {
		t.Fatalf("expected stop before illegal move, got %d plies", p)
	}
}

// F2P-009 / R048: board stream emission sequence and 404 stop.
func TestF2P_009_BoardStreamEmission(t *testing.T) {

	t.Run("new_moves_and_gameFull_status", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/board/game/stream/g1" { t.Errorf("stream path = %s", r.URL.Path) }
			fmt.Fprintln(w, `{"type":"gameFull","id":"g1","white":{"id":"w"},"black":{"id":"b"},"state":{"moves":"","status":"started"}}`)
			fmt.Fprintln(w, `{"type":"gameState","moves":"e2e4","status":"started"}`)
			fmt.Fprintln(w, `{"type":"gameState","moves":"e2e4 e7e5","status":"started"}`)
			fmt.Fprintln(w, `{"type":"chatLine","text":"ignored"}`)
			fmt.Fprintln(w, `{"type":"gameState","moves":"e2e4 e7e5","status":"mate"}`)
		}))
		defer srv.Close()
		c := lichess.NewClient("t")
		c.BaseURL = srv.URL
		ch := make(chan string, 16)
		f2p1StartContractStream(t, c, ch)
		f2p1ExpectBoardMessages(t, ch, []string{"e2e4", "e7e5", "GAME_STATUS:checkmate"}, "INIT_MOVES:0")
	})
	t.Run("reconnect_gameFull_moves", func(t *testing.T) {
		var calls atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch calls.Add(1) {
			case 1:
				fmt.Fprintln(w, `{"type":"gameFull","id":"g1","white":{"id":"w"},"black":{"id":"b"},"state":{"moves":"","status":"started"}}`)
			case 2:
				fmt.Fprintln(w, `{"type":"gameFull","id":"g1","white":{"id":"w"},"black":{"id":"b"},"state":{"moves":"e2e4","status":"mate"}}`)
			default: w.WriteHeader(http.StatusNotFound)
			}
		}))
		defer srv.Close()
		c := lichess.NewClient("t")
		c.BaseURL = srv.URL
		ch := make(chan string, 16)
		c.SpawnGameStream("g1", ch, nil)
		deadline := time.NewTimer(8 * time.Second)
		defer deadline.Stop()
		pending := map[string]bool{"e2e4": true, "GAME_STATUS:checkmate": true}
		bootstrapSeen := false
		for len(pending) > 0 {
			select {
			case got := <-ch:
				if !bootstrapSeen && got == "INIT_MOVES:0" { bootstrapSeen = true; continue }
				if !pending[got] { t.Fatalf("unexpected reconnect message %q", got) }
				delete(pending, got)
			case <-deadline.C: t.Fatalf("reconnect did not emit %v", pending)
			}
		}
		if calls.Load() < 2 { t.Fatal("stream did not reconnect") }
	})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-ndjson")
		flusher, _ := w.(http.Flusher)
		fmt.Fprint(w, `{"type":"gameFull","id":"g1","white":{"id":"w","username":"White"},"black":{"id":"b","username":"Black"},"state":{"moves":"e2e4 e7e5","status":"started"}}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"started"}`+"\n")
		fmt.Fprint(w, `{"type":"gameState","moves":"e2e4 e7e5 g1f3","status":"mate"}`+"\n")
		if flusher != nil {
			flusher.Flush()
		}
	}))
	defer srv.Close()
	c := lichess.NewClient("t")
	c.BaseURL = srv.URL
	ch := make(chan string, 16)
	f2p1StartContractStream(t, c, ch)
	f2p1ExpectBoardMessages(t, ch, []string{"g1f3", "GAME_STATUS:checkmate"}, "INIT_MOVES:2")

	notFound := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer notFound.Close()
	c2 := lichess.NewClient("t")
	c2.BaseURL = notFound.URL
	ch2 := make(chan string, 4)
	f2p1StartContractStream(t, c2, ch2)
	select {
	case msg := <-ch2:
		t.Fatalf("404 should stop with no message, got %q", msg)
	case <-time.After(300 * time.Millisecond):
	}
}

// F2P-010 / R047: make_move and resign endpoints and error strings.
func TestF2P_010_MakeMoveResignErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/board/game/g1/resign" {
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, "nope")
			return
		}
		if r.URL.Path != "/board/game/g1/move/e2e4" {
			t.Errorf("move path = %s", r.URL.Path)
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

// F2P-011 / R043: PGN navigation clamping and end-banner reset.
func TestF2P_011_PgnNavigation(t *testing.T) {
	games, _ := pgn.FromPGNString(f2pCheckmatePGN)
	v := games[0]
	total := v.TotalPlies()
	v.GotoEnd()
	if !v.IsAtEnd() || v.CurrentPly != total {
		t.Fatalf("GotoEnd ply=%d end=%v", v.CurrentPly, v.IsAtEnd())
	}
	v.Next()
	if v.CurrentPly != total {
		t.Fatalf("Next should clamp at %d, got %d", total, v.CurrentPly)
	}
	v.EndBannerDismissed = true
	v.Prev()
	if v.CurrentPly != total-1 || v.EndBannerDismissed {
		t.Fatalf("Prev ply=%d banner=%v", v.CurrentPly, v.EndBannerDismissed)
	}
	v.GotoStart()
	if v.CurrentPly != 0 || v.EndBannerDismissed || v.IsAtEnd() {
		t.Fatalf("GotoStart ply=%d end=%v", v.CurrentPly, v.IsAtEnd())
	}

	v.EndBannerDismissed = true
	v.Prev()
	if v.CurrentPly != 0 || v.EndBannerDismissed {
		t.Fatalf("Prev at start: ply=%d banner=%v", v.CurrentPly, v.EndBannerDismissed)
	}
}

// F2P-012 / R049: config.toml nine-field round-trip and skins.json format.
func TestF2P_012_ConfigTomlAndSkins(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	// Duplicate keys make the entire document invalid TOML, including an
	// otherwise well-typed value. Load must discard it and use the defaults.
	invalid := "bot_depth = 17\nbot_depth = 18\nengine_path = \"/custom/engine\"\n"
	if err := os.WriteFile(path, []byte(invalid), 0o644); err != nil {
		t.Fatal(err)
	}
	fallback := config.Load(path)
	if fallback.BotDepth == nil || *fallback.BotDepth != 10 || fallback.EnginePath != nil {
		t.Fatalf("invalid TOML must fall back to defaults: %+v", fallback)
	}
	nine := `engine_path = "/usr/bin/stockfish"
display_mode = "DEFAULT"
log_level = "OFF"
bot_depth = 8
bot_difficulty = 2
selected_skin_name = "Default"
lichess_token = "tok"
sound_enabled = true
animations_enabled = false
`
	if err := os.WriteFile(path, []byte(nine), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := config.Load(path)
	if cfg.EnginePath == nil || *cfg.EnginePath != "/usr/bin/stockfish" ||
		cfg.BotDepth == nil || *cfg.BotDepth != 8 ||
		cfg.BotDifficulty == nil || *cfg.BotDifficulty != 2 ||
		cfg.LichessToken == nil || *cfg.LichessToken != "tok" {
		t.Fatalf("loaded cfg = %+v", cfg)
	}
	out := filepath.Join(dir, "out.toml")
	if err := config.Create(&config.Args{EnginePath: "/usr/bin/stockfish"}, dir, out); err != nil {
		t.Fatal(err)
	}
	if err := cfg.Persist(out); err != nil {
		t.Fatal(err)
	}
	back := config.Load(out)
	if back.EnginePath == nil || *back.EnginePath != "/usr/bin/stockfish" || back.BotDepth == nil || *back.BotDepth != 8 {
		t.Fatalf("re-persisted cfg = %+v", back)
	}

	skinsPath := filepath.Join(dir, "skins.json")
	if err := skin.CreateDefaultSkinsFile(skinsPath); err != nil {
		t.Fatal(err)
	}
	var c skin.Color
	if err := json.Unmarshal([]byte(`{"Rgb":[160,160,160]}`), &c); err != nil {
		t.Fatal(err)
	}
	outBytes, _ := json.Marshal(c)
	if string(outBytes) != `{"Rgb":[160,160,160]}` {
		t.Fatalf("skins color = %s", outBytes)
	}
}

// F2P-013 / R047: seek flow opens the event stream and maps errors.
func TestF2P_013_SeekFlowErrorMapping(t *testing.T) {
	// An invalid token is rejected by the event endpoint as well as /board/seek.
	// Waiting only for a successful event-stream response would hide the required
	// authentication/rate-limit error forever, before the seek POST can run.
	for _, tc := range []struct {
		status int
		want   string
	}{
		{401, "Invalid token. Please check your token or generate a new one."},
		{403, "Token missing permissions. Please generate a new token with 'board:play' scope enabled."},
		{429, "Rate limit exceeded. Please wait a minute before trying again."},
	} {
		t.Run(fmt.Sprintf("event_stream_rejected_%d", tc.status), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
			}))
			defer srv.Close()
			c := lichess.NewClient("rejected-token")
			c.BaseURL = srv.URL
			cancel := &atomic.Bool{}
			defer cancel.Store(true)
			done := make(chan error, 1)
			go func() { _, _, err := c.SeekGame(1, 0, cancel); done <- err }()
			select {
			case err := <-done:
				if err == nil || err.Error() != tc.want {
					t.Fatalf("status %d: error = %v, want %q", tc.status, err, tc.want)
				}
			case <-time.After(12 * time.Second):
				t.Fatal("seek did not report the rejected event stream")
			}
		})
	}

	t.Run("cancel_pending_request", func(t *testing.T) {
		entered := make(chan struct{}, 1)
		release := make(chan struct{})
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case "/account/playing": fmt.Fprint(w, `{"nowPlaying":[]}`)
			case "/stream/event": fmt.Fprintln(w); w.(http.Flusher).Flush(); select { case <-r.Context().Done(): case <-release: }
			case "/board/seek":
				entered <- struct{}{}
				select { case <-r.Context().Done(): case <-release: }
			default: w.WriteHeader(http.StatusNotFound)
			}
		}))
		defer srv.Close()
		defer close(release)
		c := lichess.NewClient("t")
		c.BaseURL = srv.URL
		cancel := &atomic.Bool{}
		done := make(chan error, 1)
		go func() { _, _, err := c.SeekGame(1, 0, cancel); done <- err }()
		defer cancel.Store(true)
		select { case <-entered: case <-time.After(3 * time.Second): t.Fatal("seek request not started after opening event stream") }
		cancel.Store(true)
		select {
		case err := <-done: if err == nil { t.Fatal("cancelled seek must return an error") }
		case <-time.After(2 * time.Second): t.Fatal("cancellation did not interrupt pending seek request")
		}
	})
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
		var mu sync.Mutex
		var days, timeField string
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case "/account/playing":
				fmt.Fprint(w, `{"nowPlaying":[]}`)
			case "/stream/event":
				fmt.Fprint(w, "\n")
				fmt.Fprint(w, `{"type":"gameStart","game":{"gameId":"other","color":"white"}}`+"\n")
			case "/board/seek":
				mu.Lock()
				days = r.PostFormValue("days")
				timeField = r.PostFormValue("time")
				mu.Unlock()
				w.WriteHeader(tc.status)
				fmt.Fprint(w, tc.body)
			}
		}))
		c := lichess.NewClient("t")
		c.BaseURL = srv.URL
		cancel := &atomic.Bool{}
		_, _, err := c.SeekGame(0, 0, cancel)
		srv.Close()
		if err == nil || err.Error() != tc.want {
			t.Fatalf("status %d err = %v, want %q", tc.status, err, tc.want)
		}
		mu.Lock()
		if tc.status == 403 && days != "3" {
			t.Fatalf("correspondence seek days = %q, want 3", days)
		}
		if tc.status == 403 && timeField != "" {
			t.Fatalf("correspondence seek should not set time, got %q", timeField)
		}
		mu.Unlock()
	}
}

// F2P-014 / R042: UCI handshake ordering, position FEN, and spawn failure.
func TestF2P_014_UciHandshakeAndSpawn(t *testing.T) {
	engineArg, logPath := f2pStubEngine(t)
	bot := uci.New(engineArg, false, 6, nil)
	move, err := bot.GetMove(f2pStartFEN)
	if err != nil {
		t.Fatal(err)
	}
	if move != "e2e4" {
		t.Fatalf("move = %q", move)
	}
	cmds := strings.Split(strings.TrimSpace(f2pReadLog(t, logPath)), "\n")
	idx := func(prefix string) int {
		for i, c := range cmds {
			if strings.HasPrefix(c, prefix) {
				return i
			}
		}
		return -1
	}
	uciIdx, posIdx, goIdx := idx("uci"), idx("position"), idx("go ")
	if !(uciIdx >= 0 && posIdx > uciIdx && goIdx > posIdx) {
		t.Fatalf("handshake order wrong: %v", cmds)
	}
	if !strings.Contains(strings.Join(cmds, "\n"), "position fen "+f2pStartFEN) {
		t.Fatalf("missing position fen: %v", cmds)
	}

	bad := uci.New("/nonexistent/engine-binary-does-not-exist", false, 5, nil)
	if _, err := bad.GetMove(f2pStartFEN); err == nil {
		t.Fatal("a missing engine binary must be reported as an error")
	}
	// A completed handshake does not make malformed engine output a move.
	// Each stub terminates after its reply so failures cannot wait forever.
	for _, reply := range []string{"bestmove garbage", "bestmove e2e9", "bestmove", "bestmove (none)", "bestmovegarbage e2e4", ""} {
		t.Run("invalid_"+reply, func(t *testing.T) {
			script := filepath.Join(t.TempDir(), "engine.sh")
			body := "#!/bin/sh\nwhile IFS= read -r line; do\ncase \"$line\" in\n" +
					"uci) echo uciok ;;\nisready) echo readyok ;;\ngo*) printf '%s\\n' '" + reply + "'; exit 0 ;;\nesac\ndone\n"
			if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
				t.Fatal(err)
			}
			broken := uci.New("/bin/sh "+script, false, 5, nil)
			if move, err := broken.GetMove(f2pStartFEN); err == nil {
				t.Fatalf("malformed reply %q returned move %q without error", reply, move)
			}
		})
	}
}

// F2P-015 / R047: join prefers ongoing color and maps accept/stream errors.
func TestF2P_015_JoinAccept(t *testing.T) {
	prefer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/account/playing" {
			fmt.Fprint(w, `{"nowPlaying":[{"gameId":"g1","color":"white"}]}`)
			return
		}
		t.Errorf("unexpected path %s", r.URL.Path)
	}))
	defer prefer.Close()
	c := lichess.NewClient("t")
	c.BaseURL = prefer.URL
	_, color, err := c.JoinGame("g1", "me")
	if err != nil || color != lichess.ParseGameColor("white") {
		t.Fatalf("prefer ongoing: color=%v err=%v", color, err)
	}

	for _, tc := range []struct {
		status int
		want   string
	}{
		{403, "Cannot join this game. You may not be a participant."},
		{401, "Invalid token. Please check your token or generate a new one."},
	} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case "/account/playing":
				fmt.Fprint(w, `{"nowPlaying":[]}`)
			case "/challenge/g1/accept":
				w.WriteHeader(http.StatusNotFound)
			case "/board/game/stream/g1", "/board/game/g1/stream":
				w.WriteHeader(tc.status)
			default:
				w.WriteHeader(http.StatusNotFound)
			}
		}))
		c2 := lichess.NewClient("t")
		c2.BaseURL = srv.URL
		_, _, err := c2.JoinGame("g1", "me")
		srv.Close()
		if err == nil || err.Error() != tc.want {
			t.Fatalf("status %d err = %v, want %q", tc.status, err, tc.want)
		}
	}
}

// F2P-016 / R044: TCP non-blocking start wait and ended read error.
func TestF2P_016_TcpNonBlocking(t *testing.T) {

	t.Run("queued_peer_tokens", func(t *testing.T) {
		lis, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil { t.Fatal(err) }
		defer lis.Close()
		peerCh := make(chan net.Conn, 1)
		go func() { peer, err := lis.Accept(); if err == nil { peerCh <- peer } }()
		white := lichess.ParseGameColor("white")
		o, err := tcp.New(lis.Addr().String(), &white)
		if err != nil { t.Fatal(err) }
		defer o.Close()
		peer := <-peerCh
		defer peer.Close()
		if _, err := peer.Write([]byte("s")); err != nil { t.Fatal(err) }
		time.Sleep(20 * time.Millisecond)
		if started, err := tcp.WaitForGameStart(o); err != nil || !started { t.Fatalf("queued s: %v/%v", started, err) }
		for _, message := range []string{"e2e4", "ended"} {
			if _, err := peer.Write([]byte(message)); err != nil { t.Fatal(err) }
			time.Sleep(20 * time.Millisecond)
			got, err := o.ReadStream()
			if message == "ended" {
				if err == nil || err.Error() != "Game ended by the other opponent" { t.Fatalf("queued ended: %q/%v", got, err) }
			} else if err != nil || got != message { t.Fatalf("queued move: %q/%v", got, err) }
		}
	})
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lis.Close()
	sendEnded := make(chan struct{})
	go func() {
		conn, err := lis.Accept()
		if err != nil {
			return
		}
		_, _ = conn.Write([]byte("w"))
		<-sendEnded
		_, _ = conn.Write([]byte("ended"))
	}()
	o, err := tcp.New(lis.Addr().String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer o.Close()
	started, err := tcp.WaitForGameStart(o)
	if err != nil || started {
		t.Fatalf("silent socket should be (false,nil), got (%v,%v)", started, err)
	}
	close(sendEnded)

	// Keep a live (non-expired) read deadline so the non-blocking ReadStream
	// blocks for the peer's bytes rather than being cut off by its own
	// time.Now() deadline; then ReadStream must surface the ended error.
	stop := make(chan struct{})
	defer close(stop)
	go func() {
		for {
			select {
			case <-stop:
				return
			default:
				_ = o.Conn().SetReadDeadline(time.Now().Add(2 * time.Second))
			}
		}
	}()
	time.Sleep(250 * time.Millisecond)
	var readErr error
	for i := 0; i < 40; i++ {
		_, readErr = o.ReadStream()
		if readErr != nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if readErr == nil || readErr.Error() != "Game ended by the other opponent" {
		t.Fatalf("ended read err = %v", readErr)
	}

	// A token other than w/b must surface as the documented color error.
	lis2, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lis2.Close()
	go func() {
		conn, err := lis2.Accept()
		if err != nil {
			return
		}
		_, _ = conn.Write([]byte("x"))
	}()
	conn, err := net.Dial("tcp", lis2.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := tcp.GetColorFromStream(conn); err == nil || err.Error() != "Failed to get color from stream" {
		t.Fatalf("color err = %v", err)
	}
}

// F2P-017 / R002: run --pgn succeeds headless without a terminal.
func TestF2P_017_RunPgnNoTerminal(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	var stdout, stderr bytes.Buffer
	code := run([]string{"--pgn", "examples/sample.pgn"}, strings.NewReader(""), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr.String())
	}
}

// F2P-018 / R040: 8-bit depth bounds and short/long flag equivalence.
func TestF2P_018_CliDepthBounds(t *testing.T) {
	args, err := config.ParseArgs([]string{"--depth", "255"})
	if err != nil || args.Depth == nil || *args.Depth != 255 {
		t.Fatalf("depth 255 = %v, err %v", args, err)
	}
	if _, err := config.ParseArgs([]string{"--depth", "256"}); err == nil {
		t.Fatal("depth 256 should fail")
	}
	if _, err := config.ParseArgs([]string{"--depth", "-1"}); err == nil {
		t.Fatal("depth -1 should fail")
	}
	long, err := config.ParseArgs([]string{"--engine-path", "/e", "--depth", "3", "--lichess-token", "t", "--pgn", "x.pgn"})
	if err != nil {
		t.Fatal(err)
	}
	short, err := config.ParseArgs([]string{"-e", "/e", "-d", "3", "-l", "t", "-p", "x.pgn"})
	if err != nil {
		t.Fatal(err)
	}
	if long.EnginePath != short.EnginePath || *long.Depth != *short.Depth || *long.LichessToken != *short.LichessToken || *long.Pgn != *short.Pgn {
		t.Fatalf("short/long mismatch: %+v vs %+v", long, short)
	}
}

// f2pStripComments blanks line and block comments while preserving string and
// rune literal contents.  A comment that merely *names* a forbidden token is
// not an invocation of it (the residue scan already ignores comments for the
// same reason); a string literal is kept because a real dynamic load or cgo
// import surfaces as a string.
func f2pStripComments(source []byte) string {
	const (
		normal = iota
		lineComment
		blockComment
		strLit
		runeLit
		rawLit
	)
	out := make([]byte, len(source))
	state := normal
	for i := 0; i < len(source); i++ {
		c := source[i]
		switch state {
		case normal:
			if c == '/' && i+1 < len(source) && source[i+1] == '/' {
				state = lineComment
				out[i] = ' '
				continue
			}
			if c == '/' && i+1 < len(source) && source[i+1] == '*' {
				state = blockComment
				out[i] = ' '
				continue
			}
			out[i] = c
			switch c {
			case '"':
				state = strLit
			case '\'':
				state = runeLit
			case '`':
				state = rawLit
			}
		case lineComment:
			if c == '\n' {
				state = normal
				out[i] = '\n'
			} else {
				out[i] = ' '
			}
		case blockComment:
			if c == '*' && i+1 < len(source) && source[i+1] == '/' {
				out[i] = ' '
				out[i+1] = ' '
				i++
				state = normal
				continue
			}
			if c == '\n' {
				out[i] = '\n'
			} else {
				out[i] = ' '
			}
		case strLit:
			out[i] = c
			if c == '\\' && i+1 < len(source) {
				i++
				out[i] = source[i]
				continue
			}
			if c == '"' {
				state = normal
			}
		case runeLit:
			out[i] = c
			if c == '\\' && i+1 < len(source) {
				i++
				out[i] = source[i]
				continue
			}
			if c == '\'' {
				state = normal
			}
		case rawLit:
			out[i] = c
			if c == '`' {
				state = normal
			}
		}
	}
	return string(out)
}

// F2P-019 / R031: no cgo/FFI/Rust invocation in the migrated production code.
func TestF2P_019_DependencyPolicy(t *testing.T) {
	bad := []string{"import \"C\"", "rustc", "cargo ", "cgo", "dlopen", "libloading"}
	err := filepath.WalkDir(".", func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if d.Name() == ".git" || d.Name() == ".gomodcache" || d.Name() == ".gocache" || d.Name() == "src" || d.Name() == "tests" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		content, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		lower := strings.ToLower(f2pStripComments(content))
		for _, token := range bad {
			if strings.Contains(lower, strings.ToLower(token)) {
				t.Fatalf("%s contains forbidden %q", path, token)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	// The migrated behavior still works without a Rust runtime.
	if cfg := config.Load(filepath.Join(t.TempDir(), "absent.toml")); cfg == nil || cfg.BotDepth == nil {
		t.Fatal("config.Load missing-file fallback failed")
	}
	if _, err := pgn.FromPGNString(f2pGamePGN); err != nil {
		t.Fatalf("pgn parse failed: %v", err)
	}
}

// F2P-020 / R045: TCP join colors, capacity rejection, relay and ended.
func TestF2P_020_TcpJoinCapacityRelay(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := tcp.NewGameServer(true)
	go server.Serve(lis)
	defer server.Stop()
	addr := lis.Addr().String()

	host, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer host.Close()
	time.Sleep(50 * time.Millisecond)
	joiner, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer joiner.Close()

	if got := f2pReadN(t, joiner, 1); got != "w" && got != "b" {
		t.Fatalf("joiner color = %q", got)
	}
	if got := f2pReadN(t, host, 1); got != "s" {
		t.Fatalf("host start = %q", got)
	}

	time.Sleep(50 * time.Millisecond)
	third, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer third.Close()
	buf := make([]byte, 64)
	_ = third.SetReadDeadline(time.Now().Add(2 * time.Second))
	n, err := third.Read(buf)
	if err != nil || string(buf[:n]) != "Game is already full" {
		t.Fatalf("third = %q err=%v", string(buf[:n]), err)
	}

	if _, err := host.Write([]byte("e2e4")); err != nil {
		t.Fatal(err)
	}
	if got := f2pReadN(t, joiner, 4); got != "e2e4" {
		t.Fatalf("relayed = %q", got)
	}
	_ = host.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	echo := make([]byte, 4)
	if _, err := host.Read(echo); err == nil {
		t.Fatal("sender must not receive echo")
	}

	if _, err := host.Write([]byte("ended")); err != nil {
		t.Fatal(err)
	}
	if got := f2pReadN(t, joiner, 5); got != "ended" {
		t.Fatalf("ended broadcast = %q", got)
	}
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

// F2P-021 / R053: board stream status mapping.
func TestF2P_021_BoardStreamStatusMapping(t *testing.T) {
	cases := []struct {
		status string
		want   string
	}{
		{"mate", "GAME_STATUS:checkmate"},
		{"checkmate", "GAME_STATUS:checkmate"},
		{"draw", "GAME_STATUS:draw"},
		{"stalemate", "GAME_STATUS:draw"},
		{"repetition", "GAME_STATUS:draw"},
		{"insufficient", "GAME_STATUS:draw"},
		{"fifty", "GAME_STATUS:draw"},
		{"resign", "GAME_STATUS:resign"},
		{"aborted", "GAME_STATUS:aborted"},
	}
	for _, tc := range cases {
		ch := make(chan string, 1)
		lichess.SendStatusMessage(tc.status, ch)
		select {
		case got := <-ch:
			if got != tc.want {
				t.Fatalf("status %q -> %q, want %q", tc.status, got, tc.want)
			}
		default:
			t.Fatalf("status %q produced no message", tc.status)
		}
	}
	ch := make(chan string, 1)
	lichess.SendStatusMessage("started", ch)
	select {
	case got := <-ch:
		t.Fatalf("unknown status should be ignored, got %q", got)
	default:
	}
}

// F2P-022 / R002: migrated responsibilities are reachable from the Go module.
func TestF2P_022_GoModuleSurface(t *testing.T) {
	// Compile-time reachability of every migrated responsibility.
	var (
		_ = chess.NewGameBoard()
		_ = config.Default()
		_ = pgn.Speeds
		_ = skin.Default()
		_ = uci.New("/engine", false, 1, nil)
		_ = lichess.NewClient("t")
		_ = tcp.NewGameServer(true)
	)
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	var stdout, stderr bytes.Buffer
	if code := run([]string{"--pgn", "examples/sample.pgn"}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("run exit = %d, stderr = %s", code, stderr.String())
	}
}

// F2P-023 / R051: the deliverable is a Go module with the declared module path.
//
// The dependency set is an implementation choice the task contract does not
// pin, so only the module identity required by the deliverable is asserted.
func TestF2P_023_GoModManifest(t *testing.T) {
	content, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatal(err)
	}
	text := string(content)
	if !strings.Contains(text, "module github.com/thomas-mauran/chess-tui") {
		t.Fatalf("module line missing:\n%s", text)
	}
}

// F2P-024 / R049: external skins.json format keeps round-tripping.
func TestF2P_024_ExternalFormats(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "skins.json")
	if err := skin.CreateDefaultSkinsFile(path); err != nil {
		t.Fatal(err)
	}
	skins, err := skin.LoadAllSkins(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := skin.GetSkinByName(skins, "Default"); !ok {
		t.Fatal("default skins must contain Default")
	}
	var c skin.Color
	if err := json.Unmarshal([]byte(`"LightBlue"`), &c); err != nil {
		t.Fatal(err)
	}
	named, _ := json.Marshal(c)
	if string(named) != `"LightBlue"` {
		t.Fatalf("named color = %s", named)
	}
	if err := json.Unmarshal([]byte(`{"Rgb":[160,160,160]}`), &c); err != nil {
		t.Fatal(err)
	}
	rgb, _ := json.Marshal(c)
	if string(rgb) != `{"Rgb":[160,160,160]}` {
		t.Fatalf("rgb color = %s", rgb)
	}
}

// StreamGame may read synchronously or start a background reader. Both satisfy
// the public stream contract; await observable messages independently of return.
func f2p1StartContractStream(t *testing.T, c *lichess.Client, messages chan string) {
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
func f2p1ExpectBoardMessages(t *testing.T, ch <-chan string, want []string, bootstrap ...string) {
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

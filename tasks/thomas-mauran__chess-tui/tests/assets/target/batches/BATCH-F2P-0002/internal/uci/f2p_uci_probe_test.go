package uci

import (
	"bufio"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/thomas-mauran/chess-tui/internal/constants"
)

// TestF2PHelperProcess acts as a stub UCI engine when re-executed.
func TestF2PHelperProcess(t *testing.T) {
	if os.Getenv("F2P_GO_WANT_HELPER_PROCESS") != "1" {
		return
	}
	logPath := os.Getenv("F2P_STUB_LOG")
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		line := scanner.Text()
		if logPath != "" {
			if f, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
				fmt.Fprintln(f, line)
				f.Close()
			}
		}
		switch {
		case line == "uci":
			fmt.Println("id name StubEngine")
			fmt.Println("uciok")
		case strings.HasPrefix(line, "go"):
			fmt.Println("info depth 1 score cp 0")
			fmt.Println("bestmove e2e4")
		}
	}
	os.Exit(0)
}

func f2pStubEngine(t *testing.T) (string, string) {
	t.Helper()
	logPath := t.TempDir() + "/stub.log"
	return os.Args[0] + " -test.run=TestF2PHelperProcess", logPath
}

func f2pConfigureStub(t *testing.T, logPath string) {
	t.Helper()
	t.Setenv("F2P_GO_WANT_HELPER_PROCESS", "1")
	t.Setenv("F2P_STUB_LOG", logPath)
}

func f2pReadCommands(t *testing.T, path string) []string {
	t.Helper()
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read stub log: %v", err)
	}
	return strings.Split(strings.TrimSpace(string(content)), "\n")
}

const f2pStartFEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

// F2P-035 (R042): difficulty 0 emits UCI_LimitStrength/UCI_Elo and
// "go depth 1 movetime 25"; full strength emits "go depth <base>" with no
// movetime and no UCI_LimitStrength.
func TestF2P035PresetSearchParameters(t *testing.T) {
	enginePath, logPath := f2pStubEngine(t)
	f2pConfigureStub(t, logPath)

	diff := uint8(0)
	bot := New(enginePath, false, 10, &diff)
	if _, err := bot.GetMove(f2pStartFEN); err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(f2pReadCommands(t, logPath), "\n")
	for _, want := range []string{
		"setoption name UCI_LimitStrength value true",
		"setoption name UCI_Elo value 400",
		"go depth 1 movetime 25",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in:\n%s", want, joined)
		}
	}

	fullPath, fullLog := f2pStubEngine(t)
	f2pConfigureStub(t, fullLog)
	full := New(enginePath, false, 12, nil)
	if _, err := full.GetMove(f2pStartFEN); err != nil {
		t.Fatal(err)
	}
	_ = fullPath
	fullJoined := strings.Join(f2pReadCommands(t, fullLog), "\n")
	if !strings.Contains(fullJoined, "go depth 12") {
		t.Fatalf("expected 'go depth 12':\n%s", fullJoined)
	}
	if strings.Contains(fullJoined, "movetime") {
		t.Fatalf("full strength must not set movetime:\n%s", fullJoined)
	}
	if strings.Contains(fullJoined, "UCI_LimitStrength") {
		t.Fatalf("full strength must not set UCI_LimitStrength:\n%s", fullJoined)
	}
}

// F2P-045 (R041): GetMove splits the engine path on whitespace, performs the
// UCI handshake before options/position, and a nonexistent executable returns
// a "Failed to spawn engine process" error rather than a move.
func TestF2P045HandshakeOrderAndEnginePathArgs(t *testing.T) {
	enginePath, logPath := f2pStubEngine(t)
	f2pConfigureStub(t, logPath)

	bot := New(enginePath, false, 6, nil)
	move, err := bot.GetMove(f2pStartFEN)
	if err != nil {
		t.Fatal(err)
	}
	if move != "e2e4" {
		t.Fatalf("move = %q", move)
	}
	cmds := f2pReadCommands(t, logPath)
	idx := func(prefix string) int {
		for i, c := range cmds {
			if strings.HasPrefix(c, prefix) {
				return i
			}
		}
		return -1
	}
	uci, pos, goIdx := idx("uci"), idx("position fen "+f2pStartFEN), idx("go ")
	if !(uci >= 0 && pos > uci && goIdx > pos) {
		t.Fatalf("handshake/order wrong: %v", cmds)
	}
	if strings.Contains(strings.Join(cmds, "\n"), "uciok") {
		t.Fatal("uciok must not be sent to the engine")
	}

	missing := New("/definitely/not/a/real/engine-binary --uci", false, 5, nil)
	_, err = missing.GetMove(f2pStartFEN)
	if err == nil {
		t.Fatal("a missing engine binary must be reported as an error")
	}
}

// F2P-032 (R041): the difficulty preset tables select depth/ELO/movetime, and
// with difficulty unset the base depth is used with no movetime and no ELO.
func TestF2P032DifficultyPresetTables(t *testing.T) {
	wantDepth := []uint8{1, 4, 8, 20}
	wantElo := []uint16{400, 900, 1500, 2700}
	wantMove := []int{25, 120, 500, 12000}

	for i := 0; i < constants.BotDifficultyCount; i++ {
		idx := uint8(i)
		bot := New("/engine", false, 10, &idx)
		if got := bot.EffectiveDepth(); got != wantDepth[i] {
			t.Fatalf("difficulty %d depth = %d, want %d", i, got, wantDepth[i])
		}
		if got, ok := bot.Elo(); !ok || got != wantElo[i] {
			t.Fatalf("difficulty %d elo = %d/%v, want %d", i, got, ok, wantElo[i])
		}
		if got, ok := bot.MovetimeMs(); !ok || got != wantMove[i] {
			t.Fatalf("difficulty %d movetime = %d/%v, want %d", i, got, ok, wantMove[i])
		}
	}

	base := New("/engine", false, 17, nil)
	if got := base.EffectiveDepth(); got != 17 {
		t.Fatalf("base depth = %d, want 17", got)
	}
	if _, ok := base.MovetimeMs(); ok {
		t.Fatal("no difficulty must not select a movetime")
	}
	if _, ok := base.Elo(); ok {
		t.Fatal("no difficulty must not select an ELO limit")
	}
}

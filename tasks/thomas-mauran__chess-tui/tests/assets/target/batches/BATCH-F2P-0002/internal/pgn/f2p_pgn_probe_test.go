package pgn

import (
	"strings"
	"testing"
)

// F2P-044 (R043): movetext cleaning removes {comments} and nested (variations),
// skips move numbers, $n NAGs and result tokens, strips trailing !/?, tolerates
// +# suffixes and stops at the first unparsable or illegal move.
func TestF2P044MovetextCleaning(t *testing.T) {
	checkmate := `[Event "F2P044"]
[White "A"]
[Black "B"]
[Result "0-1"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 0-1
`
	games, err := FromPGNString(checkmate)
	if err != nil {
		t.Fatalf("checkmate pgn: %v", err)
	}
	v := games[0]
	if v.TotalPlies() == 0 {
		t.Fatal("expected a valid game")
	}
	if got := strings.TrimRight(v.SANs[len(v.SANs)-1], "+#"); got != "Qxf7" {
		t.Fatalf("last san = %q, want Qxf7", got)
	}

	illegal := `[Event "F2P044b"]

1. e4 e5 2. Ke2 Ke7 3. Qh5 Nc6 4. Qxf7# 0-1
`
	games2, err := FromPGNString(illegal)
	if err != nil {
		t.Fatalf("illegal pgn: %v", err)
	}
	v2 := games2[0]
	if v2.TotalPlies() == 0 {
		t.Fatal("expected some valid moves before the illegal move")
	}
	if v2.TotalPlies() >= 7 {
		t.Fatalf("expected to stop at the first illegal move, got %d plies", v2.TotalPlies())
	}

	noisy := `[Event "F2P044c"]

1. e4 {king pawn} e5 (1... c5 2. Nf3) 2. $1 Nf3 1-0
`
	games3, err := FromPGNString(noisy)
	if err != nil {
		t.Fatalf("noisy pgn: %v", err)
	}
	if games3[0].Title != "F2P044c" {
		t.Fatalf("title = %q", games3[0].Title)
	}
	if games3[0].TotalPlies() < 3 {
		t.Fatalf("expected comments/variations/nags to be skipped, got %d plies: %v", games3[0].TotalPlies(), games3[0].SANs)
	}
}

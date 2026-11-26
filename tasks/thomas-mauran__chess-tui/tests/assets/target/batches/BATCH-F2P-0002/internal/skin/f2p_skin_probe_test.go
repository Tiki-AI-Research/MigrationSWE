package skin

import (
	"encoding/json"
	"testing"
)

// F2P-031 (R049): A Color decoded from "LightBlue" marshals back to
// "LightBlue", and one decoded from {"Rgb":[160,160,160]} marshals back to
// {"Rgb":[160,160,160]}, so an existing ratatui skins.json round-trips.
func TestF2P031ColorRoundTrip(t *testing.T) {
	cases := []string{`"LightBlue"`, `{"Rgb":[160,160,160]}`}
	for _, raw := range cases {
		var c Color
		if err := json.Unmarshal([]byte(raw), &c); err != nil {
			t.Fatalf("unmarshal %s: %v", raw, err)
		}
		out, err := json.Marshal(c)
		if err != nil {
			t.Fatalf("marshal %s: %v", raw, err)
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
}

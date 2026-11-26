package dateutil

// Probe tests transplanted into the reference target's `internal/dateutil`
// package. They observe UTC-derived display labels for the timestamp formats
// the existing cache contains, plus the fallbacks for missing/unparseable
// values.

import (
	"testing"
)

func strptr(s string) *string { return &s }

func f2pAssertDateLabels(t *testing.T) {
	t.Helper()

	for _, raw := range []string{
		"2024-01-02T10:00:00.000Z",
		"2024-01-02T10:00:00Z",
	} {
		if got := ToIsoDate(strptr(raw)); got == nil || *got != "2024-01-02" {
			t.Fatalf("ToIsoDate(%q) = %v, want 2024-01-02", raw, got)
		}
		if got := ToIsoMonth(strptr(raw)); got == nil || *got != "2024-01" {
			t.Fatalf("ToIsoMonth(%q) = %v, want 2024-01", raw, got)
		}
		if got := ToWeekdayShort(strptr(raw)); got == nil || *got != "Tue" {
			t.Fatalf("ToWeekdayShort(%q) = %v, want Tue", raw, got)
		}
		h := ToUtcHour(strptr(raw))
		if h == nil || *h != 10 {
			t.Fatalf("ToUtcHour(%q) = %v, want 10", raw, h)
		}
		if got := ToYearLabel(strptr(raw)); got != "2024" {
			t.Fatalf("ToYearLabel(%q) = %q, want 2024", raw, got)
		}
		if got := ToMonthDayLabel(strptr(raw)); got != "Jan 02" {
			t.Fatalf("ToMonthDayLabel(%q) = %q, want Jan 02", raw, got)
		}
	}

	// Missing value falls back to the documented placeholders.
	if got := ToYearLabel(nil); got != "????" {
		t.Fatalf("ToYearLabel(nil) = %q, want ????", got)
	}
	if got := ToMonthDayLabel(nil); got != " ?? ??" {
		t.Fatalf("ToMonthDayLabel(nil) = %q, want ' ?? ??'", got)
	}
	// Unparseable value keeps a stable, deterministic fallback rather than
	// panicking or producing an empty label.
	if got := ToYearLabel(strptr("garbage")); got == "" {
		t.Fatalf("ToYearLabel(garbage) produced an empty label")
	}
	if got := ToMonthDayLabel(strptr("garbage")); got == "" {
		t.Fatalf("ToMonthDayLabel(garbage) produced an empty label")
	}
}

func TestF2P063DateLabels(t *testing.T) { f2pAssertDateLabels(t) }
func TestF2P071DateLabels(t *testing.T) { f2pAssertDateLabels(t) }

package itertools_test

// Target-side F2P probe for BATCH-F2P-0002.
//
// This file is a tests asset. It is injected (unchanged) into a scratch copy of
// the migrated Go module by shared/batches/BATCH-F2P-0002/harness.py and run
// with `go test -run TestF2P_`. Every function is one F2P observation and
// asserts the *target* postcondition of that observation: the behavior must be
// produced by the Go implementation. No observation asserts source behavior.

import (
	"fmt"
	"os"
	"reflect"
	"strings"
	"testing"

	itertools "itertools"
	"itertools/core"
	"itertools/free"
)

func f2pCollect[T any](it core.Iter[T]) []T {
	var out []T
	for {
		v, ok := it.Next()
		if !ok {
			return out
		}
		out = append(out, v)
	}
}

func f2pMustPanic(t *testing.T, name string, f func()) {
	t.Helper()
	defer func() {
		if r := recover(); r == nil {
			t.Fatalf("%s: expected a recovered panic, got none", name)
		}
	}()
	f()
}

func f2pEq(t *testing.T, name string, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%s: got %#v, want %#v", name, got, want)
	}
}

func interleaveShortestPost(t *testing.T) {
	t.Helper()
	got := f2pCollect(itertools.InterleaveShortest[int](
		core.FromSlice([]int{1, 2, 3}),
		core.FromSlice([]int{-1, -2}),
	))
	f2pEq(t, "InterleaveShortest", got, []int{1, -1, 2, -2, 3})
}

// F2P-025: macros.go Izip/ChainConcatenates and feature_alloc.go Iproduct.
func TestF2P_025(t *testing.T) {
	z := f2pCollect(itertools.Izip[int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{10, 20})))
	f2pEq(t, "Izip", z, [][]int{{1, 10}, {2, 20}})

	p := f2pCollect(itertools.Iproduct[int](core.FromSlice([]int{1, 2}), core.FromSlice([]int{10, 20})))
	f2pEq(t, "Iproduct", p, [][]int{{1, 10}, {1, 20}, {2, 10}, {2, 20}})

	c := f2pCollect(itertools.ChainConcatenates[int](
		core.FromSlice([]int{1}), core.FromSlice([]int{2, 3}), core.FromSlice([]int{4})))
	f2pEq(t, "ChainConcatenates", c, []int{1, 2, 3, 4})
}

// F2P-026: exported identifiers / InterleaveShortest postcondition.
func TestF2P_026(t *testing.T) { interleaveShortestPost(t) }

// F2P-027: ZipEq and Chunks panic behavior.
func TestF2P_027(t *testing.T) {
	f2pMustPanic(t, "ZipEq", func() {
		f2pCollect(itertools.ZipEq[int, int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{1, 2})))
	})
	f2pMustPanic(t, "Chunks", func() {
		c := itertools.Chunks[int](core.FromSlice([]int{1, 2, 3}), 0)
		f2pCollect(c)
	})
}

// F2P-028: adaptor laziness (Map) plus InterleaveShortest postcondition.
func TestF2P_028(t *testing.T) {
	calls := 0
	m := itertools.Map[int, int](core.FromSlice([]int{1, 2, 3}), func(v int) int { calls++; return v * 2 })
	if calls != 0 {
		t.Fatalf("Map is not lazy: f called %d times before iteration", calls)
	}
	f2pEq(t, "lazy Map result", f2pCollect(m), []int{2, 4, 6})
	if calls != 3 {
		t.Fatalf("Map did not apply f exactly 3 times, got %d", calls)
	}
	interleaveShortestPost(t)
}

// F2P-029: Dedup keeps non-adjacent repeats plus InterleaveShortest postcondition.
func TestF2P_029(t *testing.T) {
	got := f2pCollect(itertools.Dedup[int](core.FromSlice([]int{1, 1, 2, 2, 2, 3, 1}), func(a, b int) bool { return a == b }))
	f2pEq(t, "Dedup", got, []int{1, 2, 3, 1})
	interleaveShortestPost(t)
}

// F2P-030: ZipEq/Chunks panic + merge contract postcondition.
func TestF2P_030(t *testing.T) {
	merged := f2pCollect(itertools.Merge[int](core.FromSlice([]int{0, 3, 6, 9}), core.FromSlice([]int{0, 5, 10})))
	f2pEq(t, "Merge", merged, []int{0, 0, 3, 5, 6, 9, 10})
	f2pMustPanic(t, "ZipEq", func() {
		f2pCollect(itertools.ZipEq[int, int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{1, 2})))
	})
	f2pMustPanic(t, "Chunks", func() {
		f2pCollect(itertools.Chunks[int](core.FromSlice([]int{1, 2, 3}), 0))
	})
}

// F2P-031: AllEqualValueError / ExactlyOneError exact Display text.
func TestF2P_031(t *testing.T) {
	_, err := itertools.AllEqualValue[int](core.FromSlice([]int{}), func(a, b int) bool { return a == b })
	if err == nil || err.Error() != "got zero elements when all elements were expected to be equal" {
		t.Fatalf("AllEqualValue empty: got %v", err)
	}
	_, err = itertools.AllEqualValue[int](core.FromSlice([]int{1, 2}), func(a, b int) bool { return a == b })
	if err == nil || err.Error() != "got different elements when all elements were expected to be equal" {
		t.Fatalf("AllEqualValue unequal: got %v", err)
	}
	_, eo := itertools.ExactlyOne[int](core.FromSlice([]int{2, 3, 4}))
	if eo == nil || eo.Error() != "got at least 2 elements when exactly one was expected" {
		t.Fatalf("ExactlyOne >=2: got %v", eo)
	}
}

// F2P-032: exported identifiers / InterleaveShortest postcondition.
func TestF2P_032(t *testing.T) { interleaveShortestPost(t) }

// F2P-033: core.Iter protocol, exact size hints, NextBack, fused behavior.
func TestF2P_033(t *testing.T) {
	it := core.NewIterate(1, func(x int) int { return x * 2 })
	var seq []int
	for i := 0; i < 4; i++ {
		v, _ := it.Next()
		seq = append(seq, v)
	}
	f2pEq(t, "Iterate", seq, []int{1, 2, 4, 8})

	sh := itertools.Intersperse[int](core.FromSlice([]int{1, 2, 3}), 0).SizeHint()
	if sh != core.Hint(5, 5) {
		t.Fatalf("Intersperse SizeHint: got %+v, want {5 5}", sh)
	}

	m := itertools.Map[int, int](core.FromSlice([]int{1, 2, 3}), func(v int) int { return v * 10 })
	if b, ok := m.NextBack(); !ok || b != 30 {
		t.Fatalf("Map.NextBack over double-ended source: got (%d,%v)", b, ok)
	}
	interleaveShortestPost(t)
}

// F2P-034: target runtime dependency policy: Go build + core.FromSlice.
func TestF2P_034(t *testing.T) {
	f2pEq(t, "FromSlice", f2pCollect(core.FromSlice([]int{1, 2, 3})), []int{1, 2, 3})
}

// F2P-035: adaptor protocol postcondition.
func TestF2P_035(t *testing.T) { interleaveShortestPost(t) }

// F2P-036: ZipEq/Chunks panic postcondition.
func TestF2P_036(t *testing.T) {
	f2pMustPanic(t, "ZipEq", func() {
		f2pCollect(itertools.ZipEq[int, int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{1, 2})))
	})
	f2pMustPanic(t, "Chunks", func() {
		f2pCollect(itertools.Chunks[int](core.FromSlice([]int{1, 2, 3}), 0))
	})
}

// F2P-037: adaptor protocol postcondition.
func TestF2P_037(t *testing.T) { interleaveShortestPost(t) }

// F2P-038: exported identifiers / InterleaveShortest postcondition.
func TestF2P_038(t *testing.T) { interleaveShortestPost(t) }

// F2P-039: laziness and one-shot FormatWith.
func TestF2P_039(t *testing.T) {
	calls := 0
	m := itertools.Map[int, int](core.FromSlice([]int{1, 2, 3}), func(v int) int { calls++; return v })
	if calls != 0 {
		t.Fatalf("Map is not lazy: f called %d times before iteration", calls)
	}
	f2pCollect(m)
	if calls != 3 {
		t.Fatalf("Map did not apply f exactly 3 times, got %d", calls)
	}

	fw := itertools.FormatWith[int](core.FromSlice([]int{1, 2, 3}), ",", func(v int, write func(any) string) string {
		return write(v)
	})
	if got := fw.String(); got != "1,2,3" {
		t.Fatalf("FormatWith first render: got %q", got)
	}
	f2pMustPanic(t, "FormatWith second render", func() { fw.String() })

	interleaveShortestPost(t)
}

// F2P-040: CircularArrayWindows wrapping sequence.
func TestF2P_040(t *testing.T) {
	got := f2pCollect(itertools.CircularArrayWindows[int](core.FromSlice([]int{1, 2, 3, 4}), 3))
	f2pEq(t, "CircularArrayWindows k=4 n=3", got, [][]int{{1, 2, 3}, {2, 3, 4}, {3, 4, 1}, {4, 1, 2}})

	got = f2pCollect(itertools.CircularArrayWindows[int](core.FromSlice([]int{1, 2}), 3))
	f2pEq(t, "CircularArrayWindows k=2 n=3", got, [][]int{{1, 2, 1}, {2, 1, 2}})

	// ArrayWindows fixed-size boundary contract.
	aw := f2pCollect(itertools.ArrayWindows[int](core.FromSlice([]int{1, 2, 3, 4, 5}), 3))
	f2pEq(t, "ArrayWindows k=5 n=3", aw, [][]int{{1, 2, 3}, {2, 3, 4}, {3, 4, 5}})
	empty := f2pCollect(itertools.ArrayWindows[int](core.FromSlice([]int{1, 2, 3}), 0))
	f2pEq(t, "ArrayWindows k=3 n=0", empty, [][]int{{}, {}, {}, {}})
}

// F2P-041: size-hint saturation plus bulk specializations.
func TestF2P_041(t *testing.T) {
	if got := core.SubScalar(core.Hint(1, 2), 5); got != core.Hint(0, 0) {
		t.Fatalf("SubScalar: got %+v", got)
	}
	if got := core.Mul(core.Hint(3, -1), core.Hint(0, 0)); got != core.Hint(0, 0) {
		t.Fatalf("Mul with zero: got %+v", got)
	}
	if got := core.Mul(core.Hint(3, 4), core.Hint(3, 4)); got != core.Hint(9, 16) {
		t.Fatalf("Mul: got %+v", got)
	}

	if got := itertools.Fold[int, int](core.FromSlice([]int{1, 2, 3, 4}), 0, func(a, v int) int { return a + v }); got != 10 {
		t.Fatalf("Fold: got %d", got)
	}
	f2pEq(t, "CollectVec", itertools.CollectVec[int](core.FromSlice([]int{1, 2, 3})), []int{1, 2, 3})
	if s, ok := itertools.Sum1[int](core.FromSlice([]int{1, 2, 3})); !ok || s != 6 {
		t.Fatalf("Sum1: got (%d,%v)", s, ok)
	}
	if p, ok := itertools.Product1[int](core.FromSlice([]int{2, 3, 4})); !ok || p != 24 {
		t.Fatalf("Product1: got (%d,%v)", p, ok)
	}
}

// F2P-042: one-shot core.Format.String contract.
func TestF2P_042(t *testing.T) {
	f := core.NewFormat(core.FromSlice([]int{1, 2, 3}), "-", nil)
	if got := f.String(); got != "1-2-3" {
		t.Fatalf("Format first render: got %q", got)
	}
	f2pMustPanic(t, "Format second render", func() { f.String() })
}

// F2P-043: ZipEq/Chunks panic postcondition.
func TestF2P_043(t *testing.T) {
	f2pMustPanic(t, "ZipEq", func() {
		f2pCollect(itertools.ZipEq[int, int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{1, 2})))
	})
	f2pMustPanic(t, "Chunks", func() {
		f2pCollect(itertools.Chunks[int](core.FromSlice([]int{1, 2, 3}), 0))
	})
}

// F2P-044: ZipEq/Chunks panic postcondition.
func TestF2P_044(t *testing.T) {
	f2pMustPanic(t, "ZipEq", func() {
		f2pCollect(itertools.ZipEq[int, int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{1, 2})))
	})
	f2pMustPanic(t, "Chunks", func() {
		f2pCollect(itertools.Chunks[int](core.FromSlice([]int{1, 2, 3}), 0))
	})
}

// F2P-045: free.ProcessResults error propagation.
func TestF2P_045(t *testing.T) {
	pair := func(v int, err error) core.Pair[int, error] { return core.Pair[int, error]{First: v, Second: err} }
	likeCollect := func(ci core.Iter[int]) []int { return f2pCollect(ci) }

	pairs := core.FromSlice([]core.Pair[int, error]{pair(1, nil), pair(0, fmt.Errorf("boom")), pair(3, nil)})
	res, err := free.ProcessResults[int, []int](pairs, likeCollect)
	f2pEq(t, "ProcessResults error result", res, []int{1})
	if err == nil {
		t.Fatalf("ProcessResults did not propagate the error slot")
	}

	allOK := core.FromSlice([]core.Pair[int, error]{pair(1, nil), pair(2, nil)})
	res, err = free.ProcessResults[int, []int](allOK, likeCollect)
	f2pEq(t, "ProcessResults all-ok result", res, []int{1, 2})
	if err != nil {
		t.Fatalf("ProcessResults all-ok returned error %v", err)
	}
}

// F2P-046: dependency declaration: self-contained Go module, no Rust crate.
func TestF2P_046(t *testing.T) {
	got := f2pCollect(itertools.Interleave[int](core.FromSlice([]int{1, 2}), core.FromSlice([]int{3, 4})))
	f2pEq(t, "Interleave over Go slices", got, []int{1, 3, 2, 4})

	data, err := os.ReadFile("go.mod")
	if err != nil {
		t.Fatalf("Go module manifest missing: %v", err)
	}
	text := string(data)
	if !strings.Contains(text, "module itertools") {
		t.Fatalf("go.mod does not declare the itertools module: %q", text)
	}
	if strings.Contains(strings.ToLower(text), "rust") {
		t.Fatalf("go.mod references the Rust implementation: %q", text)
	}
}

// F2P-047: concrete iterator protocol from slice and repeat sources.
func TestF2P_047(t *testing.T) {
	s := core.FromSlice([]int{7})
	if got := s.SizeHint(); got != core.Hint(1, 1) {
		t.Fatalf("FromSlice initial SizeHint: got %+v", got)
	}
	if v, ok := s.Next(); !ok || v != 7 {
		t.Fatalf("FromSlice Next: got (%d,%v)", v, ok)
	}
	if got := s.SizeHint(); got != core.Hint(0, 0) {
		t.Fatalf("FromSlice exhausted SizeHint: got %+v", got)
	}
	if v, ok := s.Next(); ok {
		t.Fatalf("FromSlice Next after exhaustion: got (%d,%v)", v, ok)
	}

	f2pEq(t, "NewRepeatN", f2pCollect(core.NewRepeatN(3, 7)), []int{7, 7, 7})
}

// F2P-048: Interleave / InterleaveShortest element sequences.
func TestF2P_048(t *testing.T) {
	got := f2pCollect(itertools.Interleave[int](core.FromSlice([]int{1, 2}), core.FromSlice([]int{-1, -2})))
	f2pEq(t, "Interleave", got, []int{1, -1, 2, -2})
	interleaveShortestPost(t)
}

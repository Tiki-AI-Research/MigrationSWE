// Package itertools_test probe for BATCH-F2P-0001.
//
// Each TestF2Pxxx function executes the concrete entry/call named by the
// corresponding observation's atomic_claim and asserts its frozen-contract
// target postcondition on the real target technology (Go 1.23.0). The probe
// deliberately calls the public Go API (packages itertools, core and free)
// rather than checking that a package merely exists.
package itertools_test

import (
	"errors"
	"math"
	"reflect"
	"testing"

	it "itertools"
	"itertools/core"
	"itertools/free"
)

func f2pSlice[T any](v ...T) *core.Slice[T] { return core.FromSlice(v) }

func f2pCollect[T any](seq core.Iter[T]) []T {
	var out []T
	for {
		v, ok := seq.Next()
		if !ok {
			return out
		}
		out = append(out, v)
	}
}

func f2pPanics(f func()) (panicked bool) {
	defer func() {
		if recover() != nil {
			panicked = true
		}
	}()
	f()
	return false
}

func f2pWindows(seq *core.ArrayWindows[int]) [][]int {
	var out [][]int
	for {
		w, ok := seq.Next()
		if !ok {
			return out
		}
		out = append(out, append([]int(nil), w...))
	}
}

func f2pCircular(seq *core.CircularArrayWindows[int]) [][]int {
	var out [][]int
	for {
		w, ok := seq.Next()
		if !ok {
			return out
		}
		out = append(out, append([]int(nil), w...))
	}
}

type f2pRows interface{ Next() ([]int, bool) }

func f2pRowsOf(seq f2pRows) [][]int {
	var out [][]int
	for {
		row, ok := seq.Next()
		if !ok {
			return out
		}
		out = append(out, append([]int(nil), row...))
	}
}

// F2P-001 -> R108: InterleaveShortest keeps the trailing element of the longer
// input once the shorter one is selected.
func TestF2P001InterleaveShortest(t *testing.T) {
	got := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	want := []int{1, -1, 2, -2, 3}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("F2P-001 interleave_shortest = %v, want %v", got, want)
	}
}

// F2P-003 -> R102: SizeHint with negative Upper meaning None.
func TestF2P003SizeHintMapping(t *testing.T) {
	h := core.Hint(3, 3)
	if !h.IsExact() || h.Lower != 3 || h.Upper != 3 {
		t.Fatalf("F2P-003 Hint(3,3) = %+v exact=%v", h, h.IsExact())
	}
	r := core.RepeatIter(1).SizeHint()
	if r.Lower != math.MaxInt || r.Upper != -1 || r.Bounded() {
		t.Fatalf("F2P-003 RepeatIter size hint = %+v bounded=%v", r, r.Bounded())
	}
}

// F2P-004 -> R105: Rust panic! becomes a Go panic.
func TestF2P004PanicSemantics(t *testing.T) {
	if !f2pPanics(func() { f2pCollect(it.ZipEq(f2pSlice(1, 2, 3), f2pSlice(1, 2))) }) {
		t.Fatal("F2P-004 zip_eq unequal lengths did not panic")
	}
	if !f2pPanics(func() { _ = it.Chunks(f2pSlice(1, 2, 3), 0) }) {
		t.Fatal("F2P-004 chunks(0) did not panic")
	}
}

// F2P-005 -> R110: CircularArrayWindows wraps around.
func TestF2P005CircularArrayWindows(t *testing.T) {
	got := f2pCircular(it.CircularArrayWindows(f2pSlice(1, 2, 3, 4), 3))
	want := [][]int{{1, 2, 3}, {2, 3, 4}, {3, 4, 1}, {4, 1, 2}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("F2P-005 circular = %v, want %v", got, want)
	}
	short := f2pCircular(it.CircularArrayWindows(f2pSlice(1, 2), 3))
	wantShort := [][]int{{1, 2, 1}, {2, 1, 2}}
	if !reflect.DeepEqual(short, wantShort) {
		t.Fatalf("F2P-005 circular short = %v, want %v", short, wantShort)
	}
}

// F2P-006 -> R112: free.ProcessResults forwards the first error and stops.
func TestF2P006FreeProcessResults(t *testing.T) {
	boom := errors.New("boom")
	in := f2pSlice(
		core.Pair[int, error]{First: 1},
		core.Pair[int, error]{Second: boom},
		core.Pair[int, error]{First: 3},
	)
	var observed []int
	_, err := free.ProcessResults(in, func(seq core.Iter[int]) []int {
		observed = f2pCollect(seq)
		return observed
	})
	if err == nil || err != boom {
		t.Fatalf("F2P-006 error = %v, want %v", err, boom)
	}
	// Rust returns Err(E), not the processor's R, on failure. Check the
	// values actually supplied to the processor rather than an undefined R.
	if !reflect.DeepEqual(observed, []int{1}) {
		t.Fatalf("F2P-006 processor values = %v, want [1]", observed)
	}
	if tail, ok := in.Next(); !ok || tail.First != 3 || tail.Second != nil {
		t.Fatalf("F2P-006 consumed input after error: %v,%v", tail, ok)
	}
	okIn := f2pSlice(core.Pair[int, error]{First: 1}, core.Pair[int, error]{First: 2})
	res2, err2 := free.ProcessResults(okIn, func(seq core.Iter[int]) []int { return f2pCollect(seq) })
	if err2 != nil || !reflect.DeepEqual(res2, []int{1, 2}) {
		t.Fatalf("F2P-006 all-ok result = %v err = %v", res2, err2)
	}
}

// F2P-007 -> R109: ArrayWindows zero and short window boundaries.
func TestF2P007ArrayWindowsBoundary(t *testing.T) {
	zero := f2pWindows(it.ArrayWindows(f2pSlice(1, 2, 3), 0))
	if len(zero) != 4 {
		t.Fatalf("F2P-007 zero windows = %d, want 4", len(zero))
	}
	for i, w := range zero {
		if len(w) != 0 {
			t.Fatalf("F2P-007 window %d not empty: %v", i, w)
		}
	}
	if short := f2pWindows(it.ArrayWindows(f2pSlice(1, 2), 3)); len(short) != 0 {
		t.Fatalf("F2P-007 too short = %v, want none", short)
	}
}

// F2P-008 -> R101: Rust next() -> Option<T> maps to the (value, ok) protocol.
func TestF2P008FromSliceProtocol(t *testing.T) {
	seq := core.FromSlice([]int{7})
	if h := seq.SizeHint(); h.Lower != 1 || h.Upper != 1 {
		t.Fatalf("F2P-008 size hint before = %+v, want {1 1}", h)
	}
	if v, ok := seq.Next(); !ok || v != 7 {
		t.Fatalf("F2P-008 first Next = %v,%v", v, ok)
	}
	if v, ok := seq.Next(); ok || v != 0 {
		t.Fatalf("F2P-008 exhausted Next = %v,%v", v, ok)
	}
	if h := seq.SizeHint(); h.Lower != 0 || h.Upper != 0 {
		t.Fatalf("F2P-008 size hint exhausted = %+v, want {0 0}", h)
	}
}

// F2P-013 -> R111: iproduct!/izip!/chain! become variadic Go functions.
func TestF2P013MacrosAsVariadic(t *testing.T) {
	iz := f2pRowsOf(it.Izip(f2pSlice(1, 2, 3), f2pSlice(10, 20)))
	if !reflect.DeepEqual(iz, [][]int{{1, 10}, {2, 20}}) {
		t.Fatalf("F2P-013 izip = %v", iz)
	}
	ip := f2pRowsOf(it.Iproduct(f2pSlice(1, 2), f2pSlice(10, 20)))
	if !reflect.DeepEqual(ip, [][]int{{1, 10}, {1, 20}, {2, 10}, {2, 20}}) {
		t.Fatalf("F2P-013 iproduct = %v", ip)
	}
	cc := f2pCollect(it.ChainConcatenates(f2pSlice(1), f2pSlice(2, 3), f2pSlice(4)))
	if !reflect.DeepEqual(cc, []int{1, 2, 3, 4}) {
		t.Fatalf("F2P-013 chain = %v", cc)
	}
}

// F2P-016 -> R106: Format consumes once and panics on the second attempt.
func TestF2P016FormatOneShot(t *testing.T) {
	f := it.Format(f2pSlice(1, 2, 3), "-")
	if s := f.String(); s != "1-2-3" {
		t.Fatalf("F2P-016 format = %q", s)
	}
	if !f2pPanics(func() { _ = f.String() }) {
		t.Fatal("F2P-016 second format did not panic")
	}
}

// F2P-022 -> R103: AllEqualValueError keeps the exact Rust Display strings.
func TestF2P022AllEqualValueErrors(t *testing.T) {
	eq := func(a, b int) bool { return a == b }
	_, err := it.AllEqualValue(f2pSlice[int](), eq)
	if err == nil || err.Error() != "got zero elements when all elements were expected to be equal" {
		t.Fatalf("F2P-022 zero error = %v", err)
	}
	_, err = it.AllEqualValue(f2pSlice(1, 2), eq)
	if err == nil || err.Error() != "got different elements when all elements were expected to be equal" {
		t.Fatalf("F2P-022 unequal error = %v", err)
	}
}

// F2P-024 -> R114: ExactlyOneError restores the consumed elements.
func TestF2P024ExactlyOneRestores(t *testing.T) {
	_, err := it.ExactlyOne(f2pSlice(2, 3, 4))
	if err == nil || err.Error() != "got at least 2 elements when exactly one was expected" {
		t.Fatalf("F2P-024 error = %v", err)
	}
	if restored := f2pCollect[int](err); !reflect.DeepEqual(restored, []int{2, 3, 4}) {
		t.Fatalf("F2P-024 restored = %v, want [2 3 4]", restored)
	}
}

// F2P-002 -> R108: Interleave alternates to exhaustion, InterleaveShortest stops.
func TestF2P002InterleavePair(t *testing.T) {
	got := f2pCollect(it.Interleave(f2pSlice(1, 2), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(got, []int{1, -1, 2, -2}) {
		t.Fatalf("F2P-002 interleave = %v", got)
	}
	short := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(short, []int{1, -1, 2, -2, 3}) {
		t.Fatalf("F2P-002 interleave_shortest = %v", short)
	}
}

// F2P-009 -> src/adaptors/mod.rs scope; predicate R108 InterleaveShortest.
func TestF2P009InterleaveShortest(t *testing.T) {
	got := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(got, []int{1, -1, 2, -2, 3}) {
		t.Fatalf("F2P-009 interleave_shortest = %v", got)
	}
}

// F2P-010 -> src/next_array.rs scope; predicate R109 ArrayWindows boundary.
func TestF2P010ArrayWindowsBoundary(t *testing.T) {
	zero := f2pWindows(it.ArrayWindows(f2pSlice(1, 2, 3), 0))
	if len(zero) != 4 {
		t.Fatalf("F2P-010 zero windows = %d, want 4", len(zero))
	}
	for i, w := range zero {
		if len(w) != 0 {
			t.Fatalf("F2P-010 window %d not empty: %v", i, w)
		}
	}
	if short := f2pWindows(it.ArrayWindows(f2pSlice(1, 2), 3)); len(short) != 0 {
		t.Fatalf("F2P-010 too short = %v, want none", short)
	}
}

// F2P-011 -> src/take_while_inclusive.rs scope; predicate R108.
func TestF2P011InterleaveShortest(t *testing.T) {
	got := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(got, []int{1, -1, 2, -2, 3}) {
		t.Fatalf("F2P-011 interleave_shortest = %v", got)
	}
}

// F2P-012 -> src/k_smallest.rs scope; predicate R105 panic semantics.
func TestF2P012PanicSemantics(t *testing.T) {
	if !f2pPanics(func() { f2pCollect(it.ZipEq(f2pSlice(1, 2, 3), f2pSlice(1, 2))) }) {
		t.Fatal("F2P-012 zip_eq unequal lengths did not panic")
	}
	if !f2pPanics(func() { _ = it.Chunks(f2pSlice(1, 2, 3), 0) }) {
		t.Fatal("F2P-012 chunks(0) did not panic")
	}
}

// F2P-014 -> src/take_while_inclusive.rs scope; predicate R108.
func TestF2P014InterleaveShortest(t *testing.T) {
	got := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(got, []int{1, -1, 2, -2, 3}) {
		t.Fatalf("F2P-014 interleave_shortest = %v", got)
	}
}

// F2P-015 -> src/pad_tail.rs scope; predicate R108.
func TestF2P015InterleaveShortest(t *testing.T) {
	got := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(got, []int{1, -1, 2, -2, 3}) {
		t.Fatalf("F2P-015 interleave_shortest = %v", got)
	}
}

// F2P-017 -> src/pad_tail.rs scope; predicate R108.
func TestF2P017InterleaveShortest(t *testing.T) {
	got := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(got, []int{1, -1, 2, -2, 3}) {
		t.Fatalf("F2P-017 interleave_shortest = %v", got)
	}
}

// F2P-018 -> src/array_impl.rs scope; predicate R110 CircularArrayWindows.
func TestF2P018CircularArrayWindows(t *testing.T) {
	got := f2pCircular(it.CircularArrayWindows(f2pSlice(1, 2, 3, 4), 3))
	want := [][]int{{1, 2, 3}, {2, 3, 4}, {3, 4, 1}, {4, 1, 2}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("F2P-018 circular = %v, want %v", got, want)
	}
	short := f2pCircular(it.CircularArrayWindows(f2pSlice(1, 2), 3))
	if !reflect.DeepEqual(short, [][]int{{1, 2, 1}, {2, 1, 2}}) {
		t.Fatalf("F2P-018 circular short = %v", short)
	}
}

// F2P-019 -> src/lazy_buffer.rs scope; predicate R102 size-hint mapping.
func TestF2P019SizeHintMapping(t *testing.T) {
	h := core.Hint(3, 3)
	if !h.IsExact() || h.Lower != 3 || h.Upper != 3 {
		t.Fatalf("F2P-019 Hint(3,3) = %+v", h)
	}
	r := core.RepeatIter(1).SizeHint()
	if r.Lower != math.MaxInt || r.Upper != -1 || r.Bounded() {
		t.Fatalf("F2P-019 RepeatIter size hint = %+v", r)
	}
}

// F2P-020 -> src/repeatn.rs scope; predicate R102 size-hint mapping.
func TestF2P020SizeHintMapping(t *testing.T) {
	h := core.Hint(3, 3)
	if !h.IsExact() || h.Lower != 3 || h.Upper != 3 {
		t.Fatalf("F2P-020 Hint(3,3) = %+v", h)
	}
	r := core.RepeatIter(1).SizeHint()
	if r.Lower != math.MaxInt || r.Upper != -1 || r.Bounded() {
		t.Fatalf("F2P-020 RepeatIter size hint = %+v", r)
	}
}

// F2P-021 -> src/adaptors/mod.rs scope; predicate R108 Interleave pair.
func TestF2P021InterleavePair(t *testing.T) {
	got := f2pCollect(it.Interleave(f2pSlice(1, 2), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(got, []int{1, -1, 2, -2}) {
		t.Fatalf("F2P-021 interleave = %v", got)
	}
	short := f2pCollect(it.InterleaveShortest(f2pSlice(1, 2, 3), f2pSlice(-1, -2)))
	if !reflect.DeepEqual(short, []int{1, -1, 2, -2, 3}) {
		t.Fatalf("F2P-021 interleave_shortest = %v", short)
	}
}

// F2P-023 -> src/sources.rs scope; predicate R102 size-hint mapping.
func TestF2P023SizeHintMapping(t *testing.T) {
	h := core.Hint(3, 3)
	if !h.IsExact() || h.Lower != 3 || h.Upper != 3 {
		t.Fatalf("F2P-023 Hint(3,3) = %+v", h)
	}
	r := core.RepeatIter(1).SizeHint()
	if r.Lower != math.MaxInt || r.Upper != -1 || r.Bounded() {
		t.Fatalf("F2P-023 RepeatIter size hint = %+v", r)
	}
}

package itertools_test

// Per-observation target probes for BATCH-F2P-0003.
//
// Each TestF2P* function exercises the concrete entry point named by the
// corresponding F2P observation and asserts the behavior / required
// postcondition of its atomic claim against a real copy of the Go module.
// The file is authored for the private authoring target and is copied into a
// scratch copy of the module by run_target_probes.py; it is never the public
// harness entry point.

import (
	"errors"
	"go/ast"
	"go/build"
	"go/parser"
	"go/token"
	"path/filepath"
	"strings"
	"math"
	"reflect"
	"testing"
	"time"

	it "itertools"
	"itertools/core"
	"itertools/free"
)

func mustEq(t *testing.T, label string, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%s: got %#v, want %#v", label, got, want)
	}
}

func checkInterleave(t *testing.T) {
	t.Helper()
	got := it.CollectVec[int](it.Interleave[int](core.FromSlice([]int{1, 2}), core.FromSlice([]int{-1, -2})))
	mustEq(t, "Interleave", got, []int{1, -1, 2, -2})
	got2 := it.CollectVec[int](it.InterleaveShortest[int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{-1, -2})))
	mustEq(t, "InterleaveShortest", got2, []int{1, -1, 2, -2, 3})
}

func checkExports(t *testing.T) {
	t.Helper()
	f2pAuditSurface(t)
	iz := it.CollectVec[[]int](it.Izip[int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{10, 20})))
	mustEq(t, "Izip", iz, [][]int{{1, 10}, {2, 20}})
	ip := it.CollectVec[[]int](it.Iproduct[int](core.FromSlice([]int{1, 2}), core.FromSlice([]int{10, 20})))
	mustEq(t, "Iproduct", ip, [][]int{{1, 10}, {1, 20}, {2, 10}, {2, 20}})
	cc := it.CollectVec[int](it.ChainConcatenates[int](core.FromSlice([]int{1}), core.FromSlice([]int{2, 3}), core.FromSlice([]int{4})))
	mustEq(t, "ChainConcatenates", cc, []int{1, 2, 3, 4})
}

func checkEitherOrBoth(t *testing.T) {
	t.Helper()
	b := core.Both[int, string](1, "a")
	if !b.IsBoth() {
		t.Fatal("Both(1,a) should report IsBoth")
	}
	if v, ok := b.Left(); !ok || v != 1 {
		t.Fatalf("Both.Left = %v,%v", v, ok)
	}
	if v, ok := b.Right(); !ok || v != "a" {
		t.Fatalf("Both.Right = %v,%v", v, ok)
	}
	l := core.Left[int, string](1)
	if !l.IsLeft() {
		t.Fatal("Left(1) should report IsLeft")
	}
	if v, ok := l.JustRight(); ok || v != "" {
		t.Fatalf("Left.JustRight = %v,%v (want zero,false)", v, ok)
	}
	r := core.Right[int, string]("b")
	if r.HasLeft() {
		t.Fatal("Right(b) should not report HasLeft")
	}
}

func checkPositions(t *testing.T) {
	t.Helper()
	in := []int{1, 1, -1, -1}
	if p, ok := it.PositionMax(core.FromSlice(in)); !ok || p.First != 1 || p.Second != 1 {
		t.Fatalf("PositionMax = %#v,%v", p, ok)
	}
	if p, ok := it.PositionMin(core.FromSlice(in)); !ok || p.First != 2 || p.Second != -1 {
		t.Fatalf("PositionMin = %#v,%v", p, ok)
	}
	mm := it.PositionMinMax(core.FromSlice(in))
	if mm.Kind != core.MinMaxPair || mm.Min != 2 || mm.Max != 1 {
		t.Fatalf("PositionMinMax = %#v (want min idx 2, max idx 1)", mm)
	}
	type kv struct {
		K  int
		ID int
	}
	items := []kv{{1, 0}, {1, 1}, {-1, 2}, {-1, 3}}
	r := it.MinmaxByKey[kv](core.FromSlice(items), func(v kv) int { return v.K })
	if r.Kind != core.MinMaxPair || r.Min.ID != 2 || r.Max.ID != 1 {
		t.Fatalf("MinmaxByKey tie-break = %#v (want first min ID 2, last max ID 1)", r)
	}
}

func checkLaziness(t *testing.T) {
	t.Helper()
	f2pAuditConstructors(t)
	calls := 0
	m := it.Map[int, int](core.FromSlice([]int{1, 2, 3}), func(v int) int { calls++; return v })
	if calls != 0 {
		t.Fatalf("Map invoked its function %d times before consumption", calls)
	}
	_ = it.CollectVec[int](m)
	if calls != 3 {
		t.Fatalf("Map invoked its function %d times after consumption, want 3", calls)
	}
	f := it.FormatWith[int](core.FromSlice([]int{1, 2, 3}), ",", func(v int, write func(any) string) string { return write(v) })
	if s := f.String(); s != "1,2,3" {
		t.Fatalf("FormatWith.String = %q, want %q", s, "1,2,3")
	}
	func() {
		defer func() {
			if recover() == nil {
				t.Fatal("second FormatWith.String did not panic (one-shot contract)")
			}
		}()
		_ = f.String()
	}()
}

func checkMerge(t *testing.T) {
	t.Helper()
	got := it.CollectVec[int](it.Merge[int](core.FromSlice([]int{0, 3, 6, 9}), core.FromSlice([]int{0, 5, 10})))
	mustEq(t, "Merge", got, []int{0, 0, 3, 5, 6, 9, 10})
	km := it.CollectVec[int](it.KMerge[int]([]core.Iter[int]{
		core.FromSlice([]int{0, 2, 4}),
		core.FromSlice([]int{1, 3, 5}),
		core.FromSlice([]int{6, 7}),
	}))
	mustEq(t, "KMerge", km, []int{0, 1, 2, 3, 4, 5, 6, 7})
}

func checkPanics(t *testing.T) {
	t.Helper()
	func() {
		defer func() {
			if recover() == nil {
				t.Fatal("ZipEq with unequal lengths did not panic")
			}
		}()
		_ = it.CollectVec[core.Pair[int, int]](it.ZipEq[int, int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{1, 2})))
	}()
	func() {
		defer func() {
			if recover() == nil {
				t.Fatal("Chunks with size 0 did not panic")
			}
		}()
		_ = it.Chunks[int](core.FromSlice([]int{1, 2, 3}), 0)
	}()
}

func checkDedup(t *testing.T) {
	t.Helper()
	got := it.CollectVec[int](it.Dedup[int](core.FromSlice([]int{1, 1, 2, 2, 2, 3, 1}), func(a, b int) bool { return a == b }))
	mustEq(t, "Dedup", got, []int{1, 2, 3, 1})
}

func checkProtocol(t *testing.T) {
	t.Helper()
	f2pAuditCapabilityBehavior(t)
	f2pAuditWindows(t)
	f2pAuditIterate(t)
	f2pAuditHintArithmetic(t)
	it2 := free.Iterate[int](1, func(v int) int { return v * 2 })
	first := make([]int, 0, 4)
	for i := 0; i < 4; i++ {
		v, ok := it2.Next()
		if !ok {
			t.Fatal("Iterate ended before yielding 4 elements")
		}
		first = append(first, v)
	}
	mustEq(t, "Iterate", first, []int{1, 2, 4, 8})

	inter := it.Intersperse[int](core.FromSlice([]int{1, 2, 3}), 0)
	if h := inter.SizeHint(); h != core.Hint(5, 5) {
		t.Fatalf("Intersperse SizeHint = %#v, want {5 5}", h)
	}

	m := it.Map[int, int](core.FromSlice([]int{1, 2, 3}), func(v int) int { return v })
	if back, ok := m.NextBack(); !ok || back != 3 {
		t.Fatalf("Map.NextBack = %v,%v over a double-ended source", back, ok)
	}

	f := it.Filter[int](core.FromSlice([]int{9}), func(v int) bool { return true })
	if _, ok := f.Next(); !ok {
		t.Fatal("Filter yielded nothing")
	}
	for i := 0; i < 3; i++ {
		if _, ok := f.Next(); ok {
			t.Fatal("adaptor is not fused after exhaustion")
		}
	}
}

func checkAllEqualValue(t *testing.T) {
	t.Helper()
	_, err := it.AllEqualValue[int](core.FromSlice([]int{}), func(a, b int) bool { return a == b })
	if err == nil || err.Error() != "got zero elements when all elements were expected to be equal" {
		t.Fatalf("zero-element AllEqualValue err = %v", err)
	}
	_, err = it.AllEqualValue[int](core.FromSlice([]int{1, 2}), func(a, b int) bool { return a == b })
	if err == nil || err.Error() != "got different elements when all elements were expected to be equal" {
		t.Fatalf("unequal AllEqualValue err = %v", err)
	}
	v, err := it.AllEqualValue[int](core.FromSlice([]int{5, 5, 5}), func(a, b int) bool { return a == b })
	if err != nil || v != 5 {
		t.Fatalf("equal AllEqualValue = %v,%v", v, err)
	}
}

func checkSizeHintArith(t *testing.T) {
	t.Helper()
	if got := core.SubScalar(core.Hint(1, 2), 5); got != core.Hint(0, 0) {
		t.Fatalf("SubScalar(Hint(1,2),5) = %#v", got)
	}
	if got := core.Mul(core.Hint(3, -1), core.Hint(0, 0)); got != core.Hint(0, 0) {
		t.Fatalf("Mul(Hint(3,-1),Hint(0,0)) = %#v", got)
	}
	if got := core.Mul(core.Hint(3, 4), core.Hint(3, 4)); got != core.Hint(9, 16) {
		t.Fatalf("Mul(Hint(3,4),Hint(3,4)) = %#v", got)
	}
}

func checkRepeatHints(t *testing.T) {
	t.Helper()
	if !core.Hint(3, 3).IsExact() {
		t.Fatal("Hint(3,3) should be exact")
	}
	rh := core.RepeatIter(1).SizeHint()
	if rh != core.Hint(math.MaxInt, -1) {
		t.Fatalf("RepeatIter SizeHint = %#v, want {MaxInt -1}", rh)
	}
	if rh.Bounded() {
		t.Fatal("RepeatIter should report an unbounded size hint")
	}
	if s := core.FromSlice([]int{1, 2, 3}).SizeHint(); s != core.Hint(3, 3) {
		t.Fatalf("FromSlice SizeHint = %#v, want {3 3}", s)
	}
	mustEq(t, "RepeatN", it.CollectVec[int](core.NewRepeatN(3, 7)), []int{7, 7, 7})
}

func checkProcessResults(t *testing.T) {
	t.Helper()
	boom := errors.New("boom")
	pairs := core.FromSlice([]core.Pair[int, error]{
		{First: 1},
		{Second: boom},
		{First: 3},
	})
	var observed []int
	_, err := free.ProcessResults[int, []int](pairs, func(i core.Iter[int]) []int {
		observed = it.CollectVec[int](i)
		return observed
	})
	if err != boom {
		t.Fatalf("ProcessResults err = %v, want boom", err)
	}
	// The Rust Result contains no R on Err. The observable prefix is checked
	// inside the processor, so either Go error-result convention is legal.
	mustEq(t, "ProcessResults processor values", observed, []int{1})
	if tail, ok := pairs.Next(); !ok || tail.First != 3 || tail.Second != nil {
		t.Fatalf("ProcessResults consumed input after error: %v,%v", tail, ok)
	}

	allOK := core.FromSlice([]core.Pair[int, error]{{First: 1}, {First: 2}})
	res2, err2 := free.ProcessResults[int, []int](allOK, func(i core.Iter[int]) []int {
		return it.CollectVec[int](i)
	})
	if err2 != nil {
		t.Fatalf("ProcessResults all-ok err = %v", err2)
	}
	mustEq(t, "ProcessResults all-ok res", res2, []int{1, 2})
}

func checkExactlyOne(t *testing.T) {
	t.Helper()
	_, err := it.ExactlyOne[int](core.FromSlice([]int{2, 3, 4}))
	if err == nil {
		t.Fatal("ExactlyOne(2,3,4) should return an error")
	}
	if err.Error() != "got at least 2 elements when exactly one was expected" {
		t.Fatalf("ExactlyOne err = %q", err.Error())
	}
	mustEq(t, "ExactlyOne restore", it.CollectVec[int](err), []int{2, 3, 4})
	v, err2 := it.ExactlyOne[int](core.FromSlice([]int{7}))
	if err2 != nil || v != 7 {
		t.Fatalf("ExactlyOne(7) = %v,%v", v, err2)
	}
}

func TestF2P049Interleave(t *testing.T)     { checkInterleave(t) }
func TestF2P051Exports(t *testing.T)        { checkExports(t) }
func TestF2P052Interleave(t *testing.T)     { checkInterleave(t) }
func TestF2P053EitherOrBoth(t *testing.T)   { checkEitherOrBoth(t) }
func TestF2P054Positions(t *testing.T)      { checkPositions(t) }
func TestF2P055Laziness(t *testing.T)      { checkInterleave(t); checkLaziness(t) }
func TestF2P056MergeAndPanics(t *testing.T) { checkMerge(t); checkPanics(t) }
func TestF2P057Dedup(t *testing.T)          { checkInterleave(t); checkDedup(t) }
func TestF2P058Dedup(t *testing.T)          { checkInterleave(t); checkDedup(t) }
func TestF2P059Protocol(t *testing.T)       { checkInterleave(t); checkProtocol(t) }
func TestF2P060AllEqualValue(t *testing.T)  { checkAllEqualValue(t); checkExactlyOne(t) }
func TestF2P061EitherOrBoth(t *testing.T)   { checkEitherOrBoth(t) }
func TestF2P062Positions(t *testing.T)      { checkInterleave(t); checkPositions(t) }
func TestF2P063SizeHintArith(t *testing.T)  { checkSizeHintArith(t) }
func TestF2P064Protocol(t *testing.T)       { checkInterleave(t); checkProtocol(t) }
func TestF2P065SizeHints(t *testing.T)      { checkRepeatHints(t) }
func TestF2P066ProcessResults(t *testing.T) { checkProcessResults(t) }
func TestF2P067Laziness(t *testing.T)      { checkInterleave(t); checkLaziness(t) }
func TestF2P068Interleave(t *testing.T)     { checkInterleave(t) }
func TestF2P069Panics(t *testing.T)         { checkPanics(t) }
func TestF2P071ExactlyOne(t *testing.T)     { checkExactlyOne(t) }

// The public contract includes the complete trait/free surface, not just the
// handful of examples used by the older corpus. Inspect selected production
// files through Go's parser/build rules, so comments and test-only stubs do
// not count as exports and each named root/free operation must be generic.
func f2pAuditDeclarations(t *testing.T, dir string, tags []string) map[string]bool {
    t.Helper()
    ctx := build.Default
    ctx.BuildTags = tags
    pkg, err := ctx.ImportDir(dir, 0)
    if err != nil { t.Fatalf("public package %s: %v", dir, err) }
    names := map[string]bool{}
    for _, name := range pkg.GoFiles {
        file, err := parser.ParseFile(token.NewFileSet(), filepath.Join(dir, name), nil, 0)
        if err != nil { t.Fatal(err) }
        for _, decl := range file.Decls {
            switch d := decl.(type) {
            case *ast.FuncDecl:
                if d.Recv == nil { names[d.Name.Name] = d.Type.TypeParams != nil }
            case *ast.GenDecl:
                for _, spec := range d.Specs {
                    switch value := spec.(type) {
                    case *ast.TypeSpec: names[value.Name.Name] = true
                    case *ast.ValueSpec:
                        for _, name := range value.Names { names[name.Name] = true }
                    }
                }
            }
        }
    }
    return names
}
func f2pAuditSurface(t *testing.T) {
    t.Helper()
    // GroupBy is a deprecated Rust alias of ChunkBy, not a separate concrete
    // adaptor. The root GroupBy operation remains required below.
    required := map[string]string{
        ".": "All AllEqual AllEqualValue AllUnique AllUniqueWithHasher Any ArrayCombinations ArrayCombinationsWithReplacement ArrayWindows AtMostOne Batching CartesianProduct Chain ChainConcatenates ChunkBy Chunks CircularArrayWindows CircularTupleWindows Coalesce CollectArray CollectTuple CollectVec Combinations CombinationsWithReplacement Concat Contains Counts CountsBy CountsByWithHasher CountsWithHasher Dedup DedupBy DedupByWithCount DedupWithCount Dropping DroppingBack Duplicates DuplicatesBy DuplicatesByWithHasher DuplicatesWithHasher Enumerate ExactlyOne Filter FilterMap FilterMapOk FilterOk FindOrFirst FindOrLast FindPosition FlattenOK Fold Fold1 FoldOK FoldOptions FoldWhile Format FormatWith Get GroupBy Interleave InterleaveShortest Intersperse IntersperseWith IntoGroupMap IntoGroupMapBy IntoGroupMapByWithHasher IntoGroupMapWithHasher IntoGroupingMap IntoGroupingMapBy IntoGroupingMapByWithHasher IntoGroupingMapWithHasher Iproduct Izip Join KLargest KLargestBy KLargestByKey KLargestRelaxed KLargestRelaxedBy KLargestRelaxedByKey KMerge KMergeBy KSmallest KSmallestBy KSmallestByKey KSmallestRelaxed KSmallestRelaxedBy KSmallestRelaxedByKey MapInto MapOk MaxSet MaxSetBy MaxSetByKey Merge MergeBy MergeJoinBy MinSet MinSetBy MinSetByKey Minmax MinmaxBy MinmaxByKey MultiCartesianProduct MultiPeek Multiunzip NextArray NextTuple PadUsing PartitionMap PartitionResult PeekingTakeWhile Permutations PositionMax PositionMaxBy PositionMaxByKey PositionMin PositionMinBy PositionMinByKey PositionMinMax PositionMinmaxBy PositionMinmaxByKey Positions Powerset ProcessResults Product1 Rev SetFrom Sorted SortedBy SortedByCachedKey SortedByKey SortedUnstable SortedUnstableBy SortedUnstableByKey StripPrefix StripPrefixBy Sum1 Tail TakeWhileInclusive TakeWhileRef Tee TreeFold1 TreeReduce TryCollect TryLen TupleCombinations TupleWindows Tuples Unique UniqueBy UniqueByWithHasher UniqueWithHasher Update WhileSome WithPosition Zip ZipEq ZipLongest",
        "core": "Add AddScalar AllEqualValueError ArrayCombinations ArrayWindows Batching Both Chunk ChunkBy Chunks CircularArrayWindows CircularTupleWindows Coalesce Combinations CombinationsWithReplacement ConsTuples Dedup DedupBy DedupByWithCount DedupWithCount DoubleEnded Duplicates DuplicatesBy EitherOrBoth EmptyIter ExactlyOneError FilterMapOk FilterOk FlattenOk Format FormatWith FormatWithFunc FromSlice Fused Group GroupingMap GroupingMapBy Groups Hint Interleave InterleaveShortest Intersperse IntersperseWith IntoChunks Iter Iterate KMerge KMergeBy Left Map MapInto MapOk Max Merge MergeBy MergeJoinBy Min MinMaxNoElements MinMaxOneElement MinMaxPair MinMaxResult MinMaxResultKind Mul MulScalar MultiPeek MultiProduct NewAllEqualValueErrorUnequal NewAllEqualValueErrorZero NewArrayWindows NewCircularArrayWindows NewFormat NewFormatWith NewIterate NewMap NewRepeatN NewUnfold OnceIter PadUsing Pair PeekNth PeekingTakeWhile Permutations Positions Powerset ProcessResults Product PutBack PutBackN RcIter Repeat RepeatIter RepeatN Right SizeHint Slice SubScalar TakeWhileInclusive TakeWhileRef Tee TupleBuffer TupleCombinations TupleWindows Tuples Unfold Unique UniqueBy Update WhileSome WithPosition Zip ZipEq ZipLongest",
        "free": "All Any Chain Cloned Concat ConsTuples DiffWith Enumerate Fold Intersperse IntersperseWith Iterate Join KMergeBy Max Min Multiunzip Multizip ProcessResults RepeatN Rev Sorted SortedUnstable Unfold Zip",
    }
    for dir, list := range required {
        names := f2pAuditDeclarations(t, dir, nil)
        for _, name := range strings.Fields(list) {
            generic, exists := names[name]
            if !exists || dir != "core" && !generic {
                t.Errorf("%s.%s: missing public declaration or non-generic operation", dir, name)
            }
        }
    }
    for _, tag := range []string{"nostd", "noalloc"} {
        names := f2pAuditDeclarations(t, ".", []string{tag})
        for _, name := range strings.Fields("Map Filter Fold ArrayWindows CircularArrayWindows Format FormatWith Interleave Izip ChainConcatenates ExactlyOne TupleCombinations TryCollect PartitionMap PartitionResult Multiunzip") {
            if _, ok := names[name]; !ok { t.Errorf("%s unexpectedly absent with %s", name, tag) }
        }
        excluded := "Unique Duplicates Counts IntoGroupingMap"
        if tag == "noalloc" { excluded += " Iproduct Combinations Permutations Powerset Sorted" }
        for _, name := range strings.Fields(excluded) {
            if _, ok := names[name]; ok { t.Errorf("%s must be excluded with %s", name, tag) }
        }
    }
}

type f2pAuditSource struct { values []int; index, calls int }
func (s *f2pAuditSource) Next() (int, bool) {
    s.calls++
    if s.index == len(s.values) { return 0, false }
    value := s.values[s.index]; s.index++; return value, true
}
func (s *f2pAuditSource) SizeHint() core.SizeHint { return core.Hint(0, -1) }
func (s *f2pAuditSource) Clone() *f2pAuditSource { copy := *s; return &copy }

// Callback-based cloning is a legal projection of Rust's Clone bound.
func f2pCloneIntIterator(source core.Iter[int]) core.Iter[int] {
    method := reflect.ValueOf(source).MethodByName("Clone")
    if !method.IsValid() { panic("probe source has no independent clone") }
    return method.Call(nil)[0].Interface().(core.Iter[int])
}

// CartesianProduct's result signature is not fixed in the instruction.
// Normalize a two-item slice projection without weakening value/order checks.
func f2pProductPair(t *testing.T, value any) core.Pair[int, int] {
    t.Helper()
    switch pair := value.(type) {
    case core.Pair[int, int]: return pair
    case []int:
        if len(pair) == 2 { return core.Pair[int, int]{First: pair[0], Second: pair[1]} }
    }
    t.Fatalf("product must yield two items, got %v", value)
    return core.Pair[int, int]{}
}

type f2pFlattenProjection struct { t *testing.T; iterator reflect.Value }
func f2pFlattenIterator(t *testing.T, iterator any) *f2pFlattenProjection {
    t.Helper()
    value := reflect.ValueOf(iterator)
    for _, method := range []string{"Next", "NextBack", "SizeHint"} {
        if !value.MethodByName(method).IsValid() {
            if method == "NextBack" { t.Fatal("FlattenOK lost DoubleEnded for double-ended outer and inner inputs") }
            t.Fatalf("FlattenOK lost %s", method)
        }
    }
    return &f2pFlattenProjection{t: t, iterator: value}
}
func (f *f2pFlattenProjection) step(method string) (core.Pair[int, error], bool) {
    f.t.Helper()
    result := f.iterator.MethodByName(method).Call(nil)
    if len(result) != 2 { f.t.Fatal("FlattenOK lost two-result iteration") }
    if !result[1].Bool() { return core.Pair[int, error]{}, false }
    if pair,ok := result[0].Interface().(core.Pair[int, error]); ok { return pair, true }
    item := result[0]
    if item.Kind() == reflect.Struct {
        value,err := item.FieldByName("Value"), item.FieldByName("Err")
        if value.IsValid() && err.IsValid() {
            number,ok := value.Interface().(int)
            if ok {
                var failure error
                if err.Interface() != nil {
                    var valid bool
                    failure,valid = err.Interface().(error)
                    if !valid { f.t.Fatal("FlattenOK changed the error payload") }
                }
                return core.Pair[int, error]{First: number, Second: failure}, true
            }
        }
    }
    f.t.Fatalf("FlattenOK changed its outcome payload: %v", result[0].Interface())
    return core.Pair[int, error]{}, false
}
func (f *f2pFlattenProjection) Next() (core.Pair[int, error], bool) { return f.step("Next") }
func (f *f2pFlattenProjection) NextBack() (core.Pair[int, error], bool) { return f.step("NextBack") }
func (f *f2pFlattenProjection) SizeHint() core.SizeHint {
    return f.iterator.MethodByName("SizeHint").Call(nil)[0].Interface().(core.SizeHint)
}

func f2pAuditWindows(t *testing.T) {
    t.Helper()
    for k := 0; k <= 7; k++ {
        input := make([]int, k)
        for i := range input { input[i] = i+1 }
        for n := 0; n <= 6; n++ {
            windows := it.ArrayWindows(core.FromSlice(input), n)
            count := max(0, k-n+1)
            for remaining := count; remaining >= 0; remaining-- {
                if h := windows.SizeHint(); h != core.Hint(remaining, remaining) {
                    t.Fatalf("ArrayWindows k=%d n=%d remaining=%d hint=%v", k,n,remaining,h)
                }
                value, ok := windows.Next()
                if ok != (remaining > 0) { t.Fatalf("ArrayWindows count k=%d n=%d", k,n) }
                if ok {
                    if len(value) != n { t.Fatal("window size changed") }
                    start := count-remaining
                    for j, v := range value { if v != input[start+j] { t.Fatal("window contents changed") } }
                }
            }
            circular := it.CircularArrayWindows(core.FromSlice(input), n)
            for remaining := k; remaining >= 0; remaining-- {
                if h := circular.SizeHint(); h != core.Hint(remaining,remaining) {
                    t.Fatalf("CircularArrayWindows k=%d n=%d remaining=%d hint=%v",k,n,remaining,h)
                }
                value, ok := circular.Next()
                if ok != (remaining > 0) { t.Fatalf("circular count k=%d n=%d", k,n) }
                if ok {
                    if len(value) != n { t.Fatal("circular window size changed") }
                    start := k-remaining
                    for j,v := range value { if v != input[(start+j)%k] { t.Fatal("circular contents changed") } }
                }
            }
        }
    }
    // After observing None the circular adaptor knows its exact buffered tail,
    // even if the source itself only ever supplied a conservative size hint.
    circular := it.CircularArrayWindows[int](&f2pAuditSource{values: []int{1,2}}, 3)
    circular.Next()
    if h := circular.SizeHint(); h != core.Hint(1,1) { t.Fatalf("fused circular tail: %v",h) }
    circular.Next()
    if h := circular.SizeHint(); h != core.Hint(0,0) { t.Fatalf("fused circular exhaustion: %v",h) }
}
func f2pAuditIterate(t *testing.T) {
    t.Helper()
    calls := 0
    source := free.Iterate(4,func(v int) int { calls++; return v+1 })
    if calls != 0 { t.Fatal("Iterate is eager") }
    if h := source.SizeHint(); h != core.Hint(math.MaxInt,-1) { t.Fatalf("infinite Iterate hint: %v",h) }
    value,ok := source.Next()
    if !ok || value != 4 || calls != 1 { t.Fatalf("Iterate must update state on its first Next: %v,%v calls=%d",value,ok,calls) }
}
func f2pAuditHintArithmetic(t *testing.T) {
    t.Helper()
    // Rust's mul_scalar preserves an absent upper bound, including x == 0;
    // mul instead knows that a bounded empty factor makes the product empty.
    unknown := core.Hint(3,-1)
    if h := core.MulScalar(unknown,0); h != core.Hint(0,-1) { t.Fatalf("MulScalar absent upper bound: %v",h) }
    if h := core.Mul(unknown,core.Hint(0,0)); h != core.Hint(0,0) { t.Fatalf("Mul empty factor: %v",h) }
    if h := core.Add(core.Hint(math.MaxInt,math.MaxInt),core.Hint(1,1)); h != core.Hint(math.MaxInt,-1) { t.Fatalf("Add overflow: %v",h) }
}
type f2pAuditInfiniteGroup struct { t *testing.T; calls int }
func (s *f2pAuditInfiniteGroup) Next() (int,bool) {
    s.calls++
    if s.calls > 3 { s.t.Fatal("ChunkBy eagerly exhausted a group instead of yielding its lazy iterator") }
    return 7,true
}
func (s *f2pAuditInfiniteGroup) SizeHint() core.SizeHint { return core.Hint(math.MaxInt,-1) }
func f2pAuditLazyGroup(t *testing.T) {
    t.Helper()
    s := &f2pAuditInfiniteGroup{t:t}
    groups := reflect.ValueOf(it.ChunkBy(s,func(v int) int { return v }))
    // Rust ChunkBy is an IntoIterator container. Accept either the Go
    // container/IntoIter form or a direct iterator over lazy groups.
    for _, name := range []string{"IntoIter", "Groups", "Iter"} {
        if into := groups.MethodByName(name); into.IsValid() {
            groups = into.Call(nil)[0]
            break
        }
    }
    next := groups.MethodByName("Next")
    if !next.IsValid() { t.Fatal("ChunkBy does not expose group iteration") }
    result := next.Call(nil)
    if len(result) != 2 || !result[1].Bool() { t.Fatal("ChunkBy did not yield the first group") }
    item := result[0]
    group,ok := item.Interface().(core.Iter[int])
    if !ok {
        for item.Kind() == reflect.Pointer || item.Kind() == reflect.Interface { item = item.Elem() }
        if item.Kind() == reflect.Struct {
            if field := item.FieldByName("Second"); field.IsValid() {
                group,ok = field.Interface().(core.Iter[int])
            }
        }
    }
    if !ok { t.Fatal("ChunkBy must retain lazy group iteration") }
    if value,ok := group.Next(); !ok || value != 7 { t.Fatal("lazy group lost its first item") }
}
func f2pAuditConstructors(t *testing.T) {
    t.Helper()
    f2pAuditLazyGroup(t)
    constructors := []func(core.Iter[int]) any{
        func(s core.Iter[int]) any { return it.Map(s,func(v int) int { return v }) },
        func(s core.Iter[int]) any { return it.Filter(s,func(v int) bool { return true }) },
        func(s core.Iter[int]) any { return it.ArrayWindows(s,2) },
        func(s core.Iter[int]) any { return it.CircularArrayWindows(s,2) },
        func(s core.Iter[int]) any { return it.Chunks(s,2) },
        func(s core.Iter[int]) any { return it.Interleave(s,core.FromSlice([]int{9})) },
        func(s core.Iter[int]) any { return it.KMerge([]core.Iter[int]{s}) },
        func(s core.Iter[int]) any { return it.TupleCombinations(s,2) },
        func(s core.Iter[int]) any { return it.Iproduct(s,core.FromSlice([]int{9})) },
        func(s core.Iter[int]) any { return it.Format(s,",") },
        func(s core.Iter[int]) any { return it.FormatWith(s,",",func(v int,write func(any) string) string { return write(v) }) },
    }
    for i, constructor := range constructors {
        s := &f2pAuditSource{values: []int{1,2,3}}
        _ = constructor(s)
        if s.calls != 0 { t.Fatalf("adaptor constructor %d consumed %d source items", i,s.calls) }
    }
}

// Preserve the Rust adaptors' observable behavior beyond forward collection.
// Interface assertions keep a missing capability a local test failure.
func f2pAuditCapabilityBehavior(t *testing.T) {
    t.Helper()
    t.Run("FusedDirectionBoundaries", f2pAuditFusedDirections)
    t.Run("RcIterSharedBounds", f2pRcSharing)
    t.Run("RcIterBorrowSemantics", f2pRcBorrows)
    t.Run("CartesianProductRightResumption", f2pProductRightResumption)
    t.Run("FlattenOKMixedEnds", func(t *testing.T) {
        boom := errors.New("middle")
        outer := core.FromSlice([]core.Pair[core.Iter[int], error]{
            {First: core.FromSlice([]int{1, 2})},
            {Second: boom},
            {First: core.FromSlice([]int{})},
            {First: core.FromSlice([]int{3, 4, 5})},
        })
        forward := it.FlattenOK(outer)
        flat := f2pFlattenIterator(t, forward)
        var ok bool
        steps := []struct { back bool; value int; err error }{
            {value: 1}, {back: true, value: 5}, {back: true, value: 4},
            {value: 2}, {err: boom}, {back: true, value: 3},
        }
        for index, step := range steps {
            var value core.Pair[int, error]
            if step.back { value, ok = flat.NextBack() } else { value, ok = flat.Next() }
            if !ok || value.First != step.value || value.Second != step.err {
                t.Fatalf("mixed flatten step %d: %v,%v", index, value, ok)
            }
            if index == 1 && flat.SizeHint() != core.Hint(3, -1) {
                t.Fatalf("flatten hint omitted active inner iterators: %v", flat.SizeHint())
            }
        }
        if _, ok := flat.Next(); ok { t.Fatal("extra flattened value") }
        if _, ok := flat.NextBack(); ok { t.Fatal("extra reverse flattened value") }
        if flat.SizeHint() != core.Hint(0, 0) { t.Fatal("flatten hint after exhaustion") }

        // Each end must drain the other end's active inner iterator when the
        // outer iterator is empty, without duplicating or dropping items.
        for _, reverseFirst := range []bool{false, true} {
            single := it.FlattenOK(core.FromSlice([]core.Pair[core.Iter[int], error]{
                {First: core.FromSlice([]int{1, 2, 3})},
            }))
            both := f2pFlattenIterator(t, single)
            first, last := both.Next, both.NextBack
            if reverseFirst { first, last = last, first }
            a, aok := first(); b, bok := last(); c, cok := first()
            wantA, wantB := 1, 3
            if reverseFirst { wantA, wantB = wantB, wantA }
            if !aok || !bok || !cok || a.First != wantA || b.First != wantB || c.First != 2 {
                t.Fatalf("single inner reverse=%v: %v %v %v", reverseFirst, a, b, c)
            }
        }
    })
    t.Run("CartesianProductResumption", func(t *testing.T) {
        left := &f2pAuditPulseSource{}
        product := it.CartesianProduct(left, core.FromSlice([]int{10, 20}))
        wants := []core.Pair[int, int]{{First: 1, Second: 10}, {First: 1, Second: 20}, {}, {}, {First: 2, Second: 10}, {First: 2, Second: 20}}
        for index, want := range wants {
            value, ok := product.Next()
            expectedOK := index != 2 && index != 3
            if ok != expectedOK || ok && f2pProductPair(t, value) != want {
                t.Fatalf("product must resume at step %d: %v,%v want %v,%v", index, value, ok, want, expectedOK)
            }
        }
        untouched := &f2pAuditSource{values: []int{1}}
        empty := it.CartesianProduct(untouched, core.FromSlice([]int{}))
        if _, ok := empty.Next(); ok || untouched.calls != 0 {
            t.Fatal("an empty right product input must not poll the left input")
        }
    })
    t.Run("TeeExactHints", func(t *testing.T) {
        left, right := it.Tee(core.FromSlice([]int{1, 2, 3}))
        if left.SizeHint() != core.Hint(3, 3) || right.SizeHint() != core.Hint(3, 3) { t.Fatal("initial Tee hints") }
        left.Next(); left.Next()
        if left.SizeHint() != core.Hint(1, 1) || right.SizeHint() != core.Hint(3, 3) { t.Fatal("Tee backlog missing from hints") }
        mustEq(t, "Tee lagging side", it.CollectVec(right), []int{1, 2, 3})
        if left.SizeHint() != core.Hint(1, 1) || right.SizeHint() != core.Hint(0, 0) { t.Fatal("Tee independent remaining lengths") }
    })
}

// A legal non-fused, exact-sized double-ended source: its first empty run is
// followed by one item. Fuse must remember that empty run across both ends.
type f2pAuditRevivingExact struct { calls int }
func (s *f2pAuditRevivingExact) Next() (int, bool) {
    s.calls++
    if s.calls == 2 { return 7, true }
    return 0, false
}
func (s *f2pAuditRevivingExact) NextBack() (int, bool) { return s.Next() }
func (s *f2pAuditRevivingExact) SizeHint() core.SizeHint {
    n := 0
    if s.calls == 1 { n = 1 }
    return core.Hint(n, n)
}
func (s *f2pAuditRevivingExact) Len() int { return s.SizeHint().Lower }

func f2pAuditFusedDirections(t *testing.T) {
    t.Helper()
    for _, reviveLeft := range []bool{false, true} {
        s := &f2pAuditRevivingExact{}
        var a, b core.Iter[int] = core.FromSlice([]int{10, 20}), s
        if reviveLeft { a, b = b, a }
        forward := it.ZipLongest(a, b)
        both, ok := any(forward).(core.DoubleEnded[core.EitherOrBoth[int, int]])
        if !ok { t.Fatal("ZipLongest lost double-ended iteration") }
        first, ok := both.Next()
        if !ok || first.HasLeft() == reviveLeft || first.HasRight() != reviveLeft {
            t.Fatal("ZipLongest first unmatched item changed")
        }
        last, ok := both.NextBack()
        if !ok || last.HasLeft() == reviveLeft || last.HasRight() != reviveLeft {
            t.Fatal("ZipLongest revived a fused input when switching to NextBack")
        }
        left, _ := last.Left(); right, _ := last.Right()
        if left+right != 20 || s.calls != 1 { t.Fatal("ZipLongest repolled an exhausted input") }
        if _, ok := both.Next(); ok { t.Fatal("ZipLongest has extra items") }
        if h := both.SizeHint(); h != core.Hint(0, 0) { t.Fatalf("ZipLongest fused hint: %v", h) }
    }
    for _, reverseFirst := range []bool{false, true} {
        s := &f2pAuditRevivingExact{}
        forward := it.ZipLongest[int, int](s, core.FromSlice([]int{}))
        both, ok := any(forward).(core.DoubleEnded[core.EitherOrBoth[int, int]])
        if !ok { t.Fatal("ZipLongest lost double-ended iteration") }
        first, second := both.Next, both.NextBack
        if reverseFirst { first, second = second, first }
        if _, ok := first(); ok { t.Fatal("ZipLongest initial empty run changed") }
        if h := both.SizeHint(); h != core.Hint(0, 0) { t.Fatalf("ZipLongest exhausted hint: %v", h) }
        for i := 0; i < 2; i++ {
            if _, ok := second(); ok { t.Fatal("ZipLongest resumed after exhaustion") }
        }
        if s.calls != 1 { t.Fatal("ZipLongest polled its fused input again") }

        paddedSource := &f2pAuditRevivingExact{}
        padded := it.PadUsing[int](paddedSource, 0, func(i int) int { return 100+i })
        pad, ok := any(padded).(core.DoubleEnded[int])
        if !ok { t.Fatal("PadUsing lost double-ended iteration") }
        padFirst, padSecond := pad.Next, pad.NextBack
        if reverseFirst { padFirst, padSecond = padSecond, padFirst }
        if _, ok := padFirst(); ok { t.Fatal("PadUsing initial empty run changed") }
        if h := pad.SizeHint(); h != core.Hint(0, 0) { t.Fatalf("PadUsing exhausted hint: %v", h) }
        for i := 0; i < 2; i++ {
            if _, ok := padSecond(); ok { t.Fatal("PadUsing resumed after exhaustion") }
        }
        if paddedSource.calls != 1 { t.Fatal("PadUsing polled its fused input again") }
    }
    s := &f2pAuditRevivingExact{}
    padded := it.PadUsing[int](s, 2, func(i int) int { return 100+i })
    pad, ok := any(padded).(core.DoubleEnded[int])
    if !ok { t.Fatal("PadUsing lost double-ended iteration") }
    if v, ok := pad.Next(); !ok || v != 100 { t.Fatal("PadUsing first filler changed") }
    if h := pad.SizeHint(); h != core.Hint(1, 1) { t.Fatalf("PadUsing filler hint: %v", h) }
    if v, ok := pad.NextBack(); !ok || v != 101 { t.Fatal("PadUsing revived its input instead of padding") }
    if _, ok := pad.Next(); ok { t.Fatal("PadUsing has extra forward items") }
    if _, ok := pad.NextBack(); ok { t.Fatal("PadUsing has extra reverse items") }
    if h := pad.SizeHint(); h != core.Hint(0, 0) { t.Fatalf("PadUsing final hint: %v", h) }
    if s.calls != 1 { t.Fatal("PadUsing repolled its input after padding started") }
}

type f2pAuditPulseSource struct { calls int }
func (s *f2pAuditPulseSource) Next() (int, bool) {
    s.calls++
    return (s.calls+1)/2, s.calls%2 == 1
}
func (s *f2pAuditPulseSource) SizeHint() core.SizeHint { return core.Hint(0, -1) }

// Product's right input is cloned and reset in Rust, rather than fused. An
// initial None followed by Some must remain observable on subsequent calls.
// Clone returns an independent cursor; it does not share its mutable state.
type f2pProductRightSource struct { calls int }
func (s *f2pProductRightSource) Next() (int, bool) {
    s.calls++
    if s.calls == 2 { return 7, true }
    return 0, false
}
func (s *f2pProductRightSource) SizeHint() core.SizeHint { return core.Hint(0, -1) }
func (s *f2pProductRightSource) Clone() *f2pProductRightSource {
    clone := *s
    return &clone
}
func f2pProductRightResumption(t *testing.T) {
    product := it.CartesianProduct(core.FromSlice([]int{10, 20}), &f2pProductRightSource{})
    for index := 0; index < 6; index++ {
        value, ok := product.Next()
        wantOK := index%2 == 1
        // Resetting the right input returns None before a new left item is
        // selected, so all successful calls still use the first left item.
        if ok != wantOK || ok && f2pProductPair(t, value) != (core.Pair[int, int]{First: 10, Second: 7}) {
            t.Fatalf("product right input must resume at step %d: %v,%v", index, value, ok)
        }
    }
}

// The instruction fixes the RcIter type, but not its Go constructor.
// The runner also tries root-package factories on a compile failure.
func f2pNewRcIter(source core.Iter[int]) core.Iter[int] { return core.NewRcIter(source) }

// RcIter shares its input between all clones, so its lower size bound must
// remain zero even when the underlying source has an exact nonzero length.
func f2pRcSharing(t *testing.T) {
	rc := f2pNewRcIter(core.FromSlice([]int{1, 2, 3}))
	clone := f2pRcClone(t, rc)
	for _, handle := range []core.Iter[int]{rc, clone} {
		if h := handle.SizeHint(); h != core.Hint(0, 3) {
			t.Fatalf("shared initial hint: %v, want {0 3}", h)
		}
	}
	back, ok := clone.(core.DoubleEnded[int])
	if !ok { t.Fatal("clone lost reverse iteration") }
	if v, ok := back.NextBack(); !ok || v != 3 { t.Fatal("clone lost reverse iteration") }
	if h := rc.SizeHint(); h != core.Hint(0, 2) { t.Fatalf("hint after clone consumption: %v", h) }
	if v, ok := rc.Next(); !ok || v != 1 { t.Fatal("clone did not share source position") }
	if h := clone.SizeHint(); h != core.Hint(0, 1) { t.Fatalf("hint after original consumption: %v", h) }
	if v, ok := clone.Next(); !ok || v != 2 { t.Fatal("shared remainder changed") }
	if _, ok := rc.Next(); ok { t.Fatal("shared source has extra items") }
	if h := rc.SizeHint(); h != core.Hint(0, 0) { t.Fatalf("exhausted shared hint: %v", h) }
	infinite := f2pNewRcIter(core.RepeatIter(7))
	if h := infinite.SizeHint(); h != core.Hint(0, -1) { t.Fatalf("unbounded shared hint: %v", h) }
}

type f2pRcSource struct {
	inner *core.Slice[int]
	hook func()
}
func (s *f2pRcSource) fire() {
	fn := s.hook
	s.hook = nil
	if fn != nil { fn() }
}
func (s *f2pRcSource) Next() (int, bool) { s.fire(); return s.inner.Next() }
func (s *f2pRcSource) NextBack() (int, bool) { s.fire(); return s.inner.NextBack() }
func (s *f2pRcSource) SizeHint() core.SizeHint { s.fire(); return s.inner.SizeHint() }

// Rust RefCell permits nested shared borrows, and panics on a mutable borrow
// conflict. A callback into a clone must never block on its own borrowed state.
func f2pRcBorrows(t *testing.T) {
	invoke := func(handle core.Iter[int], operation string) {
		switch operation {
		case "next": handle.Next()
		case "back": handle.(core.DoubleEnded[int]).NextBack()
		case "hint": handle.SizeHint()
		}
	}
	cases := []struct { name, outer, inner string; wantPanic bool }{
		{"next-next", "next", "next", true},
		{"back-back", "back", "back", true},
		{"next-hint", "next", "hint", true},
		{"hint-next", "hint", "next", true},
		{"hint-hint", "hint", "hint", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			source := &f2pRcSource{inner: core.FromSlice([]int{1, 2, 3})}
			rc := f2pNewRcIter(source)
			clone := f2pRcClone(t, rc)
			source.hook = func() { invoke(clone, tc.inner) }
			done := make(chan bool, 1)
			go func() {
				defer func() { done <- recover() != nil }()
				invoke(rc, tc.outer)
			}()
			select {
			case panicked := <-done:
				if panicked != tc.wantPanic { t.Fatalf("borrow panic=%v, want %v", panicked, tc.wantPanic) }
			case <-time.After(2 * time.Second):
				t.Fatal("RcIter deadlocked on a reentrant borrow")
			}
			// Both normal returns and panics must release the borrow.
			if v, ok := rc.Next(); !ok || v != 1 { t.Fatal("borrow was not released") }
		})
	}
}

// Clone's concrete return type is an implementation detail of the Go projection.
func f2pRcClone(t *testing.T, handle core.Iter[int]) core.Iter[int] {
	t.Helper()
	method := reflect.ValueOf(handle).MethodByName("Clone")
	if !method.IsValid() { t.Fatal("RcIter does not expose shared cloning") }
	values := method.Call(nil)
	if len(values) != 1 { t.Fatal("RcIter clone must return one shared iterator") }
	clone, ok := values[0].Interface().(core.Iter[int])
	if !ok { t.Fatal("RcIter clone lost iterator protocol") }
	return clone
}

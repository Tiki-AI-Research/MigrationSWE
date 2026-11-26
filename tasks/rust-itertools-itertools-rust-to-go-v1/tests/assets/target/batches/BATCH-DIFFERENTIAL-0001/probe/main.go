// Command diffprobe exercises the shared differential corpus against a Go
// implementation of the migrated itertools module and prints one JSON object
// mapping corpus case ids to the observed, normalized values.
//
// BATCH-DIFFERENTIAL-0001 authors this private target probe. It is executed by
// target/batches/BATCH-DIFFERENTIAL-0001/entry.py against the candidate module;
// the reference target under /input/target is never copied into the public
// assets.
//
// Every corpus case is evaluated inside its own recovered scope (``safe``) so a
// single candidate panic cannot cascade: it fails only the case that actually
// panicked, instead of discarding the whole probe output. The sentinel
// ``__PROBE_PANIC__`` never compares equal to an expected corpus value.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"itertools"
	"itertools/core"
	"itertools/free"
)

const panicSentinel = "__PROBE_PANIC__"

// safe evaluates one corpus case in isolation. A panic in a single case is
// reported as the sentinel so the remaining cases keep their real values.
func safe(fn func() any) (out any) {
	defer func() {
		if r := recover(); r != nil {
			out = panicSentinel
		}
	}()
	return fn()
}

func collectInt(it core.Iter[int]) []int {
	out := []int{}
	for {
		v, ok := it.Next()
		if !ok {
			return out
		}
		out = append(out, v)
	}
}

func collectInts(it core.Iter[[]int]) [][]int {
	out := [][]int{}
	for {
		v, ok := it.Next()
		if !ok {
			return out
		}
		out = append(out, v)
	}
}

func firstN(it core.Iter[int], n int) []int {
	out := []int{}
	for i := 0; i < n; i++ {
		v, ok := it.Next()
		if !ok {
			break
		}
		out = append(out, v)
	}
	return out
}

func recoverCall(f func()) (panicked bool) {
	defer func() {
		if r := recover(); r != nil {
			panicked = true
		}
	}()
	f()
	return false
}

func fusedCount(it core.Iter[int]) int {
	n := 0
	for {
		if _, ok := it.Next(); !ok {
			break
		}
		n++
	}
	for i := 0; i < 3; i++ {
		if _, ok := it.Next(); ok {
			return -1
		}
	}
	return n
}

func main() {
	out := map[string]any{}
	eqInt := func(a, b int) bool { return a == b }

	// DIFF-001
	out["CORPUS-DIFF-001-IPRODUCT"] = safe(func() any {
		return collectInts(itertools.Iproduct[int](
			core.FromSlice([]int{1, 2}), core.FromSlice([]int{10, 20})))
	})
	out["CORPUS-DIFF-001-IZIP"] = safe(func() any {
		return collectInts(itertools.Izip[int](
			core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{10, 20})))
	})
	out["CORPUS-DIFF-001-CHAIN"] = safe(func() any {
		return collectInt(itertools.ChainConcatenates[int](
			core.FromSlice([]int{1}), core.FromSlice([]int{2, 3}), core.FromSlice([]int{4})))
	})

	// DIFF-003
	out["CORPUS-DIFF-003-FROM-SLICE"] = safe(func() any {
		return collectInt(core.FromSlice([]int{1, 2, 3}))
	})
	out["CORPUS-DIFF-003-REPEAT-N"] = safe(func() any {
		return collectInt(core.NewRepeatN(3, 7))
	})

	// DIFF-005
	out["CORPUS-DIFF-005-EITHER-OR-BOTH"] = safe(func() any {
		l := core.Left[int, string](1)
		r := core.Right[int, string]("b")
		b := core.Both(1, "a")
		lv, lok := b.Left()
		rv, rok := b.Right()
		return map[string]any{
			"left_is_left":   l.IsLeft(),
			"left_is_both":   l.IsBoth(),
			"left_value":     []any{lv, lok},
			"right_value":    []any{rv, rok},
			"right_has_left": r.HasLeft(),
			"both_is_both":   b.IsBoth(),
		}
	})

	// DIFF-006
	out["CORPUS-DIFF-006-CONCAT"] = safe(func() any {
		return itertools.Concat[int](
			core.FromSlice([][]int{{1}, {2, 3}, {4, 5, 6}}))
	})
	out["CORPUS-DIFF-006-CONCAT-EMPTY"] = safe(func() any {
		return itertools.Concat[int](core.FromSlice([][]int{}))
	})

	// DIFF-007
	out["CORPUS-DIFF-007-ARRAY-WINDOWS"] = safe(func() any {
		return collectInts(itertools.ArrayWindows[int](
			core.FromSlice([]int{1, 2, 3, 4, 5}), 3))
	})
	out["CORPUS-DIFF-007-CIRCULAR-WINDOWS"] = safe(func() any {
		return collectInts(itertools.CircularArrayWindows[int](
			core.FromSlice([]int{1, 2, 3, 4}), 3))
	})

	// DIFF-008
	out["CORPUS-DIFF-008-ALL-EQUAL-VALUE-ERROR"] = safe(func() any {
		return []any{
			core.NewAllEqualValueErrorZero[int]().Error(),
			core.NewAllEqualValueErrorUnequal(1, 2).Error(),
		}
	})
	out["CORPUS-DIFF-008-EXACTLY-ONE-ERROR"] = safe(func() any {
		_, eerr := itertools.ExactlyOne[int](core.FromSlice([]int{2, 3, 4}))
		_, eerrZero := itertools.ExactlyOne[int](core.FromSlice([]int{}))
		return []any{eerr.Error(), eerrZero.Error()}
	})

	// DIFF-009
	// Pass an explicit render func: the public contract fixes only the
	// NewFormat signature, not what render==nil should do, so relying on a nil
	// default would test undeclared internal behavior. The observable Format
	// contract (lazy join + one-shot panic) is asserted via the public
	// itertools.Format entry (F2P-016 / P2P-007).
	out["CORPUS-DIFF-009-FORMAT"] = safe(func() any {
		f := core.NewFormat[int](core.FromSlice([]int{1, 2, 3}), "-", func(v int) string {
			return fmt.Sprint(v)
		})
		first := f.String()
		return []any{first, recoverCall(func() { f.String() })}
	})
	out["CORPUS-DIFF-009-FORMAT-WITH"] = safe(func() any {
		fw := core.NewFormatWith[int](core.FromSlice([]int{1, 2, 3}), ",", func(v int, w func(any) string) string {
			return []string{"a", "b", "c"}[v-1]
		})
		firstW := fw.String()
		return []any{firstW, recoverCall(func() { fw.String() })}
	})

	// DIFF-010
	out["CORPUS-DIFF-010-ITERATE"] = safe(func() any {
		return firstN(free.Iterate(1, func(x int) int { return x * 2 }), 4)
	})
	out["CORPUS-DIFF-010-INTERSPERSE-HINT"] = safe(func() any {
		return collectInt(itertools.Intersperse[int](
			core.FromSlice([]int{1, 2, 3}), 0))
	})
	out["CORPUS-DIFF-010-MAP-NEXT-BACK"] = safe(func() any {
		mp := core.NewMap[int, int](core.FromSlice([]int{1, 2, 3}), func(x int) int { return x * 10 })
		nb, ok := mp.NextBack()
		return []any{nb, ok, fusedCount(itertools.Map[int, int](
			core.FromSlice([]int{1}), func(x int) int { return x }))}
	})

	// DIFF-012
	out["CORPUS-DIFF-012-INTERLEAVE"] = safe(func() any {
		return collectInt(itertools.Interleave[int](
			core.FromSlice([]int{1, 2}), core.FromSlice([]int{-1, -2})))
	})
	out["CORPUS-DIFF-012-INTERLEAVE-SHORTEST"] = safe(func() any {
		return collectInt(itertools.InterleaveShortest[int](
			core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{-1, -2})))
	})

	// DIFF-014
	out["CORPUS-DIFF-014-ALL-EQUAL-VALUE"] = safe(func() any {
		val, verr := itertools.AllEqualValue[int](core.FromSlice([]int{3, 3, 3}), eqInt)
		_, verrZero := itertools.AllEqualValue[int](core.FromSlice([]int{}), eqInt)
		_, verrUnequal := itertools.AllEqualValue[int](core.FromSlice([]int{1, 2}), eqInt)
		return []any{
			val, verr == nil, verrZero.Error(), verrUnequal.Error(),
		}
	})

	// DIFF-015
	out["CORPUS-DIFF-015-ARRAY-WINDOWS-EDGE"] = safe(func() any {
		return []any{
			len(collectInts(itertools.ArrayWindows[int](core.FromSlice([]int{1, 2, 3, 4, 5}), 3))),
			collectInts(itertools.ArrayWindows[int](core.FromSlice([]int{1, 2, 3}), 0)),
			len(collectInts(itertools.ArrayWindows[int](core.FromSlice([]int{1, 2}), 3))),
		}
	})

	// DIFF-016
	out["CORPUS-DIFF-016-MERGE"] = safe(func() any {
		return collectInt(itertools.Merge[int](
			core.FromSlice([]int{0, 3, 6, 9}), core.FromSlice([]int{0, 5, 10})))
	})
	out["CORPUS-DIFF-016-KMERGE"] = safe(func() any {
		return collectInt(itertools.KMerge[int]([]core.Iter[int]{
			core.FromSlice([]int{0, 2, 4}),
			core.FromSlice([]int{1, 3, 5}),
			core.FromSlice([]int{6, 7}),
		}))
	})

	// DIFF-018
	out["CORPUS-DIFF-018-FOLD"] = safe(func() any {
		return itertools.Fold[int, int](
			core.FromSlice([]int{1, 2, 3, 4}), 0, func(a, b int) int { return a + b })
	})
	out["CORPUS-DIFF-018-COLLECT-VEC"] = safe(func() any {
		return itertools.CollectVec[int](core.FromSlice([]int{1, 2, 3}))
	})
	out["CORPUS-DIFF-018-SUM1"] = safe(func() any {
		s1, sok := itertools.Sum1[int](core.FromSlice([]int{1, 2, 3}))
		return []any{s1, sok}
	})
	out["CORPUS-DIFF-018-PRODUCT1"] = safe(func() any {
		p1, pok := itertools.Product1[int](core.FromSlice([]int{2, 3, 4}))
		return []any{p1, pok}
	})

	// DIFF-019
	out["CORPUS-DIFF-019-ZIP-EQ"] = safe(func() any {
		return recoverCall(func() {
			z := itertools.ZipEq[int, int](core.FromSlice([]int{1, 2, 3}), core.FromSlice([]int{1, 2}))
			for {
				if _, ok := z.Next(); !ok {
					break
				}
			}
		})
	})
	out["CORPUS-DIFF-019-CHUNKS"] = safe(func() any {
		return recoverCall(func() {
			itertools.Chunks[int](core.FromSlice([]int{1, 2, 3}), 0)
		})
	})

	// DIFF-020
	out["CORPUS-DIFF-020-DEDUP"] = safe(func() any {
		return collectInt(itertools.Dedup[int](
			core.FromSlice([]int{1, 1, 2, 2, 2, 3, 1}), eqInt))
	})

	// DIFF-021
	out["CORPUS-DIFF-021-EXACTLY-ONE"] = safe(func() any {
		ev, eerrOne := itertools.ExactlyOne[int](core.FromSlice([]int{7}))
		_, e2 := itertools.ExactlyOne[int](core.FromSlice([]int{2, 3, 4}))
		_, e0 := itertools.ExactlyOne[int](core.FromSlice([]int{}))
		return []any{
			ev, eerrOne == nil, collectInt(e2), e0.Error(), len(collectInt(e0)),
		}
	})

	// DIFF-022
	out["CORPUS-DIFF-022-SORTED"] = safe(func() any {
		return itertools.Sorted[int](core.FromSlice([]int{3, 1, 2}))
	})
	out["CORPUS-DIFF-022-KSMALLEST"] = safe(func() any {
		return itertools.KSmallest[int](core.FromSlice([]int{5, 1, 4, 2, 3}), 2)
	})
	out["CORPUS-DIFF-022-COUNTS"] = safe(func() any {
		return itertools.Counts[int, int](
			core.FromSlice([]int{1, 1, 2}), func(x int) int { return x })
	})

	encoded, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, "diffprobe: marshal error:", err)
		os.Exit(2)
	}
	fmt.Println(string(encoded))
}

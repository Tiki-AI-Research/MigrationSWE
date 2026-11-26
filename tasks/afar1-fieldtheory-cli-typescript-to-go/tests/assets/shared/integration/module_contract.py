"""Behavior probes for discovered Go engine/prompt helpers.

Adapters inspect callable types rather than pinning a module path or public Go
API. All test code is added to a disposable copy; candidate tests stay intact.
"""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


COMMON = r'''
import (
    "errors"
    "fmt"
    "io"
    "math"
    "os"
    "os/signal"
    "path/filepath"
    "reflect"
    "strings"
    "sync"
    "syscall"
    "testing"
    "time"
)
var _ = []any{errors.Is, fmt.Sprint, io.Discard, math.IsNaN, os.Getpid, signal.Stop,
    filepath.Join, reflect.ValueOf, strings.TrimSpace, syscall.SIGINT, time.Second, sync.Once{}}

func harborSet(value reflect.Value, name string, next any) {
    field := value.FieldByName(name)
    if field.IsValid() && field.CanSet() {
        replacement := reflect.ValueOf(next)
        if replacement.Type().ConvertibleTo(field.Type()) { field.Set(replacement.Convert(field.Type())) }
    }
}
func harborText(value reflect.Value, name, text string) {
    field := value.FieldByName(name)
    if !field.IsValid() || !field.CanSet() { return }
    if field.Kind() == reflect.String { field.SetString(text) }
    if field.Kind() == reflect.Pointer && field.Type().Elem().Kind() == reflect.String && text != "" {
        next := reflect.New(field.Type().Elem()); next.Elem().SetString(text); field.Set(next)
    }
}
func harborAdapt(callable, engine reflect.Value, texts []string) []reflect.Value {
    if !callable.IsValid() || callable.Kind() != reflect.Func { panic("HARBOR_UNSUPPORTED: missing callable") }
    args := []reflect.Value{}
    next := 0
    for i := 0; i < callable.Type().NumIn(); i++ {
        kind := callable.Type().In(i)
        if engine.IsValid() && engine.Type().AssignableTo(kind) { args = append(args, engine); continue }
        if engine.IsValid() && engine.CanAddr() && engine.Addr().Type().AssignableTo(kind) { args = append(args, engine.Addr()); continue }
        if kind.Kind() == reflect.String || (kind.Kind() == reflect.Pointer && kind.Elem().Kind() == reflect.String) {
            if next >= len(texts) { panic("HARBOR_UNSUPPORTED: extra string argument") }
            text := texts[next]; next++
            if kind.Kind() == reflect.String {
                value := reflect.New(kind).Elem(); value.SetString(text); args = append(args, value)
            } else {
                value := reflect.New(kind.Elem()); value.Elem().SetString(text)
                if text == "" { value = reflect.Zero(kind) }; args = append(args, value)
            }
        } else if kind.Kind() == reflect.Struct && len(texts) == 3 {
            value := reflect.New(kind).Elem()
            harborText(value, "Engine", texts[0]); harborText(value, "Name", texts[0])
            harborText(value, "Model", texts[1]); harborText(value, "Effort", texts[2])
            args = append(args, value)
        } else { panic("HARBOR_UNSUPPORTED: callable argument " + kind.String()) }
    }
    return callable.Call(args)
}
'''

PROMPT_INTERFACE = r'''
// This fake exposes both event APIs used by the independently authored
// readline adapters. Closing the interface need not close the underlying
// process stdin, which remains owned by the caller.
type harborPrompt struct {
    mu sync.Mutex
    ready chan struct{}
    closed chan struct{}
    closeOnce sync.Once
    interruptOnce sync.Once
    answers chan string
    interrupted chan struct{}
    answer func(string)
    listeners map[string]func()
}
func (p *harborPrompt) listen(event string, listener func()) func() {
    p.mu.Lock(); p.listeners[event] = listener; p.mu.Unlock()
    return func(){ p.mu.Lock(); delete(p.listeners, event); p.mu.Unlock() }
}
func (p *harborPrompt) emit(event string) {
    if event == "SIGINT" { p.interruptOnce.Do(func(){close(p.interrupted)}) }
    p.mu.Lock(); listener := p.listeners[event]; p.mu.Unlock()
    if listener != nil { listener() }
}
HARBOR_INTERFACE_METHODS
func TestHarborPromptContract(t *testing.T) {
    for _, kind := range []string{"answer", "close", "interrupt"} {
        p := &harborPrompt{ready: make(chan struct{}), closed: make(chan struct{}), answers: make(chan string, 1), interrupted: make(chan struct{}), listeners: map[string]func(){}}
        done := make(chan reflect.Value, 1)
        go func(){ done <- reflect.ValueOf(HARBOR_INTERFACE_CALLABLE).Call([]reflect.Value{reflect.ValueOf(p), reflect.ValueOf("quality prompt")})[0] }()
        select { case <-p.ready: case <-time.After(time.Second): t.Fatal("prompt did not register callbacks") }
        switch kind {
        case "answer": p.mu.Lock(); answer := p.answer; p.mu.Unlock(); if answer != nil { answer("  codex  ") } else { p.answers <- "  codex  " }
        case "close": p.Close()
        case "interrupt": p.emit("SIGINT")
        }
        select {
        case result := <-done:
            if result.Kind() == reflect.Pointer { result = result.Elem() }
            if fmt.Sprint(result.FieldByName("Kind").Interface()) != kind { t.Fatalf("result=%v want %s", result, kind) }
            if kind == "answer" && result.FieldByName("Value").String() != "codex" { t.Fatal("answer not trimmed") }
            if kind == "interrupt" { select { case <-p.closed: case <-time.After(time.Second): t.Fatal("SIGINT did not close interface") } }
        case <-time.After(time.Second): t.Fatal("prompt did not settle")
        }
    }
}
'''

PROMPT = r'''
func TestHarborPromptContract(t *testing.T) {
    call := func(reader *os.File) reflect.Value {
        HARBOR_SETUP
        callable := HARBOR_CALLABLE
        signature := callable.Type()
        values := []reflect.Value{}
        for i := 0; i < signature.NumIn(); i++ {
            kind := signature.In(i)
            if signature.IsVariadic() && i == signature.NumIn()-1 {
                option := reflect.New(kind.Elem()).Elem()
                if option.Kind() != reflect.Struct { t.Fatal("HARBOR_UNSUPPORTED: prompt options") }
                harborSet(option, "Input", reader); harborSet(option, "In", reader)
                harborSet(option, "Output", io.Discard); harborSet(option, "Out", io.Discard)
                slice := reflect.MakeSlice(kind, 1, 1); slice.Index(0).Set(option)
                values = append(values, slice)
            } else if kind.Implements(reflect.TypeOf((*io.Reader)(nil)).Elem()) && reflect.TypeOf(reader).AssignableTo(kind) {
                values = append(values, reflect.ValueOf(reader))
            } else if kind.Kind() == reflect.String {
                values = append(values, reflect.ValueOf("quality prompt"))
            } else if reflect.TypeOf(io.Discard).AssignableTo(kind) {
                values = append(values, reflect.ValueOf(io.Discard))
            } else if kind.Kind() == reflect.Struct {
                option := reflect.New(kind).Elem()
                harborSet(option, "Input", reader); harborSet(option, "In", reader)
                harborSet(option, "Output", io.Discard); harborSet(option, "Out", io.Discard)
                values = append(values, option)
            } else { t.Fatalf("HARBOR_UNSUPPORTED: prompt argument %v", kind) }
        }
        if signature.IsVariadic() { return callable.CallSlice(values)[0] }
        return callable.Call(values)[0]
    }
    check := func(input, want, answer string, interrupt bool) {
        reader, writer, err := os.Pipe()
        if err != nil { t.Fatal(err) }
        defer reader.Close(); defer writer.Close()
        old := os.Stdin; os.Stdin = reader; defer func(){ os.Stdin = old }()
        if !interrupt { writer.WriteString(input); writer.Close() }
        // Keep SIGINT contained even if a candidate forgets to install a handler.
        signals := make(chan os.Signal, 1); signal.Notify(signals, os.Interrupt); defer signal.Stop(signals)
        done := make(chan reflect.Value, 1)
        go func(){ done <- call(reader) }()
        // A fixed sleep can deliver SIGINT before a scheduled prompt has
        // installed its listener. Retry within the same bounded deadline.
        var interruptTick <-chan time.Time
        if interrupt {
            ticker := time.NewTicker(50*time.Millisecond); defer ticker.Stop()
            interruptTick = ticker.C
        }
        deadline := time.After(3*time.Second)
        for { select {
        case result := <-done:
            if result.Kind() == reflect.Pointer { result = result.Elem() }
            if fmt.Sprint(result.FieldByName("Kind").Interface()) != want { t.Fatalf("prompt result=%v, want %s", result, want) }
            if want == "answer" && fmt.Sprint(result.FieldByName("Value").Interface()) != answer { t.Fatalf("answer not trimmed: %v", result) }
            // io.Reader ownership belongs to the caller. Closing a prompt
            // session need not close os.Stdin. Interface adapters separately
            // verify Close() on SIGINT when that lifecycle is exposed.
            return
        case <-interruptTick:
            if err := syscall.Kill(os.Getpid(), syscall.SIGINT); err != nil { t.Fatal(err) }
        case <-deadline: t.Fatal("prompt did not settle")
        } }
    }
    check("  codex  \n", "answer", "codex", false)
    check("", "close", "", false)
    check("", "interrupt", "", true)
}
'''

ENGINE = r'''
func TestHarborEngineContract(t *testing.T) {
    directory := t.TempDir()
    binary := filepath.Join(directory, "claude")
    engineName, engineModel, engineEffort := "claude", "", ""
    expectedArgs := func() []string {
        args:=[]string{"-p","--output-format","text"}
        if engineName=="codex" {args=[]string{"exec","--skip-git-repo-check"}}
        if engineModel!="" {args=append(args,"--model",engineModel)}
        if engineEffort!="" {
            if engineName=="claude" {args=append(args,"--effort",engineEffort)} else {args=append(args,"--config","model_reasoning_effort=\""+engineEffort+"\"")}
        }
        return append(args,"QUALITY_PROMPT_MUST_NOT_LEAK")
    }
    t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
    invoke := func(body string, timeout int64, cap int64) (string, error) {
        if body == "HARBOR_MISSING_BINARY" {
            os.Remove(binary)
            // Do not fall through to an installed provider on the host PATH.
            t.Setenv("PATH", directory)
        } else if err := os.WriteFile(binary, []byte("#!/bin/sh\n"+body), 0755); err != nil { t.Fatal(err) }
        callable := reflect.ValueOf(HARBOR_ENGINE_CALLABLE)
        signature := callable.Type()
        engineType := signature.In(0)
        pointer := engineType.Kind() == reflect.Pointer
        if pointer { engineType = engineType.Elem() }
        engine := reflect.New(engineType).Elem()
        harborSet(engine, "Name", engineName); harborSet(engine, "Bin", binary)
        HARBOR_INVOCATION_CONFIG_SETUP
        harborText(engine,"Model",engineModel);harborText(engine,"Effort",engineEffort)
        config := engine.FieldByName("Config")
        if config.IsValid() && config.Kind() == reflect.Struct {
            harborSet(config, "Bin", binary)
        }
        if pointer { engine = engine.Addr() }
        values := []reflect.Value{engine, reflect.ValueOf("QUALITY_PROMPT_MUST_NOT_LEAK")}
        if signature.NumIn() == 4 {
            values = append(values, reflect.ValueOf(timeout).Convert(signature.In(2)), reflect.ValueOf(cap).Convert(signature.In(3)))
        } else if signature.NumIn() > 2 {
            optionType := signature.In(2)
            if signature.IsVariadic() { optionType = optionType.Elem() }
            pointerOption := optionType.Kind() == reflect.Pointer
            if pointerOption { optionType = optionType.Elem() }
            option := reflect.New(optionType).Elem()
            for _, name := range []string{"Timeout", "TimeoutMS", "TimeoutMs"} {
                field := option.FieldByName(name)
                if field.IsValid() {
                    number := timeout
                    if field.Type() == reflect.TypeOf(time.Duration(0)) { number *= int64(time.Millisecond) }
                    harborSet(option, name, number)
                }
            }
            harborSet(option, "MaxBuffer", cap)
            if pointerOption { option = option.Addr() }
            if signature.IsVariadic() {
                slice := reflect.MakeSlice(signature.In(2), 1, 1); slice.Index(0).Set(option)
                values = append(values, slice)
            } else { values = append(values, option) }
        }
        var result []reflect.Value
        if signature.IsVariadic() { result = callable.CallSlice(values) } else { result = callable.Call(values) }
        if !result[1].IsNil() { return "", result[1].Interface().(error) }
        return result[0].String(), nil
    }
    checkError := func(err error, reason string, killed bool) {
        if err == nil { t.Fatalf("missing %s error", reason) }
        detail := reflect.ValueOf(err)
        if detail.Kind() == reflect.Pointer { detail = detail.Elem() }
        if detail.Kind() != reflect.Struct { t.Fatalf("unstructured engine error: %T", err) }
        field := detail.FieldByName("Reason")
        if !field.IsValid() || fmt.Sprint(field.Interface()) != reason { t.Fatalf("reason=%v want %s: %v", field, reason, err) }
        field = detail.FieldByName("Killed")
        if !field.IsValid() || field.Bool() != killed { t.Fatalf("killed=%v want %v", field, killed) }
        if strings.Contains(err.Error(), "QUALITY_PROMPT_MUST_NOT_LEAK") || strings.Contains(err.Error(), "ABCDEFGHIJKLMNOPQRSTUVWX") { t.Fatalf("prompt or secret in error: %v", err) }
        stderr := detail.FieldByName("Stderr")
        if !stderr.IsValid() || stderr.Kind()!=reflect.String {t.Fatal("HARBOR_UNSUPPORTED: structured stderr field")}
        if strings.Contains(stderr.String(),"ABCDEFGHIJKLMNOPQRSTUVWX") {t.Fatalf("secret in structured stderr: %q",stderr.String())}
    }
    // Check the argv that actually reaches the child. A correct unused Args
    // helper is not evidence that the invocation path calls it correctly.
    invocationType:=reflect.TypeOf(HARBOR_ENGINE_CALLABLE).In(0)
    if invocationType.Kind()==reflect.Pointer {invocationType=invocationType.Elem()}
    textField:=func(name string)bool{
        field,ok:=invocationType.FieldByName(name)
        return ok && (field.Type.Kind()==reflect.String || field.Type.Kind()==reflect.Pointer && field.Type.Elem().Kind()==reflect.String)
    }
    for _,name:=range []string{"claude","codex"} {
        for _,profile:=range [][2]string{{"",""},{"model-x",""},{"","high"},{"model-x","high"}} {
            if profile[0]!="" && !textField("Model") || profile[1]!="" && !textField("Effort") {t.Fatal("HARBOR_UNSUPPORTED: engine profile representation")}
            engineName,engineModel,engineEffort=name,profile[0],profile[1]
            binary=filepath.Join(directory,name)
            got,failure:=invoke("printf '%s\\n' \"$@\"\n",3000,1024)
            if failure!=nil || !reflect.DeepEqual(strings.Split(got,"\n"),expectedArgs()) {t.Fatalf("actual %s argv=%q want %q: %v",name,got,expectedArgs(),failure)}
        }
    }
    engineName,engineModel,engineEffort="claude","",""
    binary=filepath.Join(directory,"claude")
    stdout, err := invoke("cat >/dev/null\nprintf '  trimmed  '\n", 3000, 1024)
    if err != nil || stdout != "trimmed" { t.Fatalf("stdin EOF / trimmed stdout: %q %v", stdout, err) }
    // Normal diagnostics must survive. A program that loses/throws away stderr
    // must not pass the secret/prompt checks merely because it returns "".
    for attempt:=0;attempt<3;attempt++ {
        _, err = invoke("printf 'QUALITY_PROMPT_MUST_NOT_LEAK sk-ABCDEFGHIJKLMNOPQRSTUVWX ghp_ABCDEFGHIJKLMNOPQRSTUVWX gho_ABCDEFGHIJKLMNOPQRSTUVWX ghu_ABCDEFGHIJKLMNOPQRSTUVWX ghs_ABCDEFGHIJKLMNOPQRSTUVWX ghr_ABCDEFGHIJKLMNOPQRSTUVWX Bearer ABCDEFGHIJKLMNOPQRSTUVWX NORMAL_DIAGNOSTIC_TAIL' >&2\nexit 3\n", 3000, 1024)
        checkError(err, "exit", false)
        detail := reflect.ValueOf(err)
        if detail.Kind() == reflect.Pointer { detail = detail.Elem() }
        if !strings.Contains(detail.FieldByName("Stderr").String(),"NORMAL_DIAGNOSTIC_TAIL") {t.Fatal("normal stderr diagnostics were lost")}
        code := detail.FieldByName("Code")
        if !code.IsValid() { code = detail.FieldByName("ExitCode") }
        if code.IsValid() && code.Kind() == reflect.Pointer && !code.IsNil() { code = code.Elem() }
        if !code.IsValid() || !code.CanInt() || code.Int() != 3 { t.Fatalf("exit code=%v want 3", code) }
    }
    _,err=invoke("kill -TERM \"$$\"\n",3000,1024)
    checkError(err,"exit",false)
    signaled:=reflect.ValueOf(err);if signaled.Kind()==reflect.Pointer {signaled=signaled.Elem()}
    signalValue:=signaled.FieldByName("Signal")
    if !signalValue.IsValid(){t.Fatal("HARBOR_UNSUPPORTED: structured signal field")}
    if signalValue.Kind()==reflect.Pointer && !signalValue.IsNil(){signalValue=signalValue.Elem()}
    signalText:=fmt.Sprint(signalValue.Interface())
    if signalText!="SIGTERM" && signalText!="terminated" && signalText!="15" {t.Fatalf("signal=%s want SIGTERM",signalText)}
    _,err=invoke("printf 'EARLY_DIAGNOSTIC' >&2\nhead -c 1048576 /dev/zero >&2\nprintf 'NORMAL_DIAGNOSTIC_TAIL' >&2\nexit 3\n",3000,1024)
    checkError(err,"exit",false)
    detail:=reflect.ValueOf(err);if detail.Kind()==reflect.Pointer {detail=detail.Elem()}
    stderr:=detail.FieldByName("Stderr").String()
    if !strings.HasSuffix(strings.TrimSpace(stderr),"NORMAL_DIAGNOSTIC_TAIL") {t.Fatal("stderr does not retain its tail")}
    // Do not impose the reference implementation's particular tail length.
    if len(stderr)>=1048576+len("EARLY_DIAGNOSTICNORMAL_DIAGNOSTIC_TAIL") {t.Fatal("HARBOR_UNSUPPORTED: stderr bound exceeds the probe output budget")}
    // Exercise clipping and redaction together. If a raw tail buffer discards
    // a token prefix before redaction, the secret suffix must not leak. The
    // normal diagnostic after the token must remain usable as well.
    secretFile:=filepath.Join(directory,"long-secret-stderr")
    marker:="HARBORSECRETFRAGMENT0123456789"
    for _,prefix:=range []string{"sk-","ghp_","gho_","ghu_","ghs_","ghr_","Bearer "} {
        payload:=prefix+strings.Repeat(marker,1+1048576/len(marker))+" NORMAL_DIAGNOSTIC_TAIL"
        if failure:=os.WriteFile(secretFile,[]byte(payload),0600);failure!=nil {t.Fatal(failure)}
        // Use a file rather than an enormous argv element or shell literal.
        quoted:="'"+strings.ReplaceAll(secretFile,"'","'\"'\"'")+"'"
        _,err=invoke("cat "+quoted+" >&2\nexit 3\n",3000,1024)
        checkError(err,"exit",false)
        detail=reflect.ValueOf(err);if detail.Kind()==reflect.Pointer {detail=detail.Elem()}
        redacted:=detail.FieldByName("Stderr").String()
        if strings.Contains(redacted,marker) || strings.Contains(err.Error(),marker) {t.Fatalf("secret suffix survived stderr clipping/redaction for %q",prefix)}
        if !strings.Contains(redacted,"NORMAL_DIAGNOSTIC_TAIL") {t.Fatalf("normal diagnostic lost while redacting long %q token",prefix)}
    }
    // A long-lived producer must hit maxbuffer before the timeout, even when
    // its stdout never reaches EOF by itself.
    _, err = invoke("head -c 8192 /dev/zero\nsleep 5\n", 800, 1024)
    checkError(err, "maxbuffer", true)
    _, err = invoke("exec sleep 5\n", 80, 1024)
    checkError(err, "timeout", true)
    // Verify SIGTERM, the grace period and actual SIGKILL escalation against
    // a child that keeps running after TERM. Checking only Killed=true is not
    // evidence that the process was really terminated.
    quote:=func(value string)string{return "'"+strings.ReplaceAll(value,"'","'\"'\"'")+"'"}
    pidFile,termFile:=filepath.Join(directory,"pid"),filepath.Join(directory,"term")
    termCommand:="printf TERM > "+quote(termFile)
    body:=fmt.Sprintf("trap %s TERM\nprintf '%%s' \"$$\" > %s\nwhile :; do sleep 0.05; done\n",quote(termCommand),quote(pidFile))
    done:=make(chan error,1)
    go func(){_,failure:=invoke(body,400,1024);done<-failure}()
    pid:=0
    deadline:=time.Now().Add(3*time.Second)
    for time.Now().Before(deadline) {
        data,_:=os.ReadFile(pidFile);fmt.Sscan(string(data),&pid)
        if pid>0 {break};time.Sleep(10*time.Millisecond)
    }
    if pid<=0 {t.Fatal("timeout child did not start")}
    alive:=func()bool{
        err:=syscall.Kill(pid,0)
        if err!=nil && !errors.Is(err,syscall.EPERM) {return false}
        // A dead, not-yet-reaped process cannot execute further child work.
        if stat,err:=os.ReadFile(fmt.Sprintf("/proc/%d/stat",pid));err==nil {
            at:=strings.LastIndex(string(stat),")")
            if at>=0 {parts:=strings.Fields(string(stat)[at+1:]);if len(parts)>0 && parts[0]=="Z" {return false}}
        }
        return true
    }
    t.Cleanup(func(){if alive(){syscall.Kill(pid,syscall.SIGKILL)}})
    termSeen:=false
    for time.Now().Before(deadline) {
        if _,failure:=os.Stat(termFile);failure==nil {termSeen=true;break}
        if !alive(){break};time.Sleep(10*time.Millisecond)
    }
    if !termSeen {t.Fatal("timeout did not deliver SIGTERM before terminating child")}
    grace:=time.Now().Add(800*time.Millisecond)
    for time.Now().Before(grace) {if !alive(){t.Fatal("SIGKILL escalation did not allow a grace period")};time.Sleep(10*time.Millisecond)}
    deadline=time.Now().Add(5*time.Second)
    for alive() && time.Now().Before(deadline) {time.Sleep(20*time.Millisecond)}
    if alive(){t.Fatal("timeout left the TERM-resistant child alive")}
    select {case err=<-done:checkError(err,"timeout",true);case <-time.After(3*time.Second):t.Fatal("timeout invocation did not settle")}
    // Defaults are observable behavior, independent of constant names.
    stdout,err=invoke("head -c 1048576 /dev/zero\n",0,0)
    if err!=nil || len(stdout)!=1048576 {t.Fatalf("default stdout cap rejected 1 MiB: length=%d error=%v",len(stdout),err)}
    _,err=invoke("head -c 1048577 /dev/zero\nexec sleep 5\n",0,0)
    checkError(err,"maxbuffer",true)
    started:=time.Now()
    _,err=invoke("exec sleep 180\n",0,0)
    elapsed:=time.Since(started)
    checkError(err,"timeout",true)
    if elapsed<110*time.Second || elapsed>135*time.Second {t.Fatalf("default timeout=%v, want about 120000 ms",elapsed)}
    _, err = invoke("HARBOR_MISSING_BINARY", 3000, 1024)
    checkError(err, "spawn", false)
}
'''


ARGV = r'''
func TestHarborEngineArgvAndLabel(t *testing.T) {
    for _, name := range []string{"claude", "codex"} {
        for _, profile := range [][2]string{{"", ""}, {"model-x", ""}, {"", "high"}, {"model-x", "high"}} {
            model, effort := profile[0], profile[1]
            engine := reflect.New(reflect.TypeOf(HARBOR_ENGINE_TYPE{})).Elem()
            harborText(engine, "Name", name); harborText(engine, "Model", model); harborText(engine, "Effort", effort)
            HARBOR_CONFIG_SETUP
            args := engine.Addr().MethodByName("Args")
            if !args.IsValid() { args = engine.FieldByName("Config").FieldByName("Args") }
            result := harborAdapt(args, engine, []string{"quality prompt", model, effort})
            got := result[0].Interface().([]string)
            want := []string{"-p", "--output-format", "text"}
            if name == "codex" { want = []string{"exec", "--skip-git-repo-check"} }
            if model != "" { want = append(want, "--model", model) }
            if effort != "" {
                if name == "claude" { want = append(want, "--effort", effort) } else { want = append(want, "--config", "model_reasoning_effort=\""+effort+"\"") }
            }
            want = append(want, "quality prompt")
            if !reflect.DeepEqual(got, want) { t.Fatalf("%s argv=%q want %q", name, got, want) }
            label := ""
            HARBOR_LABEL_SETUP
            expected := name
            if model != "" { expected += "/"+model }
            if effort != "" { expected += "/effort="+effort }
            if label != expected { t.Fatalf("label=%q want %q", label, expected) }
        }
    }
}
'''

IGNORED = {'.git', '.gomodcache', '.gocache', 'vendor', 'node_modules'}
# Vendored source must stay available to the Go toolchain. It is excluded
# only from adapter discovery, where dependency helpers are not candidates.
COPY_IGNORED = IGNORED - {'vendor'}
EXPECTED_TESTS = {
    'engine': ('TestHarborEngineContract',),
    'prompt': ('TestHarborPromptContract',),
    'argv': ('TestHarborEngineArgvAndLabel',),
    'bridge': ('TestHarborEngineContract', 'TestHarborEngineArgvAndLabel'),
    'dates': ('TestHarborDatesContract',),
    'env': ('TestHarborEnvironmentContract',),
    'legacy': ('TestHarborLegacyIdeasContract',),
}


class Unsupported(ValueError):
    """No safe adapter is available; this is unverified, never PASS."""


_data_spec = importlib.util.spec_from_file_location('fieldtheory_module_data_contract', Path(__file__).with_name('module_data_contract.py'))
DATA_CONTRACT = importlib.util.module_from_spec(_data_spec)
_data_spec.loader.exec_module(DATA_CONTRACT)


def mask_go(text):
    """Keep offsets/newlines while hiding comments and string/rune contents."""
    pattern = r'//[^\n]*|/\*[\s\S]*?\*/|`[^`]*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    return re.sub(pattern, lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), text)


def close_at(text, start, opening='(', closing=')'):
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    raise Unsupported('unbalanced Go declaration')


def split_params(text):
    parts, start, depth = [], 0, 0
    for index, char in enumerate(text + ','):
        if char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    types, pending = [], []
    for part in parts:
        if not part:
            continue
        match = re.fullmatch(r'(\w+)\s+(.+)', part, re.S)
        if match:
            types.extend([match[2].strip()] * (len(pending) + 1))
            pending = []
        else:
            pending.append(part)
    types.extend(pending)
    return types


def fields(body):
    result = {}
    for line in re.split(r'[;\n]', body):
        match = re.fullmatch(r'([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+(\S.*)', line.strip())
        if match:
            for name in match[1].split(','):
                result[name.strip()] = match[2].strip()
    return result


def top_level_matches(pattern, text):
    depth, previous = 0, 0
    for match in re.finditer(pattern, text):
        prefix = text[previous:match.start()]
        depth += prefix.count('{') - prefix.count('}')
        previous = match.start()
        if depth == 0:
            yield match


def declarations(root, paths=None):
    packages = {}
    for path in sorted(paths if paths is not None else root.rglob('*.go')):
        if path.name.endswith('_test.go') or set(path.relative_to(root).parts) & IGNORED:
            continue
        raw = path.read_text(errors='replace')
        text = mask_go(raw)
        package = re.search(r'(?m)^\s*package\s+(\w+)', text)
        if not package:
            continue
        key = (path.parent.relative_to(root), package[1])
        group = packages.setdefault(key, {'types': {}, 'functions': [], 'text': ''})
        group['text'] += text + '\n'
        for match in top_level_matches(r'\btype\s+(\w+)\s+(struct|interface)\s*\{', text):
            end = close_at(text, match.end() - 1, '{', '}')
            body = text[match.end():end]
            group['types'][match[1]] = (match[2], body, fields(body))
        for match in top_level_matches(r'(?m)^\s*func\b\s*(?:\(([^)]*)\)\s*)?(\w+)\s*\(', text):
            end = close_at(text, match.end() - 1)
            body = text.find('{', end)
            if body < 0:
                continue
            result = text[end + 1:body].strip()
            if result.startswith('(') and result.endswith(')'):
                result = result[1:-1]
            group['functions'].append({'name': match[2], 'receiver': match[1],
                'params': split_params(text[match.end():end]), 'results': split_params(result),
                'body': raw[body:close_at(text, body, '{', '}') + 1]})
    return packages


def bare(kind):
    return kind.removeprefix('...').removeprefix('*')


def engine_function(group):
    matches = []
    for fn in group['functions']:
        params = fn['params']
        if fn['receiver'] or fn['results'] != ['string', 'error'] or len(params) not in {3, 4} or params[1] != 'string':
            continue
        definition = group['types'].get(bare(params[0]))
        if not definition or definition[0] != 'struct' or definition[2].get('Name') != 'string':
            continue
        if 'Bin' in definition[2] and definition[2]['Bin'] != 'string':
            continue
        config = definition[2].get('Config')
        if config:
            config_type = group['types'].get(config)
            if not config_type or config_type[0] != 'struct' or config_type[2].get('Bin') != 'string' or not re.fullmatch(r'func\([\s\S]*\)\s*\[\]string', config_type[2].get('Args', '')):
                continue
        if len(params) == 3:
            options = group['types'].get(bare(params[2]))
            numeric = {'int', 'int16', 'int32', 'int64', 'uint', 'uint16', 'uint32', 'uint64'}
            if not options or options[0] != 'struct' or options[2].get('MaxBuffer') not in numeric:
                continue
            timeouts = set(options[2]) & {'Timeout', 'TimeoutMS', 'TimeoutMs'}
            if not timeouts or any(options[2][field] not in numeric | {'time.Duration'} for field in timeouts):
                continue
        elif params[2:] != ['int', 'int']:
            continue
        matches.append(fn)
    if not matches:
        raise Unsupported('no compatible engine invocation signature')
    # Select a public wrapper over its asynchronous implementation. Names rank
    # compatible signatures, but are never an acceptance requirement.
    def rank(fn):
        return ('async' in fn['name'].lower(), fn['name'] not in {'InvokeEngine', 'Invoke', 'invokeEngine'})
    matches.sort(key=rank)
    if len(matches) > 1 and rank(matches[0]) == rank(matches[1]):
        raise Unsupported('ambiguous engine invocation APIs')
    return matches[0]


def interface_methods(body):
    methods = []
    for line in re.split(r'[;\n]', body):
        match = re.match(r'\s*(\w+)\s*\(', line)
        if not match:
            if line.strip():
                raise Unsupported('embedded prompt interface')
            continue
        end = close_at(line, match.end() - 1)
        result = line[end + 1:].strip()
        if result.startswith('(') and result.endswith(')'):
            results = split_params(result[1:-1])
            if len(results) != 1:
                raise Unsupported('prompt method result tuple')
            result = results[0]
        methods.append((match[1], split_params(line[match.end():end]), result))
    return methods


def fake_interface(body):
    generated = []
    roles = set()
    for name, params, result in interface_methods(body):
        lower = name.casefold()
        flat = re.sub(r'\s+', '', result)
        if lower == 'question' and params[:1] == ['string'] and len(params) in {1, 2}:
            roles.add('question')
            code = 'close(p.ready)'
            if len(params) == 2:
                if not re.fullmatch(r'func\(\s*(?:\w+\s+)?string\s*\)', params[1]) or result:
                    raise Unsupported('prompt answer callback signature')
                code = 'p.mu.Lock(); p.answer = arg1; p.mu.Unlock(); ' + code
            elif flat in {'<-chanstring', 'chanstring'}:
                code += '; return p.answers'
            elif result:
                raise Unsupported('prompt answer channel signature')
        elif lower == 'close' and not params and not result:
            roles.add('close')
            code = 'p.closeOnce.Do(func(){close(p.closed)}); p.emit("close")'
        elif lower in {'once', 'on', 'onceclose', 'oncesigint'}:
            event = 'arg0' if lower in {'once', 'on'} else ('"close"' if lower == 'onceclose' else '"SIGINT"')
            callback = 'arg1' if lower in {'once', 'on'} else 'arg0'
            expected = ['string', 'func()'] if lower in {'once', 'on'} else ['func()']
            if [re.sub(r'\s+', '', p) for p in params] != expected or flat not in {'', 'func()'}:
                raise Unsupported('prompt listener signature')
            code = ('return ' if result else '') + f'p.listen({event}, {callback})'
            roles.add('events')
        elif lower == 'removelistener' and len(params) == 2 and params[0] == 'string' and not result:
            code = 'p.mu.Lock(); delete(p.listeners,arg0); p.mu.Unlock()'
        elif lower == 'removelisteners' and not params and not result:
            code = 'p.mu.Lock(); p.listeners=map[string]func(){}; p.mu.Unlock()'
        elif lower in {'closed', 'done', 'interrupted', 'interrupts', 'answers'} and not params:
            channel = 'answers' if lower == 'answers' else ('interrupted' if lower in {'interrupted', 'interrupts'} else 'closed')
            expected = {'<-chanstring', 'chanstring'} if channel == 'answers' else {'<-chanstruct{}', 'chanstruct{}'}
            if flat not in expected:
                raise Unsupported('prompt event channel signature')
            code = 'return p.' + channel
            roles.add('events')
        else:
            raise Unsupported('prompt interface method ' + name)
        signature = ', '.join(f'arg{i} {kind}' for i, kind in enumerate(params))
        generated.append(f'func (p *harborPrompt) {name}({signature}) {result} {{ {code} }}')
    if not {'question', 'close', 'events'} <= roles:
        raise Unsupported('prompt lifecycle is not exposed by interface')
    return '\n'.join(generated)


def prompt_source(group):
    matches, interfaces = [], []
    for fn in group['functions']:
        if len(fn['results']) != 1 or not fn['params']:
            continue
        result = group['types'].get(bare(fn['results'][0]))
        if not result or not {'Kind', 'Value'} <= result[2].keys():
            continue
        interface = group['types'].get(bare(fn['params'][0]))
        if not fn['receiver'] and interface and interface[0] == 'interface' and fn['params'][1:] == ['string']:
            source = PROMPT_INTERFACE.replace('HARBOR_INTERFACE_CALLABLE', fn['name'])
            source = source.replace('HARBOR_INTERFACE_METHODS', fake_interface(interface[1]))
            interfaces.append(source)
            continue
        # A direct I/O adapter may own an internal prompt session. Test the
        # outcome and bounded cancellation without taking ownership of stdin.
        if 'string' not in fn['params']:
            continue
        if any(p.startswith('<-chan') or p.startswith('chan') or p.startswith('func(') for p in fn['params']):
            continue
        if fn['name'].casefold() in {'settle', 'cancel'}:
            continue
        setup, expression = '', f'reflect.ValueOf({fn["name"]})'
        if fn['receiver']:
            receiver = fn['receiver'].split()[-1].lstrip('*')
            setup = f'''app := reflect.New(reflect.TypeOf({receiver}{{}})).Elem()
            for _, name := range []string{{"In", "Input"}} {{ harborSet(app, name, reader) }}
            for _, name := range []string{{"Out", "Err", "Output"}} {{ harborSet(app, name, io.Discard) }}'''
            expression = f'reflect.ValueOf(app.Addr().Interface().(*{receiver}).{fn["name"]})'
        source = PROMPT.replace('HARBOR_SETUP', setup).replace('HARBOR_CALLABLE', expression)
        matches.append((len(fn['params']) == 1, fn['name'], source))
    if len(interfaces) > 1:
        raise Unsupported('ambiguous prompt lifecycle interfaces')
    if interfaces:
        return interfaces[0]
    if not matches:
        raise Unsupported('no compatible prompt lifecycle signature')
    matches.sort()
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        raise Unsupported('ambiguous prompt lifecycle APIs')
    return matches[0][2]


def engine_config_setup(group, engine_type, name_expression):
    """Use the candidate's registry; never fabricate its argument builder."""
    definition = group['types'][engine_type]
    config = definition[2].get('Config')
    if not config:
        return ''
    config_type = group['types'].get(config)
    if not config_type or not config_type[2].get('Args', '').startswith('func('):
        raise Unsupported('engine argument builder signature')
    maps = re.findall(r'\bvar\s+(\w+)\s*(?:=|\s)\s*map\[string\]\s*' + re.escape(config) + r'\s*\{', group['text'])
    if len(maps) != 1:
        raise Unsupported('engine argument configuration map is missing or ambiguous')
    return f'''registeredConfig := reflect.ValueOf({maps[0]}).MapIndex(reflect.ValueOf({name_expression}))
            if !registeredConfig.IsValid() {{t.Fatal("HARBOR_UNSUPPORTED: engine configuration entry")}}
            engine.FieldByName("Config").Set(registeredConfig)'''


def argv_source(group, fn):
    engine_type = bare(fn['params'][0])
    definition = group['types'][engine_type]
    config_setup = engine_config_setup(group, engine_type, 'name')
    if not definition[2].get('Config') and not any(candidate['name'] == 'Args' and candidate['receiver']
                 and bare(candidate['receiver'].split()[-1]) == engine_type
                 and candidate['results'] == ['[]string'] for candidate in group['functions']):
        raise Unsupported('engine argument method is not discoverable')
    labels = []
    for candidate in group['functions']:
        if candidate['receiver']:
            if bare(candidate['receiver'].split()[-1]) == engine_type and candidate['name'] == 'Label' and candidate['results'] == ['string'] and not candidate['params']:
                labels.append((0, 'label = engine.Addr().MethodByName("Label").Call(nil)[0].String()'))
            continue
        params, results = candidate['params'], candidate['results']
        direct = results == ['string'] and (params in [['string'] * 3, ['string', '*string', '*string']] or (len(params) == 1 and bare(params[0]) == engine_type))
        factory = definition[2].get('Label') == 'string' and len(results) == 1 and bare(results[0]) == engine_type and (params == ['string'] * 3 or (len(params) == 2 and params[0] == 'string' and bare(params[1]) in group['types']))
        if direct or factory:
            code = f'produced := harborAdapt(reflect.ValueOf({candidate["name"]}), engine, []string{{name, model, effort}})[0]\n'
            if direct:
                code += 'label = produced.String()'
            else:
                code += 'if produced.Kind() == reflect.Pointer { produced = produced.Elem() }; label = produced.FieldByName("Label").String()'
            # An engine-typed helper identifies its role more reliably than a
            # generic three-string utility. Do not guess among unrelated
            # string helpers merely because their types happen to match.
            rank = 1 if direct and len(params) == 1 else 2
            if direct and len(params) == 3 and 'effort=' not in candidate['body']:
                continue
            labels.append((rank if direct else 3, code))
    if not labels:
        raise Unsupported('engine label API is not discoverable')
    labels.sort()
    if sum(rank == labels[0][0] for rank, _ in labels) != 1:
        raise Unsupported('ambiguous engine label APIs')
    return ARGV.replace('HARBOR_ENGINE_TYPE', engine_type).replace('HARBOR_CONFIG_SETUP', config_setup).replace('HARBOR_LABEL_SETUP', labels[0][1])


def generate(root, mode, paths=None):
    candidates, unsupported = [], []
    for (relative, package), group in declarations(root, paths).items():
        try:
            if mode in {'dates', 'env', 'legacy'}:
                source = DATA_CONTRACT.generate(group, mode, Unsupported)
            elif mode == 'prompt':
                source = prompt_source(group)
            else:
                fn = engine_function(group)
                source = argv_source(group, fn) if mode == 'argv' else ENGINE.replace('HARBOR_ENGINE_CALLABLE', fn['name']).replace(
                    'HARBOR_INVOCATION_CONFIG_SETUP', engine_config_setup(group, bare(fn['params'][0]), 'engineName'))
                if mode == 'bridge':
                    source += argv_source(group, fn)
            candidates.append((relative, package, source))
        except Unsupported as exc:
            unsupported.append(str(exc))
    if not candidates:
        raise Unsupported(f'no compatible {mode} adapter: ' + '; '.join(sorted(set(unsupported))))
    if len(candidates) != 1:
        raise Unsupported(f'ambiguous {mode} adapters in {len(candidates)} packages')
    relative, package, source = candidates[0]
    if re.search(r'HARBOR_(?:SETUP|CALLABLE|ENGINE_CALLABLE|INTERFACE_METHODS|INTERFACE_CALLABLE|ENGINE_TYPE|CONFIG_SETUP|INVOCATION_CONFIG_SETUP|LABEL_SETUP|DATE_FUNCTIONS|ENV_CALLABLE|LEGACY_SETUP|LEGACY_CALLABLE)', source):
        raise Unsupported('incomplete generated adapter')
    return relative, 'package ' + package + '\n' + COMMON + source


def active_sources(root):
    result = subprocess.run(['go', 'list', '-json', './...'], cwd=root, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise Unsupported('go list failed: ' + result.stderr[-1000:])
    decoder, text, paths = json.JSONDecoder(), result.stdout, []
    while text.strip():
        text = text.lstrip()
        item, end = decoder.raw_decode(text)
        text = text[end:]
        directory = Path(item['Dir']).resolve()
        if not directory.is_relative_to(root):
            raise Unsupported('Go package outside repository')
        paths.extend(directory / name for name in item.get('GoFiles', []) + item.get('CgoFiles', []))
    return paths


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in EXPECTED_TESTS:
        print('usage: module_contract.py REPO engine|prompt|argv|bridge|dates|env|legacy', file=sys.stderr)
        return 2
    root, mode = Path(sys.argv[1]).resolve(), sys.argv[2]
    try:
        with tempfile.TemporaryDirectory(prefix='ft-module-contract-') as temporary:
            work = Path(temporary) / 'repo'
            shutil.copytree(root, work, ignore=shutil.ignore_patterns(*COPY_IGNORED))
            # Even go list may update module metadata under -mod=mod. Perform
            # discovery in the copy as well as injecting/running the tests.
            relative, source = generate(work, mode, active_sources(work))
            destination = work / relative / 'zz_harbor_contract_test.go'
            if destination.exists():
                raise Unsupported('contract test filename already exists')
            destination.write_text(source)
            expected = EXPECTED_TESTS[mode]
            pattern = expected[0] if len(expected) == 1 else '(' + '|'.join(expected) + ')'
            test_timeout = '160s' if mode in {'engine', 'bridge'} else '30s'
            command = ['go', 'test', '-json', '-vet=off', '-count=1', '-timeout=' + test_timeout, '-run', '^' + pattern + '$', './' + relative.as_posix()]
            result = subprocess.run(command, cwd=work, text=True, capture_output=True, timeout=240 if mode in {'engine', 'bridge'} else 180)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            if '[build failed]' in result.stdout or 'zz_harbor_contract_test.go:' in result.stderr:
                raise Unsupported('generated adapter could not execute; see diagnostics')
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            unsupported = {e.get('Test') for e in events if e.get('Test') in expected and 'HARBOR_UNSUPPORTED:' in e.get('Output', '')}
            if 'HARBOR_UNSUPPORTED:' in result.stdout and not unsupported:
                # An unattributed panic cannot safely be assigned to a test.
                unsupported = set(expected)
            if any(e.get('Action') == 'fail' and e.get('Test') in expected and e.get('Test') not in unsupported for e in events):
                return 1
            if unsupported:
                raise Unsupported('generated adapter could not execute; see diagnostics')
            passed = {e.get('Test') for e in events if e.get('Action') == 'pass' and e.get('Test') in expected}
            if result.returncode:
                if not any(e.get('Action') == 'run' and e.get('Test') in expected and e.get('Test') not in passed for e in events):
                    raise Unsupported('test runner failed outside the contract test; see diagnostics')
                return 1
            if passed != set(expected):
                raise Unsupported('contract test did not produce a passing execution receipt')
            return 0
    except subprocess.TimeoutExpired as exc:
        print('TIMEOUT: ' + str(exc), file=sys.stderr)
        return 124
    except (Unsupported, OSError, ValueError) as exc:
        print('INCONCLUSIVE: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

"""Go date, configuration and legacy-state probes for module_contract.py.

Names identify semantic roles only after callable signatures are checked.
Unrecognized or ambiguous interfaces are unverified, never a passing probe.
"""
from __future__ import annotations

import re


DATES = r'''
func TestHarborDatesContract(t *testing.T) {
    oldLocal := time.Local
    time.Local = time.FixedZone("harbor-non-utc", -7*3600)
    defer func(){ time.Local = oldLocal }()
    functions := map[string]any{HARBOR_DATE_FUNCTIONS}
    formats := map[string]string{"date":"2006-01-02", "month":"2006-01", "weekday":"Mon"}
    call := func(role string, input *string) (any, bool) {
        callable := reflect.ValueOf(functions[role])
        signature := callable.Type()
        values := []reflect.Value{}
        for i := 0; i < signature.NumIn(); i++ {
            kind := signature.In(i)
            value := reflect.New(kind).Elem()
            switch {
            case i == 1 && kind.Kind() == reflect.Bool:
                value.SetBool(input != nil)
            case i == 1 && kind.Kind() == reflect.String:
                value.SetString(formats[role])
            case kind.Kind() == reflect.String:
                if input == nil {
                    if signature.NumIn()!=2 || signature.In(1).Kind()!=reflect.Bool {return nil,false}
                } else {value.SetString(*input)}
            case kind.Kind() == reflect.Pointer && kind.Elem().Kind() == reflect.String:
                if input != nil { value = reflect.New(kind.Elem()); value.Elem().SetString(*input) }
            case kind.Kind() == reflect.Interface:
                if input != nil { value.Set(reflect.ValueOf(*input)) }
            default: t.Fatalf("HARBOR_UNSUPPORTED: date argument %s", kind)
            }
            values = append(values, value)
        }
        produced := callable.Call(values)
        if len(produced) == 2 {
            status := produced[1]
            if status.Kind() == reflect.Bool && !status.Bool() { return nil, true }
            if status.Type().Implements(reflect.TypeOf((*error)(nil)).Elem()) && !status.IsNil() { return nil, true }
        }
        value := produced[0]
        for value.Kind() == reflect.Pointer || value.Kind() == reflect.Interface {
            if value.IsNil() { return nil, true }; value = value.Elem()
        }
        if value.Type() == reflect.TypeOf(time.Time{}) {
            stamp := value.Interface().(time.Time)
            if stamp.IsZero() { return nil, true }; return stamp.UnixMilli(), true
        }
        switch value.Kind() {
        case reflect.String:
            if value.String() == "" && role != "year" && role != "monthday" { return nil, true }
            return value.String(), true
        case reflect.Int, reflect.Int32, reflect.Int64: return value.Int(), true
        case reflect.Float32, reflect.Float64:
            if math.IsNaN(value.Float()) || math.IsInf(value.Float(), 0) { return nil, true }
            return int64(value.Float()), true
        }
        t.Fatalf("HARBOR_UNSUPPORTED: date result %s", value.Type()); return nil, false
    }
    for _, stamp := range []string{
        "Wed Oct 10 20:19:24 +0000 2018",
        "2024-01-01T00:30:00+02:00",
        "2024-01-31T23:30:00-03:00",
        "2024-01-02T10:00:00.123Z",
        "2024-01-02",
    } {
        var parsed time.Time
        var err error
        for _, layout := range []string{time.RFC3339Nano, "Mon Jan 02 15:04:05 -0700 2006", "2006-01-02"} {
            parsed, err = time.Parse(layout, stamp); if err == nil { break }
        }
        if err != nil { t.Fatal("invalid evaluator date fixture", err) }
        utc := parsed.UTC()
        expected := map[string]any{"timestamp":utc.UnixMilli(), "date":utc.Format("2006-01-02"),
            "month":utc.Format("2006-01"), "weekday":utc.Format("Mon"), "hour":int64(utc.Hour()),
            "year":utc.Format("2006"), "monthday":utc.Format("Jan 02")}
        for role, want := range expected {
            got, supported := call(role, &stamp)
            if !supported || !reflect.DeepEqual(got, want) { t.Fatalf("%s(%q)=%v want %v", role, stamp, got, want) }
        }
    }
    for _, input := range []*string{nil, new(string), func()*string{s:="xxxxABCDEFyyy"; return &s}()} {
        for role := range functions {
            var want any
            if role == "year" { want = "????"; if input != nil { want = (*input)[max(0,len(*input)-4):] } }
            if role == "monthday" { want = " ?? ??"; if input != nil { want = (*input)[min(4,len(*input)):min(10,len(*input))] } }
            got, supported := call(role, input)
            // A string-only API cannot express absent input. Do not invent a
            // sentinel that changes the meaning of a present empty string.
            if !supported && input == nil { continue }
            if !reflect.DeepEqual(got,want) { t.Fatalf("%s fallback=%v want %v",role,got,want) }
        }
    }
}
'''

ENV = r'''
func TestHarborEnvironmentContract(t *testing.T) {
    root := t.TempDir()
    cwd, err := os.Getwd(); if err != nil { t.Fatal(err) }
    defer os.Chdir(cwd)
    work, data := filepath.Join(root,"cwd"), filepath.Join(root,"data")
    for _, path := range []string{work,data,filepath.Join(root,"home")} { if err:=os.MkdirAll(path,0700); err!=nil {t.Fatal(err)} }
    if err:=os.Chdir(work); err!=nil {t.Fatal(err)}
    t.Setenv("HOME",filepath.Join(root,"home")); t.Setenv("FT_DATA_DIR",data)
    keys := []string{"HARBOR_ENV_ORDER","HARBOR_ENV_CWD","HARBOR_ENV_DATA_LOCAL","HARBOR_ENV_DATA","HARBOR_ENV_EXISTING","HARBOR_ENV_EMPTY"}
    for _, key := range keys { t.Setenv(key,""); os.Unsetenv(key) }
    t.Setenv(keys[4],"process"); t.Setenv(keys[5],"")
    write := func(path, content string){ if err:=os.WriteFile(path,[]byte(content),0600); err!=nil {t.Fatal(err)} }
    write(filepath.Join(work,".env.local"),"HARBOR_ENV_ORDER=cwd-local\nHARBOR_ENV_EXISTING=file\nHARBOR_ENV_EMPTY=file\n")
    write(filepath.Join(work,".env"),"HARBOR_ENV_ORDER=cwd-env\nHARBOR_ENV_CWD=cwd-env\n")
    write(filepath.Join(data,".env.local"),"HARBOR_ENV_ORDER=data-local\nHARBOR_ENV_CWD=data-local\nHARBOR_ENV_DATA_LOCAL=data-local\n")
    write(filepath.Join(data,".env"),"HARBOR_ENV_ORDER=data-env\nHARBOR_ENV_CWD=data-env\nHARBOR_ENV_DATA_LOCAL=data-env\nHARBOR_ENV_DATA=data-env\n")
    values := reflect.ValueOf(HARBOR_ENV_CALLABLE).Call(nil)
    for _, value := range values {
        if value.Type().Implements(reflect.TypeOf((*error)(nil)).Elem()) && !value.IsNil() { t.Fatal(value.Interface()) }
    }
    expected:=[]string{"cwd-local","cwd-env","data-local","data-env","process",""}
    for i,key := range keys {
        got,present:=os.LookupEnv(key)
        if !present || got!=expected[i] { t.Fatalf("environment precedence %s=%q (present=%v), want %q",key,got,present,expected[i]) }
    }
}
'''

LEGACY = r'''
func TestHarborLegacyIdeasContract(t *testing.T) {
    invoke := func(){
        HARBOR_LEGACY_SETUP
        values:=reflect.ValueOf(HARBOR_LEGACY_CALLABLE).Call(arguments)
        for _, value := range values {
            if value.Type().Implements(reflect.TypeOf((*error)(nil)).Elem()) && !value.IsNil() {t.Fatal(value.Interface())}
        }
    }
    root:=t.TempDir()
    t.Setenv("HOME",filepath.Join(root,"home")); t.Setenv("FT_DATA_DIR",root)
    target:=filepath.Join(root,"ideas")
    begin:=filepath.Join(target,".migration-in-progress")
    complete:=filepath.Join(target,".migrated-from-ft-bookmarks")
    write:=func(path,content string){
        if err:=os.MkdirAll(filepath.Dir(path),0700); err!=nil {t.Fatal(err)}
        if err:=os.WriteFile(path,[]byte(content),0600); err!=nil {t.Fatal(err)}
    }
    snapshot:=func(path string) map[string]string {
        result:=map[string]string{}
        err:=filepath.Walk(path,func(p string,info os.FileInfo,err error)error{
            if os.IsNotExist(err) {return nil}; if err!=nil {return err}
            relative,err:=filepath.Rel(path,p); if err!=nil{return err}
            if info.IsDir(){result[relative+"/"]="";return nil}
            data,err:=os.ReadFile(p);if err!=nil{return err};result[relative]=string(data);return nil
        })
        if err!=nil {t.Fatal(err)};return result
    }
    invoke()
    if len(snapshot(target))!=0 {t.Fatal("created ideas root with no legacy data")}
    ideas:=filepath.Join(root,"automation","ideas","seeds","seed.json")
    adjacent:=filepath.Join(root,"automation","adjacent","cache","item.json")
    write(ideas,"legacy ideas\n"); write(adjacent,"legacy adjacent\n")
    source:=snapshot(filepath.Join(root,"automation"))
    invoke()
    for path,want:=range map[string]string{filepath.Join(target,"seeds","seed.json"):"legacy ideas\n",filepath.Join(target,"adjacent","cache","item.json"):"legacy adjacent\n"} {
        data,err:=os.ReadFile(path);if err!=nil || string(data)!=want {t.Fatalf("legacy copy %s: %q %v",path,data,err)}
    }
    if _,err:=os.Stat(complete);err!=nil {t.Fatal("missing completion marker",err)}
    if _,err:=os.Stat(begin);!os.IsNotExist(err) {t.Fatal("in-progress marker remains after success")}
    if !reflect.DeepEqual(source,snapshot(filepath.Join(root,"automation"))) {t.Fatal("legacy source was changed")}
    saved:=snapshot(target)
    write(ideas,"changed legacy\n");invoke()
    if !reflect.DeepEqual(saved,snapshot(target)) {t.Fatal("migration is not idempotent")}
    if err:=os.RemoveAll(target);err!=nil {t.Fatal(err)}
    write(begin,"{}");write(filepath.Join(target,"partial-stale"),"partial")
    invoke()
    if _,err:=os.Stat(filepath.Join(target,"partial-stale"));!os.IsNotExist(err) {t.Fatal("interrupted migration did not wipe partial target")}
    data,err:=os.ReadFile(filepath.Join(target,"seeds","seed.json"));if err!=nil || string(data)!="changed legacy\n" {t.Fatal("interrupted migration did not retry")}
    if _,err:=os.Stat(complete);err!=nil {t.Fatal("recovery missing completion marker")}
    if _,err:=os.Stat(begin);!os.IsNotExist(err) {t.Fatal("recovery left in-progress marker")}
    if err:=os.RemoveAll(target);err!=nil {t.Fatal(err)}
    write(filepath.Join(target,"user","keep.md"),"user content\n");saved=snapshot(target)
    invoke()
    if !reflect.DeepEqual(saved,snapshot(target)) {t.Fatal("user-populated target was changed")}
}
'''


def normalized(name):
    return name.lower().removeprefix('to')


def choose(items, role, unsupported):
    if len(items) != 1:
        raise unsupported(('ambiguous ' if items else 'missing ') + role + ' API')
    return items[0]


def date_source(group, unsupported):
    aliases = {'timestamp': {'parsetimestampms', 'parsetimestamp'}, 'date': {'isodate'},
               'month': {'isomonth'}, 'weekday': {'weekdayshort'}, 'hour': {'utchour'},
               'year': {'yearlabel'}, 'monthday': {'monthdaylabel'}}
    chosen = {}
    for role, names in aliases.items():
        matches = []
        for fn in group['functions']:
            params, results = fn['params'], fn['results']
            name = normalized(fn['name']).removesuffix('ptr')
            if fn['receiver'] or name not in names or not 1 <= len(params) <= 2 or not 1 <= len(results) <= 2:
                continue
            if params[0] not in {'string', '*string', 'any', 'interface{}'}:
                continue
            if len(params) == 2 and params[1] != 'bool':
                continue
            if len(results) == 2 and results[1] not in {'bool', 'error'}:
                continue
            # Prefer the public millisecond API over an internal time.Time
            # parser when both are present; equal semantic matches stay
            # ambiguous instead of depending on declaration order.
            rank = (0 if role == 'timestamp' and name == 'parsetimestampms' else 1,
                    0 if params[0] in {'*string', 'any', 'interface{}'} else 1)
            matches.append((rank, fn))
        if not matches and role in {'date', 'month', 'weekday'}:
            matches = [((2, 1), fn) for fn in group['functions']
                       if not fn['receiver'] and normalized(fn['name']) in {'datelabel', 'isolabel'}
                       and fn['params'] == ['string', 'string'] and fn['results'] in [['*string'], ['string']]]
        if not matches:
            raise unsupported('missing ' + role + ' API')
        rank = min(item[0] for item in matches)
        chosen[role] = choose([fn for score, fn in matches if score == rank], role, unsupported)['name']
    return DATES.replace('HARBOR_DATE_FUNCTIONS', ','.join('"' + role + '":' + name for role, name in chosen.items()))


def generate(group, mode, unsupported):
    if mode == 'dates':
        return date_source(group, unsupported)
    if mode == 'env':
        functions = [fn for fn in group['functions'] if not fn['receiver'] and not fn['params']
                     and fn['results'] in [[], ['error']]
                     and normalized(fn['name']) in {'loadenv', 'loadenvironment'}]
        return ENV.replace('HARBOR_ENV_CALLABLE', choose(functions, 'environment', unsupported)['name'])
    functions = [fn for fn in group['functions'] if not fn['receiver'] and len(fn['params']) <= 1
                 and re.fullmatch(r'migratelegacyideas(?:data)?', normalized(fn['name']))]
    fn = choose(functions, 'legacy ideas', unsupported)
    setup = 'arguments:=[]reflect.Value{}'
    if fn['params']:
        kind = fn['params'][0]
        factories = [candidate for candidate in group['functions'] if not candidate['receiver'] and not candidate['params']
                     and candidate['results'] in [[kind], [kind, 'error']]]
        factory = choose(factories, 'legacy path factory', unsupported)
        setup += '\nproduced:=reflect.ValueOf(' + factory['name'] + ').Call(nil)\n'
        setup += 'if len(produced)==2 && !produced[1].IsNil(){t.Fatal(produced[1].Interface())}\narguments=append(arguments,produced[0])'
    return LEGACY.replace('HARBOR_LEGACY_SETUP', setup).replace('HARBOR_LEGACY_CALLABLE', fn['name'])

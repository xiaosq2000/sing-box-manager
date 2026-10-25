package accounting

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

// The fixture is shared with tests/test_node_accounting_calendar.py.
const calendarFixtureName = "node-accounting-calendar.json"

type calendarFixture struct {
	Schema     int                `json:"schema"`
	Coverage   calendarCoverage   `json:"coverage"`
	Invariants calendarInvariants `json:"invariants"`
	Cases      []calendarCase     `json:"cases"`
}

type calendarCoverage struct {
	Zones                    []string `json:"zones"`
	BillingDays              []int    `json:"billing_days"`
	SkippedLocalMidnightCase string   `json:"skipped_local_midnight_case"`
	SkippedLocalMidnightZone string   `json:"skipped_local_midnight_zone"`
}

type calendarInvariants struct {
	Zone        string `json:"zone"`
	BillingDays []int  `json:"billing_days"`
	FirstDate   string `json:"first_date"`
	LastDate    string `json:"last_date"`
}

type calendarCase struct {
	Name        string              `json:"name"`
	Description string              `json:"description"`
	Zone        string              `json:"zone"`
	At          string              `json:"at"`
	BillingDay  int                 `json:"billing_day"`
	Expected    calendarExpectation `json:"expected"`
}

type calendarExpectation struct {
	LocalDate  string `json:"local_date"`
	CycleKey   string `json:"cycle_key"`
	CycleStart string `json:"cycle_start"`
	CycleEnd   string `json:"cycle_end"`
}

type calendarWindow struct {
	start Date
	end   Date
}

func TestSharedCalendarCases(t *testing.T) {
	fixture := loadCalendarFixture(t)
	fixture.validate(t)
	for _, testCase := range fixture.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			calendar, err := NewCalendar(testCase.Zone, testCase.BillingDay)
			if err != nil {
				t.Fatal(err)
			}
			instant, err := time.Parse(time.RFC3339, testCase.At)
			if err != nil {
				t.Fatal(err)
			}
			cycle := calendar.Resolve(instant)
			if cycle.LocalDate != mustCalendarDate(t, testCase.Expected.LocalDate) {
				t.Fatalf("local date = %s, want %s", cycle.LocalDate, testCase.Expected.LocalDate)
			}
			if cycle.Key != testCase.Expected.CycleKey {
				t.Fatalf("cycle key = %s, want %s", cycle.Key, testCase.Expected.CycleKey)
			}
			if cycle.Start != mustCalendarDate(t, testCase.Expected.CycleStart) {
				t.Fatalf("cycle start = %s, want %s", cycle.Start, testCase.Expected.CycleStart)
			}
			if cycle.End != mustCalendarDate(t, testCase.Expected.CycleEnd) {
				t.Fatalf("cycle end = %s, want %s", cycle.End, testCase.Expected.CycleEnd)
			}
		})
	}
}

func TestNewTransitionCasesRejectFixedOffset(t *testing.T) {
	fixture := loadCalendarFixture(t)
	fixture.validate(t)
	cases := make(map[string]calendarCase, len(fixture.Cases))
	for _, testCase := range fixture.Cases {
		cases[testCase.Name] = testCase
	}
	controls := []struct {
		name          string
		offsetSeconds int
		label         string
	}{
		{name: "new-york-spring-midnight-after-transition", offsetSeconds: -5 * 60 * 60, label: "UTC-5"},
		{name: "new-york-fall-midnight-after-transition", offsetSeconds: -4 * 60 * 60, label: "UTC-4"},
		{name: "santiago-midnight-after-skipped-midnight", offsetSeconds: -4 * 60 * 60, label: "UTC-4"},
	}
	for _, control := range controls {
		t.Run(control.name+"/"+control.label, func(t *testing.T) {
			testCase, exists := cases[control.name]
			if !exists {
				t.Fatalf("fixture case %q is missing", control.name)
			}
			instant, err := time.Parse(time.RFC3339, testCase.At)
			if err != nil {
				t.Fatal(err)
			}
			fixed := &Calendar{
				location:   time.FixedZone(control.label, control.offsetSeconds),
				billingDay: testCase.BillingDay,
			}
			got := fixed.Resolve(instant)
			want := Cycle{
				LocalDate: mustCalendarDate(t, testCase.Expected.LocalDate),
				Key:       testCase.Expected.CycleKey,
				Start:     mustCalendarDate(t, testCase.Expected.CycleStart),
				End:       mustCalendarDate(t, testCase.Expected.CycleEnd),
			}
			if got == want {
				t.Fatalf("fixed %s offset produced the in-zone result %+v", control.label, got)
			}
		})
	}
}

func TestCalendarCyclesTileFixtureRange(t *testing.T) {
	fixture := loadCalendarFixture(t)
	fixture.validate(t)
	first := mustCalendarDate(t, fixture.Invariants.FirstDate)
	last := mustCalendarDate(t, fixture.Invariants.LastDate)
	for _, billingDay := range fixture.Invariants.BillingDays {
		t.Run("billing-day-"+strconv.Itoa(billingDay), func(t *testing.T) {
			calendar, err := NewCalendar(fixture.Invariants.Zone, billingDay)
			if err != nil {
				t.Fatal(err)
			}
			seen := map[string]calendarWindow{}
			for day := first; !last.before(day); day = nextCalendarDate(day) {
				instant := time.Date(day.Year, day.Month, day.Day, 12, 0, 0, 0, time.UTC)
				cycle := calendar.Resolve(instant)
				if cycle.LocalDate != day {
					t.Fatalf("%s resolved to local date %s", day, cycle.LocalDate)
				}
				if day.before(cycle.Start) || cycle.End.before(day) {
					t.Fatalf("%s is outside its cycle %s..%s", day, cycle.Start, cycle.End)
				}
				if window, exists := seen[cycle.Key]; exists {
					if window.start != cycle.Start || window.end != cycle.End {
						t.Fatalf("cycle %s changed bounds from %s..%s to %s..%s", cycle.Key, window.start, window.end, cycle.Start, cycle.End)
					}
				} else {
					seen[cycle.Key] = calendarWindow{start: cycle.Start, end: cycle.End}
				}
			}
			keys := make([]string, 0, len(seen))
			for key := range seen {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			for index := 1; index < len(keys); index++ {
				previous := seen[keys[index-1]]
				current := seen[keys[index]]
				if nextCalendarDate(previous.end) != current.start {
					t.Fatalf("gap or overlap between %s..%s and %s..%s", previous.start, previous.end, current.start, current.end)
				}
			}
		})
	}
}

func TestNewCalendarRejectsInvalidZone(t *testing.T) {
	cases := []struct {
		name string
		zone string
	}{
		{name: "empty", zone: ""},
		{name: "local", zone: "Local"},
		{name: "unknown", zone: "Synthetic/Unknown"},
		{name: "absolute-path", zone: "/usr/share/zoneinfo/UTC"},
		{name: "parent-segment", zone: "America/../Asia/Shanghai"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			calendar, err := NewCalendar(testCase.zone, 1)
			if calendar != nil {
				t.Fatal("invalid zone returned a calendar")
			}
			if !errors.Is(err, ErrCalendarZone) {
				t.Fatalf("error = %v, want ErrCalendarZone", err)
			}
			if testCase.zone != "" && strings.Contains(err.Error(), testCase.zone) {
				t.Fatalf("public error echoes the rejected zone: %v", err)
			}
		})
	}
}

func TestNewCalendarRejectsInvalidBillingDay(t *testing.T) {
	for _, billingDay := range []int{-1, 0, 32, 1000} {
		t.Run(strconv.Itoa(billingDay), func(t *testing.T) {
			calendar, err := NewCalendar("UTC", billingDay)
			if calendar != nil {
				t.Fatal("invalid billing day returned a calendar")
			}
			if !errors.Is(err, ErrCalendarBillingDay) {
				t.Fatalf("error = %v, want ErrCalendarBillingDay", err)
			}
			if strings.Contains(err.Error(), strconv.Itoa(billingDay)) {
				t.Fatalf("public error echoes the rejected billing day: %v", err)
			}
		})
	}
}

func (fixture calendarFixture) validate(t *testing.T) {
	t.Helper()
	if fixture.Schema != 1 {
		t.Fatalf("fixture schema = %d, want 1", fixture.Schema)
	}
	if len(fixture.Cases) == 0 {
		t.Fatal("fixture has no cases")
	}
	zones := map[string]bool{}
	billingDays := map[int]bool{}
	names := map[string]calendarCase{}
	for _, testCase := range fixture.Cases {
		if testCase.Name == "" {
			t.Fatal("case has no name")
		}
		if _, exists := names[testCase.Name]; exists {
			t.Fatalf("duplicate case name %q", testCase.Name)
		}
		names[testCase.Name] = testCase
		if testCase.Description == "" {
			t.Fatalf("case %s has no description", testCase.Name)
		}
		if testCase.Zone == "" {
			t.Fatalf("case %s has no zone", testCase.Name)
		}
		if _, err := time.Parse(time.RFC3339, testCase.At); err != nil {
			t.Fatalf("case %s instant: %v", testCase.Name, err)
		}
		if testCase.BillingDay < 1 || testCase.BillingDay > 31 {
			t.Fatalf("case %s billing day = %d, want 1 through 31", testCase.Name, testCase.BillingDay)
		}
		localDate := mustCalendarDate(t, testCase.Expected.LocalDate)
		start := mustCalendarDate(t, testCase.Expected.CycleStart)
		end := mustCalendarDate(t, testCase.Expected.CycleEnd)
		if localDate.before(start) || end.before(localDate) {
			t.Fatalf("case %s local date %s is outside %s..%s", testCase.Name, localDate, start, end)
		}
		if testCase.Expected.CycleKey != start.String()[:7] {
			t.Fatalf("case %s cycle key %s does not name start month %s", testCase.Name, testCase.Expected.CycleKey, start)
		}
		zones[testCase.Zone] = true
		billingDays[testCase.BillingDay] = true
	}
	for _, zone := range fixture.Coverage.Zones {
		if !zones[zone] {
			t.Fatalf("coverage zone %s has no case", zone)
		}
	}
	for _, billingDay := range fixture.Coverage.BillingDays {
		if !billingDays[billingDay] {
			t.Fatalf("coverage billing day %d has no case", billingDay)
		}
	}
	skipped, exists := names[fixture.Coverage.SkippedLocalMidnightCase]
	if !exists {
		t.Fatalf("skipped-midnight case %q is missing", fixture.Coverage.SkippedLocalMidnightCase)
	}
	if skipped.Zone != fixture.Coverage.SkippedLocalMidnightZone {
		t.Fatalf("skipped-midnight case zone = %s, want %s", skipped.Zone, fixture.Coverage.SkippedLocalMidnightZone)
	}
	if skipped.Expected.LocalDate != skipped.Expected.CycleStart {
		t.Fatal("skipped-midnight case must open its cycle on its local date")
	}
	if fixture.Invariants.Zone == "" {
		t.Fatal("invariants have no zone")
	}
	if len(fixture.Invariants.BillingDays) == 0 {
		t.Fatal("invariants have no billing days")
	}
	for _, billingDay := range fixture.Invariants.BillingDays {
		if billingDay < 1 || billingDay > 31 {
			t.Fatalf("invariant billing day = %d, want 1 through 31", billingDay)
		}
	}
	first := mustCalendarDate(t, fixture.Invariants.FirstDate)
	last := mustCalendarDate(t, fixture.Invariants.LastDate)
	if last.before(first) {
		t.Fatalf("invariant range ends before it starts: %s..%s", first, last)
	}
}

func mustCalendarDate(t *testing.T, value string) Date {
	t.Helper()
	parsed, err := time.Parse("2006-01-02", value)
	if err != nil {
		t.Fatalf("calendar date %q: %v", value, err)
	}
	return Date{Year: parsed.Year(), Month: parsed.Month(), Day: parsed.Day()}
}

func nextCalendarDate(day Date) Date {
	next := time.Date(day.Year, day.Month, day.Day+1, 0, 0, 0, 0, time.UTC)
	return Date{Year: next.Year(), Month: next.Month(), Day: next.Day()}
}

func loadCalendarFixture(t *testing.T) calendarFixture {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("locate calendar fixture source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "tests", "fixtures", calendarFixtureName)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture calendarFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	return fixture
}
